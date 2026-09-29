// Planner controls in the mission header: "Plan from PRD" (a dialog with an
// optional PRD) and the planner run's status. The planner writes
// `.shipcrew/plan.json` in the mission repo; the server imports it as tasks.

import { useState, type FormEvent } from "react";
import { CircleCheckIcon, CircleXIcon, SparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type { Mission, MissionPlan } from "./types";

const IDLE_PLAN: MissionPlan = { status: "idle", session_id: null, error: null, imported_count: 0 };

/** A mission's plan, defaulting to idle for servers that predate planning. */
export function missionPlan(mission: Mission): MissionPlan {
  return mission.plan ?? IDLE_PLAN;
}

function taskCount(count: number): string {
  return count === 1 ? "1 task" : `${count} tasks`;
}

export function PlanFromPrdDialog({
  mission,
  onPlan,
  onAutoRunChange,
}: {
  mission: Mission;
  /** Starts the planner; rejects with the server's message. */
  onPlan: (prd: string | undefined) => Promise<unknown>;
  /** Saves `mission.auto_run`; the checkbox is hidden without it. */
  onAutoRunChange?: (autoRun: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [prd, setPrd] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = missionPlan(mission).status === "running";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await onPlan(prd.trim() || undefined);
      setPrd("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setError(null);
      }}
    >
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          disabled={running}
          title={running ? "The planner is already running" : undefined}
        >
          <SparklesIcon className="size-3.5" />
          Plan from PRD
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Plan from PRD</DialogTitle>
          <DialogDescription>
            A planner agent breaks the PRD into tasks for {mission.title}. Leave it empty to plan
            from the PRD already in the repository.
          </DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-3" onSubmit={(event) => void submit(event)}>
          <label className="flex flex-col gap-1 text-ui">
            PRD (optional)
            <Textarea
              autoFocus
              value={prd}
              onChange={(event) => setPrd(event.target.value)}
              placeholder="What should this mission ship?"
              className="max-h-[50vh] min-h-40 font-mono text-xs md:text-xs"
            />
          </label>
          {onAutoRunChange && (
            <label className="flex items-center gap-2 text-ui">
              <Checkbox
                checked={mission.auto_run === true}
                onCheckedChange={(checked) => onAutoRunChange(checked === true)}
              />
              Run automatically after planning
            </label>
          )}
          {error && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending}>
              Start planning
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Planner run status; renders nothing while idle. */
export function MissionPlanStatus({ plan, className }: { plan: MissionPlan; className?: string }) {
  const base = cn("inline-flex min-w-0 items-center gap-1.5 text-xs", className);
  if (plan.status === "running") {
    const label = (
      <>
        <Spinner aria-hidden className="size-3" />
        Planning…
      </>
    );
    return (
      <span className={cn(base, "text-muted-foreground")} role="status" data-plan="running">
        {plan.session_id ? (
          <Link
            to={`/c/${encodeURIComponent(plan.session_id)}`}
            className="inline-flex items-center gap-1.5 hover:text-foreground hover:underline"
            title="Open the planner session"
          >
            {label}
          </Link>
        ) : (
          label
        )}
      </span>
    );
  }
  if (plan.status === "imported") {
    return (
      <span className={cn(base, "text-success")} role="status" data-plan="imported">
        <CircleCheckIcon aria-hidden className="size-3.5" />
        Imported {taskCount(plan.imported_count)}
      </span>
    );
  }
  if (plan.status === "failed") {
    const message = plan.error ? `Plan failed: ${plan.error}` : "Plan failed";
    return (
      <span
        className={cn(base, "text-destructive")}
        role="status"
        data-plan="failed"
        title={message}
      >
        <CircleXIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate">{message}</span>
      </span>
    );
  }
  return null;
}
