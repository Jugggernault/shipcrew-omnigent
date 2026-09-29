import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { makeTask } from "./fixtures";
import {
  ApprovalBadge,
  BranchChip,
  CiBadge,
  ciAttemptsLabel,
  IssueLink,
  ReviewBadge,
} from "./TaskBadges";

afterEach(cleanup);

describe("CiBadge", () => {
  it("adds the fix attempts once the loop has spent some", () => {
    const { rerender } = render(<CiBadge ci="red" attempts={2} />);
    expect(screen.getByTestId("ci-badge")).toHaveAccessibleName("CI failed, fix 2/3");
    expect(screen.getByTestId("ci-badge")).toHaveTextContent("CIfix 2/3");

    rerender(<CiBadge ci="green" />);
    expect(screen.getByTestId("ci-badge")).toHaveAccessibleName("CI passed");
    rerender(<CiBadge ci="none" attempts={1} />);
    expect(screen.queryByTestId("ci-badge")).not.toBeInTheDocument();
  });

  it("formats attempts against the loop's limit", () => {
    expect(ciAttemptsLabel(0)).toBeNull();
    expect(ciAttemptsLabel(3)).toBe("fix 3/3");
  });
});

describe("ReviewBadge", () => {
  it("shows the verdict and nothing before a review", () => {
    const { rerender } = render(
      <ReviewBadge
        task={makeTask({ review: { verdict: "changes", summary: "", findings: [] } })}
      />,
    );
    expect(screen.getByTestId("review-badge")).toHaveAccessibleName("Review: Changes requested");

    rerender(
      <ReviewBadge
        task={makeTask({ review: { verdict: "approve", summary: "", findings: [] } })}
      />,
    );
    expect(screen.getByTestId("review-badge")).toHaveAttribute("data-verdict", "approve");

    rerender(
      <ReviewBadge task={makeTask({ review: { verdict: null, summary: "", findings: [] } })} />,
    );
    expect(screen.queryByTestId("review-badge")).not.toBeInTheDocument();
  });
});

describe("ApprovalBadge", () => {
  it("names the reasons and lists them in a tooltip", async () => {
    render(
      <TooltipProvider delayDuration={0}>
        <ApprovalBadge
          task={makeTask({
            needs_human_approval: true,
            approval_reasons: ["Touches migrations/**", "Diff above 400 lines"],
          })}
        />
      </TooltipProvider>,
    );
    const badge = screen.getByRole("button", {
      name: "Needs approval: Touches migrations/**; Diff above 400 lines",
    });
    fireEvent.focus(badge);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Touches migrations/**");
  });

  it("renders nothing when no approval is needed", () => {
    render(<ApprovalBadge task={makeTask()} />);
    expect(screen.queryByTestId("approval-badge")).not.toBeInTheDocument();
  });
});

describe("BranchChip and IssueLink", () => {
  it("shows the branch in full as its title", () => {
    render(<BranchChip branch="shipcrew/abcd1234-add-refunds" />);
    expect(screen.getByTestId("branch-chip")).toHaveAttribute(
      "title",
      "shipcrew/abcd1234-add-refunds",
    );
  });

  it("links the issue when it has a URL", () => {
    const { rerender } = render(
      <IssueLink
        task={makeTask({ issue_number: 12, issue_url: "https://github.com/acme/app/issues/12" })}
      />,
    );
    expect(screen.getByRole("link", { name: "Open issue #12" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/issues/12",
    );

    rerender(<IssueLink task={makeTask({ issue_number: 12 })} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("#12")).toBeInTheDocument();

    rerender(<IssueLink task={makeTask()} />);
    expect(screen.queryByText(/#/)).not.toBeInTheDocument();
  });
});
