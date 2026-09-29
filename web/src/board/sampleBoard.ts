// A representative mission board for stories.

import { makeTask } from "./fixtures";
import type { Task } from "./types";

export const SAMPLE_TASKS: Task[] = [
  makeTask({ id: "t1", title: "Invoice PDF export", acceptance: ["PDF matches the invoice"] }),
  makeTask({
    id: "t2",
    title: "Wire the Stripe webhook",
    status: "blocked",
    blocked_reason: "Owned paths overlap with “Billing page” (src/billing/**)",
    depends_on: ["t4"],
    position: 1,
  }),
  makeTask({ id: "t3", title: "Plan tax rules", status: "ready", role: "architect" }),
  makeTask({
    id: "t4",
    title: "Billing page with invoice list",
    status: "running",
    role: "frontend",
    assignee: { kind: "agent", id: "frontend" },
    root_session_id: "conv_1",
    cost_usd: 0.87,
  }),
  makeTask({
    id: "t5",
    title: "Customer portal link",
    status: "review",
    assignee: { kind: "agent", id: "developer" },
    pr_number: 42,
    pr_url: "https://github.com/acme/app/pull/42",
    ci: "pending",
    cost_usd: 1.42,
  }),
  makeTask({
    id: "t6",
    title: "Refund flow",
    status: "intervention",
    assignee: { kind: "human", id: "ana.lopez@example.com" },
    pr_number: 43,
    pr_url: "https://github.com/acme/app/pull/43",
    ci: "red",
    cost_usd: 2.1,
  }),
  makeTask({
    id: "t7",
    title: "Pricing table",
    status: "merged",
    pr_number: 40,
    pr_url: "https://github.com/acme/app/pull/40",
    ci: "green",
    cost_usd: 0.64,
  }),
];
