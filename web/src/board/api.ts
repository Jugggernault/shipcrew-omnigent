// Typed client for the shipcrew board API (`/v1/shipcrew/*`): fetchers,
// TanStack Query hooks, and the mission SSE stream. Task lists stay live
// through the stream; while it is down the list query polls instead.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type {
  CreateMissionInput,
  CreateTaskInput,
  Mission,
  MissionStreamEvent,
  Task,
  TaskPatch,
} from "./types";

const BASE = "/v1/shipcrew";
/** Poll interval for the task list while the SSE stream is not connected. */
export const TASKS_POLL_MS = 5_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

export const missionsQueryKey = ["shipcrew", "missions"] as const;
export function tasksQueryKey(missionId: string): readonly unknown[] {
  return ["shipcrew", "missions", missionId, "tasks"];
}

export class ShipcrewApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ShipcrewApiError";
    this.status = status;
  }
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as {
      error?: { message?: string };
      message?: string;
      detail?: unknown;
    };
    if (body.error?.message) return body.error.message;
    if (body.message) return body.message;
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // Non-JSON body; fall through to the status line.
  }
  return `${res.status} ${res.statusText}`.trim();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined) headers.set("Content-Type", "application/json");
  const res = await authenticatedFetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) throw new ShipcrewApiError(await readError(res), res.status);
  return (await res.json()) as T;
}

export async function fetchMissions(): Promise<Mission[]> {
  return (await request<{ missions: Mission[] }>("/missions")).missions;
}

export function createMission(input: CreateMissionInput): Promise<Mission> {
  return request<Mission>("/missions", { method: "POST", body: JSON.stringify(input) });
}

export async function fetchTasks(missionId: string): Promise<Task[]> {
  return (await request<{ tasks: Task[] }>(`/missions/${encodeURIComponent(missionId)}/tasks`))
    .tasks;
}

export function createTask(missionId: string, input: CreateTaskInput): Promise<Task> {
  return request<Task>(`/missions/${encodeURIComponent(missionId)}/tasks`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateTask(taskId: string, patch: TaskPatch): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function startTask(taskId: string): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}/start`, { method: "POST" });
}

export function stopTask(taskId: string): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}/stop`, { method: "POST" });
}

// ---- cache helpers --------------------------------------------------------

/** Insert or replace one task in a mission's cached list. */
export function upsertCachedTask(queryClient: QueryClient, task: Task): void {
  queryClient.setQueryData<Task[]>(tasksQueryKey(task.mission_id), (current) => {
    if (!current) return [task];
    const index = current.findIndex((item) => item.id === task.id);
    if (index < 0) return [...current, task];
    const next = current.slice();
    next[index] = task;
    return next;
  });
}

function removeCachedTask(queryClient: QueryClient, missionId: string, taskId: string): void {
  queryClient.setQueryData<Task[]>(tasksQueryKey(missionId), (current) =>
    current?.filter((item) => item.id !== taskId),
  );
}

/** Apply one stream event to the cached task list of `missionId`. */
export function applyStreamEvent(
  queryClient: QueryClient,
  missionId: string,
  event: MissionStreamEvent,
): void {
  if (event.type === "task.updated") {
    if (event.task.mission_id === missionId) upsertCachedTask(queryClient, event.task);
  } else {
    removeCachedTask(queryClient, missionId, event.id);
  }
}

// ---- SSE ------------------------------------------------------------------

function toStreamEvent(eventName: string | null, raw: string): MissionStreamEvent | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const record = data as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : eventName;
  if (type === "task.updated" && record.task && typeof record.task === "object") {
    return { type, task: record.task as Task };
  }
  if (type === "task.deleted" && typeof record.id === "string") {
    return { type, id: record.id };
  }
  return null;
}

/**
 * Parse a mission SSE byte stream. Events carry their kind in the JSON `type`
 * field; an `event:` line is honoured as a fallback. Unknown kinds, comments
 * (heartbeats) and malformed payloads are skipped.
 */
export async function* parseMissionStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<MissionStreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let eventName: string | null = null;
  let dataLines: string[] = [];
  try {
    while (true) {
      // Chunks arrive serially off the wire.
      // eslint-disable-next-line no-await-in-loop
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        let line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line === "") {
          const payload = dataLines.join("\n");
          const event = dataLines.length > 0 ? toStreamEvent(eventName, payload) : null;
          eventName = null;
          dataLines = [];
          if (event) yield event;
        } else if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          const data = line.slice(5).replace(/^ /, "");
          if (data.trim() === "[DONE]") return;
          dataLines.push(data);
        }
        newline = buffer.indexOf("\n");
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/**
 * Keep `missionId`'s cached task list in sync with its SSE stream, reconnecting
 * with backoff. Returns whether the stream is currently connected so callers
 * can fall back to polling while it is not.
 */
export function useMissionStream(missionId: string | null): boolean {
  const queryClient = useQueryClient();
  const [live, setLive] = useState(false);

  useEffect(() => {
    setLive(false);
    if (!missionId) return;
    const controller = new AbortController();
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const connect = async (): Promise<void> => {
      try {
        const res = await authenticatedFetch(
          `${BASE}/missions/${encodeURIComponent(missionId)}/stream`,
          { headers: { Accept: "text/event-stream" }, signal: controller.signal },
        );
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        attempt = 0;
        setLive(true);
        // Events may have been missed while disconnected; reconcile once.
        void queryClient.invalidateQueries({ queryKey: tasksQueryKey(missionId) });
        for await (const event of parseMissionStream(res.body)) {
          applyStreamEvent(queryClient, missionId, event);
        }
      } catch {
        // Transport drop or refused stream: fall through to reconnect.
      }
      if (controller.signal.aborted) return;
      setLive(false);
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
      attempt += 1;
      timer = setTimeout(() => void connect(), delay);
    };

    void connect();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [missionId, queryClient]);

  return live;
}

// ---- hooks ----------------------------------------------------------------

export function useMissions() {
  return useQuery({ queryKey: missionsQueryKey, queryFn: fetchMissions, staleTime: 30_000 });
}

export function useCreateMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createMission,
    onSuccess: (mission) => {
      queryClient.setQueryData<Mission[]>(missionsQueryKey, (current) => [
        ...(current ?? []).filter((item) => item.id !== mission.id),
        mission,
      ]);
    },
  });
}

/** Tasks of one mission, live through SSE with a polling fallback. */
export function useMissionTasks(missionId: string | null) {
  const live = useMissionStream(missionId);
  const query = useQuery({
    queryKey: missionId ? tasksQueryKey(missionId) : ["shipcrew", "missions", null, "tasks"],
    queryFn: () => fetchTasks(missionId as string),
    enabled: missionId !== null,
    refetchInterval: live ? false : TASKS_POLL_MS,
  });
  return { ...query, live };
}

export function useCreateTask(missionId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateTaskInput) => {
      if (!missionId) throw new Error("No mission selected");
      return createTask(missionId, input);
    },
    onSuccess: (task) => upsertCachedTask(queryClient, task),
  });
}

interface UpdateTaskVars {
  task: Task;
  patch: TaskPatch;
}

/** PATCH a task with an optimistic cache update, rolled back on failure. */
export function useUpdateTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ task, patch }: UpdateTaskVars) => updateTask(task.id, patch),
    onMutate: async ({ task, patch }: UpdateTaskVars) => {
      const key = tasksQueryKey(task.mission_id);
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<Task[]>(key);
      upsertCachedTask(queryClient, { ...task, ...patch });
      return { previous };
    },
    onError: (_error, { task }, context) => {
      if (context?.previous) {
        queryClient.setQueryData(tasksQueryKey(task.mission_id), context.previous);
      }
    },
    onSuccess: (updated) => upsertCachedTask(queryClient, updated),
  });
}

export function useStartTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (task: Task) => startTask(task.id),
    onSuccess: (updated) => upsertCachedTask(queryClient, updated),
  });
}

export function useStopTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (task: Task) => stopTask(task.id),
    onSuccess: (updated) => upsertCachedTask(queryClient, updated),
  });
}
