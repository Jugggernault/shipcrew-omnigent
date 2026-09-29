// One task on the board. Pointer users drag the whole card; keyboard users
// drag from the grip handle or use the actions menu ("Move to …"), which is
// also where the assignee is changed.

import { memo, type HTMLAttributes, type MouseEvent } from "react";
import { useDraggable } from "@dnd-kit/core";
import { BanIcon, GripVerticalIcon, LinkIcon, MoreHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { cn } from "@/lib/utils";
import { BOARD_COLUMNS, canMoveTo, columnForStatus, isBlocked, type ColumnId } from "./columns";
import { AssigneeAvatar, CiBadge, PullRequestLink, RoleBadge } from "./TaskBadges";
import type { Task, TaskAssignee } from "./types";

export interface TaskCardActions {
  onOpen: (task: Task) => void;
  onMove: (task: Task, column: ColumnId) => void;
  onAssign: (task: Task, assignee: TaskAssignee | null) => void;
}

interface TaskCardProps extends TaskCardActions {
  task: Task;
  /** Viewer id for "Assign to me"; `null` hides the option. */
  viewerId: string | null;
  /** Rendered inside the drag overlay: no drag wiring, lifted look. */
  overlay?: boolean;
}

function dependencyLabel(count: number): string {
  return count === 1 ? "1 dependency" : `${count} dependencies`;
}

type DragListeners = ReturnType<typeof useDraggable>["listeners"];

function pickListeners(
  listeners: DragListeners,
  keys: readonly string[],
): HTMLAttributes<HTMLElement> {
  if (!listeners) return {};
  return Object.fromEntries(Object.entries(listeners).filter(([key]) => keys.includes(key)));
}

interface DragWiring {
  setNodeRef?: (node: HTMLElement | null) => void;
  setActivatorNodeRef?: (node: HTMLElement | null) => void;
  attributes?: HTMLAttributes<HTMLElement>;
  listeners?: DragListeners;
  isDragging?: boolean;
}

const NO_DRAG: DragWiring = {};

function DraggableTaskCard(props: Omit<TaskCardProps, "overlay">) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useDraggable({
    id: props.task.id,
    data: { task: props.task },
  });
  return (
    <TaskCardView
      {...props}
      drag={{ attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging }}
    />
  );
}

function TaskCardView({
  task,
  viewerId,
  overlay = false,
  onOpen,
  onMove,
  onAssign,
  drag = NO_DRAG,
}: TaskCardProps & { drag?: DragWiring }) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging = false } = drag;
  const blocked = isBlocked(task);
  const currentColumn = columnForStatus(task.status);
  const stop = (event: MouseEvent) => event.stopPropagation();

  return (
    <div
      ref={setNodeRef}
      data-testid="task-card"
      data-task-id={task.id}
      data-status={task.status}
      className={cn(
        "group flex cursor-grab flex-col gap-2 rounded-lg border bg-card px-3 py-2.5 text-[13px] shadow-sm transition-colors select-none hover:border-muted-foreground/40 active:cursor-grabbing",
        blocked && "border-destructive/40",
        isDragging && "opacity-40",
        overlay && "rotate-1 shadow-lg ring-2 ring-brand-accent/25",
      )}
      {...pickListeners(listeners, ["onMouseDown", "onTouchStart", "onPointerDown"])}
      onClick={() => onOpen(task)}
    >
      <div className="flex min-w-0 items-start gap-1">
        <button
          ref={setActivatorNodeRef}
          type="button"
          className="-ml-1 mt-0.5 shrink-0 cursor-grab rounded text-muted-foreground/60 opacity-60 hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
          {...attributes}
          {...pickListeners(listeners, ["onKeyDown"])}
          aria-label={`Drag ${task.title}`}
          onClick={stop}
        >
          <GripVerticalIcon aria-hidden className="size-3.5" />
        </button>
        <button
          type="button"
          className="min-w-0 flex-1 text-left font-semibold leading-snug outline-none focus-visible:underline"
          onClick={(event) => {
            event.stopPropagation();
            onOpen(task);
          }}
        >
          <span className="line-clamp-2">{task.title}</span>
        </button>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon-xs"
              className="-mr-1 shrink-0 text-muted-foreground"
              aria-label={`Actions for ${task.title}`}
              onClick={stop}
              onMouseDown={stop}
              onPointerDown={stop}
            >
              <MoreHorizontalIcon className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" onClick={stop}>
            <DropdownMenuLabel>Move to</DropdownMenuLabel>
            {BOARD_COLUMNS.filter(
              (column) =>
                canMoveTo(column.id) && (column.id !== currentColumn || task.status === "blocked"),
            ).map((column) => (
              <DropdownMenuItem key={column.id} onSelect={() => onMove(task, column.id)}>
                {column.label}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Assign</DropdownMenuLabel>
            <DropdownMenuItem onSelect={() => onAssign(task, { kind: "agent", id: task.role })}>
              Agent ({task.role})
            </DropdownMenuItem>
            {viewerId && (
              <DropdownMenuItem onSelect={() => onAssign(task, { kind: "human", id: viewerId })}>
                Me
              </DropdownMenuItem>
            )}
            {task.assignee && (
              <DropdownMenuItem onSelect={() => onAssign(task, null)}>Unassign</DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onOpen(task)}>Open details</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <RoleBadge role={task.role} />
        {task.status === "blocked" && (
          <span className="inline-flex h-5 items-center gap-1 rounded-4xl bg-destructive/10 px-1.5 text-xs font-medium text-destructive">
            <BanIcon aria-hidden className="size-3" />
            Blocked
          </span>
        )}
        <CiBadge ci={task.ci} />
        <PullRequestLink task={task} />
      </div>

      {blocked && task.blocked_reason && (
        <p className="line-clamp-2 text-xs text-destructive" title={task.blocked_reason}>
          {task.blocked_reason}
        </p>
      )}

      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        {task.depends_on.length > 0 && (
          <span
            className="inline-flex items-center gap-1"
            title={dependencyLabel(task.depends_on.length)}
          >
            <LinkIcon aria-hidden className="size-3" />
            <span aria-label={dependencyLabel(task.depends_on.length)}>
              {task.depends_on.length}
            </span>
          </span>
        )}
        {task.cost_usd > 0 && (
          <span className="tabular-nums" aria-label={`Cost ${formatSessionCostUsd(task.cost_usd)}`}>
            {formatSessionCostUsd(task.cost_usd)}
          </span>
        )}
        <span className="flex-1" />
        <AssigneeAvatar assignee={task.assignee} />
      </div>
    </div>
  );
}

/**
 * A board card. The overlay variant (inside `DragOverlay`) must not register a
 * second draggable under the same id, so it renders the bare view.
 */
function TaskCardComponent({ overlay = false, ...props }: TaskCardProps) {
  return overlay ? <TaskCardView {...props} overlay /> : <DraggableTaskCard {...props} />;
}

export const TaskCard = memo(TaskCardComponent);
