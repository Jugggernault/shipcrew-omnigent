import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeTask } from "./fixtures";
import { MissionCommandBox, RunAllTasksButton, runnableBacklog } from "./MissionRun";

afterEach(cleanup);

describe("runnableBacklog", () => {
  it("counts backlog cards an agent may take", () => {
    const tasks = [
      makeTask({ id: "a" }),
      makeTask({ id: "b", assignee: { kind: "agent", id: "developer" } }),
      makeTask({ id: "human", assignee: { kind: "human", id: "ana" } }),
      makeTask({ id: "ready", status: "ready" }),
      makeTask({ id: "blocked", status: "blocked" }),
      makeTask({ id: "merged", status: "merged" }),
    ];
    expect(runnableBacklog(tasks).map((task) => task.id)).toEqual(["a", "b"]);
  });
});

describe("RunAllTasksButton", () => {
  it("shows the count and is disabled when there is nothing to run", () => {
    render(<RunAllTasksButton count={0} onRun={vi.fn()} />);
    const button = screen.getByRole("button", { name: /Run all tasks/ });
    expect(button).toBeDisabled();
    expect(within(button).getByTestId("run-all-count")).toHaveTextContent("0");
  });

  it("asks for confirmation with the number of tasks before running", async () => {
    const onRun = vi.fn();
    render(<RunAllTasksButton count={3} onRun={onRun} />);
    fireEvent.click(screen.getByRole("button", { name: /Run all tasks/ }));
    const dialog = await screen.findByRole("dialog", { name: "Run 3 tasks?" });
    expect(dialog).toHaveTextContent("3 tasks from the backlog move to Ready");
    expect(onRun).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Run 3 tasks" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does nothing when the confirmation is cancelled", async () => {
    const onRun = vi.fn();
    render(<RunAllTasksButton count={1} onRun={onRun} />);
    fireEvent.click(screen.getByRole("button", { name: /Run all tasks/ }));
    const dialog = await screen.findByRole("dialog", { name: "Run 1 task?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onRun).not.toHaveBeenCalled();
  });
});

describe("MissionCommandBox", () => {
  it("sends the trimmed text and clears the box", async () => {
    const onCommand = vi.fn(async () => ({ message: "Moved 2 tasks to Ready." }));
    render(<MissionCommandBox onCommand={onCommand} />);
    const input = screen.getByRole("textbox", { name: "Command for the crew" });
    expect(input).toHaveAttribute("placeholder", "Ask the crew…");
    expect(screen.getByRole("button", { name: "Send command" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "  lance tout  " } });
    fireEvent.submit(screen.getByRole("form", { name: "Ask the crew" }));
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith("lance tout"));
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("keeps the text when the command is refused", async () => {
    const onCommand = vi.fn(async () => {
      throw new Error("Unknown command");
    });
    render(<MissionCommandBox onCommand={onCommand} />);
    const input = screen.getByRole("textbox", { name: "Command for the crew" });
    fireEvent.change(input, { target: { value: "deploy" } });
    fireEvent.click(screen.getByRole("button", { name: "Send command" }));
    await waitFor(() => expect(onCommand).toHaveBeenCalledTimes(1));
    expect(input).toHaveValue("deploy");
  });
});
