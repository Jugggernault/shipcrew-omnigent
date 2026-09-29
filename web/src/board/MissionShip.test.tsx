import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeMission, makeTask } from "./fixtures";
import {
  MissionShipNotice,
  MissionStatusChip,
  PlanDecisions,
  ShipButton,
  ShipReportPanel,
  missionPhase,
  shipReady,
} from "./MissionShip";
import type { Mission, MissionShip } from "./types";

afterEach(cleanup);

function ship(overrides: Partial<MissionShip> = {}): MissionShip {
  return {
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
    ...overrides,
  };
}

function withShip(overrides: Partial<MissionShip>, mission: Partial<Mission> = {}): Mission {
  return makeMission({ ship: ship(overrides), ...mission });
}

const REPORT = [
  "# Ship report: Tiny CLI",
  "",
  "- **Deployment:** https://tiny.vercel.app",
  "",
  "## Tasks (1 of 1 merged)",
  "",
  "| # | Task | Status | PR |",
  "|---|---|---|---|",
  "| 1 | Parse args | merged | [#3](https://github.com/acme/tiny/pull/3) |",
  "",
].join("\n");

describe("missionPhase", () => {
  const merged = [makeTask({ status: "merged" })];

  it.each([
    [
      makeMission({ plan: { status: "running", session_id: "p", error: null, imported_count: 0 } }),
      merged,
      "planning",
    ],
    [makeMission(), [], "planning"],
    [makeMission(), merged, "building"],
    [withShip({ status: "deploying" }), merged, "shipping"],
    [withShip({ status: "verifying" }), merged, "shipping"],
    [withShip({ status: "done", url: "https://tiny.vercel.app" }), merged, "shipped"],
    [withShip({ status: "failed", error: "boom" }), merged, "ship-failed"],
  ] as const)("%#: %s", (mission, tasks, kind) => {
    expect(missionPhase(mission, tasks).kind).toBe(kind);
  });

  it("treats a server without the ship stage as idle", () => {
    const legacy = makeMission();
    delete legacy.ship;
    expect(missionPhase(legacy, [makeTask({ status: "running" })]).label).toBe("Building");
  });
});

describe("shipReady", () => {
  it("needs every agent task merged, human cards ignored", () => {
    const human = makeTask({ id: "h", status: "backlog", assignee: { kind: "human", id: "ana" } });
    expect(shipReady([makeTask({ status: "merged" }), human])).toBe(true);
    expect(shipReady([makeTask({ status: "merged" }), makeTask({ status: "blocked" })])).toBe(
      false,
    );
    expect(shipReady([human])).toBe(false);
    expect(shipReady([])).toBe(false);
  });
});

describe("MissionStatusChip", () => {
  it("links the deployment once shipped", () => {
    render(
      <MissionStatusChip
        mission={withShip({ status: "done", url: "https://tiny.vercel.app/" })}
        tasks={[makeTask({ status: "merged" })]}
      />,
    );
    const chip = screen.getByTestId("mission-status-chip");
    expect(chip).toHaveAttribute("data-phase", "shipped");
    expect(chip).toHaveTextContent("Shipped");
    expect(within(chip).getByRole("link", { name: "tiny.vercel.app" })).toHaveAttribute(
      "href",
      "https://tiny.vercel.app/",
    );
  });

  it("shows the failure reason as its title", () => {
    render(
      <MissionStatusChip
        mission={withShip({ status: "failed", error: "vercel is not logged in" })}
        tasks={[makeTask({ status: "merged" })]}
      />,
    );
    const chip = screen.getByTestId("mission-status-chip");
    expect(chip).toHaveTextContent("Ship failed");
    expect(chip).toHaveAttribute("title", "vercel is not logged in");
  });

  it("says Shipping while the URL is checked", () => {
    render(<MissionStatusChip mission={withShip({ status: "verifying" })} tasks={[]} />);
    const chip = screen.getByTestId("mission-status-chip");
    expect(chip).toHaveTextContent("Shipping");
    expect(chip).toHaveAttribute("title", "Checking the deployment URL");
  });
});

describe("MissionShipNotice", () => {
  it("explains why a mission does not ship", () => {
    render(
      <MissionShipNotice
        mission={withShip({ error: "not shipping: 1 card needs a human: 'QA'" })}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "not shipping: 1 card needs a human: 'QA'",
    );
  });

  it("renders nothing without an error", () => {
    render(<MissionShipNotice mission={withShip({ status: "done" })} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("PlanDecisions", () => {
  it("folds the planner decisions under a Decisions summary", () => {
    render(<PlanDecisions mission={makeMission({ plan_decisions: ["Two tasks", "No DB"] })} />);
    expect(screen.getByText("Decisions (2)")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Plan decisions", hidden: true });
    expect(within(list).getAllByRole("listitem", { hidden: true })).toHaveLength(2);
  });

  it("is absent without decisions", () => {
    render(<PlanDecisions mission={makeMission()} />);
    expect(screen.queryByTestId("plan-decisions")).not.toBeInTheDocument();
  });
});

describe("ShipButton", () => {
  const merged = [makeTask({ status: "merged" })];

  it("ships a merged mission", () => {
    const onShip = vi.fn();
    render(<ShipButton mission={withShip({})} tasks={merged} onShip={onShip} />);
    fireEvent.click(screen.getByRole("button", { name: "Ship now" }));
    expect(onShip).toHaveBeenCalledTimes(1);
  });

  it("is disabled while work is left or a ship runs, and offers a re-ship after one", () => {
    const { rerender } = render(
      <ShipButton
        mission={withShip({})}
        tasks={[makeTask({ status: "running" })]}
        onShip={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Ship now" })).toBeDisabled();
    rerender(
      <ShipButton mission={withShip({ status: "deploying" })} tasks={merged} onShip={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Ship now" })).toBeDisabled();
    rerender(
      <ShipButton mission={withShip({ status: "failed" })} tasks={merged} onShip={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Ship again" })).toBeEnabled();
  });
});

describe("ShipReportPanel", () => {
  it("is disabled until a report exists", () => {
    render(<ShipReportPanel mission={withShip({})} />);
    expect(screen.getByRole("button", { name: "Report" })).toBeDisabled();
  });

  it("shows the deploy link first, the rendered report and copies the Markdown", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(
      <ShipReportPanel
        mission={withShip({
          status: "done",
          url: "https://tiny.vercel.app",
          report_md: REPORT,
          note: "The deployment answers HTTP 401: it is live but protected",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Report" }));
    const dialog = await screen.findByRole("dialog", { name: "Ship report" });
    expect(within(dialog).getByTestId("ship-deploy-link")).toHaveAttribute(
      "href",
      "https://tiny.vercel.app",
    );
    expect(dialog).toHaveTextContent("it is live but protected");
    const report = within(dialog).getByTestId("ship-report");
    expect(
      within(report).getByRole("heading", { name: "Ship report: Tiny CLI" }),
    ).toBeInTheDocument();
    expect(within(report).getByRole("table")).toBeInTheDocument();
    const pr = within(report).getByRole("link", { name: "#3" });
    expect(pr).toHaveAttribute("href", "https://github.com/acme/tiny/pull/3");
    expect(pr).toHaveAttribute("target", "_blank");
    fireEvent.click(within(dialog).getByRole("button", { name: "Copy Markdown" }));
    expect(writeText).toHaveBeenCalledWith(REPORT);
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Copied" })).toBeInTheDocument(),
    );
  });

  it("explains a failed ship in the dialog", async () => {
    render(
      <ShipReportPanel
        mission={withShip({
          status: "failed",
          error: "deploy failed: missing env vars: API_KEY",
          report_md: "# Ship report",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Report" }));
    const dialog = await screen.findByRole("dialog", { name: "Ship report" });
    expect(dialog).toHaveTextContent("The ship failed: deploy failed: missing env vars: API_KEY");
    expect(within(dialog).queryByTestId("ship-deploy-link")).not.toBeInTheDocument();
  });
});
