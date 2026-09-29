// Wire types for the shipcrew board API (`/v1/shipcrew/*`). The server module
// implements the same contract; keep both sides in step.

export type MissionStatus = "planning" | "active" | "done";

export type PlanStatus = "idle" | "running" | "imported" | "failed";

/** The planner run that turns a PRD into tasks (`POST /missions/{id}/plan`). */
export interface MissionPlan {
  status: PlanStatus;
  /** Planner session, linkable at `/c/{session_id}` while it runs. */
  session_id: string | null;
  error: string | null;
  imported_count: number;
}

export interface Mission {
  id: string;
  title: string;
  repo_path: string;
  repo_url: string | null;
  status: MissionStatus;
  plan: MissionPlan;
  /** Unix epoch seconds (server clock). */
  created_at: number;
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

/** CI fix turns the PR loop runs before it gives up on a red build. */
export const CI_MAX_ATTEMPTS = 3;

export type CiStatus = "none" | "pending" | "green" | "red";

export type ReviewVerdict = "approve" | "changes";

export const FINDING_SEVERITIES = ["blocker", "major", "minor"] as const;

export type FindingSeverity = (typeof FINDING_SEVERITIES)[number];

export interface ReviewFinding {
  file: string;
  line: number | null;
  severity: FindingSeverity;
  message: string;
}

/** The reviewer's latest verdict on the task's pull request. */
export interface TaskReview {
  verdict: ReviewVerdict | null;
  summary: string;
  findings: ReviewFinding[];
}

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
  /** `shipcrew/<first 8 chars of id>-<slug of title>`, once a worktree exists. */
  branch: string | null;
  issue_number: number | null;
  issue_url: string | null;
  pr_number: number | null;
  pr_url: string | null;
  ci: CiStatus;
  /** CI fix turns spent so far (the loop gives up after `CI_MAX_ATTEMPTS`). */
  ci_attempts: number;
  review: TaskReview | null;
  /** The merge waits for a human (`POST /tasks/{id}/approve`). */
  needs_human_approval: boolean;
  approval_reasons: string[];
  root_session_id: string | null;
  cost_usd: number;
  position: number;
  blocked_reason: string | null;
  /** Unix epoch seconds (server clock). */
  created_at: number;
  updated_at: number;
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
  | { type: "task.updated"; task: Task }
  | { type: "task.deleted"; id: string }
  | { type: "mission.updated"; mission: Mission };
