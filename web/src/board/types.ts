// Wire types for the shipcrew board API (`/v1/shipcrew/*`). The server module
// implements the same contract; keep both sides in step.

export type MissionStatus = "planning" | "active" | "done";

export interface Mission {
  id: string;
  title: string;
  repo_path: string;
  repo_url: string | null;
  status: MissionStatus;
  created_at: string;
}

export const TASK_STATUSES = [
  "backlog",
  "ready",
  "running",
  "review",
  "intervention",
  "merged",
  "blocked",
] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];

export type CiStatus = "none" | "pending" | "green" | "red";

export interface TaskAssignee {
  kind: "agent" | "human";
  id: string;
}

export interface Task {
  id: string;
  mission_id: string;
  title: string;
  body: string;
  acceptance: string[];
  status: TaskStatus;
  assignee: TaskAssignee | null;
  /** Agent bundle that runs the task, e.g. `"developer"`. */
  role: string;
  depends_on: string[];
  owned_paths: string[];
  issue_number: number | null;
  pr_number: number | null;
  pr_url: string | null;
  ci: CiStatus;
  root_session_id: string | null;
  cost_usd: number;
  position: number;
  blocked_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateMissionInput {
  title: string;
  repo_path: string;
  repo_url?: string;
}

export interface CreateTaskInput {
  title: string;
  body?: string;
  acceptance?: string[];
  role?: string;
  depends_on?: string[];
  owned_paths?: string[];
}

export type TaskPatch = Partial<
  Pick<
    Task,
    | "status"
    | "assignee"
    | "position"
    | "depends_on"
    | "owned_paths"
    | "title"
    | "body"
    | "acceptance"
  >
>;

export type MissionStreamEvent =
  { type: "task.updated"; task: Task } | { type: "task.deleted"; id: string };
