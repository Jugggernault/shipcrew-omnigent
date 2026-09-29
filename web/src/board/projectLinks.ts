// One mission = one omnigent project. The sidebar asks which of the viewer's
// projects belong to a mission (`GET /v1/shipcrew/project-links`) to show the
// "Open board" action on those project rows only.

import { useQuery, type QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { Mission } from "./types";

export interface ProjectBoardLink {
  project_id: string;
  mission_id: string;
  title: string;
}

export const projectLinksQueryKey = ["shipcrew", "project-links"] as const;
export const PROJECT_QUERY_PARAM = "project";

export async function fetchProjectLinks(): Promise<ProjectBoardLink[]> {
  const res = await authenticatedFetch("/v1/shipcrew/project-links");
  // A server without shipcrew (404) simply has no board links.
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { links?: ProjectBoardLink[] };
  return Array.isArray(body.links) ? body.links : [];
}

/**
 * `project_id -> link` for the viewer's missions. One cached request for the
 * whole sidebar; failures just mean no board icons (no retries, no toasts).
 */
export function useProjectBoardLinks(): Map<string, ProjectBoardLink> {
  const { data } = useQuery({
    queryKey: projectLinksQueryKey,
    queryFn: fetchProjectLinks,
    staleTime: 30_000,
    retry: false,
  });
  const map = new Map<string, ProjectBoardLink>();
  for (const link of data ?? []) map.set(link.project_id, link);
  return map;
}

/** URL of the board page with the mission selected. */
export function boardUrlForMission(missionId: string): string {
  return `/board?mission=${encodeURIComponent(missionId)}`;
}

/** Keep the cached links in step with a mission the board just received. */
export function syncProjectLink(queryClient: QueryClient, mission: Mission): void {
  const projectId = mission.project_id;
  if (!projectId) return;
  const current = queryClient.getQueryData<ProjectBoardLink[]>(projectLinksQueryKey);
  if (current === undefined) return;
  const link = { project_id: projectId, mission_id: mission.id, title: mission.title };
  const others = current.filter((item) => item.mission_id !== mission.id);
  const existing = current.find((item) => item.mission_id === mission.id);
  if (existing && existing.project_id === projectId && existing.title === mission.title) return;
  queryClient.setQueryData<ProjectBoardLink[]>(projectLinksQueryKey, [...others, link]);
}

/**
 * The mission `/board` shows: `?mission=` first, else the mission of
 * `?project=` (a project row's board link), else the first one.
 */
export function selectMission(
  missions: Mission[],
  missionParam: string | null,
  projectParam: string | null,
): Mission | null {
  const byId = missionParam ? missions.find((item) => item.id === missionParam) : undefined;
  if (byId) return byId;
  const byProject = projectParam
    ? missions.find((item) => item.project_id === projectParam)
    : undefined;
  return byProject ?? missions[0] ?? null;
}
