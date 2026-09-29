import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeMission } from "./fixtures";
import { MissionPlanStatus, missionPlan, PlanFromPrdDialog } from "./MissionPlan";
import type { Mission, MissionPlan } from "./types";

afterEach(cleanup);

function plan(overrides: Partial<MissionPlan>): MissionPlan {
  return { status: "idle", session_id: null, error: null, imported_count: 0, ...overrides };
}

function renderStatus(value: MissionPlan) {
  return render(
    <MemoryRouter>
      <MissionPlanStatus plan={value} />
    </MemoryRouter>,
  );
}

describe("MissionPlanStatus", () => {
  it("renders nothing while idle", () => {
    renderStatus(plan({}));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("links a running plan to the planner session", () => {
    renderStatus(plan({ status: "running", session_id: "conv plan" }));
    expect(screen.getByRole("link", { name: /Planning/ })).toHaveAttribute(
      "href",
      "/c/conv%20plan",
    );
  });

  it("reports how many tasks were imported", () => {
    renderStatus(plan({ status: "imported", imported_count: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent("Imported 1 task");
  });

  it("shows why the plan failed", () => {
    renderStatus(plan({ status: "failed", error: "plan.json is invalid" }));
    expect(screen.getByRole("status")).toHaveTextContent("Plan failed: plan.json is invalid");
  });

  it("defaults to idle for a mission without a plan", () => {
    const legacy = { ...makeMission(), plan: undefined } as unknown as Mission;
    expect(missionPlan(legacy).status).toBe("idle");
  });
});

describe("PlanFromPrdDialog", () => {
  it("sends the PRD, or nothing when it is left empty", async () => {
    const onPlan = vi.fn(async () => {});
    render(<PlanFromPrdDialog mission={makeMission()} onPlan={onPlan} />);

    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    let dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    fireEvent.change(within(dialog).getByLabelText("PRD (optional)"), {
      target: { value: "  Ship refunds  " },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start planning" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onPlan).toHaveBeenLastCalledWith("Ship refunds");

    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    expect(within(dialog).getByLabelText("PRD (optional)")).toHaveValue("");
    fireEvent.click(within(dialog).getByRole("button", { name: "Start planning" }));
    await waitFor(() => expect(onPlan).toHaveBeenCalledTimes(2));
    expect(onPlan).toHaveBeenLastCalledWith(undefined);
  });

  it("keeps the dialog open with the server's error", async () => {
    const onPlan = vi.fn(async () => {
      throw new Error("Mission repo is not a git repository");
    });
    render(<PlanFromPrdDialog mission={makeMission()} onPlan={onPlan} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    const dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start planning" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Mission repo is not a git repository",
    );
  });

  it("binds the auto-run checkbox to the mission setting", async () => {
    const onAutoRunChange = vi.fn();
    const { rerender } = render(
      <PlanFromPrdDialog
        mission={makeMission()}
        onPlan={vi.fn()}
        onAutoRunChange={onAutoRunChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    const dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    const box = within(dialog).getByRole("checkbox", { name: "Run automatically after planning" });
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    expect(onAutoRunChange).toHaveBeenLastCalledWith(true);

    rerender(
      <PlanFromPrdDialog
        mission={makeMission({ auto_run: true })}
        onPlan={vi.fn()}
        onAutoRunChange={onAutoRunChange}
      />,
    );
    const checked = within(screen.getByRole("dialog")).getByRole("checkbox", {
      name: "Run automatically after planning",
    });
    expect(checked).toBeChecked();
    fireEvent.click(checked);
    expect(onAutoRunChange).toHaveBeenLastCalledWith(false);
  });

  it("hides the auto-run checkbox without a handler", async () => {
    render(<PlanFromPrdDialog mission={makeMission()} onPlan={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    const dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("is disabled while the planner runs", () => {
    render(
      <PlanFromPrdDialog
        mission={makeMission({ plan: plan({ status: "running", session_id: "conv_plan" }) })}
        onPlan={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Plan from PRD" })).toBeDisabled();
  });
});
