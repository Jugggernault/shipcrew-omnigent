// Typed client for the shipcrew board API (`/v1/shipcrew/*`): fetchers,
// TanStack Query hooks, and the mission SSE stream. Task lists and the
// mission (plan status) stay live through the stream; while it is down the
// task list polls instead.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type {
  CreateMissionInput,
  CreateTaskInput,
  Mission,
  MissionCommandResponse,
  MissionPatch,
  MissionStreamEvent,
  StartAllResponse,
  SyncMissionResponse,
  Task,
  TaskPatch,
} from "./types";
import { syncProjectLink } from "./projectLinks";

const BASE = "/v1/shipcrew";
/** Poll interval for the task list while the SSE stream is not connected. */
export const TASKS_POLL_MS = 5_000;
/** Poll interval for the mission list while a planner run is in flight. */
export const PLAN_POLL_MS = 5_000;
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

/** Start a planner run; `prd` is optional (the planner reads the repo otherwise). */
export function planMission(missionId: string, prd?: string): Promise<Mission> {
  return request<Mission>(`/missions/${encodeURIComponent(missionId)}/plan`, {
    method: "POST",
    body: JSON.stringify(prd ? { prd } : {}),
  });
}

/** Force a GitHub issue/PR sync of the mission now. */
export function syncMission(missionId: string): Promise<SyncMissionResponse> {
  return request<SyncMissionResponse>(`/missions/${encodeURIComponent(missionId)}/sync`, {
    method: "POST",
  });
}

/** Mission settings (`auto_run`, `auto_ship`). */
export function updateMission(missionId: string, patch: MissionPatch): Promise<Mission> {
  return request<Mission>(`/missions/${encodeURIComponent(missionId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Deploy the mission now (every agent task must be merged); 409 says why not. */
export function shipMission(missionId: string): Promise<Mission> {
  return request<Mission>(`/missions/${encodeURIComponent(missionId)}/ship`, { method: "POST" });
}

/** Move every backlog task of the mission to Ready; the scheduler gates decide what runs. */
export function startAllTasks(missionId: string): Promise<StartAllResponse> {
  return request<StartAllResponse>(`/missions/${encodeURIComponent(missionId)}/start-all`, {
    method: "POST",
  });
}

/** Send a free-text command ("run all", "lance tout", "sync", ...) to the mission. */
export function sendMissionCommand(
  missionId: string,
  text: string,
): Promise<MissionCommandResponse> {
  return request<MissionCommandResponse>(`/missions/${encodeURIComponent(missionId)}/command`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

/** Approve a merge that waits on `needs_human_approval`. */
export function approveTask(taskId: string): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}/approve`, { method: "POST" });
}

/** Send feedback to the developer's root session; the task goes back to running. */
export function requestTaskChanges(taskId: string, message: string): Promise<Task> {
  return request<Task>(`/tasks/${encodeURIComponent(taskId)}/request-changes`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

// ---- cache helpers --------------------------------------------------------

/** Insert or replace one mission in the cached mission list. */
export function upsertCachedMission(queryClient: QueryClient, mission: Mission): void {
  syncProjectLink(queryClient, mission);
  queryClient.setQueryData<Mission[]>(missionsQueryKey, (current) => {
    if (!current) return [mission];
    const index = current.findIndex((item) => item.id === mission.id);
    if (index < 0) return [...current, mission];
    const next = current.slice();
    next[index] = mission;
    return next;
  });
}

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
  } else if (event.type === "mission.updated") {
    if (event.mission.id === missionId) upsertCachedMission(queryClient, event.mission);
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
  if (type === "mission.updated" && record.mission && typeof record.mission === "object") {
    return { type, mission: record.mission as Mission };
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

function planRunning(missions: readonly Mission[] | undefined): boolean {
  return (
    missions?.some(
      (mission) =>
        mission.plan?.status === "running" ||
        mission.ship?.status === "deploying" ||
        mission.ship?.status === "verifying" ||
        mission.preview?.status === "deploying",
    ) ?? false
  );
}

/** Missions; polled while a planner, ship or preview deploy is in flight so its outcome shows up. */
export function useMissions() {
  return useQuery({
    queryKey: missionsQueryKey,
    queryFn: fetchMissions,
    staleTime: 30_000,
    refetchInterval: (query) => (planRunning(query.state.data) ? PLAN_POLL_MS : false),
  });
}

export function useCreateMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createMission,
    onSuccess: (mission) => {
      syncProjectLink(queryClient, mission);
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

export function usePlanMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ mission, prd }: { mission: Mission; prd?: string }) =>
      planMission(mission.id, prd),
    onSuccess: (updated) => upsertCachedMission(queryClient, updated),
  });
}

export function useSyncMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (mission: Mission) => syncMission(mission.id),
    onSuccess: ({ sync: _report, ...updated }) => {
      upsertCachedMission(queryClient, updated);
      // Issue and PR links land on the tasks; reconcile in case events were missed.
      void queryClient.invalidateQueries({ queryKey: tasksQueryKey(updated.id) });
    },
  });
}

export function useUpdateMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ mission, patch }: { mission: Mission; patch: MissionPatch }) =>
      updateMission(mission.id, patch),
    onSuccess: (updated) => upsertCachedMission(queryClient, updated),
  });
}

export function useStartAllTasks() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (mission: Mission) => startAllTasks(mission.id),
    onSuccess: ({ mission }) => {
      upsertCachedMission(queryClient, mission);
      // The moved cards arrive over SSE; reconcile in case the stream is down.
      void queryClient.invalidateQueries({ queryKey: tasksQueryKey(mission.id) });
    },
  });
}

export function useShipMission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (mission: Mission) => shipMission(mission.id),
    onSuccess: (updated) => upsertCachedMission(queryClient, updated),
  });
}

export function useMissionCommand() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ mission, text }: { mission: Mission; text: string }) =>
      sendMissionCommand(mission.id, text),
    onSuccess: ({ mission }) => {
      upsertCachedMission(queryClient, mission);
      void queryClient.invalidateQueries({ queryKey: tasksQueryKey(mission.id) });
    },
  });
}

export function useApproveTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (task: Task) => approveTask(task.id),
    onSuccess: (updated) => upsertCachedTask(queryClient, updated),
  });
}

export function useRequestTaskChanges() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ task, message }: { task: Task; message: string }) =>
      requestTaskChanges(task.id, message),
    onSuccess: (updated) => upsertCachedTask(queryClient, updated),
  });
}
