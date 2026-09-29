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
import type { Mission, Task } from "@/board/types";
import { showToast } from "@/components/ui/toast";
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
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "ana@example.com" }));
vi.mock("@/shell/SubagentsGraphView", () => ({
  SubagentsGraphView: ({ rootSessionId }: { rootSessionId: string }) => (
    <div data-testid="subagents-graph">{rootSessionId}</div>
  ),
}));

const fetchMock = vi.mocked(authenticatedFetch);
let tasks: Task[] = [];
let missions: Mission[] = [];
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
  if (url === "/v1/shipcrew/missions") return json({ missions });
  if (url === "/v1/sessions/projects") {
    return json([
      { id: "proj_1", name: "Billing launch" },
      { id: "proj_2", name: "Docs site" },
    ]);
  }
  if (url === "/v1/shipcrew/missions/mission_1/plan" && method === "POST") {
    const planned: Mission = {
      ...missions[0],
      plan: { status: "running", session_id: "conv_plan", error: null, imported_count: 0 },
    };
    missions = [planned];
    return json(planned);
  }
  if (url === "/v1/shipcrew/missions/mission_1/sync" && method === "POST") {
    return json(missions[0]);
  }
  if (url === "/v1/shipcrew/missions/mission_1/start-all" && method === "POST") {
    const started = tasks.filter((task) => task.status === "backlog").map((task) => task.id);
    tasks = tasks.map((task) =>
      started.includes(task.id) ? { ...task, status: "ready" as const } : task,
    );
    return json({ mission: missions[0], started });
  }
  if (url === "/v1/shipcrew/missions/mission_1" && method === "PATCH") {
    missions = [{ ...missions[0], ...(body as Partial<Mission>) }];
    return json(missions[0]);
  }
  if (url === "/v1/shipcrew/missions/mission_1/command" && method === "POST") {
    const text = (body as { text: string }).text;
    if (text !== "lance tout") {
      return json({ error: { code: "invalid_input", message: "Unknown command" } }, 400);
    }
    return json({ intent: "start_all", message: "Moved 1 task to Ready.", mission: missions[0] });
  }
  if (url === "/v1/shipcrew/missions/mission_1/ship" && method === "POST") {
    const shipping: Mission = {
      ...missions[0],
      ship: {
        status: "deploying",
        url: null,
        report_md: null,
        error: null,
        note: null,
        started_at: 1_788_260_000,
        finished_at: null,
        session_id: "conv_ship",
        decisions: [],
        cost_usd: 0,
      },
    };
    missions = [shipping];
    return json(shipping);
  }
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
  const match = /^\/v1\/shipcrew\/tasks\/([^/]+)(?:\/(start|stop|approve|request-changes))?$/.exec(
    url,
  );
  if (match) {
    const [, id, action] = match;
    const current = tasks.find((task) => task.id === id);
    if (!current) return json({ detail: "not found" }, 404);
    const next: Task =
      action === "start"
        ? { ...current, status: "running", root_session_id: "conv_started" }
        : action === "stop"
          ? { ...current, status: "backlog" }
          : action === "approve"
            ? { ...current, status: "merged", needs_human_approval: false }
            : action === "request-changes"
              ? { ...current, status: "running" }
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
  vi.mocked(showToast).mockClear();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input, init) => serve(input, init));
  requests = [];
  dndProps.current = null;
  missions = [makeMission()];
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
  it("shows the PR loop state on cards and why Intervention cards wait", async () => {
    tasks = [
      ...tasks.map((task) =>
        task.id === "t_review"
          ? {
              ...task,
              branch: "shipcrew/t_review-billing-page",
              issue_number: 7,
              issue_url: "https://github.com/acme/app/issues/7",
              ci: "red" as const,
              ci_attempts: 2,
              review: { verdict: "changes" as const, summary: "", findings: [] },
            }
          : task,
      ),
      makeTask({
        id: "t_gate",
        title: "Migrate invoices",
        status: "intervention",
        needs_human_approval: true,
        approval_reasons: ["Touches migrations/**"],
      }),
      makeTask({
        id: "t_rounds",
        title: "Refund flow",
        status: "intervention",
        pr_number: 9,
        blocked_reason: "reviewer requested changes 3 times: refunds untested",
        review: { verdict: "changes", summary: "", findings: [] },
        position: 1,
      }),
      makeTask({ id: "t_ask", title: "Send receipts", status: "intervention", position: 2 }),
    ];
    renderBoard();
    await screen.findByText("Migrate invoices");

    const review = within(cardFor("Billing page"));
    expect(review.getByTestId("branch-chip")).toHaveTextContent("shipcrew/t_review-billing-page");
    expect(review.getByRole("link", { name: "Open issue #7" })).toBeInTheDocument();
    expect(review.getByTestId("ci-badge")).toHaveTextContent("fix 2/3");
    expect(review.getByTestId("review-badge")).toHaveAttribute("data-verdict", "changes");
    expect(review.queryByTestId("intervention-reason")).not.toBeInTheDocument();

    const gate = within(cardFor("Migrate invoices"));
    expect(
      gate.getByRole("button", { name: "Needs approval: Touches migrations/**" }),
    ).toBeVisible();
    expect(gate.getByTestId("intervention-reason")).toHaveAttribute("data-kind", "approval");
    expect(gate.getByLabelText("Needs your approval")).toHaveTextContent("Approve to merge");
    expect(
      within(cardFor("Refund flow")).getByLabelText("Review rounds exhausted"),
    ).toBeInTheDocument();
    const ask = within(cardFor("Send receipts"));
    expect(ask.getByLabelText("Guardrail ask pending")).toHaveTextContent("Guardrail ask");
    expect(ask.getByRole("link", { name: "Inbox" })).toHaveAttribute("href", "/inbox");
  });

  it("starts the planner from a PRD and links its session", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    const dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    fireEvent.change(within(dialog).getByLabelText("PRD (optional)"), {
      target: { value: "Ship refunds" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start planning" }));

    expect(await screen.findByRole("link", { name: /Planning/ })).toHaveAttribute(
      "href",
      "/c/conv_plan",
    );
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/missions/mission_1/plan",
      body: { prd: "Ship refunds" },
    });
    expect(screen.getByRole("button", { name: "Plan from PRD" })).toBeDisabled();
  });

  it("shows the imported plan of a mission", async () => {
    missions = [
      makeMission({
        plan: { status: "imported", session_id: "conv_plan", error: null, imported_count: 4 },
      }),
    ];
    renderBoard();
    expect(await screen.findByText("Imported 4 tasks")).toBeInTheDocument();
  });

  it("ships a merged mission and shows the chip, the report and the plan decisions", async () => {
    tasks = [makeTask({ id: "t_done", title: "Parse args", status: "merged" })];
    missions = [makeMission({ plan_decisions: ["Kept one task"] })];
    renderBoard();
    await screen.findByText("Parse args");
    expect(screen.getByTestId("mission-status-chip")).toHaveTextContent("Building");
    expect(screen.getByText("Decisions (1)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Report" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Ship now" }));
    await waitFor(() =>
      expect(screen.getByTestId("mission-status-chip")).toHaveTextContent("Shipping"),
    );
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/missions/mission_1/ship",
      body: undefined,
    });
    expect(vi.mocked(showToast)).toHaveBeenCalledWith("Deploying to Vercel…");
    expect(screen.getByRole("button", { name: "Ship now" })).toBeDisabled();
  });

  it("forces a GitHub sync", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    fireEvent.click(screen.getByRole("button", { name: "Sync GitHub" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "POST",
        url: "/v1/shipcrew/missions/mission_1/sync",
        body: undefined,
      }),
    );
  });

  it("runs every backlog task after a confirmation", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    const button = screen.getByRole("button", { name: /Run all tasks/ });
    expect(within(button).getByTestId("run-all-count")).toHaveTextContent("1");
    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Run 1 task?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run 1 task" }));
    await waitFor(() => expect(column("ready").getByText("Write the schema")).toBeInTheDocument());
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/missions/mission_1/start-all",
      body: undefined,
    });
    expect(vi.mocked(showToast)).toHaveBeenCalledWith("Moved 1 task to Ready");
    expect(screen.getByRole("button", { name: /Run all tasks/ })).toBeDisabled();
  });

  it("saves the auto-run setting from the plan dialog", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    fireEvent.click(screen.getByRole("button", { name: "Plan from PRD" }));
    const dialog = await screen.findByRole("dialog", { name: "Plan from PRD" });
    fireEvent.click(
      within(dialog).getByRole("checkbox", { name: "Run automatically after planning" }),
    );
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "PATCH",
        url: "/v1/shipcrew/missions/mission_1",
        body: { auto_run: true },
      }),
    );
    await waitFor(() =>
      expect(
        within(dialog).getByRole("checkbox", { name: "Run automatically after planning" }),
      ).toBeChecked(),
    );
  });

  it("sends a command to the crew and toasts what it did", async () => {
    renderBoard();
    await screen.findByText("Billing page");
    const input = screen.getByRole("textbox", { name: "Command for the crew" });
    fireEvent.change(input, { target: { value: "lance tout" } });
    fireEvent.submit(screen.getByRole("form", { name: "Ask the crew" }));
    await waitFor(() =>
      expect(vi.mocked(showToast)).toHaveBeenCalledWith("Moved 1 task to Ready."),
    );
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/missions/mission_1/command",
      body: { text: "lance tout" },
    });

    fireEvent.change(input, { target: { value: "deploy" } });
    fireEvent.submit(screen.getByRole("form", { name: "Ask the crew" }));
    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalledWith("Unknown command"));
    expect(input).toHaveValue("deploy");
  });

  it("approves a gated merge from the drawer", async () => {
    tasks = [
      ...tasks,
      makeTask({
        id: "t_gate",
        title: "Migrate invoices",
        status: "intervention",
        needs_human_approval: true,
        approval_reasons: ["Touches migrations/**"],
      }),
    ];
    renderBoard("/board?task=t_gate");
    const inDrawer = within(await screen.findByTestId("task-drawer"));
    fireEvent.click(inDrawer.getByRole("button", { name: "Approve merge" }));
    await waitFor(() => expect(inDrawer.getByTestId("drawer-status")).toHaveTextContent("Merged"));
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/tasks/t_gate/approve",
      body: undefined,
    });
    expect(inDrawer.queryByTestId("drawer-approval")).not.toBeInTheDocument();
  });

  it("sends requested changes to the developer from the drawer", async () => {
    renderBoard("/board?task=t_review");
    const inDrawer = within(await screen.findByTestId("task-drawer"));
    fireEvent.change(inDrawer.getByLabelText("Feedback for the developer"), {
      target: { value: "Paginate the invoice list" },
    });
    fireEvent.click(
      within(inDrawer.getByRole("form", { name: "Request changes" })).getByRole("button", {
        name: "Request changes",
      }),
    );
    await waitFor(() => expect(inDrawer.getByTestId("drawer-status")).toHaveTextContent("Running"));
    expect(requests).toContainEqual({
      method: "POST",
      url: "/v1/shipcrew/tasks/t_review/request-changes",
      body: { message: "Paginate the invoice list" },
    });
    expect(column("running").getByText("Billing page")).toBeInTheDocument();
  });

  describe("mission project", () => {
    beforeEach(() => {
      missions = [
        makeMission({ project_id: "proj_1" }),
        makeMission({ id: "mission_2", title: "Docs site", project_id: "proj_2" }),
      ];
    });

    function selectedTab(): HTMLElement {
      const tabs = within(screen.getByRole("tablist"));
      const selected = tabs.getAllByRole("tab").find((tab) => tab.ariaSelected === "true");
      if (!selected) throw new Error("no selected mission tab");
      return selected;
    }

    it("selects the mission of ?project= (the sidebar's board link)", async () => {
      renderBoard("/board?project=proj_2");
      await screen.findByRole("tablist");
      expect(selectedTab()).toHaveTextContent("Docs site");
    });

    it("prefers ?mission= over ?project=", async () => {
      renderBoard("/board?mission=mission_1&project=proj_2");
      await screen.findByRole("tablist");
      expect(selectedTab()).toHaveTextContent("Billing launch");
    });

    it("falls back to the first mission for an unknown project", async () => {
      renderBoard("/board?project=nope");
      await screen.findByRole("tablist");
      expect(selectedTab()).toHaveTextContent("Billing launch");
    });

    it("links the header back to the project's sessions", async () => {
      renderBoard("/board?mission=mission_2");
      const link = await screen.findByTestId("mission-project-link");
      expect(link).toHaveTextContent("Sessions in Docs site");
      expect(link).toHaveAttribute("href", "/?project=Docs%20site");
    });

    it("drops ?project= once another mission tab is picked", async () => {
      renderBoard("/board?project=proj_2");
      await screen.findByRole("tablist");
      fireEvent.click(screen.getByRole("tab", { name: "Billing launch" }));
      await waitFor(() =>
        expect(screen.getByTestId("location")).toHaveTextContent("/board?mission=mission_1"),
      );
      expect(selectedTab()).toHaveTextContent("Billing launch");
    });

    it("has no project link for a mission without a project", async () => {
      missions = [makeMission()];
      renderBoard("/board");
      await screen.findByRole("tablist");
      expect(screen.queryByTestId("mission-project-link")).not.toBeInTheDocument();
    });
  });
});
