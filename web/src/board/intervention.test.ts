import { describe, expect, it } from "vitest";
import { makeTask } from "./fixtures";
import { interventionReason } from "./intervention";

describe("interventionReason", () => {
  it("is null outside Intervention", () => {
    expect(interventionReason(makeTask({ status: "review", needs_human_approval: true }))).toBe(
      null,
    );
  });

  it("names a pending merge approval first", () => {
    const task = makeTask({
      status: "intervention",
      needs_human_approval: true,
      review: { verdict: "changes", summary: "", findings: [] },
    });
    expect(interventionReason(task)?.kind).toBe("approval");
  });

  it("names exhausted review rounds when the last verdict asked for changes", () => {
    const task = makeTask({
      status: "intervention",
      review: { verdict: "changes", summary: "", findings: [] },
    });
    expect(interventionReason(task)).toMatchObject({
      kind: "review",
      label: "Review rounds exhausted",
    });
  });

  it("falls back to a guardrail ask", () => {
    expect(interventionReason(makeTask({ status: "intervention" }))).toMatchObject({
      kind: "guardrail",
      label: "Guardrail ask pending",
    });
    const approved = makeTask({
      status: "intervention",
      review: { verdict: "approve", summary: "", findings: [] },
    });
    expect(interventionReason(approved)?.kind).toBe("guardrail");
  });
});
