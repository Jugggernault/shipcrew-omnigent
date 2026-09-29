// Rendering tests for the task drawer's PR loop sections. The sub-agent graph
// and its child-session query are stubbed at their module seams.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeTask } from "./fixtures";
import { SAMPLE_REVIEW } from "./sampleBoard";
import { canRequestChanges, TaskDrawer } from "./TaskDrawer";
import type { Task } from "./types";

vi.mock("@/hooks/useChildSessions", () => ({
  useChildSessions: () => ({ children: [{ id: "child" }], isLoading: false, error: null }),
}));
vi.mock("@/shell/SubagentsGraphView", () => ({
  SubagentsGraphView: ({ rootSessionId }: { rootSessionId: string }) => (
    <div data-testid="subagents-graph">{rootSessionId}</div>
  ),
}));

afterEach(cleanup);

function renderDrawer(task: Task, overrides: Partial<Parameters<typeof TaskDrawer>[0]> = {}) {
  const props = {
    task,
    tasks: [task],
    onClose: vi.fn(),
    onStart: vi.fn(),
    onStop: vi.fn(),
    onApprove: vi.fn(),
    onRequestChanges: vi.fn(async () => {}),
    ...overrides,
  };
  render(
    <MemoryRouter>
      <TaskDrawer {...props} />
    </MemoryRouter>,
  );
  return { props, drawer: within(screen.getByTestId("task-drawer")) };
}

const inReview = makeTask({
  status: "review",
  branch: "shipcrew/task_1-refund-flow",
  issue_number: 13,
  issue_url: "https://github.com/acme/app/issues/13",
  pr_number: 43,
  pr_url: "https://github.com/acme/app/pull/43",
  ci: "red",
  ci_attempts: 2,
  review: SAMPLE_REVIEW,
  root_session_id: "conv_root",
});

describe("TaskDrawer PR loop", () => {
  it("shows the pull request, CI and its fix attempts", () => {
    const { drawer } = renderDrawer(inReview);
    const section = within(drawer.getByRole("heading", { name: "Pull request" }).parentElement!);
    expect(section.getByRole("link", { name: "Open pull request #43" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/43",
    );
    expect(section.getByTestId("branch-chip")).toHaveTextContent("shipcrew/task_1-refund-flow");
    expect(section.getByTestId("drawer-ci")).toHaveTextContent("Failed");
    expect(section.getByTestId("drawer-ci-attempts")).toHaveTextContent("2 of 3");
    expect(drawer.getByRole("link", { name: "Open issue #13" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/issues/13",
    );
    expect(drawer.getByTestId("ci-badge")).toHaveAccessibleName("CI failed, fix 2/3");
  });

  it("groups review findings by severity with their location", () => {
    const { drawer } = renderDrawer(inReview);
    expect(drawer.getByTestId("review-summary")).toHaveTextContent(SAMPLE_REVIEW.summary);
    expect(drawer.getAllByTestId("review-badge")[0]).toHaveAttribute("data-verdict", "changes");

    const blockers = within(drawer.getByRole("list", { name: "Blockers findings" }));
    expect(blockers.getByText("src/billing/refund.py:48")).toBeInTheDocument();
    expect(blockers.getAllByRole("listitem")).toHaveLength(1);
    expect(
      within(drawer.getByRole("list", { name: "Major findings" })).getByText(
        "src/billing/refund.py:71",
      ),
    ).toBeInTheDocument();
    // A finding without a line shows the file alone.
    expect(
      within(drawer.getByRole("list", { name: "Minor findings" })).getByText(
        "tests/test_refund.py",
      ),
    ).toBeInTheDocument();
    expect(drawer.getByRole("heading", { name: "Blockers (1)" })).toBeInTheDocument();
  });

  it("hides the PR and review sections before the loop has anything", () => {
    const { drawer } = renderDrawer(makeTask());
    expect(drawer.queryByRole("heading", { name: "Pull request" })).not.toBeInTheDocument();
    expect(drawer.queryByRole("heading", { name: "Review" })).not.toBeInTheDocument();
    expect(drawer.queryByRole("form", { name: "Request changes" })).not.toBeInTheDocument();
    expect(drawer.queryByTestId("drawer-approval")).not.toBeInTheDocument();
  });

  it("approves a merge that waits on a human", () => {
    const task = makeTask({
      status: "intervention",
      needs_human_approval: true,
      approval_reasons: ["Touches migrations/**", "Diff above 400 lines"],
      root_session_id: "conv_root",
    });
    const { drawer, props } = renderDrawer(task);
    expect(drawer.getByTestId("drawer-intervention")).toHaveAttribute("data-kind", "approval");
    const approval = within(drawer.getByTestId("drawer-approval"));
    expect(
      within(approval.getByRole("list", { name: "Approval reasons" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Touches migrations/**", "Diff above 400 lines"]);
    fireEvent.click(approval.getByRole("button", { name: "Approve merge" }));
    expect(props.onApprove).toHaveBeenCalledWith(task);
  });

  it("sends change requests and clears the box once accepted", async () => {
    const { drawer, props } = renderDrawer(inReview);
    const form = within(drawer.getByRole("form", { name: "Request changes" }));
    const submit = form.getByRole("button", { name: "Request changes" });
    expect(submit).toBeDisabled();

    fireEvent.change(form.getByLabelText("Feedback for the developer"), {
      target: { value: "  Use one transaction  " },
    });
    fireEvent.click(submit);
    await waitFor(() =>
      expect(props.onRequestChanges).toHaveBeenCalledWith(inReview, "Use one transaction"),
    );
    await waitFor(() => expect(form.getByLabelText("Feedback for the developer")).toHaveValue(""));
  });

  it("keeps the feedback and shows the error when the request fails", async () => {
    const { drawer } = renderDrawer(inReview, {
      onRequestChanges: vi.fn(async () => {
        throw new Error("Task has no session");
      }),
    });
    const form = within(drawer.getByRole("form", { name: "Request changes" }));
    fireEvent.change(form.getByLabelText("Feedback for the developer"), {
      target: { value: "Retry" },
    });
    fireEvent.click(form.getByRole("button", { name: "Request changes" }));
    expect(await form.findByRole("alert")).toHaveTextContent("Task has no session");
    expect(form.getByLabelText("Feedback for the developer")).toHaveValue("Retry");
  });

  it("points a guardrail ask at the Inbox and keeps the agent tree", () => {
    const { drawer } = renderDrawer(
      makeTask({ status: "intervention", root_session_id: "conv_root" }),
    );
    expect(drawer.getByTestId("drawer-intervention")).toHaveTextContent("Guardrail ask pending");
    expect(drawer.getByRole("link", { name: "Open Inbox" })).toHaveAttribute("href", "/inbox");
    expect(drawer.getByTestId("subagents-graph")).toHaveTextContent("conv_root");
  });
});

describe("canRequestChanges", () => {
  it("needs a developer session and a task under review or waiting on a human", () => {
    expect(canRequestChanges(makeTask({ status: "review", root_session_id: "c" }))).toBe(true);
    expect(canRequestChanges(makeTask({ status: "intervention", root_session_id: "c" }))).toBe(
      true,
    );
    expect(canRequestChanges(makeTask({ status: "review" }))).toBe(false);
    expect(canRequestChanges(makeTask({ status: "running", root_session_id: "c" }))).toBe(false);
    expect(canRequestChanges(makeTask({ status: "merged", root_session_id: "c" }))).toBe(false);
  });
});
