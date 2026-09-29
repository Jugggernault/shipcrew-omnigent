// Tests for the Board page (`/board`). The shipcrew API is served by an
// in-memory fake behind `authenticatedFetch`; dnd-kit runs for real but its
// DndContext props are captured so tests can drive drops directly, and the
// sub-agent graph is stubbed at its module seam.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type * as DndKit from "@dnd-kit/core";
import type * as Identity from "@/lib/identity";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { makeMission, makeTask } from "@/board/fixtures";
import type { Task } from "@/board/types";
import { authenticatedFetch } from "@/lib/identity";
import { BoardPage } from "./BoardPage";

const { dndProps } = vi.hoisted(() => ({
  dndProps: { current: null as Record<string, unknown> | null },
}));

vi.mock("@dnd-kit/core", async (importActual) => {
  const actual = await importActual<typeof DndKit>();
  return {
    ...actual,
    DndContext: (props: ComponentProps<typeof actual.DndContext>) => {
      dndProps.current = props as Record<string, unknown>;
      return <actual.DndContext {...props} />;
    },
  };
});
vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof Identity>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "ana@example.com" }));
vi.mock("@/shell/SubagentsGraphView", () => ({
  SubagentsGraphView: ({ rootSessionId }: { rootSessionId: string }) => (
    <div data-testid="subagents-graph">{rootSessionId}</div>
  ),
}));

const fetchMock = vi.mocked(authenticatedFetch);
let tasks: Task[] = [];
let requests: { method: string; url: string; body: unknown }[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function serve(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = input.toString();
  const method = init?.method ?? "GET";
  const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
  requests.push({ method, url, body });
  if (url === "/v1/shipcrew/missions") return json({ missions: [makeMission()] });
  if (url.endsWith("/stream")) return new Response("unavailable", { status: 503 });
  if (/^\/v1\/sessions\/[^/]+\/child_sessions$/.test(url)) {
    return json({ object: "list", data: [] });
  }
  if (url === "/v1/shipcrew/missions/mission_1/tasks" && method === "GET") {
    return json({ tasks });
  }
  if (url === "/v1/shipcrew/missions/mission_1/tasks" && method === "POST") {
    const created = makeTask({ id: `task_${tasks.length + 1}`, ...(body as Partial<Task>) });
    tasks = [...tasks, created];
    return json(created);
  }
  const match = /^\/v1\/shipcrew\/tasks\/([^/]+)(?:\/(start|stop))?$/.exec(url);
  if (match) {
    const [, id, action] = match;
    const current = tasks.find((task) => task.id === id);
    if (!current) return json({ detail: "not found" }, 404);
    const next: Task =
      action === "start"
        ? { ...current, status: "running", root_session_id: "conv_started" }
        : action === "stop"
          ? { ...current, status: "backlog" }
          : { ...current, ...(body as Partial<Task>) };
    tasks = tasks.map((task) => (task.id === id ? next : task));
    return json(next);
  }
  return json({ detail: `unexpected ${method} ${url}` }, 500);
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderBoard(initial = "/board") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initial]}>
          <Routes>
            <Route
              path="/board"
              element={
                <>
                  <BoardPage />
                  <LocationProbe />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function column(id: string) {
  return within(screen.getByTestId(`board-column-${id}`));
}

function cardFor(title: string): HTMLElement {
  const card = screen.getByText(title).closest<HTMLElement>("[data-testid=task-card]");
  if (!card) throw new Error(`no card for ${title}`);
  return card;
}

function drop(task: Task, overId: string) {
  const onDragEnd = dndProps.current?.onDragEnd as (event: unknown) => void;
  act(() => {
    onDragEnd({ active: { id: task.id, data: { current: { task } } }, over: { id: overId } });
  });
}

function mutations(method: string) {
  return requests.filter((request) => request.method === method);
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input, init) => serve(input, init));
  requests = [];
  dndProps.current = null;
  tasks = [
    makeTask({
      id: "t_backlog",
      title: "Write the schema",
      depends_on: ["t_review"],
      position: 0,
    }),
    makeTask({
      id: "t_blocked",
      title: "Wire the webhook",
      status: "blocked",
      blocked_reason: "Waiting on the schema",
      position: 1,
    }),
    makeTask({
      id: "t_review",
      title: "Billing page",
      status: "review",
      role: "frontend",
      pr_number: 42,
      pr_url: "https://github.com/acme/app/pull/42",
      ci: "green",
      cost_usd: 1.5,
      assignee: { kind: "agent", id: "frontend" },
      root_session_id: "conv_root",
      acceptance: ["Shows the invoice list", "Links to Stripe"],
    }),
  ];
});

afterEach(cleanup);

describe("BoardPage", () => {
  it("projects tasks into the six columns with their card details", async () => {
    renderBoard();
    await screen.findByText("Billing page");

    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Backlog", "Ready", "Running", "Review", "Intervention", "Merged"]);
    expect(column("backlog").getByText("Write the schema")).toBeInTheDocument();
    expect(column("backlog").getByText("Wire the webhook")).toBeInTheDocument();
    expect(column("backlog").getByTestId("column-count")).toHaveTextContent("2");
    expect(column("review").getByTestId("column-count")).toHaveTextContent("1");

    const review = within(cardFor("Billing page"));
    expect(review.getByText("frontend")).toBeInTheDocument();
    expect(review.getByRole("link", { name: "Open pull request #42" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/42",
    );
    expect(review.getByTestId("ci-badge")).toHaveAttribute("data-ci", "green");
    expect(review.getByText("$1.50")).toBeInTheDocument();
    expect(review.getByLabelText("Agent frontend")).toBeInTheDocument();

    expect(within(cardFor("Write the schema")).getByLabelText("1 dependency")).toBeInTheDocument();
    const blocked = within(cardFor("Wire the webhook"));
    expect(blocked.getByText("Blocked")).toBeInTheDocument();
    expect(blocked.getByText("Waiting on the schema")).toBeInTheDocument();
  });

  it("filters the board to blocked tasks", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    const toggle = screen.getByRole("button", { name: /Blocked/ });
    expect(toggle).toHaveTextContent("1");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("Billing page")).not.toBeInTheDocument();
    expect(screen.getByText("Wire the webhook")).toBeInTheDocument();
  });

  it("PATCHes the status when a card is dropped on another column", async () => {
    renderBoard();
    await screen.findByText("Write the schema");
    drop(tasks[0], "ready");

    expect(await column("ready").findByText("Write the schema")).toBeInTheDocument();
    await waitFor(() => expect(mutations("PATCH")).toHaveLength(1));
    expect(mutations("PATCH")[0]).toEqual({
      method: "PATCH",
      url: "/v1/shipcrew/tasks/t_backlog",
      body: { status: "ready" },
    });
  });

  it("ignores drops on the card's own column", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    drop(tasks[2], "review");
    expect(mutations("PATCH")).toHaveLength(0);
  });

  it("starts the task when it is dropped on Running", async () => {
    renderBoard();
    await screen.findByText("Write the schema");
    drop(tasks[0], "running");
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "POST",
        url: "/v1/shipcrew/tasks/t_backlog/start",
        body: undefined,
      }),
    );
    await waitFor(() =>
      expect(column("running").getByText("Write the schema")).toBeInTheDocument(),
    );
    expect(mutations("PATCH")).toHaveLength(0);
  });

  it("confirms before a manual move to Merged", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    drop(tasks[2], "merged");
    const dialog = await screen.findByRole("dialog", { name: "Mark as merged?" });
    expect(mutations("PATCH")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Mark as merged" }));
    await waitFor(() =>
      expect(mutations("PATCH")).toEqual([
        { method: "PATCH", url: "/v1/shipcrew/tasks/t_review", body: { status: "merged" } },
      ]),
    );
  });

  it("offers the same moves and assignment from the card menu", async () => {
    renderBoard();
    await screen.findByText("Write the schema");
    const trigger = screen.getByRole("button", { name: "Actions for Write the schema" });

    fireEvent.pointerDown(trigger, { button: 0 });
    const items = screen.getAllByRole("menuitem").map((item) => item.textContent);
    expect(items).not.toContain("Backlog");
    fireEvent.click(screen.getByRole("menuitem", { name: "Review" }));
    await waitFor(() => expect(mutations("PATCH").at(-1)?.body).toEqual({ status: "review" }));

    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for Write the schema" }), {
      button: 0,
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Me" }));
    await waitFor(() =>
      expect(mutations("PATCH").at(-1)?.body).toEqual({
        assignee: { kind: "human", id: "ana@example.com" },
      }),
    );
    await waitFor(() =>
      expect(
        within(cardFor("Write the schema")).getByLabelText("ana@example.com"),
      ).toBeInTheDocument(),
    );
  });

  it("opens the drawer with acceptance criteria and the sub-agent tree", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    fireEvent.click(screen.getByRole("button", { name: "Billing page" }));

    const drawer = await screen.findByTestId("task-drawer");
    expect(screen.getByTestId("location")).toHaveTextContent("/board?task=t_review");
    const inDrawer = within(drawer);
    expect(inDrawer.getByRole("heading", { name: "Billing page" })).toBeInTheDocument();
    expect(inDrawer.getByTestId("drawer-status")).toHaveTextContent("Review");
    expect(inDrawer.getByTestId("ci-badge")).toHaveAttribute("data-ci", "green");
    expect(
      within(inDrawer.getByRole("list", { name: "Acceptance criteria" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Shows the invoice list", "Links to Stripe"]);
    expect(await inDrawer.findByTestId("subagents-graph")).toHaveTextContent("conv_root");
    expect(inDrawer.getByRole("link", { name: "Open session" })).toHaveAttribute(
      "href",
      "/c/conv_root",
    );

    fireEvent.click(inDrawer.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("task-drawer")).not.toBeInTheDocument());
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/board$/);
  });

  it("shows an empty tree for an unstarted task and starts it from the drawer", async () => {
    renderBoard("/board?task=t_backlog");
    const drawer = await screen.findByTestId("task-drawer");
    const inDrawer = within(drawer);
    expect(inDrawer.getByTestId("task-agent-tree-empty")).toBeInTheDocument();
    expect(inDrawer.queryByRole("link", { name: "Open session" })).not.toBeInTheDocument();
    expect(inDrawer.getByText("Billing page")).toBeInTheDocument(); // dependency name

    fireEvent.click(inDrawer.getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(inDrawer.getByTestId("subagents-graph")).toHaveTextContent("conv_started"),
    );
    expect(inDrawer.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("mounts the agent graph only after the child sessions have loaded", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (input, init) => {
      if (input.toString().endsWith("/child_sessions")) await gate;
      return serve(input, init);
    });
    renderBoard("/board?task=t_review");
    const inDrawer = within(await screen.findByTestId("task-drawer"));
    expect(inDrawer.getByTestId("task-agent-tree-pending")).toHaveTextContent("Loading agents");
    expect(inDrawer.queryByTestId("subagents-graph")).not.toBeInTheDocument();
    release();
    expect(await inDrawer.findByTestId("subagents-graph")).toHaveTextContent("conv_root");
  });

  it("creates a task from the inline form", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    fireEvent.click(screen.getByRole("button", { name: "New task" }));
    const form = screen.getByRole("form", { name: "New task" });
    fireEvent.change(within(form).getByLabelText("Task title"), {
      target: { value: "Add refunds" },
    });
    fireEvent.change(within(form).getByLabelText("Acceptance criteria"), {
      target: { value: "Refund button\n\nAudit log entry" },
    });
    fireEvent.change(within(form).getByLabelText("Owned paths"), {
      target: { value: "src/refunds/**, api/refunds.py" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Add task" }));

    await waitFor(() => expect(column("backlog").getByText("Add refunds")).toBeInTheDocument());
    expect(mutations("POST")).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/missions/mission_1/tasks",
      body: {
        title: "Add refunds",
        role: "developer",
        acceptance: ["Refund button", "Audit log entry"],
        owned_paths: ["src/refunds/**", "api/refunds.py"],
      },
    });
  });
});
