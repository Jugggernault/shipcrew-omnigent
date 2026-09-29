// Right-side drawer for one task: header (status, PR, CI), acceptance
// checklist, dependencies, and the live sub-agent tree of the task's root
// session, reusing the chat view's SubagentsGraphView.

import * as DialogPrimitive from "radix-ui/dialog";
import {
  CircleCheckIcon,
  CircleIcon,
  ExternalLinkIcon,
  PlayIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useChildSessions } from "@/hooks/useChildSessions";
import { getEmbedRoot } from "@/lib/host";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SubagentsGraphView } from "@/shell/SubagentsGraphView";
import { STATUS_LABELS } from "./columns";
import { assigneeLabel, CiBadge, PullRequestLink, RoleBadge } from "./TaskBadges";
import type { Task } from "./types";

interface TaskDrawerProps {
  task: Task | null;
  /** All tasks of the mission, to name dependencies. */
  tasks: readonly Task[];
  onClose: () => void;
  onStart: (task: Task) => void;
  onStop: (task: Task) => void;
  pending?: boolean;
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

/** The server refuses (409) or ignores a start on these cards. */
export function canStartTask(task: Task): boolean {
  if (task.assignee?.kind === "human") return false;
  return task.status !== "running" && task.status !== "intervention" && task.status !== "merged";
}

export function TaskDrawer({ task, tasks, onClose, onStart, onStop, pending }: TaskDrawerProps) {
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
          {task && (
            <TaskDrawerBody
              task={task}
              tasks={tasks}
              onStart={onStart}
              onStop={onStop}
              pending={pending}
            />
          )}
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
  pending,
}: Omit<TaskDrawerProps, "task" | "onClose"> & { task: Task }) {
  const merged = task.status === "merged";
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
          <CiBadge ci={task.ci} />
          <PullRequestLink task={task} />
          {task.issue_number !== null && <span>Issue #{task.issue_number}</span>}
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
