// The mission's ship stage on the board: a status chip (Planning / Building /
// Shipping / Shipped <url> / Ship failed), the "Ship now" button, the report
// dialog (deploy link first, rendered Markdown, copy button) and the plan
// decisions. The server does the work: once every agent task is merged it
// deploys `main` to Vercel, checks the URL itself and writes the report.

import { useState, type ComponentProps } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CheckIcon,
  CopyIcon,
  ExternalLinkIcon,
  FileTextIcon,
  RocketIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import type { Mission, MissionShip, Task } from "./types";

export type MissionPhaseKind = "planning" | "building" | "shipping" | "shipped" | "ship-failed";

export interface MissionPhase {
  kind: MissionPhaseKind;
  label: string;
  /** The deployment, for "shipped". */
  url: string | null;
}

const IDLE_SHIP: MissionShip = {
  status: "idle",
  url: null,
  report_md: null,
  error: null,
  note: null,
  started_at: null,
  finished_at: null,
  session_id: null,
  decisions: [],
  cost_usd: 0,
};

/** The mission's ship state; servers that predate the ship stage send none. */
export function missionShip(mission: Mission): MissionShip {
  return mission.ship ?? IDLE_SHIP;
}

/** Where the mission is, for the header chip. */
export function missionPhase(mission: Mission, tasks: readonly Task[]): MissionPhase {
  const ship = missionShip(mission);
  if (ship.status === "deploying" || ship.status === "verifying") {
    return { kind: "shipping", label: "Shipping", url: null };
  }
  if (ship.status === "done") return { kind: "shipped", label: "Shipped", url: ship.url };
  if (ship.status === "failed") return { kind: "ship-failed", label: "Ship failed", url: null };
  if (mission.plan.status === "running" || tasks.length === 0) {
    return { kind: "planning", label: "Planning", url: null };
  }
  return { kind: "building", label: "Building", url: null };
}

/** Same rule as the server: every agent task merged, no card waiting on a human. */
export function shipReady(tasks: readonly Task[]): boolean {
  const agent = tasks.filter((task) => task.assignee?.kind !== "human");
  return agent.length > 0 && agent.every((task) => task.status === "merged");
}

const PHASE_TONES: Record<MissionPhaseKind, string> = {
  planning: "bg-muted text-muted-foreground",
  building: "bg-brand-accent/10 text-foreground",
  shipping: "bg-warning/10 text-warning",
  shipped: "bg-success/10 text-success",
  "ship-failed": "bg-destructive/10 text-destructive",
};

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function MissionStatusChip({
  mission,
  tasks,
  className,
}: {
  mission: Mission;
  tasks: readonly Task[];
  className?: string;
}) {
  const phase = missionPhase(mission, tasks);
  const ship = missionShip(mission);
  const title =
    phase.kind === "ship-failed"
      ? (ship.error ?? undefined)
      : phase.kind === "shipping"
        ? ship.status === "verifying"
          ? "Checking the deployment URL"
          : "Deploying to Vercel"
        : undefined;
  return (
    <Badge
      data-testid="mission-status-chip"
      data-phase={phase.kind}
      title={title}
      className={cn("gap-1 text-xs", PHASE_TONES[phase.kind], className)}
    >
      {phase.kind === "shipping" && <Spinner className="size-3" aria-hidden />}
      {phase.kind === "ship-failed" && <TriangleAlertIcon aria-hidden />}
      <span>{phase.label}</span>
      {phase.kind === "shipped" && phase.url && (
        <a
          href={phase.url}
          target="_blank"
          rel="noopener noreferrer"
          className="font-mono underline-offset-2 hover:underline"
        >
          {hostOf(phase.url)}
        </a>
      )}
    </Badge>
  );
}

/** Why the mission does not ship (idle) or why its ship failed. */
export function MissionShipNotice({ mission }: { mission: Mission }) {
  const ship = missionShip(mission);
  if (!ship.error || (ship.status !== "idle" && ship.status !== "failed")) return null;
  return (
    <p
      role="status"
      className="mt-1 flex max-w-[560px] items-start gap-1.5 text-xs text-destructive"
      data-testid="mission-ship-notice"
    >
      <TriangleAlertIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>{ship.status === "failed" ? `Ship failed: ${ship.error}` : ship.error}</span>
    </p>
  );
}

export function DecisionsList({
  decisions,
  label,
  className,
}: {
  decisions: readonly string[];
  label: string;
  className?: string;
}) {
  return (
    <ul aria-label={label} className={cn("flex list-disc flex-col gap-1 pl-4 text-ui", className)}>
      {decisions.map((decision) => (
        <li key={decision}>{decision}</li>
      ))}
    </ul>
  );
}

/** The planner's decisions, folded under the mission header. */
export function PlanDecisions({ mission }: { mission: Mission }) {
  const decisions = mission.plan_decisions ?? [];
  if (decisions.length === 0) return null;
  return (
    <details className="mt-1 max-w-[560px] text-xs" data-testid="plan-decisions">
      <summary className="cursor-pointer text-muted-foreground select-none hover:text-foreground">
        Decisions ({decisions.length})
      </summary>
      <DecisionsList decisions={decisions} label="Plan decisions" className="mt-1" />
    </details>
  );
}

export function ShipButton({
  mission,
  tasks,
  onShip,
  pending = false,
}: {
  mission: Mission;
  tasks: readonly Task[];
  onShip: () => void;
  pending?: boolean;
}) {
  const ship = missionShip(mission);
  const shipping = ship.status === "deploying" || ship.status === "verifying";
  const ready = shipReady(tasks);
  const again = ship.status === "done" || ship.status === "failed";
  return (
    <Button
      variant="ghost"
      size="sm"
      disabled={shipping || !ready}
      loading={pending}
      title={
        shipping
          ? "Shipping…"
          : ready
            ? "Deploy main to Vercel production"
            : "Every agent task must be merged first"
      }
      onClick={onShip}
    >
      <RocketIcon className="size-3.5" />
      {again ? "Ship again" : "Ship now"}
    </Button>
  );
}

function CopyReportButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
      {copied ? "Copied" : "Copy Markdown"}
    </Button>
  );
}

/** Report links (PRs, the deployment) open in a new tab. */
function ReportLink({ node: _node, ...props }: ComponentProps<"a"> & { node?: unknown }) {
  return <a {...props} target="_blank" rel="noopener noreferrer" />;
}

const REPORT_COMPONENTS: Components = { a: ReportLink };

/** "Report" button + dialog: the deploy link first, then the rendered report. */
export function ShipReportPanel({ mission }: { mission: Mission }) {
  const [open, setOpen] = useState(false);
  const ship = missionShip(mission);
  const report = ship.report_md;
  return (
    <>
      <Button
        variant={ship.status === "done" ? "secondary" : "ghost"}
        size="sm"
        disabled={!report}
        title={report ? undefined : "The report is written when the mission ships"}
        onClick={() => setOpen(true)}
      >
        <FileTextIcon className="size-3.5" />
        Report
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Ship report</DialogTitle>
            <DialogDescription>
              {ship.status === "done"
                ? "Deployed and checked by the server."
                : ship.status === "failed"
                  ? `The ship failed: ${ship.error ?? "unknown error"}`
                  : "The mission has not shipped yet."}
            </DialogDescription>
          </DialogHeader>
          {ship.url && (
            <a
              href={ship.url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="ship-deploy-link"
              className={cn(
                "flex items-center gap-2 rounded-lg border px-3 py-2 font-mono text-sm break-all",
                ship.status === "done"
                  ? "border-success/40 bg-success/5 text-success"
                  : "text-muted-foreground",
              )}
            >
              <ExternalLinkIcon aria-hidden className="size-4 shrink-0" />
              {ship.url}
            </a>
          )}
          {ship.note && <p className="text-xs text-warning">{ship.note}</p>}
          <div className="flex justify-end">{report && <CopyReportButton text={report} />}</div>
          <div
            className="min-h-0 flex-1 overflow-auto prose prose-sm max-w-none dark:prose-invert prose-code:before:content-none prose-code:after:content-none"
            data-testid="ship-report"
          >
            {report ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={REPORT_COMPONENTS}>
                {report}
              </ReactMarkdown>
            ) : (
              <p>No report yet.</p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
