import type { Meta, StoryObj } from "@storybook/react-vite";
import { DndContext } from "@dnd-kit/core";
import { fn } from "storybook/test";
import { makeTask } from "./fixtures";
import { TaskCard } from "./TaskCard";

const meta = {
  title: "Board/TaskCard",
  component: TaskCard,
  decorators: [
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
      ci: "green",
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
