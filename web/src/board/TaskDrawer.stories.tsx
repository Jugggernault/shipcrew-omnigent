import type { Meta, StoryObj } from "@storybook/react-vite";
import { fn } from "storybook/test";
import { makeTask } from "./fixtures";
import { SAMPLE_TASKS } from "./sampleBoard";
import { TaskDrawer } from "./TaskDrawer";

const meta = {
  title: "Board/TaskDrawer",
  component: TaskDrawer,
  args: {
    tasks: SAMPLE_TASKS,
    onClose: fn(),
    onStart: fn(),
    onStop: fn(),
  },
} satisfies Meta<typeof TaskDrawer>;

export default meta;
type Story = StoryObj<typeof meta>;

export const NotStarted: Story = {
  args: {
    task: makeTask({
      title: "Wire the Stripe webhook",
      body: "Receive `invoice.paid` and mark the invoice as settled.",
      acceptance: ["Signature is verified", "Duplicate events are ignored"],
      depends_on: ["t4"],
      owned_paths: ["src/billing/webhook/**"],
      pr_number: 44,
      pr_url: "https://github.com/acme/app/pull/44",
      ci: "pending",
      cost_usd: 0.31,
    }),
  },
};
