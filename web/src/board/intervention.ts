// Why a task sits in Intervention. The server sets the status for a guardrail
// ask, a merge awaiting approval, review rounds used up, or another PR-loop
// hold (CI still red, a failed reviewer...); the task fields tell them apart,
// most specific first. Loop holds always carry a blocked_reason, a guardrail
// ask never does (the loop clears it before each developer turn), so a
// "changes" verdict left over from an earlier round is not mistaken for one.

import type { Task } from "./types";

export type InterventionKind = "approval" | "review" | "hold" | "guardrail";

export interface InterventionReason {
  kind: InterventionKind;
  label: string;
  /** Fits a narrow board card. */
  short: string;
  detail: string;
}

const REASONS: Record<InterventionKind, Omit<InterventionReason, "kind">> = {
  approval: {
    label: "Needs your approval",
    short: "Approve to merge",
    detail: "The merge waits for a human. Approve it from the task drawer.",
  },
  review: {
    label: "Review rounds exhausted",
    short: "Reviews used up",
    detail: "The reviewer asked for changes three times. Read the review and step in.",
  },
  hold: {
    label: "Held by the PR loop",
    short: "PR loop hold",
    detail: "The PR loop stopped on this card. Read the reason, then request changes or fix it.",
  },
  guardrail: {
    label: "Guardrail ask pending",
    short: "Guardrail ask",
    detail: "An agent is waiting on an approval prompt. Answer it in the Inbox.",
  },
};

export function interventionReason(task: Task): InterventionReason | null {
  if (task.status !== "intervention") return null;
  const loopHold = task.pr_number !== null && Boolean(task.blocked_reason);
  const kind: InterventionKind = task.needs_human_approval
    ? "approval"
    : loopHold && task.review?.verdict === "changes"
      ? "review"
      : loopHold
        ? "hold"
        : "guardrail";
  return { kind, ...REASONS[kind] };
}
