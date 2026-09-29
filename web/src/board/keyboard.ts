// Keyboard dragging across columns: arrow keys jump the dragged card to the
// neighbouring column instead of nudging it by a few pixels.

import { KeyboardCode, type KeyboardCoordinateGetter } from "@dnd-kit/core";

const INSET = 12;

export const columnKeyboardCoordinates: KeyboardCoordinateGetter = (
  event,
  { context, currentCoordinates },
) => {
  const direction =
    event.code === KeyboardCode.Right ? 1 : event.code === KeyboardCode.Left ? -1 : 0;
  if (direction === 0) return undefined;
  event.preventDefault();
  const rects = [...context.droppableRects.values()].sort((left, right) => left.left - right.left);
  if (rects.length === 0) return undefined;
  const x = currentCoordinates.x;
  const currentIndex = rects.findIndex((rect) => x >= rect.left - INSET && x < rect.right);
  const nextIndex =
    currentIndex < 0
      ? direction > 0
        ? rects.findIndex((rect) => rect.left > x)
        : rects.findLastIndex((rect) => rect.right <= x)
      : currentIndex + direction;
  const target = rects[nextIndex];
  if (!target) return currentCoordinates;
  return { x: target.left + INSET, y: Math.max(currentCoordinates.y, target.top + INSET) };
};
