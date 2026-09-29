// The sidebar's "Open board" hover action on project rows (one shipcrew
// mission = one omnigent project). Rendered through the real Sidebar so the
// placement in the project header's hover/focus controls cluster is covered.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type * as Identity from "@/lib/identity";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof Identity>()),
  authenticatedFetch: vi.fn(),
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  // A mission's project, a plain project, and a label-only folder.
  useProjects: () => ({
    data: [
      { id: "p_mission", name: "Tiny shop" },
      { id: "p_plain", name: "Notes" },
      { id: null, name: "Legacy label" },
    ],
  }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { Sidebar } from "@/shell/Sidebar";

const fetchMock = vi.mocked(authenticatedFetch);
const useConvMock = vi.mocked(useConversations);
let links: { project_id: string; mission_id: string; title: string }[] = [];
let linksStatus = 200;

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(
    () =>
      ({
        data: {
          pages: [{ data: conversations, first_id: null, last_id: null, has_more: false }],
          pageParams: [undefined],
        },
        isLoading: false,
        isError: false,
        error: null,
        fetchNextPage: vi.fn(),
        hasNextPage: false,
        isFetchingNextPage: false,
      }) as unknown as ReturnType<typeof useConversations>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Routes>
              <Route
                path="*"
                element={
                  <>
                    <Sidebar open={true} onClose={vi.fn()} />
                    <LocationProbe />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

/** The header block (toggle + controls cluster) of a project folder. */
function projectHeader(name: string): HTMLElement {
  const toggle = screen.getByRole("button", { name });
  const header = toggle.closest<HTMLElement>(".group\\/header");
  if (!header) throw new Error(`no header for ${name}`);
  return header;
}

beforeEach(() => {
  mockConversations([]);
  links = [{ project_id: "p_mission", mission_id: "m_1", title: "Tiny shop" }];
  linksStatus = 200;
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input) => {
    const url = input.toString();
    if (url === "/v1/shipcrew/project-links") {
      return new Response(JSON.stringify({ links }), {
        status: linksStatus,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
});

describe("project row board action", () => {
  it("appears only on the mission's project row", async () => {
    renderSidebar();
    const button = await within(projectHeader("Tiny shop")).findByRole("link", {
      name: "Open board for Tiny shop",
    });
    expect(button).toHaveAttribute("href", "/board?mission=m_1");
    expect(button).toHaveAttribute("data-testid", "project-open-board");
    expect(within(projectHeader("Notes")).queryByTestId("project-open-board")).toBeNull();
    expect(within(projectHeader("Legacy label")).queryByTestId("project-open-board")).toBeNull();
    expect(screen.getAllByTestId("project-open-board")).toHaveLength(1);
  });

  it("sits in the hover/focus-revealed controls next to the other row actions", async () => {
    renderSidebar();
    const header = projectHeader("Tiny shop");
    const button = await within(header).findByTestId("project-open-board");
    const controls = button.closest<HTMLElement>("[data-header-controls]");
    expect(controls).not.toBeNull();
    // Same cluster as "New session in project" and the kebab.
    expect(within(controls as HTMLElement).getByTestId("project-new-session")).toBeInTheDocument();
    // Hidden until the header is hovered or a control inside it has focus.
    const reveal = button.closest<HTMLElement>(".transition-opacity");
    const classes = reveal?.getAttribute("class") ?? "";
    expect(classes).toMatch(/md:opacity-0/);
    expect(classes).toMatch(/group-hover\/header:opacity-100/);
    expect(classes).toMatch(/\[data-header-controls\]:focus-within\]\/header:opacity-100/);
  });

  it("is reachable by keyboard and opens the board with Enter", async () => {
    renderSidebar();
    const button = await screen.findByRole("link", { name: "Open board for Tiny shop" });
    button.focus();
    expect(button).toHaveFocus();
    expect(button.closest("[data-header-controls]")).toContainElement(
      document.activeElement as HTMLElement,
    );
    // A link activates on Enter (the browser turns it into a click).
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/board?mission=m_1"),
    );
  });

  it("shows the tooltip on focus", async () => {
    renderSidebar();
    const button = await screen.findByRole("link", { name: "Open board for Tiny shop" });
    fireEvent.focus(button);
    expect(await screen.findAllByText("Open board")).not.toHaveLength(0);
  });

  it("renders no board action when shipcrew is not available", async () => {
    linksStatus = 404;
    renderSidebar();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/v1/shipcrew/project-links"));
    expect(screen.queryByTestId("project-open-board")).toBeNull();
  });
});
