import type { Meta, StoryObj } from "@storybook/react-vite";
import { DndContext } from "@dnd-kit/core";
import { fn } from "storybook/test";
import { makeTask } from "./fixtures";
import { SAMPLE_REVIEW } from "./sampleBoard";
import { withBoardProviders } from "./storyDecorators";
import { TaskCard } from "./TaskCard";

const meta = {
  title: "Board/TaskCard",
  component: TaskCard,
  decorators: [
    withBoardProviders,
    (Story) => (
      <DndContext>
        <div className="w-[264px]">
          <Story />
        </div>
      </DndContext>
    ),
  ],
  args: {
    task: makeTask(),
    viewerId: "ana@example.com",
    onOpen: fn(),
    onMove: fn(),
    onAssign: fn(),
  },
} satisfies Meta<typeof TaskCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Backlog: Story = {};

export const InReview: Story = {
  args: {
    task: makeTask({
      title: "Billing page with invoice list",
      status: "review",
      role: "frontend",
      assignee: { kind: "agent", id: "frontend" },
      pr_number: 42,
      pr_url: "https://github.com/acme/app/pull/42",
      branch: "shipcrew/task_1-billing-page-with-invoice-list",
      issue_number: 12,
      issue_url: "https://github.com/acme/app/issues/12",
      ci: "pending",
      ci_attempts: 1,
      review: { verdict: "approve", summary: "", findings: [] },
      cost_usd: 1.42,
      depends_on: ["task_0"],
    }),
  },
};

export const FailingCi: Story = {
  args: {
    task: makeTask({
      status: "running",
      assignee: { kind: "human", id: "ana.lopez@example.com" },
      pr_number: 43,
      pr_url: "https://github.com/acme/app/pull/43",
      ci: "red",
      cost_usd: 0.004,
    }),
  },
};

export const Blocked: Story = {
  args: {
    task: makeTask({
      title: "Wire the Stripe webhook",
      status: "blocked",
      blocked_reason: "Owned paths overlap with “Billing page” (src/billing/**)",
      depends_on: ["task_2", "task_3"],
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
      review: { verdict: "approve", summary: "", findings: [] },
      needs_human_approval: true,
      approval_reasons: ["Touches migrations/**", "Diff above 400 lines"],
    }),
  },
};

export const ReviewRoundsExhausted: Story = {
  args: {
    task: makeTask({
      title: "Refund flow",
      status: "intervention",
      branch: "shipcrew/task_1-refund-flow",
      pr_number: 43,
      pr_url: "https://github.com/acme/app/pull/43",
      ci: "red",
      ci_attempts: 3,
      blocked_reason: "reviewer requested changes 3 times: refunds are not idempotent",
      review: SAMPLE_REVIEW,
    }),
  },
};

export const GuardrailAsk: Story = {
  args: {
    task: makeTask({
      title: "Send receipt emails",
      status: "intervention",
      assignee: { kind: "agent", id: "developer" },
      branch: "shipcrew/task_1-send-receipt-emails",
    }),
  },
};
