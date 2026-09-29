// "New mission" dialog: a mission is a title plus the repository it works on.

import { useState, type FormEvent } from "react";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { CreateMissionInput, Mission } from "./types";

export function NewMissionDialog({
  onCreate,
}: {
  onCreate: (input: CreateMissionInput) => Promise<Mission>;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim() || !repoPath.trim()) return;
    setPending(true);
    setError(null);
    try {
      await onCreate({
        title: title.trim(),
        repo_path: repoPath.trim(),
        repo_url: repoUrl.trim() || undefined,
      });
      setTitle("");
      setRepoPath("");
      setRepoUrl("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" className="shrink-0 text-muted-foreground">
          <PlusIcon className="size-4" />
          New mission
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New mission</DialogTitle>
          <DialogDescription>
            Tasks of a mission run in worktrees of its repository.
          </DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-3" onSubmit={(event) => void submit(event)}>
          <label className="flex flex-col gap-1 text-ui">
            Title
            <Input
              autoFocus
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Launch the billing page"
            />
          </label>
          <label className="flex flex-col gap-1 text-ui">
            Repository path
            <Input
              className="font-mono"
              value={repoPath}
              onChange={(event) => setRepoPath(event.target.value)}
              placeholder="/home/me/code/app"
            />
          </label>
          <label className="flex flex-col gap-1 text-ui">
            Repository URL (optional)
            <Input
              className="font-mono"
              value={repoUrl}
              onChange={(event) => setRepoUrl(event.target.value)}
              placeholder="https://github.com/acme/app"
            />
          </label>
          {error && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!title.trim() || !repoPath.trim()} loading={pending}>
              Create mission
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
