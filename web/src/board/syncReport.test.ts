import { describe, expect, it } from "vitest";
import { syncToast } from "./syncReport";

describe("syncToast", () => {
  it("says why a sync did nothing", () => {
    expect(
      syncToast({ ok: false, reason: "gh is not logged in", created: 0, updated: 0, synced_at: 1 }),
    ).toBe("GitHub sync skipped: gh is not logged in");
  });

  it("counts opened issues and updated cards", () => {
    expect(syncToast({ ok: true, reason: null, created: 2, updated: 1, synced_at: 1 })).toBe(
      "GitHub sync done: 2 issues opened, 1 card updated",
    );
  });

  it("falls back when the server sends no report", () => {
    expect(syncToast(undefined)).toBe("GitHub sync done");
  });
});
