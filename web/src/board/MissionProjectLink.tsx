// Board header link back to the mission's project in the chat view: the
// project-scoped composer (`/?project=<name>`), whose sidebar folder lists the
// mission's sessions (planner, tasks, reviewers, deploy).

import { FolderIcon } from "lucide-react";
import { useProjects } from "@/hooks/useConversations";
import { Link } from "@/lib/routing";
import type { Mission } from "./types";

export function MissionProjectLink({ mission }: { mission: Mission }) {
  const { data: projects } = useProjects();
  const projectId = mission.project_id;
  if (!projectId) return null;
  const name = projects?.find((project) => project.id === projectId)?.name;
  // The folder is found by name; until the project list loads there is no link.
  if (!name) return null;
  return (
    <Link
      to={`/?project=${encodeURIComponent(name)}`}
      className="inline-flex min-w-0 items-center gap-1 rounded-sm text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      title={`Open the sessions of project ${name}`}
      data-testid="mission-project-link"
    >
      <FolderIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="truncate">Sessions in {name}</span>
    </Link>
  );
}
