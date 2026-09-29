// One droppable board column.

import type { ReactNode } from "react";
import { useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";
import type { BoardColumn as BoardColumnSpec } from "./columns";
import { TaskCard, type TaskCardActions } from "./TaskCard";
import type { Task } from "./types";

interface BoardColumnProps extends TaskCardActions {
  column: BoardColumnSpec;
  tasks: readonly Task[];
  viewerId: string | null;
  /** Extra content above the cards (the Backlog column's new-task form). */
  header?: ReactNode;
}

export function BoardColumn({ column, tasks, viewerId, header, ...actions }: BoardColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: column.id });
  const headingId = `board-column-${column.id}`;
  return (
    <section
      ref={setNodeRef}
      aria-labelledby={headingId}
      data-testid={`board-column-${column.id}`}
      className={cn(
        // Columns share the width so all six fit next to a default sidebar at
        // 1600px; below ~1250px of board width they scroll horizontally.
        "flex min-w-[192px] max-w-[340px] flex-1 basis-0 flex-col rounded-xl border bg-muted/30 transition-colors",
        isOver && "border-brand-accent/60 bg-brand-accent/5",
      )}
    >
      <header className="flex items-baseline gap-2 px-3 pt-3 pb-2">
        <h2 id={headingId} className="text-ui font-semibold">
          {column.label}
        </h2>
        <span className="text-xs text-muted-foreground tabular-nums" data-testid="column-count">
          {tasks.length}
        </span>
        <span className="ml-auto truncate text-xs text-muted-foreground" title={column.hint}>
          {column.hint}
        </span>
      </header>
      <div className="flex min-h-24 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2">
        {header}
        {tasks.map((task) => (
          <TaskCard key={task.id} task={task} viewerId={viewerId} {...actions} />
        ))}
      </div>
    </section>
  );
}
