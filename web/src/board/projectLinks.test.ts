import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { makeMission } from "./fixtures";
import {
  boardUrlForMission,
  projectLinksQueryKey,
  selectMission,
  syncProjectLink,
  type ProjectBoardLink,
} from "./projectLinks";

describe("selectMission", () => {
  const a = makeMission({ id: "a", project_id: "pa" });
  const b = makeMission({ id: "b", project_id: "pb" });

  it("prefers ?mission=, then ?project=, then the first mission", () => {
    expect(selectMission([a, b], "b", "pa")?.id).toBe("b");
    expect(selectMission([a, b], null, "pb")?.id).toBe("b");
    expect(selectMission([a, b], "gone", "gone")?.id).toBe("a");
    expect(selectMission([], null, "pb")).toBeNull();
  });
});

describe("syncProjectLink", () => {
  it("adds or updates the link of a mission once the links are cached", () => {
    const client = new QueryClient();
    syncProjectLink(client, makeMission({ id: "a", project_id: "pa" }));
    expect(client.getQueryData(projectLinksQueryKey)).toBeUndefined();
    client.setQueryData<ProjectBoardLink[]>(projectLinksQueryKey, []);
    syncProjectLink(client, makeMission({ id: "a", title: "A", project_id: "pa" }));
    syncProjectLink(client, makeMission({ id: "a", title: "A2", project_id: "pa" }));
    syncProjectLink(client, makeMission({ id: "b", project_id: null }));
    expect(client.getQueryData(projectLinksQueryKey)).toEqual([
      { project_id: "pa", mission_id: "a", title: "A2" },
    ]);
  });

  it("builds the board URL of a mission", () => {
    expect(boardUrlForMission("m 1")).toBe("/board?mission=m%201");
  });
});
