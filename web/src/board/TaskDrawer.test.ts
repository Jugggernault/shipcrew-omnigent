import { describe, expect, it } from "vitest";
import { makeTask } from "./fixtures";
import { canStartTask } from "./TaskDrawer";

describe("canStartTask", () => {
  it("offers Start only where the server would start the agent", () => {
    expect(canStartTask(makeTask({ status: "backlog" }))).toBe(true);
    expect(canStartTask(makeTask({ status: "blocked" }))).toBe(true);
    expect(canStartTask(makeTask({ status: "running" }))).toBe(false);
    expect(canStartTask(makeTask({ status: "intervention" }))).toBe(false);
    expect(canStartTask(makeTask({ status: "merged" }))).toBe(false);
    expect(canStartTask(makeTask({ assignee: { kind: "human", id: "bob" } }))).toBe(false);
  });
});
