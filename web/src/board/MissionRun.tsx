// "Run everything" controls in the mission header: the "Run all tasks" button
// (with a confirm step) and the "Ask the crew…" command box. Both only queue
// work: cards move to Ready and the server's scheduler gates (dependencies,
// capacity, owned paths, budget) decide what actually starts.

import { useState, type FormEvent } from "react";
import { PlayIcon, SendHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Task } from "./types";

/** Backlog cards that "Run all tasks" moves to Ready (human-assigned ones stay). */
export function runnableBacklog(tasks: readonly Task[]): Task[] {
  return tasks.filter((task) => task.status === "backlog" && task.assignee?.kind !== "human");
}

function taskCount(count: number): string {
  return count === 1 ? "1 task" : `${count} tasks`;
}

export function RunAllTasksButton({
  count,
  onRun,
  pending = false,
}: {
  /** Backlog tasks the run would move to Ready. */
  count: number;
  onRun: () => void;
  pending?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <Button
        size="sm"
        disabled={count === 0}
        loading={pending}
        title={count === 0 ? "No backlog task to run" : undefined}
        onClick={() => setConfirming(true)}
      >
        <PlayIcon className="size-3.5" />
        Run all tasks
        <span className="tabular-nums" data-testid="run-all-count">
          {count}
        </span>
      </Button>
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Run {taskCount(count)}?</DialogTitle>
            <DialogDescription>
              {`${taskCount(count)} from the backlog move to Ready. They start as soon as their dependencies are merged and capacity, owned paths and budget allow.`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                setConfirming(false);
                onRun();
              }}
            >
              Run {taskCount(count)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * Free-text orders for the mission ("run all", "lance tout", "sync", "stop all").
 * The server maps them with fixed rules and answers what it did.
 */
export function MissionCommandBox({
  onCommand,
}: {
  /** Sends the text; rejects with the server's message (e.g. the supported commands). */
  onCommand: (text: string) => Promise<unknown>;
}) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || pending) return;
    setPending(true);
    try {
      await onCommand(value);
      setText("");
    } catch {
      // The caller reports the error; keep the text so it can be fixed.
    } finally {
      setPending(false);
    }
  };

  return (
    <form
      aria-label="Ask the crew"
      className="flex items-center gap-1"
      onSubmit={(event) => void submit(event)}
    >
      <Input
        aria-label="Command for the crew"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Ask the crew…"
        title='Try "run all", "plan", "sync" or "stop all" (French works too)'
        maxLength={2000}
        className="h-7 w-44 text-xs md:text-xs"
      />
      <Button
        type="submit"
        variant="ghost"
        size="sm"
        aria-label="Send command"
        disabled={!text.trim()}
        loading={pending}
      >
        <SendHorizontalIcon className="size-3.5" />
      </Button>
    </form>
  );
}
