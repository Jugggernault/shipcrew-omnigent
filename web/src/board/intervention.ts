// Why a task sits in Intervention. The server sets the status for three
// reasons (a guardrail ask, a merge awaiting approval, review rounds used up);
// the task fields tell them apart, most specific first.

import type { Task } from "./types";

export type InterventionKind = "approval" | "review" | "guardrail";

export interface InterventionReason {
  kind: InterventionKind;
  label: string;
  detail: string;
}

const REASONS: Record<InterventionKind, Omit<InterventionReason, "kind">> = {
  approval: {
    label: "Needs your approval",
    detail: "The merge waits for a human. Approve it from the task drawer.",
  },
  review: {
    label: "Review rounds exhausted",
    detail: "The reviewer asked for changes three times. Read the review and step in.",
  },
  guardrail: {
    label: "Guardrail ask pending",
    detail: "An agent is waiting on an approval prompt. Answer it in the Inbox.",
  },
};

export function interventionReason(task: Task): InterventionReason | null {
  if (task.status !== "intervention") return null;
  const kind: InterventionKind = task.needs_human_approval
    ? "approval"
    : task.review?.verdict === "changes"
      ? "review"
      : "guardrail";
  return { kind, ...REASONS[kind] };
}
