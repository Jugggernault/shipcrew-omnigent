import type { Meta, StoryObj } from "@storybook/react-vite";
import { fn } from "storybook/test";
import { makeMission } from "./fixtures";
import { MissionPlanStatus, PlanFromPrdDialog } from "./MissionPlan";
import type { MissionPlan } from "./types";
import { withBoardProviders } from "./storyDecorators";

const PLANS: Record<string, MissionPlan> = {
  running: { status: "running", session_id: "conv_plan", error: null, imported_count: 0 },
  imported: { status: "imported", session_id: "conv_plan", error: null, imported_count: 7 },
  failed: {
    status: "failed",
    session_id: "conv_plan",
    error: ".shipcrew/plan.json is missing the tasks array",
    imported_count: 0,
  },
};

function Header({ plan }: { plan: MissionPlan }) {
  return (
    <div className="flex items-start justify-between gap-4 p-4">
      <MissionPlanStatus plan={plan} />
      <PlanFromPrdDialog mission={makeMission({ plan })} onPlan={fn(async () => {})} />
    </div>
  );
}

const meta = {
  title: "Board/MissionPlan",
  component: Header,
  decorators: [withBoardProviders],
  args: { plan: PLANS.running },
} satisfies Meta<typeof Header>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Running: Story = {};
export const Imported: Story = { args: { plan: PLANS.imported } };
export const Failed: Story = { args: { plan: PLANS.failed } };
