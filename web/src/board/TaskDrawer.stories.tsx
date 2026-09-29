import type { Meta, StoryObj } from "@storybook/react-vite";
import { fn } from "storybook/test";
import { makeTask } from "./fixtures";
import { SAMPLE_REVIEW, SAMPLE_TASKS } from "./sampleBoard";
import { TaskDrawer } from "./TaskDrawer";
import { withBoardProviders } from "./storyDecorators";

const meta = {
  title: "Board/TaskDrawer",
  component: TaskDrawer,
  decorators: [withBoardProviders],
  args: {
    tasks: SAMPLE_TASKS,
    onClose: fn(),
    onStart: fn(),
    onStop: fn(),
    onApprove: fn(),
    onRequestChanges: fn(async () => {}),
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
      issue_number: 14,
      issue_url: "https://github.com/acme/app/issues/14",
      cost_usd: 0.31,
    }),
  },
};

/** PR open, CI green after two fix turns, reviewer asked for changes. */
export const ReviewChanges: Story = {
  args: {
    task: makeTask({
      title: "Refund flow",
      status: "review",
      acceptance: ["Refunds post to the ledger"],
      branch: "shipcrew/task_1-refund-flow",
      issue_number: 13,
      issue_url: "https://github.com/acme/app/issues/13",
      pr_number: 43,
      pr_url: "https://github.com/acme/app/pull/43",
      ci: "green",
      ci_attempts: 2,
      review: SAMPLE_REVIEW,
      root_session_id: "conv_refund",
      cost_usd: 2.1,
    }),
  },
};

export const NeedsApproval: Story = {
  args: {
    task: makeTask({
      title: "Migrate the invoices table",
      status: "intervention",
      branch: "shipcrew/task_1-migrate-the-invoices-table",
      pr_number: 44,
      pr_url: "https://github.com/acme/app/pull/44",
      ci: "green",
      review: { verdict: "approve", summary: "Migration is reversible.", findings: [] },
      needs_human_approval: true,
      approval_reasons: ["Touches migrations/**", "Diff above 400 lines"],
    }),
  },
};

export const GuardrailAsk: Story = {
  args: {
    task: makeTask({
      title: "Send receipt emails",
      status: "intervention",
      branch: "shipcrew/task_1-send-receipt-emails",
      ci: "red",
      ci_attempts: 1,
    }),
  },
};
