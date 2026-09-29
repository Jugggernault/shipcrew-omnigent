// "Open board" hover action of a sidebar project row. Renders only for the
// projects that belong to one of the viewer's shipcrew missions (one mission =
// one project); the sidebar's header-controls cluster reveals it on hover and
// on keyboard focus like the other row actions.

import type { MouseEvent } from "react";
import { KanbanSquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link } from "@/lib/routing";
import { boardUrlForMission, useProjectBoardLinks } from "./projectLinks";

export function ProjectBoardButton({
  projectId,
  projectName,
  onNavigate,
}: {
  /** First-class project id, or null for a label-only folder (never a mission). */
  projectId: string | null;
  projectName: string;
  onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const links = useProjectBoardLinks();
  const link = projectId ? links.get(projectId) : undefined;
  if (!link) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant="ghost"
          size="icon-xs"
          aria-label={`Open board for ${projectName}`}
          data-testid="project-open-board"
          className="text-muted-foreground"
        >
          <Link
            to={boardUrlForMission(link.mission_id)}
            onClick={(event) => {
              event.stopPropagation();
              onNavigate?.(event);
            }}
          >
            <KanbanSquareIcon className="size-3.5" data-icon-size="14" />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">Open board</TooltipContent>
    </Tooltip>
  );
}
