// Test and story fixtures for board tasks and missions.

import type { Mission, Task } from "./types";

export function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task_1",
    mission_id: "mission_1",
    title: "Add the billing page",
    body: "",
    acceptance: [],
    status: "backlog",
    assignee: null,
    role: "developer",
    depends_on: [],
    owned_paths: [],
    issue_number: null,
    pr_number: null,
    pr_url: null,
    ci: "none",
    root_session_id: null,
    cost_usd: 0,
    position: 0,
    blocked_reason: null,
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

export function makeMission(overrides: Partial<Mission> = {}): Mission {
  return {
    id: "mission_1",
    title: "Billing launch",
    repo_path: "/home/dev/code/app",
    repo_url: null,
    status: "active",
    created_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}
