// Sidebar entry for the Board page. Owns its active-state check so the
// sidebar only needs to render it.

import type { MouseEvent } from "react";
import { KanbanSquareIcon } from "lucide-react";
import { useLocation } from "@/lib/routing";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";

export const BOARD_ROUTE_LEAF = "board";

/** Whether `pathname` is the Board route (standalone or under an embed basename). */
export function isBoardPath(pathname: string): boolean {
  return pathname.split("/").filter(Boolean).at(-1) === BOARD_ROUTE_LEAF;
}

export function BoardNavLink({
  onClick,
}: {
  onClick?: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const { pathname } = useLocation();
  return (
    <PrimaryNavLink
      to={`/${BOARD_ROUTE_LEAF}`}
      label="Board"
      icon={KanbanSquareIcon}
      active={isBoardPath(pathname)}
      onClick={onClick}
      componentId="sidebar.board"
      testId="board-nav"
    />
  );
}
