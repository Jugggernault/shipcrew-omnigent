// Inline "new task" form at the top of the Backlog column.

import { useState, type FormEvent } from "react";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { CreateTaskInput } from "./types";

const DEFAULT_ROLE = "developer";

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function csv(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function NewTaskForm({
  onCreate,
  pending,
}: {
  onCreate: (input: CreateTaskInput) => Promise<unknown>;
  pending?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [role, setRole] = useState(DEFAULT_ROLE);
  const [acceptance, setAcceptance] = useState("");
  const [ownedPaths, setOwnedPaths] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setTitle("");
    setBody("");
    setRole(DEFAULT_ROLE);
    setAcceptance("");
    setOwnedPaths("");
    setError(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    setError(null);
    try {
      await onCreate({
        title: title.trim(),
        body: body.trim() || undefined,
        role: role.trim() || DEFAULT_ROLE,
        acceptance: lines(acceptance),
        owned_paths: csv(ownedPaths),
      });
      reset();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (!open) {
    return (
      <Button
        variant="ghost"
        size="sm"
        className="w-full justify-start text-muted-foreground"
        onClick={() => setOpen(true)}
      >
        <PlusIcon className="size-4" />
        New task
      </Button>
    );
  }

  return (
    <form
      aria-label="New task"
      className="flex flex-col gap-2 rounded-lg border bg-card p-2.5 shadow-sm"
      onSubmit={(event) => void submit(event)}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          setOpen(false);
        }
      }}
    >
      <Input
        autoFocus
        aria-label="Task title"
        placeholder="Task title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
      />
      <Textarea
        aria-label="Description"
        placeholder="Description (optional)"
        rows={2}
        value={body}
        onChange={(event) => setBody(event.target.value)}
      />
      <Textarea
        aria-label="Acceptance criteria"
        placeholder="Acceptance criteria, one per line"
        rows={2}
        value={acceptance}
        onChange={(event) => setAcceptance(event.target.value)}
      />
      <div className="flex gap-2">
        <Input
          aria-label="Role"
          placeholder="Role"
          className="font-mono"
          value={role}
          onChange={(event) => setRole(event.target.value)}
        />
        <Input
          aria-label="Owned paths"
          placeholder="Owned paths (globs, comma-separated)"
          value={ownedPaths}
          onChange={(event) => setOwnedPaths(event.target.value)}
        />
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            reset();
            setOpen(false);
          }}
        >
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={!title.trim() || pending} loading={pending}>
          Add task
        </Button>
      </div>
    </form>
  );
}
