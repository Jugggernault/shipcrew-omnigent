import { describe, expect, it } from "vitest";
import { makeTask } from "./fixtures";
import { BOARD_COLUMNS, columnForStatus, dropAction, isBlocked, projectColumns } from "./columns";

describe("projectColumns", () => {
  it("places every status in its column and blocked tasks in Backlog", () => {
    const tasks = [
      makeTask({ id: "a", status: "backlog" }),
      makeTask({ id: "b", status: "ready" }),
      makeTask({ id: "c", status: "running" }),
      makeTask({ id: "d", status: "review" }),
      makeTask({ id: "e", status: "intervention" }),
      makeTask({ id: "f", status: "merged" }),
      makeTask({ id: "g", status: "blocked", blocked_reason: "Waiting on a" }),
    ];
    const columns = projectColumns(tasks);
    expect(Object.keys(columns)).toEqual(BOARD_COLUMNS.map((column) => column.id));
    expect(columns.backlog.map((task) => task.id)).toEqual(["a", "g"]);
    expect(columns.ready.map((task) => task.id)).toEqual(["b"]);
    expect(columns.running.map((task) => task.id)).toEqual(["c"]);
    expect(columns.review.map((task) => task.id)).toEqual(["d"]);
    expect(columns.intervention.map((task) => task.id)).toEqual(["e"]);
    expect(columns.merged.map((task) => task.id)).toEqual(["f"]);
  });

  it("orders a column by position, then creation time", () => {
    const tasks = [
      makeTask({ id: "late", position: 2, created_at: 1_767_225_600 }),
      makeTask({ id: "second", position: 1, created_at: 1_767_312_000 }),
      makeTask({ id: "first", position: 1, created_at: 1_767_225_600 }),
    ];
    expect(projectColumns(tasks).backlog.map((task) => task.id)).toEqual([
      "first",
      "second",
      "late",
    ]);
  });

  it("narrows every column to blocked tasks with the Blocked filter", () => {
    const tasks = [
      makeTask({ id: "free", status: "ready" }),
      makeTask({ id: "stuck", status: "blocked" }),
      makeTask({ id: "gated", status: "ready", blocked_reason: "Owned paths overlap" }),
    ];
    const columns = projectColumns(tasks, { blockedOnly: true });
    expect(columns.backlog.map((task) => task.id)).toEqual(["stuck"]);
    expect(columns.ready.map((task) => task.id)).toEqual(["gated"]);
    expect(isBlocked(tasks[0])).toBe(false);
  });
});

describe("dropAction", () => {
  it("does nothing when a card is dropped on its own column", () => {
    expect(dropAction(makeTask({ status: "review" }), "review")).toEqual({ kind: "none" });
  });

  it("schedules on Ready and starts on Running", () => {
    expect(dropAction(makeTask(), "ready")).toEqual({ kind: "patch", status: "ready" });
    expect(dropAction(makeTask(), "running")).toEqual({ kind: "start" });
  });

  it("ignores drops on Intervention, which only the agent session sets", () => {
    expect(dropAction(makeTask({ status: "running" }), "intervention")).toEqual({ kind: "none" });
  });

  it("asks before a manual merge", () => {
    expect(dropAction(makeTask({ status: "review" }), "merged")).toEqual({ kind: "confirm-merge" });
  });

  it("unblocks a blocked task dropped back on Backlog", () => {
    const task = makeTask({ status: "blocked" });
    expect(columnForStatus(task.status)).toBe("backlog");
    expect(dropAction(task, "backlog")).toEqual({ kind: "patch", status: "backlog" });
  });
});
