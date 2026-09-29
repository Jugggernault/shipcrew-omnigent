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

export type ShipStatus = "idle" | "deploying" | "verifying" | "done" | "failed";

/**
 * The ship stage: once every agent task is merged, a devops session deploys
 * `main` to Vercel and the server checks the URL itself, then writes the
 * report (`POST /missions/{id}/ship` runs it by hand).
 */
export interface MissionShip {
  status: ShipStatus;
  /** Deployment URL, set once the agent reported it (verified when `done`). */
  url: string | null;
  /** Server-generated Markdown report (done or failed). */
  report_md: string | null;
  /** Why the ship failed, or (while idle) why the mission does not ship. */
  error: string | null;
  /** E.g. the deployment is protected (HTTP 401/403). */
  note: string | null;
  /** Unix epoch seconds (server clock). */
  started_at: number | null;
  finished_at: number | null;
  /** The devops session while it deploys. */
  session_id: string | null;
  decisions: string[];
  cost_usd: number;
}

export interface Mission {
  id: string;
  title: string;
  repo_path: string;
  repo_url: string | null;
  status: MissionStatus;
  plan: MissionPlan;
  /**
   * A plan import moves its tasks to Ready right away ("Run automatically after
   * planning"). Optional: servers that predate it omit the field.
   */
  auto_run?: boolean;
  /** The planner's "Decisions:" list. Optional: older servers omit it. */
  plan_decisions?: string[];
  /** Deploy as soon as every agent task is merged (default on). */
  auto_ship?: boolean;
  ship?: MissionShip;
  /**
   * The omnigent project (sidebar folder) that groups the mission's sessions.
   * Optional: older servers omit it; `null` until the first use created it.
   */
  project_id?: string | null;
  /** Unix epoch seconds (server clock). */
  created_at: number;
}

/** `POST /missions/{id}/start-all`: backlog tasks moved to Ready. */
export interface StartAllResponse {
  mission: Mission;
  /** Ids of the tasks moved to Ready (the scheduler gates decide what runs). */
  started: string[];
}

export type MissionCommandIntent = "start_all" | "plan" | "sync" | "stop_all" | "ship";

/** `POST /missions/{id}/command`: what the rule-based command box did. */
export interface MissionCommandResponse {
  intent: MissionCommandIntent;
  /** Human-readable outcome, shown as a toast. */
  message: string;
  mission: Mission;
  started?: string[];
  stopped?: string[];
  sync?: MissionSyncReport;
}

export interface MissionPatch {
  auto_run?: boolean;
  auto_ship?: boolean;
  /** Renames the mission (and its project when that name is free). */
  title?: string;
}

/**
 * Outcome of `POST /missions/{id}/sync`. The route returns the Mission plus this
 * `sync` key (an additive extension of the contract) so a no-op can say why.
 */
export interface MissionSyncReport {
  /** The sync ran against GitHub. */
  ok: boolean;
  /** Why it did not run or failed (e.g. gh not logged in), else null. */
  reason: string | null;
  created: number;
  updated: number;
  synced_at: number;
}

export type SyncMissionResponse = Mission & { sync?: MissionSyncReport };

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
  /** What the agent chose without asking (its "Decisions:" lists). */
  decisions?: string[];
  /** Unix epoch seconds of the first start, or null. */
  started_at?: number | null;
  /** Every time the card needed a human. */
  interventions?: TaskIntervention[];
  /** Unix epoch seconds (server clock). */
  created_at: number;
  updated_at: number;
}

export interface TaskIntervention {
  /** Unix epoch seconds. */
  at: number;
  reason: string;
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
