/**
 * Board page (``/board``) — shipcrew's kanban of mission tasks.
 *
 * - Missions and tasks come from `/v1/shipcrew/*` (`@/board/api`); the task
 *   list follows the mission SSE stream and polls while the stream is down.
 * - Columns are a projection of task status (`@/board/columns`). Dragging a
 *   card PATCHes its status: Ready schedules it, Running starts it now, and
 *   Merged asks for confirmation since it normally comes from the PR loop.
 *   The card menu offers the same moves for keyboard users, plus assignment.
 * - A card opens a drawer with the task's acceptance criteria and its live
 *   sub-agent tree (the chat view's `SubagentsGraphView`).
 * - The selected mission and task live in `?mission=` / `?task=`.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  rectIntersection,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { BanIcon, TriangleAlertIcon } from "lucide-react";
import {
  useCreateMission,
  useCreateTask,
  useMissions,
  useMissionTasks,
  useStartTask,
  useStopTask,
  useUpdateTask,
} from "@/board/api";
import { BoardColumn } from "@/board/BoardColumn";
import {
  BOARD_COLUMNS,
  dropAction,
  isBlocked,
  projectColumns,
  type ColumnId,
} from "@/board/columns";
import { columnKeyboardCoordinates } from "@/board/keyboard";
import { NewMissionDialog } from "@/board/NewMissionDialog";
import { NewTaskForm } from "@/board/NewTaskForm";
import { TaskCard } from "@/board/TaskCard";
import { TaskDrawer } from "@/board/TaskDrawer";
import type { Mission, Task, TaskAssignee, TaskPatch } from "@/board/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { showToast } from "@/components/ui/toast";
import { useViewerId } from "@/hooks/useViewerId";
import { useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";

export const MISSION_QUERY_PARAM = "mission";
export const TASK_QUERY_PARAM = "task";

const EMPTY_MISSIONS: Mission[] = [];
const EMPTY_TASKS: Task[] = [];
/** A click that lands right after a drop is the drop, not an open. */
const CLICK_AFTER_DROP_MS = 250;

const TAB_CLASS =
  "flex h-7 max-w-[220px] shrink-0 items-center gap-1.5 rounded-md px-2.5 text-ui text-muted-foreground transition-colors hover:bg-muted hover:text-foreground aria-selected:bg-brand-accent/10 aria-selected:text-foreground";

const collisionDetection: CollisionDetection = (args) => {
  const hits = pointerWithin(args);
  return hits.length > 0 ? hits : rectIntersection(args);
};

function columnLabel(id: unknown): string {
  return BOARD_COLUMNS.find((column) => column.id === id)?.label ?? String(id);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function BoardPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const viewerId = useViewerId();
  const missionsQuery = useMissions();
  const missions = missionsQuery.data ?? EMPTY_MISSIONS;
  const requestedMission = searchParams.get(MISSION_QUERY_PARAM);
  const mission = missions.find((item) => item.id === requestedMission) ?? missions[0] ?? null;
  const missionId = mission?.id ?? null;

  const tasksQuery = useMissionTasks(missionId);
  const tasks = tasksQuery.data ?? EMPTY_TASKS;
  const [blockedOnly, setBlockedOnly] = useState(false);
  const columns = useMemo(() => projectColumns(tasks, { blockedOnly }), [tasks, blockedOnly]);
  const blockedCount = useMemo(() => tasks.filter(isBlocked).length, [tasks]);

  const createMission = useCreateMission();
  const createTask = useCreateTask(missionId);
  const { mutate: mutateTask } = useUpdateTask();
  const { mutate: mutateStart, isPending: starting } = useStartTask();
  const { mutate: mutateStop, isPending: stopping } = useStopTask();

  const selectedTaskId = searchParams.get(TASK_QUERY_PARAM);
  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? null;
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [pendingMerge, setPendingMerge] = useState<Task | null>(null);
  const lastDropRef = useRef(0);

  const setParam = useCallback(
    (key: string, value: string | null) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (value === null) next.delete(key);
          else next.set(key, value);
          if (key === MISSION_QUERY_PARAM) next.delete(TASK_QUERY_PARAM);
          return next;
        },
        { replace: key === TASK_QUERY_PARAM },
      );
    },
    [setSearchParams],
  );

  const patch = useCallback(
    (task: Task, changes: TaskPatch) => {
      mutateTask(
        { task, patch: changes },
        {
          onError: (error) => showToast(`Could not update "${task.title}": ${errorMessage(error)}`),
        },
      );
    },
    [mutateTask],
  );

  const start = useCallback(
    (task: Task) =>
      mutateStart(task, {
        onError: (error) => showToast(`Could not start "${task.title}": ${errorMessage(error)}`),
      }),
    [mutateStart],
  );
  const stop = useCallback(
    (task: Task) =>
      mutateStop(task, {
        onError: (error) => showToast(`Could not stop "${task.title}": ${errorMessage(error)}`),
      }),
    [mutateStop],
  );

  const moveTask = useCallback(
    (task: Task, column: ColumnId) => {
      const action = dropAction(task, column);
      if (action.kind === "start") start(task);
      else if (action.kind === "confirm-merge") setPendingMerge(task);
      else if (action.kind === "patch") patch(task, { status: action.status });
    },
    [patch, start],
  );

  const assignTask = useCallback(
    (task: Task, assignee: TaskAssignee | null) => patch(task, { assignee }),
    [patch],
  );

  const openTask = useCallback(
    (task: Task) => {
      if (Date.now() - lastDropRef.current < CLICK_AFTER_DROP_MS) return;
      setParam(TASK_QUERY_PARAM, task.id);
    },
    [setParam],
  );

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: columnKeyboardCoordinates }),
  );

  const taskTitle = useCallback(
    (id: unknown) => tasks.find((task) => task.id === id)?.title ?? "task",
    [tasks],
  );
  const announcements: Announcements = useMemo(
    () => ({
      onDragStart: ({ active }) => `Picked up ${taskTitle(active.id)}.`,
      onDragOver: ({ active, over }) =>
        over
          ? `${taskTitle(active.id)} is over ${columnLabel(over.id)}.`
          : `${taskTitle(active.id)} is not over a column.`,
      onDragEnd: ({ active, over }) =>
        over
          ? `${taskTitle(active.id)} dropped in ${columnLabel(over.id)}.`
          : `${taskTitle(active.id)} dropped.`,
      onDragCancel: ({ active }) => `Moving ${taskTitle(active.id)} was cancelled.`,
    }),
    [taskTitle],
  );

  const onDragStart = useCallback((event: DragStartEvent) => {
    setActiveTask((event.active.data.current?.task as Task | undefined) ?? null);
  }, []);

  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      setActiveTask(null);
      lastDropRef.current = Date.now();
      const task = event.active.data.current?.task as Task | undefined;
      if (!task || !event.over) return;
      moveTask(task, event.over.id as ColumnId);
    },
    [moveTask],
  );

  if (missionsQuery.isPending) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" data-testid="board-page">
        <Spinner className="size-5 text-muted-foreground" aria-label="Loading board" />
      </div>
    );
  }

  if (missionsQuery.isError) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" data-testid="board-page">
        <div role="alert" className="flex flex-col items-center gap-2 p-6 text-center">
          <TriangleAlertIcon aria-hidden className="size-5 text-muted-foreground" />
          <strong className="font-medium">Board could not load</strong>
          <span className="text-ui text-muted-foreground">{errorMessage(missionsQuery.error)}</span>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => void missionsQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="board-page"
      style={{
        paddingTop: "calc(var(--omnigent-header-height) + var(--omnigent-inset-top))",
        paddingBottom: "var(--omnigent-inset-bottom)",
      }}
    >
      <header className="flex items-start justify-between gap-4 px-6 pt-5">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold">Board</h1>
          <div className="flex items-center gap-1.5 text-ui text-muted-foreground">
            {mission ? (
              <span className="truncate font-mono text-xs" title={mission.repo_path}>
                {mission.repo_path}
              </span>
            ) : (
              <span>No missions yet</span>
            )}
            {mission && !tasksQuery.live && (
              <span className="text-xs" title="Live updates unavailable; refreshing periodically">
                · polling
              </span>
            )}
          </div>
        </div>
        {mission && (
          <Button
            variant={blockedOnly ? "secondary" : "ghost"}
            size="sm"
            aria-pressed={blockedOnly}
            onClick={() => setBlockedOnly((value) => !value)}
            className={cn(blockedCount > 0 && !blockedOnly && "text-destructive")}
          >
            <BanIcon className="size-3.5" />
            Blocked
            <span className="tabular-nums">{blockedCount}</span>
          </Button>
        )}
      </header>

      <nav
        aria-label="Missions"
        className="flex items-center gap-2 overflow-x-auto px-6 py-3 [scrollbar-width:thin]"
      >
        {missions.length > 0 && (
          <div role="tablist" className="flex gap-1">
            {missions.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                className={TAB_CLASS}
                aria-selected={item.id === missionId}
                title={item.title}
                onClick={() => setParam(MISSION_QUERY_PARAM, item.id)}
              >
                <span className="truncate">{item.title}</span>
              </button>
            ))}
          </div>
        )}
        <NewMissionDialog
          onCreate={async (input) => {
            const created = await createMission.mutateAsync(input);
            setParam(MISSION_QUERY_PARAM, created.id);
            return created;
          }}
        />
      </nav>

      {!mission ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-muted-foreground">
          <p>Create a mission to start planning tasks.</p>
        </div>
      ) : tasksQuery.isError && tasks.length === 0 ? (
        <div
          role="alert"
          className="mx-6 rounded-md border px-3 py-2 text-ui text-muted-foreground"
        >
          Tasks could not load: {errorMessage(tasksQuery.error)}
        </div>
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={collisionDetection}
          accessibility={{ announcements }}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          onDragCancel={() => setActiveTask(null)}
        >
          <div
            className="flex min-h-0 flex-1 gap-3 overflow-x-auto border-t px-6 py-4"
            data-testid="board-columns"
          >
            {BOARD_COLUMNS.map((column) => (
              <BoardColumn
                key={column.id}
                column={column}
                tasks={columns[column.id]}
                viewerId={viewerId}
                onOpen={openTask}
                onMove={moveTask}
                onAssign={assignTask}
                header={
                  column.id === "backlog" && !blockedOnly ? (
                    <NewTaskForm
                      onCreate={(input) => createTask.mutateAsync(input)}
                      pending={createTask.isPending}
                    />
                  ) : null
                }
              />
            ))}
          </div>
          <DragOverlay dropAnimation={null}>
            {activeTask && (
              <TaskCard
                task={activeTask}
                viewerId={viewerId}
                overlay
                onOpen={openTask}
                onMove={moveTask}
                onAssign={assignTask}
              />
            )}
          </DragOverlay>
        </DndContext>
      )}

      <TaskDrawer
        task={selectedTask}
        tasks={tasks}
        onClose={() => setParam(TASK_QUERY_PARAM, null)}
        onStart={start}
        onStop={stop}
        pending={starting || stopping}
      />

      <Dialog open={pendingMerge !== null} onOpenChange={(open) => !open && setPendingMerge(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Mark as merged?</DialogTitle>
            <DialogDescription>
              {pendingMerge
                ? `"${pendingMerge.title}" normally reaches Merged through its pull request. Marking it by hand skips CI and review.`
                : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingMerge(null)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                if (pendingMerge) patch(pendingMerge, { status: "merged" });
                setPendingMerge(null);
              }}
            >
              Mark as merged
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
