// Small presentational pieces shared by the board card and the task drawer.

import type { MouseEvent, PointerEvent } from "react";
import {
  BotIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  CircleXIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  MessageSquareWarningIcon,
  ShieldAlertIcon,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { CI_MAX_ATTEMPTS, type CiStatus, type Task, type TaskAssignee } from "./types";

/** Links and triggers inside a draggable card must not start a drag or open it. */
const stopCardGesture = {
  onClick: (event: MouseEvent) => event.stopPropagation(),
  onPointerDown: (event: PointerEvent) => event.stopPropagation(),
  onMouseDown: (event: MouseEvent) => event.stopPropagation(),
};

const CI_STYLES: Record<Exclude<CiStatus, "none">, { label: string; className: string }> = {
  pending: { label: "CI pending", className: "bg-warning/15 text-warning" },
  green: { label: "CI passed", className: "bg-success/15 text-success" },
  red: { label: "CI failed", className: "bg-destructive/10 text-destructive" },
};

/** "fix 2/3" once the PR loop has spent CI fix turns, else `null`. */
export function ciAttemptsLabel(attempts: number): string | null {
  return attempts > 0 ? `fix ${attempts}/${CI_MAX_ATTEMPTS}` : null;
}

export function CiBadge({
  ci,
  attempts = 0,
  className,
}: {
  ci: CiStatus;
  /** CI fix turns spent (`task.ci_attempts`). */
  attempts?: number;
  className?: string;
}) {
  if (ci === "none") return null;
  const style = CI_STYLES[ci];
  const Icon = ci === "green" ? CircleCheckIcon : ci === "red" ? CircleXIcon : CircleDashedIcon;
  const fix = ciAttemptsLabel(attempts);
  const label = fix ? `${style.label}, ${fix}` : style.label;
  return (
    <Badge
      className={cn("h-5 gap-1 px-1.5 text-xs", style.className, className)}
      data-testid="ci-badge"
      data-ci={ci}
      aria-label={label}
      title={label}
    >
      <Icon aria-hidden />
      CI
      {fix && <span className="font-normal tabular-nums">{fix}</span>}
    </Badge>
  );
}

const REVIEW_STYLES = {
  approve: { label: "Approved", className: "bg-success/15 text-success", Icon: CircleCheckIcon },
  changes: {
    label: "Changes requested",
    className: "bg-warning/15 text-warning",
    Icon: MessageSquareWarningIcon,
  },
} as const;

/** The reviewer's verdict, or nothing before the first review. */
export function ReviewBadge({ task, className }: { task: Task; className?: string }) {
  const verdict = task.review?.verdict;
  if (!verdict) return null;
  const { label, className: tone, Icon } = REVIEW_STYLES[verdict];
  return (
    <Badge
      className={cn("h-5 gap-1 px-1.5 text-xs", tone, className)}
      data-testid="review-badge"
      data-verdict={verdict}
      aria-label={`Review: ${label}`}
      title={`Review: ${label}`}
    >
      <Icon aria-hidden />
      {verdict === "approve" ? "Approved" : "Changes"}
    </Badge>
  );
}

/** "Needs approval", with the policy reasons in a tooltip. */
export function ApprovalBadge({ task, className }: { task: Task; className?: string }) {
  if (!task.needs_human_approval) return null;
  const reasons = task.approval_reasons;
  const label =
    reasons.length > 0 ? `Needs approval: ${reasons.join("; ")}` : "Needs approval before merge";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex h-5 items-center gap-1 rounded-4xl bg-warning/15 px-1.5 text-xs font-medium text-warning outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
            className,
          )}
          data-testid="approval-badge"
          aria-label={label}
          {...stopCardGesture}
        >
          <ShieldAlertIcon aria-hidden className="size-3" />
          Needs approval
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="flex-col items-start">
        {reasons.length > 0 ? (
          <ul className="list-disc pl-4">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : (
          "Needs approval before merge"
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** The task's git branch, truncated to fit the card. */
export function BranchChip({ branch, className }: { branch: string | null; className?: string }) {
  if (!branch) return null;
  return (
    <span
      className={cn(
        "inline-flex h-5 max-w-full min-w-0 items-center gap-1 rounded-md bg-muted px-1.5 font-mono text-[11px] text-muted-foreground",
        className,
      )}
      title={branch}
      data-testid="branch-chip"
    >
      <GitBranchIcon aria-hidden className="size-3 shrink-0" />
      <span className="truncate">{branch}</span>
    </span>
  );
}

/** Link to the task's GitHub issue ("#12"), or plain text without a URL. */
export function IssueLink({ task, className }: { task: Task; className?: string }) {
  if (task.issue_number === null) return null;
  const content = (
    <>
      <CircleDotIcon aria-hidden className="size-3.5 shrink-0" />#{task.issue_number}
    </>
  );
  const classes = cn("inline-flex min-w-0 items-center gap-1 text-xs font-medium", className);
  if (!task.issue_url) {
    return (
      <span className={cn(classes, "text-muted-foreground")} title={`Issue #${task.issue_number}`}>
        {content}
      </span>
    );
  }
  return (
    <a
      href={task.issue_url}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(classes, "text-muted-foreground hover:text-foreground hover:underline")}
      aria-label={`Open issue #${task.issue_number}`}
      {...stopCardGesture}
    >
      {content}
    </a>
  );
}

function initials(id: string): string {
  const name = id.split("@")[0] ?? id;
  const parts = name.split(/[.\-_\s]+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return letters.toUpperCase();
}

export function assigneeLabel(assignee: TaskAssignee | null): string {
  if (!assignee) return "Unassigned";
  return assignee.kind === "agent" ? `Agent ${assignee.id}` : assignee.id;
}

export function AssigneeAvatar({ assignee }: { assignee: TaskAssignee | null }) {
  if (!assignee) return null;
  const label = assigneeLabel(assignee);
  return (
    <Avatar size="sm" title={label} aria-label={label} data-testid="assignee-avatar">
      <AvatarFallback
        className={cn(
          "text-[10px] font-medium",
          assignee.kind === "agent" && "bg-brand-accent/10 text-brand-accent",
        )}
      >
        {assignee.kind === "agent" ? (
          <BotIcon aria-hidden className="size-3.5" />
        ) : (
          initials(assignee.id)
        )}
      </AvatarFallback>
    </Avatar>
  );
}

export function RoleBadge({ role }: { role: string }) {
  return (
    <Badge variant="outline" className="h-5 px-1.5 font-mono text-xs text-muted-foreground">
      {role}
    </Badge>
  );
}

/** PR link, or nothing when the task has no pull request yet. */
export function PullRequestLink({ task, className }: { task: Task; className?: string }) {
  if (!task.pr_url || task.pr_number === null) return null;
  return (
    <a
      href={task.pr_url}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(
        "inline-flex min-w-0 items-center gap-1 text-xs font-medium text-brand-accent hover:underline",
        className,
      )}
      aria-label={`Open pull request #${task.pr_number}`}
      {...stopCardGesture}
    >
      <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />#{task.pr_number}
    </a>
  );
}
