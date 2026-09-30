import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { makeMission } from "./fixtures";
import { LivePreviewChip, ago, missionPreview } from "./MissionPreview";
import type { Mission, MissionPreview } from "./types";

afterEach(cleanup);

const NOW = 1_788_300_000;

function preview(overrides: Partial<MissionPreview> = {}): MissionPreview {
  return {
    status: "live",
    url: "https://quiet-river-demo.trycloudflare.com",
    sha: "8c277a98b7777e0aabec6e22cf8fad37ce536d82",
    updated_at: NOW - 240,
    live_since: NOW - 3600,
    error: null,
    target: "docker",
    deploying_sha: null,
    ...overrides,
  };
}

function withPreview(overrides: Partial<MissionPreview>): Mission {
  return makeMission({ preview: preview(overrides) });
}

describe("ago", () => {
  it.each([
    [10, "just now"],
    [240, "4m ago"],
    [7200, "2h ago"],
    [3 * 86_400, "3d ago"],
  ])("%is -> %s", (seconds, text) => {
    expect(ago(NOW - seconds, NOW)).toBe(text);
  });
});

describe("LivePreviewChip", () => {
  it("shows the live URL, the deployed sha and the deploy time", () => {
    render(<LivePreviewChip mission={withPreview({})} nowS={NOW} />);
    const chip = screen.getByTestId("live-preview-chip");
    expect(chip).toHaveAttribute("data-state", "live");
    expect(chip).toHaveTextContent("Live:");
    expect(chip).toHaveTextContent("quiet-river-demo.trycloudflare.com");
    expect(chip).toHaveTextContent("8c277a9");
    expect(chip).toHaveTextContent("4m ago");
    const link = screen.getByTestId("live-preview-link");
    expect(link).toHaveAttribute("href", "https://quiet-river-demo.trycloudflare.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(chip.getAttribute("title")).toContain("Live since");
    expect(chip.getAttribute("title")).toContain("target: docker");
  });

  it("keeps the live URL while a redeploy runs", () => {
    render(
      <LivePreviewChip
        mission={withPreview({ status: "deploying", deploying_sha: "e07dc4ae1b5ff81b" })}
        nowS={NOW}
      />,
    );
    const chip = screen.getByTestId("live-preview-chip");
    expect(chip).toHaveAttribute("data-state", "deploying");
    expect(chip).toHaveTextContent("quiet-river-demo.trycloudflare.com");
    expect(chip).toHaveTextContent("deploying e07dc4a…");
  });

  it("says when the last redeploy failed but the old version still serves", () => {
    render(<LivePreviewChip mission={withPreview({ error: "docker build failed" })} nowS={NOW} />);
    const chip = screen.getByTestId("live-preview-chip");
    expect(chip).toHaveAttribute("data-state", "stale");
    expect(chip).toHaveTextContent("last deploy failed");
    expect(chip.getAttribute("title")).toContain("Last deploy failed: docker build failed");
  });

  it("shows the first deploy in flight, and a first deploy that failed", () => {
    const { rerender } = render(
      <LivePreviewChip
        mission={withPreview({ status: "deploying", url: null, sha: null, updated_at: null })}
      />,
    );
    expect(screen.getByTestId("live-preview-chip")).toHaveTextContent(
      "Deploying the first version…",
    );
    rerender(
      <LivePreviewChip
        mission={withPreview({ status: "failed", url: null, sha: null, error: "no docker" })}
      />,
    );
    const chip = screen.getByTestId("live-preview-chip");
    expect(chip).toHaveAttribute("data-state", "failed");
    expect(chip).toHaveAttribute("title", "no docker");
  });

  it("renders nothing without a preview (older servers, vercel target)", () => {
    const legacy = makeMission();
    expect(missionPreview(legacy)).toBeNull();
    const { container } = render(<LivePreviewChip mission={legacy} />);
    expect(container).toBeEmptyDOMElement();
    expect(missionPreview(withPreview({ status: "idle", url: null }))).toBeNull();
  });
});
