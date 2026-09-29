// Right-side drawer for one task: header (status, PR, CI, why it needs a
// human), the approval gate, pull request and review, a "request changes"
// box, acceptance checklist, dependencies, and the live sub-agent tree of the
// task's root session (reviewer and integrator children show up there too),
// reusing the chat view's SubagentsGraphView.

import { useState, type FormEvent } from "react";
import * as DialogPrimitive from "radix-ui/dialog";
import {
  CircleCheckIcon,
  CircleIcon,
  ExternalLinkIcon,
  InboxIcon,
  PlayIcon,
  ShieldCheckIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useChildSessions } from "@/hooks/useChildSessions";
import { getEmbedRoot } from "@/lib/host";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SubagentsGraphView } from "@/shell/SubagentsGraphView";
import { STATUS_LABELS } from "./columns";
import { interventionReason } from "./intervention";
import {
  assigneeLabel,
  BranchChip,
  CiBadge,
  ciAttemptsLabel,
  IssueLink,
  PullRequestLink,
  ReviewBadge,
  RoleBadge,
} from "./TaskBadges";
import {
  CI_MAX_ATTEMPTS,
  FINDING_SEVERITIES,
  type CiStatus,
  type FindingSeverity,
  type ReviewFinding,
  type Task,
  type TaskReview,
} from "./types";

interface TaskDrawerProps {
  task: Task | null;
  /** All tasks of the mission, to name dependencies. */
  tasks: readonly Task[];
  onClose: () => void;
  onStart: (task: Task) => void;
  onStop: (task: Task) => void;
  /** Approve a merge that waits on `needs_human_approval`. */
  onApprove: (task: Task) => void;
  /** Send feedback to the developer; resolves once the server accepted it. */
  onRequestChanges: (task: Task, message: string) => Promise<unknown>;
  pending?: boolean;
  approving?: boolean;
}

/**
 * The task's sub-agent graph. Mounted only once the child list has loaded:
 * before that `useChildSessions` returns a fresh `[]` per render, which the
 * graph's render-time sync turns into an update loop (the chat panel waits
 * the same way).
 */
function AgentTree({ rootSessionId }: { rootSessionId: string }) {
  const { children, isLoading, error } = useChildSessions(rootSessionId);
  if (children.length === 0 && (isLoading || error)) {
    return (
      <div
        className="flex h-32 items-center justify-center rounded-lg border text-xs text-muted-foreground"
        data-testid="task-agent-tree-pending"
      >
        {error ? "Could not load the agents." : "Loading agents…"}
      </div>
    );
  }
  return (
    <div className="h-80 overflow-hidden rounded-lg border" data-testid="task-agent-tree">
      <SubagentsGraphView conversationId={rootSessionId} rootSessionId={rootSessionId} />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      {children}
    </section>
  );
}

const CI_TEXT: Record<CiStatus, string> = {
  none: "Not run yet",
  pending: "Running",
  green: "Passed",
  red: "Failed",
};

const CI_TONES: Record<CiStatus, string> = {
  none: "text-muted-foreground",
  pending: "text-warning",
  green: "text-success",
  red: "text-destructive",
};

const SEVERITY_LABELS: Record<FindingSeverity, string> = {
  blocker: "Blockers",
  major: "Major",
  minor: "Minor",
};

const SEVERITY_TONES: Record<FindingSeverity, string> = {
  blocker: "text-destructive",
  major: "text-warning",
  minor: "text-muted-foreground",
};

function findingLocation(finding: ReviewFinding): string {
  return finding.line === null ? finding.file : `${finding.file}:${finding.line}`;
}

function PullRequestSection({ task }: { task: Task }) {
  const fix = ciAttemptsLabel(task.ci_attempts);
  return (
    <Section title="Pull request">
      <dl className="grid grid-cols-[88px_1fr] items-center gap-x-3 gap-y-1.5 text-ui">
        <dt className="text-xs text-muted-foreground">PR</dt>
        <dd className="min-w-0">
          {task.pr_url ? (
            <PullRequestLink task={task} className="text-ui" />
          ) : (
            <span className="text-xs text-muted-foreground">Not opened yet</span>
          )}
        </dd>
        {task.branch && (
          <>
            <dt className="text-xs text-muted-foreground">Branch</dt>
            <dd className="flex min-w-0">
              <BranchChip branch={task.branch} />
            </dd>
          </>
        )}
        <dt className="text-xs text-muted-foreground">CI</dt>
        <dd className={cn("text-ui", CI_TONES[task.ci])} data-testid="drawer-ci" data-ci={task.ci}>
          {CI_TEXT[task.ci]}
        </dd>
        <dt className="text-xs text-muted-foreground">Fix attempts</dt>
        <dd className="text-xs tabular-nums" data-testid="drawer-ci-attempts">
          {fix ? `${task.ci_attempts} of ${CI_MAX_ATTEMPTS}` : `None of ${CI_MAX_ATTEMPTS} used`}
        </dd>
      </dl>
    </Section>
  );
}

function ReviewSection({ task, review }: { task: Task; review: TaskReview }) {
  const groups = FINDING_SEVERITIES.map((severity) => ({
    severity,
    findings: review.findings.filter((finding) => finding.severity === severity),
  })).filter((group) => group.findings.length > 0);
  return (
    <Section title="Review">
      <div className="flex items-center gap-2">
        {review.verdict ? (
          <ReviewBadge task={task} />
        ) : (
          <span className="text-xs text-muted-foreground">Review in progress</span>
        )}
      </div>
      {review.summary && (
        <p className="whitespace-pre-wrap text-ui" data-testid="review-summary">
          {review.summary}
        </p>
      )}
      {groups.length === 0 ? (
        review.verdict && <p className="text-xs text-muted-foreground">No findings.</p>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map(({ severity, findings }) => (
            <div key={severity} className="flex flex-col gap-1">
              <h4 className={cn("text-xs font-medium", SEVERITY_TONES[severity])}>
                {SEVERITY_LABELS[severity]} ({findings.length})
              </h4>
              <ul
                className="flex flex-col gap-1.5"
                aria-label={`${SEVERITY_LABELS[severity]} findings`}
              >
                {findings.map((finding, index) => (
                  <li
                    // Findings carry no id; the same file:line can repeat.
                    // eslint-disable-next-line react/no-array-index-key
                    key={`${findingLocation(finding)}-${index}`}
                    className="flex flex-col gap-0.5 rounded-md border px-2 py-1.5"
                  >
                    <code className="truncate font-mono text-xs text-muted-foreground">
                      {findingLocation(finding)}
                    </code>
                    <span className="whitespace-pre-wrap">{finding.message}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function ApprovalSection({
  task,
  onApprove,
  approving,
}: {
  task: Task;
  onApprove: (task: Task) => void;
  approving?: boolean;
}) {
  return (
    <section
      className="flex flex-col gap-2 rounded-lg border border-warning/40 bg-warning/5 p-3"
      aria-labelledby="task-approval-heading"
      data-testid="drawer-approval"
    >
      <h3 id="task-approval-heading" className="text-ui font-semibold">
        Merge needs your approval
      </h3>
      {task.approval_reasons.length > 0 ? (
        <ul className="list-disc pl-5 text-ui" aria-label="Approval reasons">
          {task.approval_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">The approval policy holds this merge.</p>
      )}
      <div>
        <Button size="sm" onClick={() => onApprove(task)} loading={approving}>
          <ShieldCheckIcon className="size-3.5" />
          Approve merge
        </Button>
      </div>
    </section>
  );
}

function RequestChangesForm({
  task,
  onRequestChanges,
}: {
  task: Task;
  onRequestChanges: (task: Task, message: string) => Promise<unknown>;
}) {
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = message.trim();
    if (!text) return;
    setPending(true);
    setError(null);
    try {
      await onRequestChanges(task, text);
      setMessage("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <Section title="Request changes">
      <form
        className="flex flex-col gap-2"
        aria-label="Request changes"
        onSubmit={(event) => void submit(event)}
      >
        <Textarea
          aria-label="Feedback for the developer"
          placeholder="What should the developer change?"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          className="min-h-20"
        />
        {error && (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        )}
        <div>
          <Button
            type="submit"
            size="sm"
            variant="outline"
            disabled={!message.trim()}
            loading={pending}
          >
            Request changes
          </Button>
        </div>
      </form>
    </Section>
  );
}

/** Feedback goes to the developer's root session, so it needs one; merged work is done. */
export function canRequestChanges(task: Task): boolean {
  return (
    task.root_session_id !== null && (task.status === "review" || task.status === "intervention")
  );
}

/** The server refuses (409) or ignores a start on these cards. */
export function canStartTask(task: Task): boolean {
  if (task.assignee?.kind === "human") return false;
  return task.status !== "running" && task.status !== "intervention" && task.status !== "merged";
}

export function TaskDrawer({ task, onClose, ...props }: TaskDrawerProps) {
  return (
    <DialogPrimitive.Root open={task !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogPrimitive.Portal container={getEmbedRoot() ?? undefined}>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-background/40 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <DialogPrimitive.Content
          data-testid="task-drawer"
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full max-w-[560px] flex-col border-l bg-popover text-ui text-popover-foreground shadow-dialog outline-none",
            "duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] data-open:animate-in data-open:slide-in-from-right data-closed:animate-out data-closed:slide-out-to-right",
          )}
          style={{
            paddingTop: "var(--omnigent-inset-top)",
            paddingBottom: "var(--omnigent-inset-bottom)",
          }}
        >
          {task && <TaskDrawerBody task={task} {...props} />}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function TaskDrawerBody({
  task,
  tasks,
  onStart,
  onStop,
  onApprove,
  onRequestChanges,
  pending,
  approving,
}: Omit<TaskDrawerProps, "task" | "onClose"> & { task: Task }) {
  const merged = task.status === "merged";
  const intervention = interventionReason(task);
  const showPullRequest = task.pr_url !== null || task.branch !== null || task.ci !== "none";
  const byId = new Map(tasks.map((item) => [item.id, item]));

  return (
    <>
      <header className="flex flex-col gap-2 border-b px-5 pt-4 pb-3">
        <div className="flex items-start gap-2">
          <DialogPrimitive.Title className="min-w-0 flex-1 text-base leading-snug font-semibold">
            {task.title}
          </DialogPrimitive.Title>
          <DialogPrimitive.Close asChild>
            <Button variant="ghost" size="icon-sm" aria-label="Close">
              <XIcon className="size-4 text-muted-foreground" />
            </Button>
          </DialogPrimitive.Close>
        </div>
        <DialogPrimitive.Description className="sr-only">
          Task details and sub-agent tree
        </DialogPrimitive.Description>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge
            variant={task.status === "blocked" ? "destructive" : "secondary"}
            className="h-5 px-1.5 text-xs"
            data-testid="drawer-status"
          >
            {STATUS_LABELS[task.status]}
          </Badge>
          <RoleBadge role={task.role} />
          <CiBadge ci={task.ci} attempts={task.ci_attempts} />
          <ReviewBadge task={task} />
          <PullRequestLink task={task} />
          <IssueLink task={task} />
          <span>{assigneeLabel(task.assignee)}</span>
          {task.cost_usd > 0 && (
            <span className="tabular-nums">{formatSessionCostUsd(task.cost_usd)}</span>
          )}
        </div>
        {task.blocked_reason && (
          <p role="status" className="text-xs text-destructive">
            {task.blocked_reason}
          </p>
        )}
        {intervention && (
          <div
            role="status"
            className="flex items-start gap-2 rounded-md bg-warning/10 px-2.5 py-2 text-xs text-warning"
            data-testid="drawer-intervention"
            data-kind={intervention.kind}
          >
            <div className="min-w-0 flex-1">
              <p className="font-medium">{intervention.label}</p>
              <p className="text-foreground/80">{intervention.detail}</p>
            </div>
            {intervention.kind === "guardrail" && (
              <Button size="xs" variant="outline" asChild>
                <Link to="/inbox">
                  <InboxIcon className="size-3" />
                  Open Inbox
                </Link>
              </Button>
            )}
          </div>
        )}
        <div className="flex items-center gap-2 pt-1">
          {task.status === "running" ? (
            <Button size="sm" variant="outline" onClick={() => onStop(task)} disabled={pending}>
              <SquareIcon className="size-3.5" />
              Stop
            </Button>
          ) : (
            <Button
              size="sm"
              onClick={() => onStart(task)}
              disabled={pending || !canStartTask(task)}
            >
              <PlayIcon className="size-3.5" />
              Start
            </Button>
          )}
          {task.root_session_id && (
            <Button size="sm" variant="ghost" asChild>
              <Link to={`/c/${encodeURIComponent(task.root_session_id)}`}>
                <ExternalLinkIcon className="size-3.5" />
                Open session
              </Link>
            </Button>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-4">
        {task.body && <p className="whitespace-pre-wrap text-ui">{task.body}</p>}

        {task.needs_human_approval && (
          <ApprovalSection task={task} onApprove={onApprove} approving={approving} />
        )}

        {showPullRequest && <PullRequestSection task={task} />}

        {task.review && <ReviewSection task={task} review={task.review} />}

        {canRequestChanges(task) && (
          <RequestChangesForm key={task.id} task={task} onRequestChanges={onRequestChanges} />
        )}

        <Section title="Acceptance">
          {task.acceptance.length === 0 ? (
            <p className="text-xs text-muted-foreground">No acceptance criteria.</p>
          ) : (
            <ul className="flex flex-col gap-1.5" aria-label="Acceptance criteria">
              {task.acceptance.map((item) => (
                <li key={item} className="flex items-start gap-2">
                  {merged ? (
                    <CircleCheckIcon
                      aria-label="Met"
                      className="mt-0.5 size-4 shrink-0 text-success"
                    />
                  ) : (
                    <CircleIcon
                      aria-label="Open"
                      className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                    />
                  )}
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {task.depends_on.length > 0 && (
          <Section title="Depends on">
            <ul className="flex flex-col gap-1 text-ui">
              {task.depends_on.map((id) => {
                const dependency = byId.get(id);
                return (
                  <li key={id} className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate">{dependency?.title ?? id}</span>
                    {dependency && (
                      <span className="text-xs text-muted-foreground">
                        {STATUS_LABELS[dependency.status]}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          </Section>
        )}

        {task.owned_paths.length > 0 && (
          <Section title="Owned paths">
            <ul className="flex flex-wrap gap-1">
              {task.owned_paths.map((path) => (
                <li key={path}>
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{path}</code>
                </li>
              ))}
            </ul>
          </Section>
        )}

        <Section title="Agents">
          {task.root_session_id ? (
            <AgentTree rootSessionId={task.root_session_id} />
          ) : (
            <div
              className="flex h-32 items-center justify-center rounded-lg border border-dashed text-xs text-muted-foreground"
              data-testid="task-agent-tree-empty"
            >
              Not started yet. Start the task to see its agents here.
            </div>
          )}
        </Section>
      </div>
    </>
  );
}
