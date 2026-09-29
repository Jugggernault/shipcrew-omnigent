"""Guardrail policies for the shipcrew role bundles.

Five :class:`FunctionPolicy` factories, registered through
:data:`POLICY_REGISTRY` so uploaded bundles may use them:

* :func:`shell_allowlist` -- a per-role shell allowlist. A shell command runs
  with no prompt only when every simple command in it matches the role's
  allowlist; anything else is ASK (an approval card in the Inbox, which moves
  the board card to Intervention). Unanalyzable commands (command
  substitution, heredocs, unbalanced quotes) are ASK too. ``$?``-style
  special parameters and ``${PIPESTATUS[n]}`` always pass; ``$VAR`` only for
  vetted names, in read-only commands or as a plain argument of a command
  that writes nothing; ``sed -i`` / ``perl -pi`` substitutions and ``curl
  -o`` are modelled as writes, ``sed`` as a stdin filter and ``curl`` to a
  local host are built-in matchers. For a role that may edit in place, any
  other ``sed -i`` / ``perl -pi`` (not one simple ``s///``) is DENY with a
  hint to use the Edit tool: the agent picked the wrong tool, no human is
  needed. ``pnpm|npm|yarn`` output-only options (``-s``, ``--silent``,
  ``--loglevel x``, ...) are dropped before matching.
* :func:`owned_paths` -- the task's ``owned_paths`` contract. File writes
  (``Write`` / ``Edit`` / ``sys_os_write`` / shell redirections / ``cp``,
  ``mv``, ``rm``, ``git mv`` ... / formatter ``--write`` / dependency changes)
  outside the owned globs are ASK, and so are writes to the shared contract
  files (``package.json``, lockfiles) unless the task owns them by name
  (owning a manifest owns the lockfiles next to it).
  Reads are never gated. The shipcrew server injects ``root`` and
  ``owned_paths`` into the bundle when it starts a task; without them the
  policy abstains. A write to a file another active task of the mission
  owns (``other_tasks``, injected at start) is DENY with a hint to work
  against the shared contract.
* :func:`test_writes_only` -- verify roles (qa, security) write test files
  and their report only; any other write is DENY, and so is removing,
  renaming or truncating a test that is already on the base branch.
* :func:`workflows_guard` -- ASK before a write to ``.github/workflows/**``.
* :func:`push_guard` -- the orchestrator may push only task branches named
  ``shipcrew/<8 hex>-<slug>``, every push segment explicitly.

These policies return ALLOW or ASK, and DENY where no human decision is
needed (the push guard, the test-writes guard, a complex in-place edit,
another task's file); the
catastrophic DENY set stays in omnigent's ``blast_radius`` and the shipcrew CEL
fragments. Evaluation is most-restrictive-wins across policies.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from omnigent.policies.builtins._shell import SHELL_TOOLS
from omnigent.policies.builtins.safety import NATIVE_WRITE_TOOLS

_Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
_Evaluator: TypeAlias = Callable[[_Json, _Json], _Json]

_ALLOW: _Json = {"result": "ALLOW"}

WRITE_TOOLS: frozenset[str] = NATIVE_WRITE_TOOLS | {"sys_os_write", "sys_os_edit", "write", "edit"}
_PATH_KEYS = ("file_path", "path", "notebook_path")

# Env assignments a command may be prefixed with (``CI=1 npm test``). Anything
# else (``PATH=``, ``LD_PRELOAD=``, ``NODE_OPTIONS=``) changes what runs: ASK.
DEFAULT_ENV_ALLOW: tuple[str, ...] = (
    "CI",
    "CHROMIUM_PATH",
    "NODE_ENV",
    "PORT",
    "HOST",
    "HOSTNAME",
    "BASE_URL",
    "TZ",
    "LANG",
    "LC_ALL",
    "FORCE_COLOR",
    "NO_COLOR",
    "DEBUG",
    "PWDEBUG",
    "NEXT_TELEMETRY_DISABLED",
    "PLAYWRIGHT_HTML_OPEN",
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
)

# Variables a read-only command may expand besides the env-allow names and the
# names assigned earlier in the same command (``S=/x; cat $S/a``). Secrets
# (``$DATABASE_URL``, ``$API_KEY``) are not here: printing one would put its
# value in the model context.
DEFAULT_EXPAND_ALLOW: tuple[str, ...] = (
    "HOME",
    "PWD",
    "OLDPWD",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "TMPDIR",
    "RUNNER_TEMP",
)

# The read-only commands a variable expansion may appear in when a bundle does
# not pass its own ``read_only`` list (the shipcrew bundles pass their
# read_only + git_read groups).
DEFAULT_READ_ONLY: tuple[str, ...] = (
    "echo", "cat", "ls", "head", "tail", "wc", "test", "[", "pwd", "basename", "dirname",
    "stat", "file",
)  # fmt: skip

# Names a bare assignment (``NAME=value;``) may never set: they change what
# the shell or the next command runs (``PATH=/tmp/x; cat f`` runs /tmp/x/cat).
_DANGEROUS_VAR = re.compile(
    r"^(PATH|IFS|CDPATH|BASH_ENV|ENV|HOME|SHELL|SHELLOPTS|BASHOPTS|PS4|PROMPT_COMMAND|"
    r"GLOBIGNORE|EDITOR|VISUAL|BROWSER|PAGER|TMPDIR|LD_\w*|DYLD_\w*|GIT_\w*|\w*_PAGER|"
    r"LESS\w*|GREP_\w*|NODE_\w*|NPM_\w*|npm_\w*|PNPM_\w*|YARN_\w*|COREPACK_\w*|PYTHON\w*|"
    r"PERL\w*|RUBY\w*|SSH_\w*|BASH_FUNC_\w*)$"
)
# Programs whose arguments can turn an expanded word into code or a state
# change even when they read only (``printf -v PATH``, ``cd $X``, ``find -exec``).
_EXPANSION_UNSAFE_PROGRAMS = frozenset(
    {"cd", "printf", "find", "xargs", "env", "command", "exec", "eval", "source", ".",
     "sed", "awk", "perl", "python", "python3", "node", "sh", "bash", "jq", "yq"}
)  # fmt: skip

# Shared contract files: a task changes them only when it owns them by name.
DEFAULT_SHARED_PATHS: tuple[str, ...] = (
    "**/package.json",
    "**/package-lock.json",
    "**/npm-shrinkwrap.json",
    "**/pnpm-lock.yaml",
    "**/pnpm-workspace.yaml",
    "**/yarn.lock",
    "**/bun.lock",
    "**/bun.lockb",
    "**/pyproject.toml",
    "**/uv.lock",
    "**/poetry.lock",
)

# Build output, caches and scratch space: always writable.
DEFAULT_FREE_PATHS: tuple[str, ...] = (
    "**/node_modules/**",
    ".next/**",
    "dist/**",
    "out/**",
    "coverage/**",
    "test-results/**",
    "playwright-report/**",
    "blob-report/**",
    ".turbo/**",
    ".cache/**",
    "**/__pycache__/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".mypy_cache/**",
    ".venv/**",
    "*.tsbuildinfo",
    "/tmp/**",
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
)

# Test files a verify role (qa, security) may write. test/, tests/ and e2e/
# only at the top level: a tests/ folder deep in the app could be imported by it.
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "test/**",
    "tests/**",
    "e2e/**",
    "**/__tests__/**",
    "**/__snapshots__/**",
    "**/*.test.*",
    "**/*.spec.*",
)

DEFAULT_BRANCH_PATTERN = r"shipcrew/[0-9a-f]{8}-[a-z0-9]+(?:-[a-z0-9]+)*"

# ── Shell parsing ────────────────────────────────────────────────────────────

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", "\n", "(", ")", ";;"})
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
_READ_REDIRECTS = frozenset({"<", "<<<"})
_TRUNCATING_REDIRECTS = frozenset({">", ">|", "&>", ">&"})
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Marks a real parameter expansion (``$X``, ``${X}``) in the parsed words, so a
# single-quoted ``'$X'`` (a literal) is told apart after shlex.
EXPANSION_MARK = "\x00"
_MARKED_NAME = re.compile(EXPANSION_MARK + r"\{?([A-Za-z_][A-Za-z0-9_]*)")
# ``$?``, ``$#``, ``$$``, ``$!``: always a number, harmless anywhere.
_SPECIAL_PARAMS = frozenset("?#$!")
# ``${PIPESTATUS[0]}`` / ``${PIPESTATUS[@]}`` / ``$PIPESTATUS``: exit codes of
# the last pipeline, numbers like ``$?``.
_PIPESTATUS = re.compile(r"\{PIPESTATUS(?:\[(?:\d+|@|\*)\])?\}|PIPESTATUS(?![A-Za-z0-9_\[])")
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# ``${NAME}``, ``${NAME:-literal}``, ``${NAME-literal}``, ``${?}``.
_BRACED_PARAM = re.compile(
    r"\{(?P<name>[A-Za-z_][A-Za-z0-9_]*|[?#$!])(?::?-(?P<default>[\w./:@%+,=-]*))?\}"
)
# ``git commit -m "$(cat <<'EOF' ... EOF)"`` is how Claude Code writes commit
# messages: the substitution only produces a literal string, so it is replaced
# by a placeholder before the unanalyzable-construct check. The delimiter must
# be quoted: with a bare ``<<EOF`` the shell expands ``$(..)`` in the body.
_HEREDOC_STRING = re.compile(
    r"\$\(\s*cat\s+<<-?\s*(['\"])(\w+)\1[ \t]*\n.*?\n[ \t]*\2[ \t]*\n?\s*\)", re.DOTALL
)
# fd duplication / close (``2>&1``, ``>&2``, ``3>&-``): not a file write.
_FD_DUP = re.compile(r"(^|[\s;&|(])\d*[<>]&\s*(\d+|-)(?=$|[\s;&|)])")
# ``2>file`` / ``2>>file``: drop the fd number glued to the operator.
_FD_NUMBER = re.compile(r"(^|[\s;&|(])\d+(?=>)")
_BIN_PREFIXES = (
    "/usr/local/bin/",
    "/usr/bin/",
    "/bin/",
    "./node_modules/.bin/",
    "node_modules/.bin/",
)
_NPX_FLAGS = frozenset(
    {"-y", "--yes", "--no-install", "--no", "--prefer-offline", "--offline", "-q", "--quiet"}
)


@dataclass
class Segment:
    """One simple command of a shell command line.

    :param argv: The command words, env assignments removed, program
        normalized (``/usr/bin/git`` -> ``git``, ``pnpm exec vitest`` ->
        ``npx vitest``).
    :param env: Names of the leading ``NAME=value`` assignments.
    :param writes: Targets of file-writing redirections (``>``, ``>>``, ``&>``).
    :param raw: The segment words as written, for messages.
    :param truncates: The :attr:`writes` targets opened for truncation
        (``>``, ``>|``, ``&>``, ``>&``; not ``>>``).
    :param expanded: Names of the parameters the shell expands in this
        segment (only with ``parse_command(..., params=True)``; the words
        then carry :data:`EXPANSION_MARK` where each ``$`` was).
    """

    argv: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    expanded: list[str] = field(default_factory=list)
    truncates: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.raw)


def _tokens(command: str) -> list[str] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return None


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in ";&|<>()\n" for c in token)


_TIMEOUT_VALUE_FLAGS = frozenset({"-s", "--signal", "-k", "--kill-after"})


def _unwrap(argv: list[str]) -> list[str]:
    """Drop ``timeout [opts] DURATION`` / ``time`` / ``nohup`` in front of a command."""
    while argv:
        if argv[0] in ("time", "nohup"):
            argv = argv[1:]
        elif argv[0] == "timeout":
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in _TIMEOUT_VALUE_FLAGS else 1
            argv = argv[i + 1 :]  # skip the duration
        else:
            return argv
    return argv


# Output-only package-manager options: ``pnpm -s lint`` is ``pnpm lint``.
_PM_OUTPUT_FLAGS = frozenset({"-s", "--silent", "--color", "--no-color", "-w", "--workspace-root"})
_PM_OUTPUT_VALUE_FLAGS = frozenset({"--loglevel"})
_PM_OUTPUT_PREFIXES = ("--loglevel=", "--reporter=")
# Subcommands that change dependencies: ``-w`` there picks the root manifest,
# so it is kept (the entries judge it as they always did).
_PM_DEP_SUBCOMMANDS = frozenset(
    {"add", "install", "i", "remove", "rm", "uninstall", "un", "update", "up", "upgrade",
     "ci", "link", "unlink", "dedupe", "prune", "import"}
)  # fmt: skip


def _strip_pm_output_flags(argv: list[str]) -> list[str]:
    """``pnpm|npm|yarn`` *argv* without output-only options before the script name.

    Stripped: ``-s``/``--silent``, ``--color``/``--no-color``, ``--loglevel
    <x>`` / ``--loglevel=<x>``, ``--reporter=<x>`` and (except for a
    dependency subcommand) ``-w``/``--workspace-root``, in front of the
    subcommand and, after ``run`` / ``run-script``, in front of the script
    name. Later words (the script's own arguments) are kept.
    """
    head, rest = argv[0], argv[1:]

    def _skip(words: list[str], *, keep_w: bool) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(words):
            word = words[i]
            if word in _PM_OUTPUT_VALUE_FLAGS and i + 1 < len(words):
                i += 2
                continue
            if word.startswith(_PM_OUTPUT_PREFIXES) or (
                word in _PM_OUTPUT_FLAGS and not (keep_w and word in ("-w", "--workspace-root"))
            ):
                i += 1
                continue
            if not word.startswith("-"):
                return [*out, *words[i:]]
            out.append(word)
            i += 1
        return out

    rest = _skip(rest, keep_w=bool(_PM_DEP_SUBCOMMANDS.intersection(rest)))
    lead = 0
    while lead < len(rest) and rest[lead].startswith("-"):
        lead += 1
    if lead < len(rest) and rest[lead] in ("run", "run-script"):
        rest = [*rest[: lead + 1], *_skip(rest[lead + 1 :], keep_w=False)]
    return [head, *rest]


def _normalize_program(argv: list[str]) -> list[str]:
    argv = _unwrap(argv)
    if not argv:
        return argv
    head = argv[0]
    for prefix in _BIN_PREFIXES:
        if head.startswith(prefix) and "/" not in head[len(prefix) :]:
            argv = [head[len(prefix) :], *argv[1:]]
            break
    if argv[0] in ("pnpm", "npm", "yarn"):
        argv = _strip_pm_output_flags(argv)
    if len(argv) >= 2 and argv[0] in ("pnpm", "npm", "yarn") and argv[1] == "exec":
        rest = argv[2:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        argv = ["npx", *rest]
    if argv and argv[0] == "npx":
        rest = argv[1:]
        while rest and rest[0] in _NPX_FLAGS:
            rest = rest[1:]
        argv = ["npx", *rest]
    if argv and argv[0] == "git":
        # Output-only globals: `git --no-pager log` is `git log`.
        rest = argv[1:]
        while rest and rest[0] in ("--no-pager", "-P", "--no-optional-locks"):
            rest = rest[1:]
        argv = ["git", *rest]
    return argv


_GLOB_CHARS = frozenset("*?[")


def _scan_expansions(text: str) -> tuple[str, str]:
    """Classify the expansions of *text* and mark the parameter ones.

    :returns: ``(marked, kind)``. *kind* is ``""`` (no expansion), ``"param"``
        (only ``$NAME`` / ``${NAME}`` / ``${NAME:-literal}`` outside single
        quotes; in *marked* each such ``$`` is :data:`EXPANSION_MARK`) or
        ``"other"``: ANSI-C quoting (``$'..'``), a complex ``${..}``, a
        positional parameter, unquoted brace expansion (``--{ha,}rd``) or an
        unquoted glob in an option word (``--ha?d``), which turn a word the
        allowlist saw into another one. The special parameters ``$?``,
        ``$#``, ``$$`` and ``$!`` are numbers: not an expansion here.
    """
    out: list[str] = []
    kind = ""
    quote: str | None = None
    word_start = True
    option_word = False  # in the name part of a word starting with "-"
    brace = 0
    brace_list = False
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(text[i : i + 2])
            i += 2
            word_start = False
            continue
        if c == "$" and i + 1 < len(text) and not text[i + 1].isspace() and text[i + 1] != '"':
            nxt = text[i + 1]
            if nxt in _SPECIAL_PARAMS:
                out.append(text[i : i + 2])
                i += 2
                word_start = False
                continue
            status = _PIPESTATUS.match(text, i + 1)
            if status is not None:
                out.append(text[i : status.end()])
                i = status.end()
                word_start = False
                continue
            name = _PARAM_NAME.match(text, i + 1)
            braced = _BRACED_PARAM.match(text, i + 1) if nxt == "{" else None
            if name is None and braced is None:
                return text, "other"
            end = name.end() if name is not None else braced.end()  # type: ignore[union-attr]
            special = braced is not None and braced.group("name") in _SPECIAL_PARAMS
            out.append(("$" if special else EXPANSION_MARK) + text[i + 1 : end])
            kind = kind or ("" if special else "param")
            i = end
            word_start = False
            continue
        out.append(c)
        if quote == '"':
            if c == '"':
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            word_start = False
        elif c.isspace() or c in ";&|<>()":
            word_start, option_word, brace, brace_list = True, False, 0, False
        else:
            if word_start:
                option_word = c == "-"
                word_start = False
            if c == "=":
                option_word = False  # ``--include=*.ts``: only the value globs
            elif c == "{":
                brace += 1
            elif c == "}" and brace:
                if brace_list:
                    return text, "other"
                brace -= 1
            elif brace and (c == "," or text.startswith("..", i)):
                brace_list = True
            elif option_word and c in _GLOB_CHARS:
                return text, "other"
        i += 1
    return "".join(out), kind


def _has_expansion(text: str) -> bool:
    """Whether the shell would rewrite a word of *text* before running it.

    Parameter / ANSI-C expansion (``$X``, ``${X}``, ``$'..'``) outside single
    quotes, unquoted brace expansion (``--{ha,}rd``) and an unquoted glob in an
    option word (``--ha?d``) all turn a word the allowlist saw into another
    one (``CI=--output=f; git diff $CI``). ``$?``/``$#``/``$$``/``$!`` do not.
    """
    return _scan_expansions(text)[1] != ""


def parse_command(
    command: str, *, strict: bool = True, params: bool = False
) -> list[Segment] | None:
    """Split *command* into simple commands, or ``None`` when it cannot be analyzed.

    Command substitution, process substitution, backticks and heredocs make a
    command unanalyzable (their inner commands would run unseen), except the
    ``$(cat <<'EOF' ... EOF)`` literal-string idiom; so do parameter, brace
    and option-glob expansions (see :func:`_scan_expansions`) unless
    ``strict=False`` (the owned-paths check, which refuses any target holding
    a ``$`` itself). With ``params=True`` plain parameter expansions are
    parsed: each segment lists them in :attr:`Segment.expanded` and its
    ``argv`` / ``writes`` words carry :data:`EXPANSION_MARK` for their ``$``
    (the shell allowlist then judges them, see :func:`shell_allowlist`).
    Newlines separate commands like ``;``.
    """
    text = command.replace("\\\n", " ")
    text = _HEREDOC_STRING.sub("HEREDOC", text)
    if any(marker in text for marker in ("$(", "`", "<(", ">(")):
        return None
    if strict:
        marked, kind = _scan_expansions(text)
        if kind == "other" or (kind == "param" and not params):
            return None
        text = marked
    text = _FD_DUP.sub(r"\1", text)
    text = _FD_NUMBER.sub(r"\1", text)
    # ``cmd &`` / ``a |`` at a line end: shlex would glue the operator and the
    # newline into one ``&\n`` token.
    text = text.replace("\n", " \n ")
    tokens = _tokens(text)
    if tokens is None:
        return None
    segments: list[Segment] = []
    current = Segment()
    in_comment = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if in_comment:
            if token == "\n":
                in_comment = False
            i += 1
            continue
        if token in _SEPARATORS:
            if current.raw:
                segments.append(current)
            current = Segment()
            i += 1
            continue
        if _is_operator(token):
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if target is None or _is_operator(target):
                return None
            current.raw += [token, target]
            if token in _WRITE_REDIRECTS:
                current.writes.append(target)
                if token in _TRUNCATING_REDIRECTS:
                    current.truncates.append(target)
            elif token not in _READ_REDIRECTS:
                return None  # heredoc (<<, <<-) or an operator we do not model
            i += 2
            continue
        if token.startswith("#") and not current.raw:
            # A whole-line comment. A ``#`` word later in a command is kept as
            # an argument: after shlex a quoted "#x" looks the same, and
            # keeping it is the conservative reading.
            in_comment = True
            i += 1
            continue
        current.raw.append(token)
        if not current.argv and _ENV_ASSIGN.match(token):
            current.env.append(token.split("=", 1)[0])
        else:
            current.argv.append(token)
        i += 1
    if current.raw:
        segments.append(current)
    for seg in segments:
        seg.argv = _normalize_program(seg.argv)
        if any(EXPANSION_MARK in t for t in seg.raw):
            seg.expanded = [m for t in seg.raw for m in _MARKED_NAME.findall(t)]
            seg.raw = [t.replace(EXPANSION_MARK, "$") for t in seg.raw]
    return segments


def _shell_command(event: _Json) -> str | None:
    if event.get("type") != "tool_call":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("name") not in SHELL_TOOLS:
        return None
    args = data.get("arguments")
    command = args.get("command") if isinstance(args, dict) else None
    return command if isinstance(command, str) else None


# Write tools that replace a whole file (Edit / MultiEdit change part of it).
_OVERWRITE_TOOLS = frozenset({"Write", "write", "sys_os_write"})


def _tool_name(event: _Json) -> str | None:
    data = event.get("data")
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def _write_tool_paths(event: _Json) -> list[str] | None:
    if event.get("type") != "tool_call":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("name") not in WRITE_TOOLS:
        return None
    args = data.get("arguments")
    if not isinstance(args, dict):
        return []
    return [str(args[k]) for k in _PATH_KEYS if isinstance(args.get(k), str) and args.get(k)]


# ── Allowlist ────────────────────────────────────────────────────────────────

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "0.0.0.0"})
# curl options with no value that only change what is printed or how it connects.
_CURL_SHORT_FLAGS = frozenset("sSiIfLvkgG46Nq#")
_CURL_LONG_FLAGS = frozenset(
    {
        "--silent", "--show-error", "--include", "--head", "--fail", "--fail-with-body",
        "--location", "--verbose", "--insecure", "--globoff", "--compressed", "--http1.0",
        "--http1.1", "--http2", "--ipv4", "--ipv6", "--no-buffer", "--no-progress-meter",
        "--progress-bar", "--get", "--raw", "--no-keepalive", "--create-dirs", "--disable",
        "--no-sessionid", "--tr-encoding", "--path-as-is",
    }
)  # fmt: skip
# Short options that take a value (stuck ``-XPOST`` or the next word).
_CURL_SHORT_VALUE = frozenset("HdXAebFwmuroT")
_CURL_LONG_VALUE: dict[str, str] = {
    "--header": "H", "--data": "d", "--data-ascii": "d", "--data-binary": "d",
    "--data-raw": "raw", "--data-urlencode": "urlencode", "--json": "d", "--request": "X",
    "--user-agent": "A", "--referer": "e", "--cookie": "b", "--form": "F",
    "--form-string": "raw", "--write-out": "w", "--max-time": "m", "--connect-timeout": "m",
    "--retry": "m", "--retry-delay": "m", "--retry-max-time": "m", "--max-redirs": "m",
    "--expect100-timeout": "m", "--user": "u", "--range": "r", "--output": "o",
    "--upload-file": "T", "--url": "url",
}  # fmt: skip
_URL = re.compile(r"^(?:https?://)?(?P<host>\[[^\]]+\]|[^/:?#]+)(?::\d+)?(?:[/?#].*)?$")
_ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]*)?$")


def _local_file(path: str) -> bool:
    """A file curl may read (``-d @f``, ``-T f``): relative, inside the cwd, not a secret.

    ``.env*`` files are refused (their values would reach the model through
    the local server's echo); ``.env.example`` too, for simplicity.
    """
    norm = posixpath.normpath(path) if path else ""
    return bool(norm) and not (
        posixpath.isabs(norm)
        or norm == ".."
        or norm.startswith(("../", "~", "-"))
        or "$" in norm
        or _ENV_FILE.search(norm)
    )


def _curl_value_ok(kind: str, value: str) -> bool:
    """Whether the value of a curl option reads no file outside the worktree."""
    if kind in ("d", "H", "w"):  # ``@file`` reads a file (``-H @f``: curl >= 7.55)
        return not value.startswith("@") or _local_file(value[1:])
    if kind == "urlencode":  # ``name@file`` / ``@file`` read a file, ``name=x`` is literal
        eq, at = value.find("="), value.find("@")
        return at < 0 or (0 <= eq < at) or _local_file(value[at + 1 :])
    if kind == "F":  # ``name=@file`` / ``name=<file`` read a file
        _, _, content = value.partition("=")
        if content.startswith(("@", "<")):
            return _local_file(content[1:].split(";", 1)[0])
        return True
    if kind == "b":  # ``-b name=value`` is a cookie, ``-b file`` reads a cookie file
        return "=" in value
    if kind == "T":
        return _local_file(value)
    return True


def _local_url(arg: str) -> bool:
    match = _URL.match(arg)
    return match is not None and match.group("host").lower() in _LOCAL_HOSTS


def curl_output_targets(args: list[str]) -> list[str] | None:
    """The files a local ``curl`` writes (``-o FILE``), ``None`` when it is not modelled.

    Every URL must be a local host (``localhost``, ``127.0.0.1``, ``[::1]``,
    ``0.0.0.0``, any port, any method). Headers, inline data, ``-w``, ``-s``,
    ``-i`` ... are free; ``-d @file`` / ``-T file`` / ``-F f=@file`` only for a
    relative file inside the cwd that is no ``.env*``; ``-b`` only as
    ``name=value``. Refused: remote hosts, ``-K``/``--config``, ``-c`` (cookie
    jar), ``-D``/``--trace`` (files), ``-O`` (remote name), proxies,
    ``--unix-socket``, ``--resolve``/``--connect-to``, ``-n`` (.netrc) and any
    option not listed here.
    """
    targets: list[str] = []
    urls = 0
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            if arg in _CURL_LONG_FLAGS:
                i += 1
                continue
            kind = _CURL_LONG_VALUE.get(arg)
            if kind is None or i + 1 >= len(args):
                return None
            value = args[i + 1]
            i += 2
        elif arg.startswith("-") and len(arg) > 1:
            kind = value = None
            for j, c in enumerate(arg[1:], 1):
                if c in _CURL_SHORT_FLAGS:
                    continue
                if c not in _CURL_SHORT_VALUE:
                    return None
                kind = c
                value = arg[j + 1 :] or None
                break
            if kind is None:
                i += 1
                continue
            if value is None:
                if i + 1 >= len(args):
                    return None
                value = args[i + 1]
                i += 2
            else:
                i += 1
        else:
            if not _local_url(arg):
                return None
            urls += 1
            i += 1
            continue
        if kind == "url":
            if not _local_url(value):
                return None
            urls += 1
        elif kind == "o":
            targets.append(value)
        elif not _curl_value_ok(kind, value):
            return None
    return targets if urls else None


def _curl_localhost(args: list[str]) -> bool:
    """``curl`` whose every URL is a local host (see :func:`curl_output_targets`).

    Output files are write targets (:func:`shell_write_targets`): the
    owned-paths and test-writes guards judge them, ``/dev/null`` and ``/tmp``
    are always fine.
    """
    return curl_output_targets(args) is not None


_SED_ADDRESS = re.compile(r"(\d+|\$)(,(\d+|\$))?")
_SED_FLAGS = frozenset("gIiMm0123456789")


def _delimited(script: str, i: int, delim: str) -> int | None:
    """Index just past the next unescaped *delim* from *i*, or ``None``."""
    while i < len(script):
        if script[i] == "\\":
            i += 2
            continue
        if script[i] == delim:
            return i + 1
        i += 1
    return None


def _safe_sed_script(script: str) -> bool:
    """One simple substitution: ``[N[,M]]s<d>re<d>repl<d>[gIiMm0-9]``.

    Anything else is refused (and, for a role that may edit files, DENY with a
    hint to use the Edit tool, see :func:`shell_allowlist`): several commands
    (``;``, newlines, several ``-e``), regex addresses (``/re/d``,
    ``/re/,+1d``), ``w``/``e``/``r`` commands or flags (they write files or
    run commands), a newline in the script, and a replacement escape other
    than a back-reference (``\\1``), ``\\&``, the delimiter or a backslash
    (``\\n`` inserts a line).
    """
    text = script.strip(" \t")
    if "\n" in text:
        return False
    i = 0
    address = _SED_ADDRESS.match(text, i)
    if address:
        i = address.end()
    if not text.startswith("s", i) or i + 1 >= len(text):
        return False
    delim = text[i + 1]
    if delim in "\\;" or delim.isspace() or delim.isalnum():
        return False
    mid = _delimited(text, i + 2, delim)
    end = _delimited(text, mid, delim) if mid is not None else None
    if mid is None or end is None:
        return False
    replacement = text[mid : end - 1]
    j = 0
    while j < len(replacement):
        if replacement[j] == "\\":
            nxt = replacement[j + 1 : j + 2]
            if not (nxt.isdigit() or nxt in ("&", "\\", delim)):
                return False
            j += 2
            continue
        j += 1
    flags = text[end:].rstrip("; \t")
    return set(flags) <= _SED_FLAGS


def _sed_in_place(args: list[str]) -> bool:
    """``sed -i [-E] [-e SCRIPT] SCRIPT FILE...`` with one simple substitution.

    Exact flags only (``-i.bak``, ``-ni``, ``-f file``, ``--expression=`` ask),
    at least one file. :func:`shell_write_targets` reports the files as writes.
    """
    scripts: list[str] = []
    files: list[str] = []
    in_place = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-i", "--in-place"):
            in_place = True
        elif arg in ("-E", "-r", "--regexp-extended"):
            pass
        elif arg in ("-e", "--expression"):
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        elif arg == "--":
            files.extend(args[i + 1 :])
            break
        elif arg.startswith("-"):
            return False
        else:
            files.append(arg)
        i += 1
    if not scripts and files:
        scripts.append(files.pop(0))
    # One script only: several ``-e`` are several commands.
    return in_place and len(scripts) == 1 and bool(files) and _safe_sed_script(scripts[0])


# sed commands that only transform the stream: no r/R (read a file), w/W
# (write one), e (run a command), a/i/c (text arguments, not modelled).
_SED_FILTER_COMMANDS = frozenset("pdDnNPhHgGxlq=Qz")
_SED_FILTER_S_FLAGS = frozenset("gIiMmp0123456789")
_SED_FILTER_OPTIONS = frozenset(
    {"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-u", "--unbuffered",
     "-z", "--null-data", "--posix", "--sandbox", "--debug"}
)  # fmt: skip


def _sed_regex_end(script: str, i: int, delim: str) -> int | None:
    """Index just past the unescaped *delim* closing a regex that starts at *i*.

    A bracket expression holding the delimiter (``[/]``) is refused: sed
    implementations disagree on whether it ends the regex there.
    """
    while i < len(script):
        c = script[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            first = i + 1 + (script.startswith("^", i + 1))
            close = script.find("]", first + 1 if script.startswith("]", first) else first)
            if close < 0 or delim in script[i + 1 : close]:
                return None
            i = close + 1
            continue
        if c == "\n":
            return None
        if c == delim:
            return i + 1
        i += 1
    return None


def _sed_address(script: str, i: int) -> int | None:
    """Index past one sed address (``12``, ``$``, ``/re/[IM]``, ``\\%re%``) at *i*, or *i*."""
    if i < len(script) and script[i] == "$":
        return i + 1
    digits = re.match(r"\d+(~\d+)?", script[i:])
    if digits:
        return i + digits.end()
    if i < len(script) and script[i] in "/\\":
        delim, start = ("/", i + 1) if script[i] == "/" else (script[i + 1 : i + 2], i + 2)
        if not delim or delim in "\n\\":
            return None
        end = _sed_regex_end(script, start, delim)
        if end is None:
            return None
        while end < len(script) and script[end] in "IM":
            end += 1
        return end
    return i


def safe_sed_filter_script(script: str) -> bool:
    """A sed script that only rewrites its input stream.

    Commands: ``s`` (flags ``g I i M m p`` and a number; never ``w`` or
    ``e``), ``y``, ``p d D n N P h H g G x l = z q Q`` (``q``/``Q`` with an
    optional exit code), ``{ }`` blocks, ``!``, addresses and ranges. No
    ``r R w W e`` commands (they read or write files, or run a command) and
    no ``a i c`` text commands.
    """
    i, count, depth = 0, 0, 0
    n = len(script)
    while True:
        while i < n and script[i] in " \t\n;":
            i += 1
        if i >= n:
            return count > 0 and depth == 0
        if script[i] == "}":
            depth -= 1
            if depth < 0:
                return False
            i += 1
            continue
        end = _sed_address(script, i)
        if end is None:
            return False
        if end != i and end < n and script[end] == ",":
            second = _sed_address(script, end + 1)
            if second is None or second == end + 1:
                return False
            end = second
        i = end
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            return False
        cmd = script[i]
        if cmd == "{":
            depth += 1
            i += 1
            continue
        if cmd in "sy":
            delim = script[i + 1 : i + 2]
            if not delim or delim in "\\\n" or delim.isspace():
                return False
            mid = _sed_regex_end(script, i + 2, delim)
            end = _delimited(script, mid, delim) if mid is not None else None
            if end is None or "\n" in script[i:end]:
                return False
            i = end
            if cmd == "s":
                while i < n and script[i] in _SED_FILTER_S_FLAGS:
                    i += 1
        elif cmd in _SED_FILTER_COMMANDS:
            i += 1
            if cmd in "qQ":
                while i < n and script[i].isdigit():
                    i += 1
        else:
            return False
        count += 1
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] not in "\n;}":
            return False


def _sed_filter(args: list[str]) -> bool:
    """``sed [-n] [-E] [-e SCRIPT]... [SCRIPT]`` reading stdin only (a pipe filter).

    No file operand (a file is read by the other sed entries, never here), no
    ``-i``/``-f``/``-s``, and every script passes :func:`safe_sed_filter_script`.
    """
    scripts: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--expression"):
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        if arg in _SED_FILTER_OPTIONS or (
            len(arg) > 2 and arg[0] == "-" and arg[1] != "-" and set(arg[1:]) <= set("nEruz")
        ):
            i += 1
            continue
        if arg.startswith("-"):
            return False
        rest.append(arg)
        i += 1
    if not scripts and rest:
        scripts.append(rest.pop(0))
    return bool(scripts) and not rest and all(map(safe_sed_filter_script, scripts))


_PERL_DELIMS = frozenset("/|#!,:~%")
_PERL_FLAGS = frozenset("gimsx")
_PERL_CODE = re.compile(r"\(\?\??\{|\(\*\{")
# ``$`` in a Perl pattern: an anchor before a delimiter, ``)``, ``|`` or the end.
_PERL_VAR_IN_PATTERN = re.compile(r"\$(?![)|]|$)")
# ``$`` in a replacement: only ``$1`` / ``${1}`` / ``$&``.
_PERL_VAR_IN_REPLACEMENT = re.compile(r"\$(?!\d|\{\d+\}|&)")


def _safe_perl_substitution(script: str) -> bool:
    """One ``s<d>re<d>repl<d>[gimsx]`` with no code in it.

    Refused: the ``e`` flag, ``(?{..})`` / ``(??{..})`` code blocks, ``@``
    (``@{[ system .. ]}`` runs code in either part) and ``$`` interpolation
    other than a pattern anchor or a ``$1`` / ``$&`` back-reference.
    """
    text = script.strip()
    if len(text) < 4 or text[0] != "s" or text[1] not in _PERL_DELIMS:
        return False
    delim = text[1]
    mid = _delimited(text, 2, delim)
    end = _delimited(text, mid, delim) if mid is not None else None
    if mid is None or end is None:
        return False
    pattern, replacement = text[2 : mid - 1], text[mid : end - 1]
    flags = text[end:].rstrip().removesuffix(";").rstrip()
    if not set(flags) <= _PERL_FLAGS:
        return False
    if "@" in pattern or "@" in replacement or _PERL_CODE.search(pattern):
        return False
    stripped = pattern.replace("\\\\", "").replace("\\$", "")
    if _PERL_VAR_IN_PATTERN.search(stripped):
        return False
    return not _PERL_VAR_IN_REPLACEMENT.search(replacement.replace("\\\\", "").replace("\\$", ""))


def _perl_in_place_parts(args: list[str]) -> tuple[str, list[str]] | None:
    """``(script, files)`` of ``perl -pi -e SCRIPT FILE...`` (or ``-p -i -e``, ``-i -pe``)."""
    flags: set[str] = set()
    script: str | None = None
    files: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-p", "-i", "-pi", "-ip"):
            flags |= set(arg[1:])
        elif arg in ("-e", "-pe"):
            if script is not None or i + 1 >= len(args):
                return None
            flags |= set(arg[1:])
            script = args[i + 1]
            i += 2
            continue
        elif arg.startswith("-"):
            return None  # -pie (backup suffix "e"), -n, -M, -0 ...: not modelled
        else:
            files.append(arg)
        i += 1
    if script is None or not {"p", "i"} <= flags:
        return None
    return script, files


def _perl_in_place(args: list[str]) -> bool:
    """``perl -pi -e 's/a/b/g' FILE...``: one code-free substitution, in place."""
    parts = _perl_in_place_parts(args)
    return parts is not None and bool(parts[1]) and _safe_perl_substitution(parts[0])


IN_PLACE_EDIT_HINT = (
    "Edit files with the Edit tool (it only needs the file to be in your owned paths); "
    "sed -i is only for one simple s/// substitution."
)


def _in_place_editor(argv: list[str]) -> bool:
    """Whether *argv* is a ``sed`` / ``perl`` run that rewrites files in place.

    ``sed -i`` / ``-i.bak`` / ``-ni`` / ``--in-place[=SUF]``, ``perl -pi`` /
    ``-i`` / ``-pi.bak`` (an ``-i`` in an option cluster before ``--``).
    """
    if not argv or argv[0] not in ("sed", "perl"):
        return False
    for arg in argv[1:]:
        if arg == "--":
            return False
        if argv[0] == "sed" and arg.startswith("--in-place"):
            return True
        if re.match(r"^-[A-Za-z0-9]*i", arg) and not arg.startswith("--"):
            return True
    return False


_BUILTIN_MATCHERS: dict[str, Callable[[list[str]], bool]] = {
    "curl_localhost": _curl_localhost,
    "sed_in_place": _sed_in_place,
    "sed_filter": _sed_filter,
    "perl_in_place": _perl_in_place,
}


def _banned_hit(arg: str, banned: str) -> bool:
    """Whether *arg* is the refused option *banned* in any spelling getopt accepts.

    ``fnmatch`` first; then, for a long option (``--hard``, ``--upload-pack*``),
    any unambiguous-prefix abbreviation (``--har``, ``--upload-p=cmd``: git and
    GNU tools expand them); and for a one-letter option (``-x``), a cluster or
    a stuck value containing it (``-vx``, ``-xcmd``; ``-o*`` likewise refuses
    ``-uo``).
    """
    if fnmatch.fnmatchcase(arg, banned):
        return True
    if banned.startswith("--"):
        literal = re.split(r"[*?\[=]", banned, maxsplit=1)[0]
        name = arg.split("=", 1)[0]
        return name.startswith("--") and len(name) > 3 and literal.startswith(name)
    if re.fullmatch(r"-[A-Za-z0-9]\*?", banned):
        return (
            arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and banned[1] in arg
        )
    return False


@dataclass(frozen=True)
class _Pattern:
    source: str
    words: tuple[str, ...] = ()
    banned: tuple[str, ...] = ()
    exact: bool = False
    regex: re.Pattern[str] | None = None
    builtin: str | None = None

    def matches(self, argv: list[str]) -> bool:
        if not argv:
            return False
        if self.builtin is not None:
            head, _, name = self.builtin.partition(":")
            return argv[0] == head and _BUILTIN_MATCHERS[name](argv[1:])
        if self.regex is not None:
            return self.regex.match(" ".join(argv)) is not None
        if len(argv) < len(self.words):
            return False
        if not all(fnmatch.fnmatchcase(a, w) for a, w in zip(argv, self.words, strict=False)):
            return False
        rest = argv[len(self.words) :]
        if self.exact and rest:
            return False
        return not any(_banned_hit(a, b) for a in rest for b in self.banned)

    @property
    def expansion_safe(self) -> bool:
        """Whether an expanded word in its free arguments cannot make it write or run code.

        Only plain word entries: no regex, no built-in matcher, no ``!banned``
        option (an expanded word could become that option, ``git diff $X``
        with ``X=--output=f``), no glob word, and not a program in
        :data:`_EXPANSION_UNSAFE_PROGRAMS`.
        """
        return (
            self.regex is None
            and self.builtin is None
            and not self.banned
            and not any(_GLOB_CHARS.intersection(w) for w in self.words)
            and self.words[0] not in _EXPANSION_UNSAFE_PROGRAMS
        )


def compile_pattern(source: str) -> _Pattern:
    """Compile one allowlist entry.

    * ``"git diff !--output*"`` -- the command starts with the words
      ``git diff`` (each an ``fnmatch`` glob, ``*`` = any one word); the
      remaining arguments are free except those matching a ``!glob``
      (abbreviated long options and short-option clusters included, see
      :func:`_banned_hit`).
      A final ``$`` word forbids any remaining argument.
    * ``"re:<regex>"`` -- a Python regex matched at the start of the
      normalized command (words joined by one space).
    * ``"@curl:curl_localhost"`` -- a built-in matcher for the program
      (``curl_localhost``, ``sed_in_place``, ``sed_filter``, ``perl_in_place``).
    """
    text = source.strip()
    if text.startswith("re:"):
        return _Pattern(source=source, regex=re.compile(text[3:]))
    if text.startswith("@"):
        head, sep, name = text[1:].partition(":")
        if not sep or name not in _BUILTIN_MATCHERS:
            raise ValueError(f"unknown built-in allowlist matcher: {source!r}")
        return _Pattern(source=source, builtin=f"{head}:{name}")
    words: list[str] = []
    banned: list[str] = []
    exact = False
    for word in text.split():
        if word == "$":
            exact = True
        elif word.startswith("!"):
            banned.append(word[1:])
        else:
            words.append(word)
    if not words:
        raise ValueError(f"empty allowlist entry: {source!r}")
    return _Pattern(source=source, words=tuple(words), banned=tuple(banned), exact=exact)


# A vetted variable's value, for matching a command that expands it: the
# ``${NAME:-literal}`` default when there is one, else a typical value.
_EXPANSION_PLACEHOLDER: dict[str, str] = {
    "PORT": "3000",
    "CHROMIUM_PATH": "/usr/bin/chromium",
    "HOME": "/home/user",
    "PWD": "/work",
    "OLDPWD": "/work",
    "TMPDIR": "/tmp",
    "RUNNER_TEMP": "/tmp",
}
_MARKED_PARAM = re.compile(
    EXPANSION_MARK
    + r"(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[\w./:@%+,=-]*))?\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
# Commands that write files or git state: an expanded word there may be a path
# or an option the allowlist never saw, whatever the variable.
_EXPANSION_WRITERS = frozenset(
    {"git", "mkdir", "touch", "cp", "mv", "rm", "rmdir", "chmod", "chown", "ln", "tee",
     "truncate", "unlink", "install", "rsync", "tar", "unzip", "dd"}
) | _EXPANSION_UNSAFE_PROGRAMS  # fmt: skip


def _substitute_expansions(word: str) -> str:
    """*word* with each marked expansion replaced by its default / placeholder value."""
    return _MARKED_PARAM.sub(
        lambda m: (
            m["default"]
            if m["default"] is not None
            else _EXPANSION_PLACEHOLDER.get(m["braced"] or m["bare"], "x")
        ),
        word,
    )


def _expansion_smuggles(argv: list[str]) -> bool:
    """Whether an expansion in *argv* could change which program or option runs.

    True for an expansion in the program word (``$X test``, ``npx $X``), in
    an option name (``--$X``, ``-$X``; a value after ``=`` is fine), or
    anywhere in a command that writes files or git state
    (:data:`_EXPANSION_WRITERS`).
    """
    if not argv:
        return False
    head = argv[1:2] if argv[0] == "npx" else argv[:1]
    if any(EXPANSION_MARK in w for w in (argv[0], *head)):
        return True
    program = head[0] if head else argv[0]
    if program in _EXPANSION_WRITERS and any(EXPANSION_MARK in w for w in argv):
        return True
    return any(w.startswith("-") and EXPANSION_MARK in w.split("=", 1)[0] for w in argv)


def _scratch_target(target: str) -> bool:
    norm = posixpath.normpath(target)
    return norm in ("/dev/null", "/dev/stdout", "/dev/stderr") or norm.startswith("/tmp/")


def shell_allowlist(
    *,
    allow: Sequence[str],
    role: str = "this role",
    env_allow: Sequence[str] | None = None,
    shell_writes: bool = True,
    reason: str | None = None,
    read_only: Sequence[str] | None = None,
    expand_allow: Sequence[str] | None = None,
) -> _Evaluator:
    """Factory: ALLOW a shell command only when every part is allowlisted, else ASK.

    Chains (``;``, ``&&``, ``||``, newlines, a background ``&``) and pipes pass
    when every simple command does. Variables:

    * ``$?`` / ``$#`` / ``$$`` / ``$!`` are numbers: always fine.
    * ``${PIPESTATUS[n]}`` is a number too.
    * ``$NAME`` / ``${NAME}`` / ``${NAME:-literal}`` in a command word:
      ``NAME`` must be an *env_allow* / *expand_allow* name or assigned
      earlier in the command. In an expansion-safe *read_only* command (see
      :attr:`_Pattern.expansion_safe`) that is enough. In any other
      allowlisted command (``npx next start -p ${PORT:-3000}``, ``curl
      localhost:$PORT/x``, ``pkill -f "next start -p $PORT"``) the name must
      also not be assigned in the command (its value then comes from the
      session's environment), the expansion must be a plain argument (not the
      program, not an option name) of a command that writes no file or git
      state (:func:`_expansion_smuggles`), and the command must match with
      the ``:-`` default (or a typical value) in place of the expansion. In
      the value of a vetted env prefix (``PORT=${PORT:-3000} npx playwright
      test``) only the name rule applies. A write target with an expansion
      asks.
    * A bare assignment (``S=/tmp/x;``) of a name that is not vetted makes
      every later command of the chain read-only-only; names that change
      what runs (``PATH``, ``LD_*``, ``GIT_*``, ``NODE_*`` ...) ask.

    :param allow: Allowlist entries, see :func:`compile_pattern`.
    :param role: Role name used in the approval-card text.
    :param env_allow: Env names a command may be prefixed with
        (default :data:`DEFAULT_ENV_ALLOW`).
    :param shell_writes: ``False`` for roles that write no file from the
        shell (reviewer, planner, qa, devops): a command that writes a file
        (redirection, ``cp`` / ``mv`` / ``rm``, formatter ``--write``, a
        dependency change, see :func:`shell_write_targets`) other than
        ``/dev/null`` or ``/tmp/**`` is ASK. ``True`` leaves write targets to
        :func:`owned_paths`.
    :param reason: Optional text appended to the approval-card reason.
    :param read_only: Entries of commands that only read (default
        :data:`DEFAULT_READ_ONLY`); the expansion-safe ones may carry a
        variable expansion. A command must match *allow* too.
    :param expand_allow: Extra names a command may expand (default
        :data:`DEFAULT_EXPAND_ALLOW`), on top of *env_allow*.
    :returns: An evaluator ``fn(event, config)``; non-shell tool calls ALLOW.
    """
    patterns = [compile_pattern(entry) for entry in allow]
    safe = [
        p
        for p in (
            compile_pattern(e) for e in (DEFAULT_READ_ONLY if read_only is None else read_only)
        )
        if p.expansion_safe
    ]
    envs = frozenset(DEFAULT_ENV_ALLOW if env_allow is None else env_allow)
    expandable = envs | frozenset(DEFAULT_EXPAND_ALLOW if expand_allow is None else expand_allow)
    suffix = f" {reason}" if reason else ""

    # A role that may edit files in place: any other ``sed -i`` / ``perl -pi``
    # is refused with a hint (the agent picked the wrong tool; no human needed).
    edits_in_place = any(p.builtin in ("sed:sed_in_place", "perl:perl_in_place") for p in patterns)

    def _ask(what: str) -> _Json:
        return {
            "result": "ASK",
            "reason": f"{what} is not on the {role} shell allowlist.{suffix}",
        }

    def _refuse(seg: Segment) -> _Json:
        if edits_in_place and _in_place_editor(seg.argv):
            return {"result": "DENY", "reason": f"`{seg.text}` refused. {IN_PLACE_EDIT_HINT}"}
        return _ask(f"`{seg.text}`")

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        segments = parse_command(command, params=True)
        if segments is None:
            return _ask(
                "A command with substitutions, heredocs, complex expansions or unbalanced quotes"
            )
        assigned = {n for s in segments if not s.argv and not s.writes for n in s.env}
        read_only_after = False  # a bare assignment of an unvetted name happened
        for seg in segments:
            unknown = sorted({n for n in seg.expanded if n not in expandable | assigned})
            if unknown and edits_in_place and _in_place_editor(seg.argv):
                return _refuse(seg)
            if unknown:
                return _ask(f"`{seg.text}` (expands {', '.join('$' + n for n in unknown)})")
            if not seg.argv and seg.env and not seg.writes:
                danger = [n for n in seg.env if _DANGEROUS_VAR.match(n)]
                if danger:
                    return _ask(f"`{seg.text}` (sets {', '.join(danger)})")
                read_only_after = read_only_after or any(n not in envs for n in seg.env)
                continue
            bad_env = [name for name in seg.env if name not in envs]
            if bad_env:
                return _ask(f"`{seg.text}` (env {', '.join(bad_env)})")
            if any(EXPANSION_MARK in t for t in seg.writes):
                return _ask(f"`{seg.text}` (writes to an expanded path)")
            if not shell_writes and not all(
                _scratch_target(t) for t in [*seg.writes, *shell_write_targets(seg.argv)]
            ):
                return _ask(f"`{seg.text}` (writes a file)")
            if not seg.argv:
                if seg.writes:
                    return _ask(f"`{seg.text}`")
                continue
            argv_expanded = any(EXPANSION_MARK in a for a in seg.argv)
            if not argv_expanded:
                if not any(p.matches(seg.argv) for p in patterns):
                    return _refuse(seg)
                if read_only_after and not any(p.matches(seg.argv) for p in safe):
                    return _ask(
                        f"`{seg.text}` (runs after a shell variable; only read-only commands may)"
                    )
                continue
            if any(p.matches(seg.argv) for p in safe) and any(
                p.matches(seg.argv) for p in patterns
            ):
                continue  # an expansion-safe read-only command: any vetted or assigned name
            # Elsewhere only a vetted name the command does not assign (its value
            # comes from the session's environment, or the ``:-`` default), as
            # a plain argument, and never in a file / git writer.
            untrusted = sorted({n for n in seg.expanded if n in assigned})
            if edits_in_place and _in_place_editor(seg.argv):
                return _refuse(seg)  # a variable in an in-place edit: use the Edit tool
            if read_only_after or untrusted or _expansion_smuggles(seg.argv):
                return _ask(f"`{seg.text}` (expands a variable; only read-only commands may)")
            if not any(p.matches([_substitute_expansions(a) for a in seg.argv]) for p in patterns):
                return _ask(f"`{seg.text}`")
        return _ALLOW

    return _evaluate


# ── Owned paths ──────────────────────────────────────────────────────────────


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 1 :]:
            end = pattern.index("]", i + 1)
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


_WILDCARD = re.compile(r"[*?\[]")


def _norm_glob(pattern: str) -> str:
    p = pattern.strip()
    while p.startswith("./"):
        p = p[2:]
    if p.endswith("/"):
        p += "**"
    return p


class _GlobSet:
    """Owned / free / shared globs, with directory-subtree semantics.

    A literal entry (``app/cart``) matches itself and everything below it; an
    entry ending in ``/**`` also matches its directory itself.
    """

    def __init__(self, patterns: Iterable[str]):
        self.patterns = [_norm_glob(p) for p in patterns if p and p.strip()]
        self._compiled: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
            (p, _glob_regex(p), _glob_regex(p[:-3]) if p.endswith("/**") else None)
            for p in self.patterns
        ]

    def match(self, path: str) -> bool:
        for pattern, regex, base in self._compiled:
            if regex.match(path) or (base is not None and base.match(path)):
                return True
            if not _WILDCARD.search(pattern) and (
                path == pattern or path.startswith(pattern + "/")
            ):
                return True
        return False

    def covers_tree(self, directory: str) -> bool:
        """Whether every path under *directory* matches (for wildcard targets)."""
        parts = directory.split("/") if directory else []
        ancestors = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if directory.startswith("/"):
            ancestors = [a or "/" for a in ancestors]
        for pattern, _regex, base in self._compiled:
            if pattern == "**":
                return True
            if base is not None:
                if any(base.match(a) for a in ancestors):
                    return True
            elif not _WILDCARD.search(pattern) and (
                directory == pattern or directory.startswith(pattern + "/")
            ):
                return True
        return False


_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "cp": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "mv": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "ln": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "touch": frozenset({"-d", "--date", "-t", "-r", "--reference"}),
    "mkdir": frozenset({"-m", "--mode"}),
    "truncate": frozenset({"-s", "--size", "-r", "--reference"}),
    "sed": frozenset({"-e", "--expression", "-f", "--file", "-l", "--line-length"}),
    "prettier": frozenset({"--config", "--ignore-path", "--plugin", "--parser", "--log-level"}),
    "eslint": frozenset({"-c", "--config", "--ext", "-f", "--format", "--rule", "--parser",
                         "--resolve-plugins-relative-to", "--rulesdir", "--max-warnings"}),
    "ruff": frozenset({"--config", "--select", "--ignore", "--extend-select", "--target-version",
                       "--line-length"}),
    "restore": frozenset({"-s", "--source"}),
}  # fmt: skip


def _positionals(args: list[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.extend(args[i + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            i += 2 if arg in value_flags else 1
            continue
        out.append(arg)
        i += 1
    return out


def _flag_value(args: list[str], names: Iterable[str]) -> str | None:
    names = tuple(names)
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


_DEP_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"install", "i", "add", "uninstall", "un", "remove", "rm", "r", "update",
                      "up", "upgrade", "dedupe", "prune"}),
    "pnpm": frozenset({"add", "install", "i", "remove", "rm", "uninstall", "update", "up",
                       "upgrade", "dedupe"}),
    "yarn": frozenset({"add", "remove", "install", "upgrade", "up", "dedupe"}),
    "bun": frozenset({"add", "install", "i", "remove", "rm", "update"}),
    "uv": frozenset({"add", "remove", "lock"}),
}  # fmt: skip
_FROZEN_FLAGS = frozenset({"--frozen-lockfile", "--immutable", "--frozen", "--locked"})
_MANIFESTS = {
    "npm": ("package.json", "package-lock.json"),
    "pnpm": ("package.json", "pnpm-lock.yaml"),
    "yarn": ("package.json", "yarn.lock"),
    "bun": ("package.json", "bun.lock"),
    "uv": ("pyproject.toml", "uv.lock"),
}


# Global package-manager options that take a value (``npm -w web install x``).
_PM_GLOBAL_VALUE_FLAGS = frozenset(
    {"-w", "--workspace", "-F", "--filter", "-C", "--dir", "--prefix", "--cwd"}
)


def _pm_subcommand(args: list[str]) -> str | None:
    """The subcommand of a package-manager *args*, past its leading options.

    ``pnpm -w add x`` -> ``add``; ``npm -w web install x`` -> ``install``
    (``-w`` takes a value for npm; either reading finds the dependency command).
    """
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return None
    first = args.index(words[0])
    if first > 0 and args[first - 1] in _PM_GLOBAL_VALUE_FLAGS and len(words) > 1:
        return words[0] if words[0] in _PM_DEP_SUBCOMMANDS else words[1]
    return words[0]


def shell_write_targets(argv: list[str]) -> list[str]:
    """Paths a simple command writes, as written (relative or absolute).

    Only the commands the shipcrew allowlists admit are modelled; any other
    command returns ``[]`` and is left to :func:`shell_allowlist`.
    """
    if not argv:
        return []
    prog, args = argv[0], argv[1:]
    if prog == "npx" and args:
        prog, args = args[0], args[1:]
    if prog in ("rm", "rmdir", "touch", "mkdir", "truncate", "tee", "chmod", "chown"):
        targets = _positionals(args, _VALUE_FLAGS.get(prog, frozenset()))
        return targets[1:] if prog in ("chmod", "chown") else targets
    if prog in ("cp", "ln"):
        target_dir = _flag_value(args, ("-t", "--target-directory"))
        if target_dir is not None:
            return [target_dir]
        pos = _positionals(args, _VALUE_FLAGS[prog])
        if prog == "ln" and len(pos) == 1:
            return [posixpath.basename(pos[0].rstrip("/"))]  # link created in the cwd
        return pos[-1:] if len(pos) >= 2 else []
    if prog == "mv":
        target_dir = _flag_value(args, ("-t", "--target-directory"))
        pos = _positionals(args, _VALUE_FLAGS["mv"])
        return [*pos, target_dir] if target_dir is not None else pos
    if prog == "sed" and any(
        a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--") and "i" in a)
        for a in args
    ):
        # -i, -i.bak, -ni (clustered), --in-place[=SUF].
        pos = _positionals(args, _VALUE_FLAGS["sed"])
        has_script_flag = any(
            a in ("-e", "--expression", "-f", "--file")
            or a.startswith(("--expression=", "--file="))
            or (a.startswith(("-e", "-f")) and len(a) > 2)
            for a in args
        )
        return pos if has_script_flag else pos[1:]
    if prog == "perl" and any(
        a.startswith("-") and not a.startswith("--") and "i" in a for a in args
    ):
        parts = _perl_in_place_parts(args)
        if parts is not None:
            return parts[1]
        # Not the modelled form: every positional may be a file it rewrites.
        return _positionals(args, frozenset({"-e", "-E", "-M", "-I"}))
    if prog == "curl":
        modelled = curl_output_targets(args)
        if modelled is not None:
            return modelled
        # Not the modelled form (e.g. ``localhost:$PORT`` as the guards see it
        # unexpanded): every ``-o`` value, conservatively.
        found: list[str] = []
        for i, arg in enumerate(args):
            if arg in ("-o", "--output") and i + 1 < len(args):
                found.append(args[i + 1])
            elif arg.startswith("-") and not arg.startswith("--") and "o" in arg[1:]:
                rest = arg[arg.index("o", 1) + 1 :]
                found.append(rest or (args[i + 1] if i + 1 < len(args) else ""))
        return [t for t in found if t]
    if prog == "uniq":
        pos = _positionals(args, frozenset({"-f", "--skip-fields", "-s", "--skip-chars", "-w",
                                            "--check-chars"}))  # fmt: skip
        return pos[1:2]  # ``uniq IN OUT`` writes OUT
    if prog == "git" and args:
        sub, rest = args[0], args[1:]
        if sub in ("mv", "rm"):
            return _positionals(rest)
        if sub == "restore":
            return _positionals(rest, _VALUE_FLAGS["restore"])
        if sub == "checkout":
            if "--" in rest:
                return rest[rest.index("--") + 1 :]
            # ``git checkout <tree-ish> <path>...`` rewrites those paths; a lone
            # word may be a path or a branch (a switch rewrites the worktree):
            # either way it is judged as a path.
            pos = _positionals(rest, frozenset({"-b", "-B", "--orphan", "--conflict"}))
            return pos[1:] if len(pos) > 1 else pos
        return []
    if prog == "prettier" and any(a in ("--write", "-w") for a in args):
        return _positionals(args, _VALUE_FLAGS["prettier"]) or ["."]
    if prog == "eslint":
        out = _flag_value(args, ("-o", "--output-file"))
        targets = [out] if out else []
        if any(a.startswith("--fix") for a in args):
            flags = _VALUE_FLAGS["eslint"] | {"-o", "--output-file"}
            targets += _positionals(args, flags) or ["."]
        return targets
    if prog == "ruff" and args:
        if args[0] == "format" or (args[0] == "check" and "--fix" in args):
            return _positionals(args[1:], _VALUE_FLAGS["ruff"]) or ["."]
        return []
    if prog == "black":
        return _positionals(args) or ["."]
    if prog in _DEP_SUBCOMMANDS and _pm_subcommand(args) in _DEP_SUBCOMMANDS[prog]:
        if _FROZEN_FLAGS.intersection(args):
            return []
        return list(_MANIFESTS[prog])
    return []


def shell_removal_targets(argv: list[str]) -> list[str]:
    """Paths a simple command deletes, renames away or truncates.

    ``rm`` / ``rmdir`` / ``unlink`` / ``truncate`` targets, ``git rm`` paths,
    and the sources of ``mv`` / ``git mv`` (a subset of
    :func:`shell_write_targets`, for the verify roles' add-only rule).
    """
    if not argv:
        return []
    prog, args = argv[0], argv[1:]
    if prog in ("rm", "rmdir", "unlink", "truncate"):
        return _positionals(args, _VALUE_FLAGS.get(prog, frozenset()))
    if prog == "mv":
        pos = _positionals(args, _VALUE_FLAGS["mv"])
        return pos if _flag_value(args, ("-t", "--target-directory")) else pos[:-1]
    if prog == "git" and len(args) > 1 and args[0] in ("rm", "mv"):
        pos = _positionals(args[1:])
        return pos if args[0] == "rm" else pos[:-1]
    return []


# Owning a manifest by name owns its lockfiles and the package-manager files
# next to it: a dependency change or a toolchain setup rewrites them together
# (``pnpm add`` can touch ``pnpm-workspace.yaml``, pnpm 10 writes
# ``onlyBuiltDependencies`` there).
_LOCKFILES_OF: dict[str, tuple[str, ...]] = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
                     "bun.lock", "bun.lockb", "pnpm-workspace.yaml", ".npmrc", ".nvmrc",
                     ".node-version"),
    "pyproject.toml": ("uv.lock", "poetry.lock"),
}  # fmt: skip


def _with_lockfiles(owned_list: Sequence[str]) -> list[str]:
    """*owned_list* plus the lockfiles and package-manager files next to every
    manifest it owns by exact name (see :data:`_LOCKFILES_OF`)."""
    out = list(owned_list)
    for entry in owned_list:
        norm = _norm_glob(entry)
        if _WILDCARD.search(norm):
            continue
        directory, _, name = norm.rpartition("/")
        for lock in _LOCKFILES_OF.get(name, ()):
            path = f"{directory}/{lock}" if directory else lock
            if path not in out:
                out.append(path)
    return out


# Marks a :func:`owned_paths` finding as "another task's file" (DENY).
_OTHER_TASK = "\x00other-task:"
OTHER_TASK_HINT = (
    "Do not edit it; work against the shared contract (e.g. lib/api-client.ts, lib/db.ts) "
    "and mock it in your tests; if the contract lacks something, say so in your final reply."
)


def owned_paths(
    *,
    owned_paths: Sequence[str] | None = None,
    root: str | None = None,
    shared_paths: Sequence[str] | None = None,
    free_paths: Sequence[str] | None = None,
    extra_free_paths: Sequence[str] | None = None,
    reason: str | None = None,
    other_tasks: Sequence[Any] | None = None,
) -> _Evaluator:
    """Factory: ASK when a write leaves the task's owned paths.

    A write to a file another in-progress task of the mission owns (see
    *other_tasks*) is DENY with a hint instead: the agent crossed into
    another task's files and corrects itself, no human needed.

    :param owned_paths: The task's owned globs, relative to *root*
        (``["app/cart/**", "e2e/cart.spec.ts"]``). ``None`` or empty: no task
        contract was injected and the policy abstains (ALLOW).
    :param root: Absolute path of the task worktree; relative tool paths
        resolve against it. Writes outside it (other than free paths) ASK.
    :param shared_paths: Contract files that ASK unless owned by exact name
        (default :data:`DEFAULT_SHARED_PATHS`).
    :param free_paths: Always-writable build output / caches / scratch
        (default :data:`DEFAULT_FREE_PATHS`); absolute entries start with ``/``.
    :param extra_free_paths: Added to *free_paths*, e.g. a role's report file
        (``.shipcrew/qa.json``).
    :param reason: Optional text appended to the approval-card reason.
    :param other_tasks: The mission's other active tasks at session start,
        ``[{"title": ..., "owned_paths": [...]}]`` (injected by the server,
        see ``omnigent.shipcrew.sessions.inject_task_contract``).
    :returns: An evaluator ``fn(event, config)``; reads always ALLOW.
    """
    owned_list = [p for p in (owned_paths or []) if isinstance(p, str) and p.strip()]
    if not owned_list:

        def _abstain(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
            return _ALLOW

        return _abstain

    shown_list = owned_list
    owned_list = _with_lockfiles(owned_list)
    owned = _GlobSet(owned_list)
    exact_owned = {_norm_glob(p) for p in owned_list if not _WILDCARD.search(_norm_glob(p))}
    shared = _GlobSet(DEFAULT_SHARED_PATHS if shared_paths is None else shared_paths)
    free_all = [
        *(DEFAULT_FREE_PATHS if free_paths is None else free_paths),
        *(extra_free_paths or ()),
    ]
    free_rel = _GlobSet(p for p in free_all if not p.startswith("/"))
    free_abs = _GlobSet(p for p in free_all if p.startswith("/"))
    root_abs = posixpath.normpath(root) if root else None
    owned_text = ", ".join(shown_list)
    suffix = f" {reason}" if reason else ""
    shared_names = [
        p.rsplit("/", 1)[-1] for p in shared.patterns if not _WILDCARD.search(p.rsplit("/", 1)[-1])
    ]
    others: list[tuple[str, _GlobSet]] = []
    for other in other_tasks or ():
        if not isinstance(other, dict):
            continue
        globs = [g for g in other.get("owned_paths") or [] if isinstance(g, str) and g.strip()]
        if globs:
            others.append((str(other.get("title") or "another task"), _GlobSet(globs)))

    def _other_owner(rel: str) -> str | None:
        """Title of the other active task that owns *rel*, if any."""
        return next((title for title, globs in others if globs.match(rel)), None)

    def _check(target: str, cwd: str | None) -> str | None:
        """Why writing *target* needs approval, or ``None`` when it is fine."""
        if "$" in target or target.startswith("~"):
            return f"`{target}` cannot be resolved to a path in the worktree"
        if posixpath.isabs(target):
            absolute = posixpath.normpath(target)
        elif cwd is not None:
            absolute = posixpath.normpath(posixpath.join(cwd, target))
        else:
            return f"`{target}` is relative to an unknown directory"
        if root_abs is None or (
            absolute != root_abs and not absolute.startswith(root_abs.rstrip("/") + "/")
        ):
            # Outside the worktree only the absolute free paths (/tmp, /dev/null)
            # are writable. Checked after the worktree test: a worktree may
            # itself live under /tmp.
            if free_abs.match(absolute) or (
                _WILDCARD.search(absolute) and free_abs.covers_tree(posixpath.dirname(absolute))
            ):
                return None
            if root_abs is None:
                return f"`{target}` cannot be checked: the task worktree is unknown"
            return f"`{target}` is outside the task worktree"
        rel = posixpath.relpath(absolute, root_abs)
        rel = "" if rel == "." else rel
        if _WILDCARD.search(rel):
            literal = rel[: _WILDCARD.search(rel).start()]  # type: ignore[union-attr]
            directory = literal.rsplit("/", 1)[0] if "/" in literal else ""
            if free_rel.covers_tree(directory):
                return None
            glob = _glob_regex(rel)
            probes = [f"{directory}/{n}" if directory else n for n in shared_names]
            if any(glob.match(p) for p in probes):
                return f"`{rel}` may match a shared contract file"
            if directory and owned.covers_tree(directory):
                return None
            return f"`{rel}` is outside this task's owned paths ({owned_text})"
        if free_rel.match(rel):
            return None
        is_shared = shared.match(rel) and rel not in exact_owned
        if rel and not is_shared and owned.match(rel):
            return None
        owner = _other_owner(rel) if rel else None
        if owner is not None:
            return _OTHER_TASK + f"`{rel}` belongs to task '{owner}' (in progress)"
        if is_shared:
            return f"`{rel}` is a shared contract file (own it by name to change it)"
        return f"`{rel or '.'}` is outside this task's owned paths ({owned_text})"

    def _ask(why: str) -> _Json:
        if why.startswith(_OTHER_TASK):
            return {
                "result": "DENY",
                "reason": f"{why.removeprefix(_OTHER_TASK)}. {OTHER_TASK_HINT}",
            }
        return {"result": "ASK", "reason": f"Write needs approval: {why}.{suffix}"}

    def _verdict(found: list[str | None]) -> _Json:
        """Another task's file first (DENY), else the first approval (ASK)."""
        problems = [w for w in found if w]
        if not problems:
            return _ALLOW
        return _ask(next((w for w in problems if w.startswith(_OTHER_TASK)), problems[0]))

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        paths = _write_tool_paths(event)
        if paths is not None:
            return _verdict([_check(path, root_abs) for path in paths])
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        targets = _shell_targets(command, root_abs)
        if targets is None:
            return _ALLOW  # unanalyzable: shell_allowlist asks for it
        return _verdict([_check(target, seg_cwd) for target, seg_cwd in targets])

    return _evaluate


def _shell_targets(
    command: str, root: str | None, *, removals: bool = False
) -> list[tuple[str, str | None]] | None:
    """``(target, cwd)`` for every file a shell command writes; ``None`` if unanalyzable.

    With ``removals=True`` only the files it deletes, renames away or
    truncates (:func:`shell_removal_targets` and ``>`` redirections).
    ``cd`` and ``git -C`` are tracked; *cwd* is ``None`` once it is unknown.
    """
    segments = parse_command(command, strict=False)
    if segments is None:
        return None
    out: list[tuple[str, str | None]] = []
    cwd = root
    for seg in segments:
        argv = seg.argv
        if argv[:1] == ["cd"]:
            dest = _positionals(argv[1:])
            if not dest or dest[0] == "-" or "$" in dest[0] or dest[0].startswith("~"):
                cwd = None
            elif posixpath.isabs(dest[0]):
                cwd = posixpath.normpath(dest[0])
            elif cwd is not None:
                cwd = posixpath.normpath(posixpath.join(cwd, dest[0]))
            continue
        seg_cwd = cwd
        if argv[:1] == ["git"] and len(argv) > 2 and argv[1] == "-C":
            base = argv[2]
            seg_cwd = (
                posixpath.normpath(base)
                if posixpath.isabs(base)
                else (posixpath.normpath(posixpath.join(cwd, base)) if cwd else None)
            )
            argv = ["git", *argv[3:]]
        found = (
            [*seg.truncates, *shell_removal_targets(argv)]
            if removals
            else [*seg.writes, *shell_write_targets(argv)]
        )
        out += [(t, seg_cwd) for t in found]
    return out


def is_test_path(path: str, test_globs: Sequence[str] = DEFAULT_TEST_GLOBS) -> bool:
    """Whether a repo-relative *path* is a test file (see :data:`DEFAULT_TEST_GLOBS`)."""
    norm = posixpath.normpath(path.strip().replace("\\", "/")).lstrip("/")
    return norm not in ("", ".") and _GlobSet(test_globs).match(norm)


def test_writes_only(
    *,
    role: str = "this role",
    test_globs: Sequence[str] | None = None,
    extra_paths: Sequence[str] | None = None,
    root: str | None = None,
    free_paths: Sequence[str] | None = None,
    reason: str | None = None,
    base_ref: str = "origin/main",
) -> _Evaluator:
    """Factory: DENY every write that is not a test file (verify roles: qa, security).

    Add-only for tests other tasks wrote: deleting, renaming away or
    truncating a test file that exists at *base_ref* (``rm``, ``git rm``,
    ``mv`` / ``git mv`` source, ``truncate``, a ``>`` redirection, a full
    ``Write`` over it) is DENY; ``Edit`` and appends stay allowed. A removal
    whose target cannot be checked (no git answer, a glob) is DENY too; a
    test file this task added itself may be removed.

    Judged on the write tools and on shell write targets (redirections, ``cp``
    / ``mv`` / ``rm``, ``git checkout <path>``, formatters, dependency
    changes). Ownership is not checked here: :func:`owned_paths` still asks
    for a test file outside the task's owned paths.

    :param role: Role name for the refusal text.
    :param test_globs: Writable test globs, repo-relative (default
        :data:`DEFAULT_TEST_GLOBS`).
    :param extra_paths: Other writable repo-relative globs (the role's report
        file, e.g. ``.shipcrew/qa.json``).
    :param root: Absolute task worktree (injected like the owned-paths
        contract). Unknown: an absolute path is judged by its trailing
        components (``/any/where/e2e/a.spec.ts`` is a test file).
    :param free_paths: Always-writable build output and scratch (default
        :data:`DEFAULT_FREE_PATHS`, which includes ``/tmp``).
    :param base_ref: The ref whose test files are protected (``origin/main``).
    :returns: An evaluator ``fn(event, config)``; reads always ALLOW.
    """
    writable = _GlobSet([*(DEFAULT_TEST_GLOBS if test_globs is None else test_globs),
                         *(extra_paths or ())])  # fmt: skip
    free_all = list(DEFAULT_FREE_PATHS if free_paths is None else free_paths)
    free_rel = _GlobSet(p for p in free_all if not p.startswith("/"))
    free_abs = _GlobSet(p for p in free_all if p.startswith("/"))
    root_abs = posixpath.normpath(root) if root else None
    shown = ", ".join([*(DEFAULT_TEST_GLOBS if test_globs is None else test_globs),
                       *(extra_paths or ())])  # fmt: skip
    suffix = f" {reason}" if reason else ""

    def _rel_ok(rel: str) -> bool:
        if _WILDCARD.search(rel):
            literal = rel[: _WILDCARD.search(rel).start()]  # type: ignore[union-attr]
            directory = literal.rsplit("/", 1)[0] if "/" in literal else ""
            return bool(directory) and (
                writable.covers_tree(directory) or free_rel.covers_tree(directory)
            )
        return free_rel.match(rel) or writable.match(rel)

    def _ok(target: str, cwd: str | None) -> bool:
        if "$" in target or target.startswith("~"):
            return False
        if posixpath.isabs(target):
            absolute = posixpath.normpath(target)
        elif cwd is not None:
            absolute = posixpath.normpath(posixpath.join(cwd, target))
        else:
            return _rel_ok(posixpath.normpath(target))
        if root_abs is not None and (
            absolute == root_abs or absolute.startswith(root_abs.rstrip("/") + "/")
        ):
            return _rel_ok(posixpath.relpath(absolute, root_abs))
        if free_abs.match(absolute) or (
            _WILDCARD.search(absolute) and free_abs.covers_tree(posixpath.dirname(absolute))
        ):
            return True
        if root_abs is not None:
            return False  # outside the worktree
        parts = absolute.strip("/").split("/")
        return any(_rel_ok("/".join(parts[i:])) for i in range(1, len(parts)))

    def _repo_rel(target: str, cwd: str | None) -> str | None:
        if root_abs is None or "$" in target or target.startswith("~"):
            return None
        absolute = posixpath.normpath(
            target if posixpath.isabs(target) else posixpath.join(cwd or root_abs, target)
        )
        if absolute != root_abs and not absolute.startswith(root_abs.rstrip("/") + "/"):
            return None
        return posixpath.relpath(absolute, root_abs)

    def _at_base(rel: str) -> bool | None:
        """Whether *rel* exists at *base_ref*; ``None`` when git cannot tell."""
        try:
            r = subprocess.run(
                ["git", "-C", root_abs or ".", "cat-file", "-e", f"{base_ref}:{rel}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            known = subprocess.run(
                ["git", "-C", root_abs or ".", "rev-parse", "--verify", "--quiet", base_ref],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if known.returncode:
            return None
        return r.returncode == 0

    def _base_files(directory: str) -> list[str] | None:
        """Files under *directory* at *base_ref*; ``None`` when git cannot tell."""
        if _at_base(".") is None:
            return None
        try:
            r = subprocess.run(
                ["git", "-C", root_abs or ".", "ls-tree", "-r", "--name-only", base_ref,
                 "--", directory or "."],
                capture_output=True, text=True, timeout=5, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.splitlines() if r.returncode == 0 else None

    def _protected(target: str, cwd: str | None, *, removal: bool) -> bool:
        """A test file another task wrote (at the base); for a removal, also "cannot tell"."""
        rel = _repo_rel(target, cwd)
        if rel is None:
            return False  # outside the worktree: the write check judges it
        if _WILDCARD.search(rel):
            literal = rel[: _WILDCARD.search(rel).start()]  # type: ignore[union-attr]
            directory = literal.rsplit("/", 1)[0] if "/" in literal else ""
            if not removal or free_rel.covers_tree(directory):
                return False
            listed = _base_files(directory)
            if listed is None:
                return True  # cannot tell what the glob removes
            glob = _glob_regex(rel)
            return any(glob.match(f) and writable.match(f) for f in listed)
        if not writable.match(rel) or free_rel.match(rel):
            return False
        existing = _at_base(rel)
        return removal if existing is None else existing

    def _deny_removal(target: str) -> _Json:
        return {
            "result": "DENY",
            "reason": f"The {role} only adds tests: `{target}` is an existing test (another "
            "task's, on the base branch) and may not be deleted, renamed or overwritten. "
            f"Report a wrong or redundant test as a finding instead.{suffix}",
        }

    def _deny(target: str) -> _Json:
        return {
            "result": "DENY",
            "reason": f"The {role} writes test files only ({shown}); `{target}` is not one. "
            "Report the defect with file:line and a repro instead of fixing it: the board "
            f"turns your findings into a fix task.{suffix}",
        }

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        paths = _write_tool_paths(event)
        if paths is not None:
            for path in paths:
                if not _ok(path, root_abs):
                    return _deny(path)
            if _tool_name(event) in _OVERWRITE_TOOLS:
                for path in paths:
                    if _protected(path, root_abs, removal=False):
                        return _deny_removal(path)
            return _ALLOW
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        targets = _shell_targets(command, root_abs)
        for target, cwd in targets or []:
            if not _ok(target, cwd):
                return _deny(target)
        for target, cwd in _shell_targets(command, root_abs, removals=True) or []:
            truncating = target in {t for s in (parse_command(command, strict=False) or [])
                                    for t in s.truncates}  # fmt: skip
            if _protected(target, cwd, removal=not truncating):
                return _deny_removal(target)
        return _ALLOW

    return _evaluate


def paths_outside_owned(
    changed: Iterable[str],
    owned_paths: Sequence[str],
    shared_paths: Sequence[str] | None = None,
) -> list[str]:
    """Repo-relative *changed* paths the task's ``owned_paths`` do not cover.

    The same rules as :func:`owned_paths` (a shared contract file counts as
    owned only when listed by name), applied to a diff. Empty *owned_paths*
    means no contract: nothing is outside.
    """
    owned_list = [p for p in owned_paths if isinstance(p, str) and p.strip()]
    if not owned_list:
        return []
    owned_list = _with_lockfiles(owned_list)
    owned = _GlobSet(owned_list)
    exact_owned = {_norm_glob(p) for p in owned_list if not _WILDCARD.search(_norm_glob(p))}
    shared = _GlobSet(DEFAULT_SHARED_PATHS if shared_paths is None else shared_paths)
    outside: list[str] = []
    for path in changed:
        if (shared.match(path) and path not in exact_owned) or not owned.match(path):
            outside.append(path)
    return outside


# ── Workflow files ───────────────────────────────────────────────────────────

_WORKFLOWS = re.compile(r"(^|[/=:])\.github/workflows(/|$)")
# Commands that only read what they name (a workflow path in their arguments).
_WORKFLOW_READERS = frozenset(
    {"cat", "ls", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "diff", "stat",
     "file", "less", "tree", "echo", "printf", "test", "["}
)  # fmt: skip
_GIT_WORKFLOW_READS = frozenset({"diff", "log", "show", "status", "ls-files", "blame", "grep"})


def _reads_workflows_only(argv: list[str]) -> bool:
    if not argv:
        return True
    prog, args = argv[0], argv[1:]
    if prog in _WORKFLOW_READERS:
        return True
    if prog == "yq":
        return not any(a.startswith(("-i", "--inplace")) for a in args)
    if prog == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args)
    if prog == "git" and args and args[0] in _GIT_WORKFLOW_READS:
        return not any(a.startswith(("--output", "-o")) for a in args[1:])
    return False


def workflows_guard(*, reason: str | None = None) -> _Evaluator:
    """Factory: ASK before any write to ``.github/workflows/**``; reads pass.

    Write tools on a workflow path ASK. A shell command ASKs when a write
    target (redirection, ``cp``/``mv``/``tee``/``sed -i``/``git checkout
    <path>`` ...) is a workflow path, or when a simple command that names a
    workflow path (or runs after ``cd`` into one) is not a known reader. Other
    simple commands of the same chain do not matter: ``git checkout x -- a.js
    && ls .github/workflows`` passes (the old regex asked for it). A command
    naming workflows that cannot be analyzed (substitutions, heredocs) ASKs.
    """
    text = reason or "Editing .github/workflows/** needs human approval."
    ask: _Json = {"result": "ASK", "reason": text}

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        paths = _write_tool_paths(event)
        if paths is not None:
            return ask if any(_WORKFLOWS.search(p) for p in paths) else _ALLOW
        command = _shell_command(event)
        if command is None or "workflows" not in command:
            return _ALLOW
        segments = parse_command(command, strict=False)
        if segments is None:
            return ask
        inside = False
        for seg in segments:
            argv = seg.argv
            if argv[:1] == ["cd"]:
                # After `cd .github` (or deeper) every later command may touch workflows.
                inside = inside or any(".github" in a or "workflows" in a for a in argv[1:])
                continue
            if any(_WORKFLOWS.search(t) for t in [*seg.writes, *shell_write_targets(argv)]):
                return ask
            names = inside or any(_WORKFLOWS.search(a) for a in argv)
            writes = [w for w in seg.writes if not _scratch_target(w)]
            if names and (writes or not _reads_workflows_only(argv)):
                return ask
        return _ALLOW

    return _evaluate


# ── Orchestrator push guard ──────────────────────────────────────────────────

_PUSH_BANNED_FLAGS = re.compile(
    r"^(--all|--mirror|--tags|--delete|-d|--prune|--force.*|-f|--follow-tags|"
    r"--receive-pack.*|--exec.*|--repo.*)$"
)
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GH_ASK = re.compile(r"^gh (pr merge|repo delete|release create)\b")


def push_guard(
    *,
    branch_pattern: str = DEFAULT_BRANCH_PATTERN,
    protected: Sequence[str] = ("main", "master", "HEAD"),
) -> _Evaluator:
    """Factory: pushes name only task branches; merges, repo deletes and releases ASK.

    Every ``git push`` in a command must name at least one refspec, and every
    refspec must be a task branch (``src`` or ``src:dst`` with both sides a
    task branch). ``--all`` / ``--mirror`` / ``--tags`` / ``--delete`` /
    ``+refspec`` are refused. An unanalyzable command containing a push is DENY.

    :param branch_pattern: Regex (full match) of a task branch; default
        ``shipcrew/<8 hex>-<slug>``.
    :param protected: Branch names whose push gets the specific refusal text.
    """
    branch = re.compile(branch_pattern)
    protected_set = frozenset(protected)
    usage = "git -C .worktrees/<key> push -u origin shipcrew/<id8>-<slug>"

    def _deny(reason: str) -> _Json:
        return {"result": "DENY", "reason": reason}

    def _check_push(args: list[str]) -> _Json | None:
        pos: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("-"):
                if _PUSH_BANNED_FLAGS.match(arg):
                    return _deny(f"`git push {arg}` is not allowed: push one task branch by name.")
                i += 2 if arg in ("-o", "--push-option") else 1
                continue
            pos.append(arg)
            i += 1
        refspecs = pos[1:]
        if not refspecs:
            return _deny(f"Push only task branches, named explicitly: {usage}.")
        for spec in refspecs:
            sides = spec.split(":")
            if spec.startswith("+") or len(sides) > 2:
                return _deny(f"Refspec `{spec}` is not allowed: no forced or multi-part refspecs.")
            names = [s.removeprefix("refs/heads/") for s in sides]
            if any(n in protected_set for n in names):
                return _deny(
                    "Never push main/master (or HEAD) from the orchestrator: push the "
                    "shipcrew/<id8>-<slug> task branch by name and open a draft PR."
                )
            if not all(branch.fullmatch(n) for n in names):
                return _deny(
                    f"`{spec}` is not a task branch: push only shipcrew/<id8>-<slug> "
                    f"branches, named explicitly ({usage})."
                )
        return None

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        segments = parse_command(command)
        if segments is None:
            if re.search(r"\bpush\b", command):
                return _deny("Push commands must be plain: no substitutions or heredocs.")
            segments = []
        ask = False
        for seg in segments:
            argv = seg.argv
            if argv[:1] == ["git"]:
                i = 1
                while i < len(argv) and argv[i].startswith("-"):
                    i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
                if i < len(argv) and argv[i] == "push":
                    verdict = _check_push(argv[i + 1 :])
                    if verdict is not None:
                        return verdict
            elif _GH_ASK.match(" ".join(argv)):
                ask = True
        if ask:
            return {
                "result": "ASK",
                "reason": "Merging, deleting a repo or cutting a release needs a human.",
            }
        return _ALLOW

    return _evaluate


# ── Registry ─────────────────────────────────────────────────────────────────

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

POLICY_REGISTRY: list[dict[str, object]] = [
    {
        "handler": "omnigent.shipcrew.policies.shell_allowlist",
        "kind": "factory",
        "name": "shipcrew: Shell Allowlist",
        "description": "Runs allowlisted shell commands without a prompt; every other command "
        "(or an unanalyzable one) asks for approval.",
        "params_schema": {
            "type": "object",
            "properties": {
                "allow": {**_STRING_LIST, "description": "Allowlist entries."},
                "role": {"type": "string", "default": "this role"},
                "env_allow": {**_STRING_LIST, "description": "Allowed env-assignment names."},
                "shell_writes": {"type": "boolean", "default": True},
                "reason": {"type": "string"},
                "read_only": {
                    **_STRING_LIST,
                    "description": "Read-only entries that may carry a variable expansion.",
                },
                "expand_allow": {**_STRING_LIST, "description": "Extra expandable names."},
            },
            "required": ["allow"],
        },
    },
    {
        "handler": "omnigent.shipcrew.policies.owned_paths",
        "kind": "factory",
        "name": "shipcrew: Owned Paths",
        "description": "Asks for approval before a write outside the task's owned paths or to "
        "a shared contract file (package.json, lockfiles). Reads are never gated.",
        "params_schema": {
            "type": "object",
            "properties": {
                "owned_paths": {**_STRING_LIST, "description": "Owned globs (repo-relative)."},
                "root": {"type": "string", "description": "Absolute task worktree path."},
                "shared_paths": _STRING_LIST,
                "free_paths": _STRING_LIST,
                "extra_free_paths": _STRING_LIST,
                "reason": {"type": "string"},
            },
        },
    },
    {
        "handler": "omnigent.shipcrew.policies.test_writes_only",
        "kind": "factory",
        "name": "shipcrew: Test Writes Only",
        "description": "Verify roles (qa, security) may write test files and their report "
        "only; every other write is refused.",
        "params_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "default": "this role"},
                "test_globs": {**_STRING_LIST, "description": "Writable test globs."},
                "extra_paths": {**_STRING_LIST, "description": "Other writable globs."},
                "root": {"type": "string", "description": "Absolute task worktree path."},
                "free_paths": _STRING_LIST,
                "reason": {"type": "string"},
            },
        },
    },
    {
        "handler": "omnigent.shipcrew.policies.workflows_guard",
        "kind": "factory",
        "name": "shipcrew: Workflows Guard",
        "description": "Asks before any write to .github/workflows/**; reads (also chained with "
        "other commands) pass.",
        "params_schema": {"type": "object", "properties": {"reason": {"type": "string"}}},
    },
    {
        "handler": "omnigent.shipcrew.policies.push_guard",
        "kind": "factory",
        "name": "shipcrew: Push Guard",
        "description": "Orchestrator pushes only shipcrew/<id8>-<slug> task branches named "
        "explicitly; gh pr merge / repo delete / release create ask.",
        "params_schema": {
            "type": "object",
            "properties": {
                "branch_pattern": {"type": "string", "default": DEFAULT_BRANCH_PATTERN},
                "protected": _STRING_LIST,
            },
        },
    },
]
