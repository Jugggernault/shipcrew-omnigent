"""Round 6: the approval cards of live run 2 for safe work are gone, the protections stay.

Every live command that asked is here as ALLOW for the roles that have the
command, next to the negative cases proving the gates behind it. The groups
mirror the shipcrew bundles (``agents/_shared/policies/allowlists``). Also:
the manifest companions (``pnpm-workspace.yaml`` ...), worktree preparation
(agent notes excluded, ``node_modules`` seeded from a fallback), and child
session interventions in the store and the report.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from omnigent.shipcrew import deps_seed, main_deps, worktree_prep
from omnigent.shipcrew.policies import (
    curl_output_targets,
    owned_paths,
    parse_command,
    paths_outside_owned,
    safe_sed_filter_script,
    shell_allowlist,
    shell_write_targets,
    workflows_guard,
)
from omnigent.shipcrew.policies import test_writes_only as verify_writes_policy
from omnigent.shipcrew.report import build_report
from omnigent.shipcrew.sessions import snapshot_from_payload
from omnigent.shipcrew.store import Mission, ShipcrewStore, Task
from omnigent.shipcrew.worktree_prep import exclude_agent_notes, prepare_worktree

ROOT = "/wt/task"

READ_ONLY = [
    "ls", "cat", "head", "tail", "wc", "rg !--pre*", "grep", "echo", "printf !-v*", "test",
    "[", "pwd", "true", "cd", "sort !-o* !--output*",
    r"re:^sed -n (-[Er] )?(\d+(,(\d+|\$))?p|\$p|/[^/]*/(,/[^/]*/)?p)( [^ ]+)*$",
    "@sed:sed_filter",
]  # fmt: skip
GIT_READ = ["git status", "git diff !--output*", "git log !--output*"]
GIT_WRITE = ["git add", "git commit", "git checkout", "git mv"]
FS_WRITE = ["mkdir", "touch", "cp", "mv", "rm", "tee"]
DEV_TOOLS = ["npm test", "npm run *", "pnpm test", "npx vitest", "npx playwright test",
             "pnpm install --frozen-lockfile"]  # fmt: skip
DEPS = ["pnpm install !-g !--global !-C !--dir* !-w !--workspace* !-r !--recursive !-F"]
RUN_APP = ["npx next dev", "npx next start", "next start", "ps", "pgrep", "kill", "pkill"]
LOCAL_HTTP = ["@curl:curl_localhost"]
FS_EDIT = ["@sed:sed_in_place"]

QA_ALLOW = [*READ_ONLY, *GIT_READ, "git add", "git commit", *FS_WRITE, *DEV_TOOLS, *RUN_APP,
            *LOCAL_HTTP]  # fmt: skip
BUILDER_ALLOW = [*READ_ONLY, *GIT_READ, *GIT_WRITE, *FS_WRITE, *DEV_TOOLS, *DEPS, *FS_EDIT,
                 *LOCAL_HTTP]  # fmt: skip
REVIEWER_ALLOW = [*READ_ONLY, *GIT_READ, "npx vitest run", "pnpm install --frozen-lockfile"]

FOUNDATION = ["**", "package.json"]
FEATURE = ["app/**", "e2e/cart.spec.ts"]
_RANK = {"ALLOW": 0, "ASK": 1, "DENY": 2}


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


def _write(path: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Write", "arguments": {"file_path": path}}}


def _verdict(policies: list[Any], event: dict[str, Any]) -> str:
    worst = "ALLOW"
    for policy in policies:
        result = policy(event, {})["result"]
        worst = result if _RANK[result] > _RANK[worst] else worst
    return worst


def _qa(owned: list[str] = FEATURE) -> list[Any]:
    return [
        shell_allowlist(allow=QA_ALLOW, read_only=[*READ_ONLY, *GIT_READ], role="qa"),
        owned_paths(owned_paths=owned, root=ROOT, extra_free_paths=[".shipcrew/qa.json"]),
        verify_writes_policy(role="qa", root=ROOT, extra_paths=[".shipcrew/qa.json"]),
        workflows_guard(),
    ]


def _builder(owned: list[str] = FEATURE) -> list[Any]:
    return [
        shell_allowlist(allow=BUILDER_ALLOW, read_only=[*READ_ONLY, *GIT_READ], role="builder"),
        owned_paths(owned_paths=owned, root=ROOT),
        workflows_guard(),
    ]


def _reviewer() -> list[Any]:
    return [
        shell_allowlist(
            allow=REVIEWER_ALLOW,
            read_only=[*READ_ONLY, *GIT_READ],
            role="reviewer",
            shell_writes=False,
        )
    ]


# ── 1. vetted variables as arguments of run / test / app commands ──

LIVE_RUN_APP = [
    "pnpm exec next start -p ${PORT:-3000}",
    'pkill -f "next start -p ${PORT:-3000}"; true',
    "npx next start -p $PORT",
    "CHROMIUM_PATH=${CHROMIUM_PATH:-/usr/bin/chromium} npx playwright test",
    "npx playwright test --project=chromium --output=${TMPDIR:-/tmp}/pw",
]


@pytest.mark.parametrize("command", LIVE_RUN_APP)
def test_live_app_commands_with_vetted_variables_pass_for_qa(command: str) -> None:
    assert _verdict(_qa(), _bash(command)) == "ALLOW"


def test_vetted_variable_passes_in_a_builder_runner_too() -> None:
    assert _verdict(_builder(), _bash("npx vitest run --reporter=dot --port $PORT")) == "ALLOW"
    # ... but a builder has no `next start` on its allowlist: that asks as before.
    assert _verdict(_builder(), _bash("npx next start -p ${PORT:-3000}")) == "ASK"


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("npm test -- $ARGS", "expands $ARGS"),  # unvetted name
        ("npx next start -p $DATABASE_URL", "expands $DATABASE_URL"),
        ("P=3000; npx next start -p $P", "only read-only"),  # assigned: its value is ours
        ("PORT=--inspect=0.0.0.0; npx next start $PORT", "only read-only"),
        ("npx $PORT", "only read-only"),  # the program itself
        ("$PORT test", "only read-only"),
        ("npx next start --$PORT", "only read-only"),  # an option name
        ("npx next start -$PORT", "only read-only"),
        ("git checkout ${PORT:-main}", "only read-only"),  # git writers: never
        ("git diff ${PORT:---output=/tmp/x}", "only read-only"),
        ("rm -rf ${TMPDIR:-/tmp}/x", "only read-only"),  # fs writers: never
        ("cp app/a.ts $HOME/x", "only read-only"),
        ("S=/tmp/x; npm test", "runs after a shell variable"),
    ],
)
def test_expansions_that_still_ask(command: str, why: str) -> None:
    out = _qa()[0](_bash(command), {})
    assert out["result"] == "ASK" and why in out["reason"], out


def test_default_literal_is_what_the_pattern_sees() -> None:
    # `${X:-literal}` is matched with its default in place: a banned option
    # hidden in a default is still refused.
    policy = shell_allowlist(allow=["sort !-o*", "npx vitest"], read_only=[], role="r")
    assert policy(_bash("sort ${PORT:--o/tmp/x} a.txt"), {})["result"] == "ASK"
    assert policy(_bash("npx vitest run --port ${PORT:-5173}"), {})["result"] == "ALLOW"


# ── 2. curl to the local app ──

LIVE_CURL = [
    'curl -s -o /dev/null -w "%{http_code}\\n" -X POST localhost:3000/api/polls '
    "-H 'content-type: application/json' -d '{bad'",
    'curl -s -o /dev/null -w "%{http_code}\\n" localhost:${PORT:-3000}/favicon.ico',
    "curl -si -X DELETE 'http://[::1]:4000/api/polls/1'",
    "curl -sS --max-time 5 http://127.0.0.1:$PORT/api/health",
    "curl -s -d @e2e/fixtures/poll.json -H 'content-type: application/json' localhost:3000/api",
    "curl -sXPOST --data-urlencode 'q=a@b' localhost:3000/search",
    "curl -so /tmp/page.html localhost:3000/",
    "curl -s -o test-results/page.html localhost:3000/",
]


@pytest.mark.parametrize("command", LIVE_CURL)
def test_live_curl_passes_for_qa_and_builders(command: str) -> None:
    assert _verdict(_qa(), _bash(command)) == "ALLOW"
    assert _verdict(_builder(), _bash(command)) == "ALLOW"


def test_reviewer_has_no_curl() -> None:
    assert _verdict(_reviewer(), _bash("curl -s localhost:3000")) == "ASK"


@pytest.mark.parametrize(
    "command",
    [
        "curl -s https://example.com",
        "curl -s localhost:3000 https://example.com",
        "curl -s --url https://example.com",
        "curl -s http://localhost@evil.com/",
        "curl -s $BASE_URL/api",  # the host is not ours to trust
        "curl -s -d @.env localhost:3000",
        "curl -s -d @app/.env.local localhost:3000",
        "curl -s --data-binary @/etc/passwd localhost:3000",
        "curl -s -d @../other/secret localhost:3000",
        "curl -s --data-urlencode q@/etc/passwd localhost:3000",
        "curl -s -F file=@/etc/passwd localhost:3000",
        "curl -s -F 'file=</etc/passwd' localhost:3000",
        "curl -s -H @/etc/hosts localhost:3000",
        "curl -s -T /etc/passwd localhost:3000",
        "curl -s -b /tmp/cookies localhost:3000",
        "curl -K /tmp/cfg localhost:3000",
        "curl --config /tmp/cfg localhost:3000",
        "curl -c /tmp/jar localhost:3000",
        "curl -D /tmp/h localhost:3000",
        "curl --trace /tmp/t localhost:3000",
        "curl -O localhost:3000/file",
        "curl -x http://proxy:8080 localhost:3000",
        "curl --unix-socket /var/run/docker.sock localhost/containers/json",
        "curl --resolve localhost:80:1.2.3.4 localhost",
        "curl -n localhost:3000",
        "curl -s",
    ],
)
def test_curl_refusals(command: str) -> None:
    assert _verdict(_builder(), _bash(command)) == "ASK", command


def test_curl_output_is_a_write_target() -> None:
    assert curl_output_targets(["-so", "/dev/null", "localhost"]) == ["/dev/null"]
    assert shell_write_targets(["curl", "-s", "-o", "app/x.html", "localhost:3000"]) == [
        "app/x.html"
    ]
    # Unmodelled (a remote URL: the allowlist asks anyway): -o still counted.
    assert shell_write_targets(["curl", "https://example.com", "-o", "a"]) == ["a"]
    # Outside the worktree / over a source file: owned paths and test writes judge it.
    assert _verdict(_builder(), _bash("curl -so /home/user/x localhost:3000")) == "ASK"
    assert _verdict(_builder(), _bash("curl -so app/page.tsx localhost:3000")) == "ALLOW"
    assert _verdict(_builder(), _bash("curl -so lib/db.ts localhost:3000")) == "ASK"
    assert _verdict(_qa(), _bash("curl -so app/page.tsx localhost:3000")) == "DENY"
    # The guards see `localhost:$PORT` unexpanded: the -o target is still judged.
    assert _verdict(_builder(), _bash("curl -so lib/db.ts localhost:$PORT/x")) == "ASK"
    assert _verdict(_builder(), _bash("curl -sSo lib/db.ts localhost:$PORT/x")) == "ASK"
    assert _verdict(_qa(), _bash("curl -s -o app/page.tsx localhost:${PORT:-3000}")) == "DENY"
    assert _verdict(_qa(), _bash("curl -s -o /dev/null localhost:${PORT:-3000}")) == "ALLOW"
    assert _verdict(_builder(), _bash("curl -so .github/workflows/ci.yml localhost:3000")) == "ASK"


# ── 3. ${PIPESTATUS[n]} ──


@pytest.mark.parametrize(
    "command",
    [
        "pnpm install --frozen-lockfile --prefer-offline 2>&1 | tail -5; "
        "echo rc=${PIPESTATUS[0]}; ls",
        'npm test 2>&1 | tail -3; echo "exit ${PIPESTATUS[0]} ${PIPESTATUS[@]}"',
        "npm test | tail -1; test $PIPESTATUS -eq 0",
    ],
)
def test_pipestatus_is_a_special_parameter(command: str) -> None:
    segments = parse_command(command, params=True)
    assert segments is not None and all(not s.expanded for s in segments)
    assert _verdict(_qa(), _bash(command)) == "ALLOW"


def test_pipestatus_does_not_launder_other_names() -> None:
    assert _verdict(_qa(), _bash("echo ${PIPESTATUS[0]} $API_KEY")) == "ASK"
    assert parse_command("echo ${PIPESTATUS[x]}", params=True) is None
    assert parse_command("echo $PIPESTATUSX", params=True)[0].expanded == ["PIPESTATUSX"]  # type: ignore[index]


def test_foundation_install_with_pipestatus() -> None:
    command = "pnpm install 2>&1 | tail -5; echo rc=${PIPESTATUS[0]}; ls"
    assert _verdict(_builder(FOUNDATION), _bash(command)) == "ALLOW"
    assert _verdict(_builder(FEATURE), _bash(command)) == "ASK"  # package.json not owned


# ── 4. sed as a pipe filter ──


@pytest.mark.parametrize(
    "script",
    [
        r"s/\x1b\[[0-9;]*m//g",
        "s|a|b|g;s/c/d/2",
        "/^$/d",
        "1,/^---$/d",
        "$!N;s/\\n/ /",
        "10q",
        "/re/,$p",
        "y/abc/xyz/",
        "/x/!{s/a/b/;p}",
        r"s/\([0-9a-f]*\) .*/\1/p",
    ],
)
def test_safe_filter_scripts(script: str) -> None:
    assert safe_sed_filter_script(script), script


@pytest.mark.parametrize(
    "script",
    [
        "s/a/b/w /tmp/x",
        "s/a/b/e",
        "s/a/b/gw /tmp/x",
        "1e id",
        "e id",
        "w /tmp/x",
        "W /tmp/x",
        "r /etc/passwd",
        "R .env",
        "a text",
        "1i text",
        "c text",
        "s/[/]/x/w /tmp/x",  # a delimiter in a bracket: implementations disagree
        "s/a/b/;w /tmp/x",
        "{s/a/b/",
        "",
    ],
)
def test_unsafe_filter_scripts(script: str) -> None:
    assert not safe_sed_filter_script(script), script


def test_sed_filter_in_pipes() -> None:
    live = "npx vitest run components 2>&1 | sed 's/\\x1b\\[[0-9;]*m//g' | grep -E \"Tests|FAIL\""
    assert _verdict(_qa(), _bash(live)) == "ALLOW"
    assert _verdict(_reviewer(), _bash(live)) == "ALLOW"
    for bad in (
        "cat a | sed -i 's/a/b/'",
        "cat a | sed -f script.sed",
        "cat a | sed 's/a/b/' app/page.tsx",  # a file operand
        "cat a | sed -s 's/a/b/'",
        "cat a | sed --expression='s/a/b/'",
        "cat a | sed 's/a/b/w /tmp/x'",
        "cat a | sed -n 'r /etc/passwd'",
    ):
        assert _verdict(_reviewer(), _bash(bad)) == "ASK", bad
    assert shell_write_targets(["sed", "-n", "s/a/b/p"]) == []


# ── 5. manifest companions (pnpm-workspace.yaml ...) ──


@pytest.mark.parametrize("name", ["pnpm-workspace.yaml", ".npmrc", ".nvmrc", ".node-version"])
def test_owning_package_json_owns_its_package_manager_files(name: str) -> None:
    assert paths_outside_owned([name, "package.json", "app/x.ts"], FOUNDATION) == []
    assert paths_outside_owned([f"web/{name}"], ["web/package.json"]) == []
    assert _verdict(_builder(FOUNDATION), _write(f"{ROOT}/{name}")) == "ALLOW"


def test_pnpm_workspace_still_shared_without_package_json() -> None:
    # Live run 2: a Foundation task owning `**` + `package.json` was held on
    # pnpm-workspace.yaml; owning only `**` still does not own it.
    assert paths_outside_owned(["pnpm-workspace.yaml"], ["**"]) == ["pnpm-workspace.yaml"]
    assert _verdict(_builder(["**"]), _write(f"{ROOT}/pnpm-workspace.yaml")) == "ASK"
    assert _verdict(_builder(FEATURE), _write(f"{ROOT}/pnpm-workspace.yaml")) == "ASK"
    assert _verdict(_builder(FOUNDATION), _bash("sed -i 's/a/b/' pnpm-workspace.yaml")) == "ALLOW"
    # Owning package.json in one folder owns nothing next to another one.
    assert paths_outside_owned(["pnpm-workspace.yaml"], ["web/package.json"]) == [
        "pnpm-workspace.yaml"
    ]


# ── 6 + 7. worktree preparation ──


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("node_modules\n")
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _worktree(repo: Path, name: str) -> Path:
    wt = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", f"shipcrew/0000000{len(name) % 10}-{name}", str(wt))
    return wt


class TestAgentNotes:
    def test_untracked_notes_are_excluded_in_every_worktree(self, git_repo: Path) -> None:
        wt = _worktree(git_repo, "a")
        assert exclude_agent_notes(str(wt)) == ["/AGENTS.md", "/CLAUDE.md"]
        assert exclude_agent_notes(str(wt)) == []  # idempotent
        # What `next dev` writes is never staged by `git add -A`.
        (wt / "AGENTS.md").write_text("<!-- next -->\n")
        (wt / "CLAUDE.md").write_text("@AGENTS.md\n")
        (wt / "app").mkdir()
        (wt / "app" / "AGENTS.md").write_text("nested notes are real files\n")
        _git(wt, "add", "-A")
        staged = _git(wt, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["app/AGENTS.md"]
        # info/exclude lives in the common git dir: other worktrees get it too.
        other = _worktree(git_repo, "b")
        (other / "AGENTS.md").write_text("x\n")
        assert _git(other, "status", "--porcelain") == ""
        exclude = Path(_git(git_repo, "rev-parse", "--git-path", "info/exclude"))
        text = (git_repo / exclude if not exclude.is_absolute() else exclude).read_text()
        assert text.count("/AGENTS.md") == 1

    def test_tracked_notes_stay_tracked(self, git_repo: Path) -> None:
        (git_repo / "AGENTS.md").write_text("our crew notes\n")
        _git(git_repo, "add", "AGENTS.md")
        _git(git_repo, "commit", "-qm", "notes")
        wt = _worktree(git_repo, "c")
        assert exclude_agent_notes(str(wt)) == ["/CLAUDE.md"]
        (wt / "AGENTS.md").write_text("changed\n")
        assert _git(wt, "status", "--porcelain") == "M AGENTS.md"

    def test_not_a_repo_is_a_no_op(self, tmp_path: Path) -> None:
        assert exclude_agent_notes(str(tmp_path)) == []


class TestSeedFallbacks:
    def _modules(self, root: Path) -> None:
        (root / "node_modules" / "left-pad").mkdir(parents=True)
        (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")

    def test_seeded_from_a_fallback_when_main_has_none(self, git_repo: Path) -> None:
        task_wt = _worktree(git_repo, "task")
        self._modules(task_wt)
        review = _worktree(git_repo, "review")
        method = prepare_worktree(str(git_repo), str(review), seed_from=[str(task_wt)])
        assert method in ("reflink", "hardlink")
        assert (review / "node_modules" / "left-pad" / "index.js").is_file()
        assert _git(review, "check-ignore", "AGENTS.md") == "AGENTS.md"

    def test_main_first_then_fallbacks(self, git_repo: Path) -> None:
        self._modules(git_repo)
        (git_repo / "node_modules" / "main-only").mkdir()
        task_wt = _worktree(git_repo, "task")
        self._modules(task_wt)
        review = _worktree(git_repo, "review")
        assert deps_seed.seed_node_modules(str(git_repo), str(review), fallbacks=[str(task_wt)])
        assert (review / "node_modules" / "main-only").is_dir()

    def test_a_different_lockfile_is_never_used(self, git_repo: Path) -> None:
        task_wt = _worktree(git_repo, "task")
        self._modules(task_wt)
        (task_wt / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nother: 1\n")
        review = _worktree(git_repo, "review")
        assert prepare_worktree(str(git_repo), str(review), seed_from=[str(task_wt)]) is None
        assert not (review / "node_modules").exists()

    def test_a_running_main_install_is_not_copied(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._modules(git_repo)  # half written by the running install
        task_wt = _worktree(git_repo, "task")
        review = _worktree(git_repo, "review")
        waited: list[float] = []

        def busy(repo: Path, timeout: float) -> bool:
            waited.append(timeout)
            return False

        monkeypatch.setattr(main_deps, "wait_idle", busy)
        assert prepare_worktree(str(git_repo), str(review), seed_from=[str(task_wt)]) is None
        assert waited == [worktree_prep.MAIN_INSTALL_WAIT_S]
        assert not (review / "node_modules").exists()
        self._modules(task_wt)
        assert prepare_worktree(str(git_repo), str(review), seed_from=[str(task_wt)])


# ── 8. interventions from child sessions ──


def test_snapshot_keeps_the_elicitation_id() -> None:
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_9",
        "params": {"policy_name": "p", "content_preview": "npm i"},
    }
    snap = snapshot_from_payload({"status": "waiting", "pending_elicitations": [{}, event]})
    assert snap.pending_ask == {"policy": "p", "preview": "npm i"}
    assert snap.pending_ask_id == "elicit_9"
    assert snapshot_from_payload({"status": "idle"}).pending_ask_id is None


def test_store_records_child_interventions_once(tmp_path: Path) -> None:
    store = ShipcrewStore(create_engine(f"sqlite:///{tmp_path / 's.db'}"))
    mission = store.create_mission("M", "/r")
    task = store.create_task(mission.id, "T")
    entry = {"reason": "reviewer: p: npm i", "policy": "p", "preview": "npm i",
             "role": "reviewer", "ask_id": "e1"}  # fmt: skip
    store.record_intervention(task.id, entry)
    store.record_intervention(task.id, entry)
    after = store.record_intervention(task.id, {**entry, "ask_id": "e2"})
    assert after is not None
    assert [i["ask_id"] for i in after.interventions] == ["e1", "e2"]
    assert after.status == task.status
    assert store.record_intervention("nope", entry) is None


def test_report_counts_child_interventions() -> None:
    mission = Mission(
        id="m", title="Polls", repo_path="/r", repo_url=None, status="done", created_at=0
    )
    task = Task(
        id="t",
        mission_id="m",
        title="Votes",
        role="developer",
        interventions=[
            {"at": 0, "reason": "p: a", "policy": "shipcrew_owned_paths", "preview": "a"},
            {
                "at": 0,
                "reason": "reviewer: s: pnpm i",
                "policy": "shipcrew_shell_allowlist",
                "preview": "pnpm i",
                "role": "reviewer",
                "ask_id": "e1",
            },
        ],
    )
    report = build_report(mission, [task])
    assert "- **Human interventions:** 2 (" in report
    assert "; 1 in reviewer/integrator sessions)" in report
    assert "(1970-01-01 00:00 UTC, reviewer): shipcrew_shell_allowlist: pnpm i" in report
