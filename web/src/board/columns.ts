// Board columns as a projection of task status. Blocked tasks have no column
// of their own: they sit in Backlog with a badge, and the Blocked filter
// narrows every column to them.

import type { Task, TaskStatus } from "./types";

export type ColumnId = "backlog" | "ready" | "running" | "review" | "intervention" | "merged";

export interface BoardColumn {
  id: ColumnId;
  label: string;
  /** Short explanation shown under the column title. */
  hint: string;
}

export const BOARD_COLUMNS: readonly BoardColumn[] = [
  { id: "backlog", label: "Backlog", hint: "Not scheduled" },
  { id: "ready", label: "Ready", hint: "Starts when its gates pass" },
  { id: "running", label: "Running", hint: "An agent is working" },
  { id: "review", label: "Review", hint: "PR open, CI and review" },
  { id: "intervention", label: "Intervention", hint: "Needs a human" },
  { id: "merged", label: "Merged", hint: "Landed on main" },
];

export const STATUS_LABELS: Record<TaskStatus, string> = {
  backlog: "Backlog",
  ready: "Ready",
  running: "Running",
  review: "Review",
  intervention: "Intervention",
  merged: "Merged",
  blocked: "Blocked",
};

export function columnForStatus(status: TaskStatus): ColumnId {
  return status === "blocked" ? "backlog" : status;
}

export function isBlocked(task: Task): boolean {
  return task.status === "blocked" || Boolean(task.blocked_reason);
}

export interface ProjectOptions {
  /** Keep only blocked tasks. */
  blockedOnly?: boolean;
}

export type ColumnTasks = Record<ColumnId, Task[]>;

/** Group tasks by column, each column ordered by `position` then creation. */
export function projectColumns(tasks: readonly Task[], options: ProjectOptions = {}): ColumnTasks {
  const columns: ColumnTasks = {
    backlog: [],
    ready: [],
    running: [],
    review: [],
    intervention: [],
    merged: [],
  };
  for (const task of tasks) {
    if (options.blockedOnly && !isBlocked(task)) continue;
    columns[columnForStatus(task.status)].push(task);
  }
  for (const list of Object.values(columns)) {
    list.sort(
      (left, right) => left.position - right.position || left.created_at - right.created_at,
    );
  }
  return columns;
}

/** Intervention is set by the agent session (a pending approval), never by a move. */
export function canMoveTo(column: ColumnId): boolean {
  return column !== "intervention";
}

export type DropAction =
  | { kind: "none" }
  | { kind: "start" }
  | { kind: "confirm-merge" }
  | { kind: "patch"; status: TaskStatus };

/**
 * What dropping `task` on `column` should do. Running means "start it now";
 * Merged normally comes from the PR loop, so a manual move asks first.
 */
export function dropAction(task: Task, column: ColumnId): DropAction {
  if (!canMoveTo(column)) return { kind: "none" };
  if (columnForStatus(task.status) === column && task.status !== "blocked") return { kind: "none" };
  if (task.status === "blocked" && column === "backlog")
    return { kind: "patch", status: "backlog" };
  if (column === "running") return { kind: "start" };
  if (column === "merged") return { kind: "confirm-merge" };
  return { kind: "patch", status: column };
}
