// The mission's live preview in the board header: "Live: <url>" with the
// deployed commit and time. With a server-side deploy target (docker, argocd)
// the server redeploys `main` after every merge, from the first one on, so the
// URL exists early and follows the build; the chip shows a redeploy in flight
// and a failed one (the previous version keeps serving).

import { ExternalLinkIcon, RadioIcon, TriangleAlertIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import type { Mission, MissionPreview } from "./types";

/** The mission's preview, or `null` when there is nothing to show. */
export function missionPreview(mission: Mission): MissionPreview | null {
  const preview = mission.preview;
  if (!preview || preview.status === "idle") return null;
  return preview;
}

/** `"just now"`, `"4m ago"`, `"2h ago"`, `"3d ago"`. */
export function ago(epochS: number, nowS: number): string {
  const seconds = Math.max(0, Math.round(nowS - epochS));
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

function short(sha: string | null): string | null {
  return sha ? sha.slice(0, 7) : null;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function stamp(epochS: number): string {
  return new Date(epochS * 1000).toLocaleString();
}

export function LivePreviewChip({
  mission,
  className,
  nowS,
}: {
  mission: Mission;
  className?: string;
  /** Current time in epoch seconds (tests); defaults to the clock. */
  nowS?: number;
}) {
  const preview = missionPreview(mission);
  if (!preview) return null;
  const now = nowS ?? Date.now() / 1000;
  const deploying = preview.status === "deploying";
  const sha = short(preview.sha);
  const next = short(preview.deploying_sha);

  if (!preview.url) {
    const label = deploying ? "Deploying the first version…" : "Preview failed";
    return (
      <Badge
        data-testid="live-preview-chip"
        data-state={deploying ? "deploying" : "failed"}
        title={preview.error ?? undefined}
        className={cn(
          "gap-1 text-xs",
          deploying ? "bg-warning/10 text-warning" : "bg-destructive/10 text-destructive",
          className,
        )}
      >
        {deploying ? <Spinner className="size-3" aria-hidden /> : <TriangleAlertIcon aria-hidden />}
        <span>{label}</span>
      </Badge>
    );
  }

  const failedAfter = !deploying && preview.error;
  const titleParts = [
    preview.live_since ? `Live since ${stamp(preview.live_since)}` : null,
    preview.updated_at && sha ? `${sha} deployed ${stamp(preview.updated_at)}` : null,
    preview.target ? `target: ${preview.target}` : null,
    failedAfter ? `Last deploy failed: ${preview.error}` : null,
  ].filter(Boolean);
  return (
    <Badge
      data-testid="live-preview-chip"
      data-state={deploying ? "deploying" : failedAfter ? "stale" : preview.status}
      title={titleParts.join("\n")}
      className={cn(
        "max-w-full gap-1.5 text-xs",
        preview.status === "failed" || failedAfter
          ? "bg-warning/10 text-warning"
          : "bg-success/10 text-success",
        className,
      )}
    >
      {deploying ? (
        <Spinner className="size-3" aria-hidden />
      ) : failedAfter || preview.status === "failed" ? (
        <TriangleAlertIcon aria-hidden />
      ) : (
        <RadioIcon aria-hidden />
      )}
      <span className="font-medium">Live:</span>
      <a
        href={preview.url}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="live-preview-link"
        className="inline-flex min-w-0 items-center gap-1 truncate font-mono underline-offset-2 hover:underline"
      >
        {hostOf(preview.url)}
        <ExternalLinkIcon aria-hidden className="size-3 shrink-0" />
      </a>
      {sha && <span className="font-mono text-muted-foreground">{sha}</span>}
      {preview.updated_at !== null && (
        <span className="text-muted-foreground">{ago(preview.updated_at, now)}</span>
      )}
      {deploying && next && next !== sha && (
        <span className="text-muted-foreground">· deploying {next}…</span>
      )}
      {failedAfter && <span>· last deploy failed</span>}
    </Badge>
  );
}
