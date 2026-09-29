// Small presentational pieces shared by the board card and the task drawer.

import {
  BotIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleXIcon,
  GitPullRequestIcon,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CiStatus, Task, TaskAssignee } from "./types";

const CI_STYLES: Record<Exclude<CiStatus, "none">, { label: string; className: string }> = {
  pending: { label: "CI pending", className: "bg-warning/15 text-warning" },
  green: { label: "CI passed", className: "bg-success/15 text-success" },
  red: { label: "CI failed", className: "bg-destructive/10 text-destructive" },
};

export function CiBadge({ ci, className }: { ci: CiStatus; className?: string }) {
  if (ci === "none") return null;
  const style = CI_STYLES[ci];
  const Icon = ci === "green" ? CircleCheckIcon : ci === "red" ? CircleXIcon : CircleDashedIcon;
  return (
    <Badge
      className={cn("h-5 gap-1 px-1.5 text-xs", style.className, className)}
      data-testid="ci-badge"
      data-ci={ci}
      aria-label={style.label}
      title={style.label}
    >
      <Icon aria-hidden />
      CI
    </Badge>
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
      onClick={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />#{task.pr_number}
    </a>
  );
}
