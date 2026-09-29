import type { MissionSyncReport } from "./types";

/** Toast text for a finished `POST /missions/{id}/sync`. */
export function syncToast(report: MissionSyncReport | undefined): string {
  if (!report) return "GitHub sync done";
  if (!report.ok) return `GitHub sync skipped: ${report.reason ?? "unknown reason"}`;
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  return `GitHub sync done: ${plural(report.created, "issue")} opened, ${plural(report.updated, "card")} updated`;
}
