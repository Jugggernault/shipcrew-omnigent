import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import {
  applyStreamEvent,
  createTask,
  fetchMissions,
  parseMissionStream,
  tasksQueryKey,
  updateTask,
} from "./api";
import { makeTask } from "./fixtures";
import type { MissionStreamEvent, Task } from "./types";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>): Promise<MissionStreamEvent[]> {
  const events: MissionStreamEvent[] = [];
  for await (const event of parseMissionStream(body)) events.push(event);
  return events;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => fetchMock.mockReset());

describe("parseMissionStream", () => {
  it("parses events split across chunks and skips heartbeats and unknown kinds", async () => {
    const task = makeTask({ id: "t1", status: "running" });
    const payload = JSON.stringify({ type: "task.updated", task });
    const events = await collect(
      streamOf([
        ": heartbeat\n\n",
        `data: ${payload.slice(0, 20)}`,
        `${payload.slice(20)}\n\n`,
        'data: {"type":"mission.updated"}\n\n',
        "data: not json\n\n",
        'event: task.deleted\r\ndata: {"id":"t2"}\r\n\r\n',
      ]),
    );
    expect(events).toEqual([
      { type: "task.updated", task },
      { type: "task.deleted", id: "t2" },
    ]);
  });

  it("stops at the [DONE] sentinel", async () => {
    const events = await collect(
      streamOf([
        'data: {"type":"task.deleted","id":"a"}\n\n',
        "data: [DONE]\n\n",
        'data: {"type":"task.deleted","id":"b"}\n\n',
      ]),
    );
    expect(events).toEqual([{ type: "task.deleted", id: "a" }]);
  });
});

describe("applyStreamEvent", () => {
  it("upserts updated tasks and removes deleted ones", () => {
    const client = new QueryClient();
    const key = tasksQueryKey("mission_1");
    client.setQueryData<Task[]>(key, [makeTask({ id: "a" }), makeTask({ id: "b" })]);

    applyStreamEvent(client, "mission_1", {
      type: "task.updated",
      task: makeTask({ id: "a", status: "review" }),
    });
    applyStreamEvent(client, "mission_1", { type: "task.updated", task: makeTask({ id: "c" }) });
    applyStreamEvent(client, "mission_1", { type: "task.deleted", id: "b" });
    // A task from another mission never lands in this list.
    applyStreamEvent(client, "mission_1", {
      type: "task.updated",
      task: makeTask({ id: "x", mission_id: "other" }),
    });

    const tasks = client.getQueryData<Task[]>(key) ?? [];
    expect(tasks.map((task) => [task.id, task.status])).toEqual([
      ["a", "review"],
      ["c", "backlog"],
    ]);
  });
});

describe("requests", () => {
  it("unwraps the mission list", async () => {
    fetchMock.mockResolvedValueOnce(json({ missions: [{ id: "m1" }] }));
    await expect(fetchMissions()).resolves.toEqual([{ id: "m1" }]);
    expect(fetchMock).toHaveBeenCalledWith("/v1/shipcrew/missions", expect.anything());
  });

  it("sends JSON bodies to the contract paths", async () => {
    fetchMock.mockImplementation(async () => json(makeTask()));
    await createTask("m 1", { title: "Do it", role: "developer" });
    await updateTask("t1", { status: "ready" });

    const [createUrl, createInit] = fetchMock.mock.calls[0];
    expect(createUrl).toBe("/v1/shipcrew/missions/m%201/tasks");
    expect(createInit?.method).toBe("POST");
    expect(JSON.parse(createInit?.body as string)).toEqual({ title: "Do it", role: "developer" });
    expect(new Headers(createInit?.headers).get("Content-Type")).toBe("application/json");

    const [patchUrl, patchInit] = fetchMock.mock.calls[1];
    expect(patchUrl).toBe("/v1/shipcrew/tasks/t1");
    expect(patchInit?.method).toBe("PATCH");
    expect(JSON.parse(patchInit?.body as string)).toEqual({ status: "ready" });
  });

  it("surfaces the server's error message", async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: "Task is merged" }, 409));
    await expect(updateTask("t1", { status: "ready" })).rejects.toThrow("Task is merged");
  });
});
