import type { Meta, StoryObj } from "@storybook/react-vite";
import { DndContext } from "@dnd-kit/core";
import { fn } from "storybook/test";
import { BoardColumn } from "./BoardColumn";
import { BOARD_COLUMNS, projectColumns } from "./columns";
import { SAMPLE_TASKS } from "./sampleBoard";
import { withBoardProviders } from "./storyDecorators";

function Columns() {
  const columns = projectColumns(SAMPLE_TASKS);
  return (
    <DndContext>
      {/* The board's width at a 1600px window next to the default 320px sidebar. */}
      <div className="flex w-[1280px] gap-2.5 px-5">
        {BOARD_COLUMNS.map((column) => (
          <BoardColumn
            key={column.id}
            column={column}
            tasks={columns[column.id]}
            viewerId="ana@example.com"
            onOpen={fn()}
            onMove={fn()}
            onAssign={fn()}
          />
        ))}
      </div>
    </DndContext>
  );
}

const meta = {
  title: "Board/Columns",
  component: Columns,
  decorators: [withBoardProviders],
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof Columns>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Mission: Story = {};
