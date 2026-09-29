"""Guardrail policies for the shipcrew role bundles.

Four :class:`FunctionPolicy` factories, registered through
:data:`POLICY_REGISTRY` so uploaded bundles may use them:

* :func:`shell_allowlist` -- a per-role shell allowlist. A shell command runs
  with no prompt only when every simple command in it matches the role's
  allowlist; anything else is ASK (an approval card in the Inbox, which moves
  the board card to Intervention). Unanalyzable commands (command
  substitution, heredocs, unbalanced quotes) are ASK too.
* :func:`owned_paths` -- the task's ``owned_paths`` contract. File writes
  (``Write`` / ``Edit`` / ``sys_os_write`` / shell redirections / ``cp``,
  ``mv``, ``rm``, ``git mv`` ... / formatter ``--write`` / dependency changes)
  outside the owned globs are ASK, and so are writes to the shared contract
  files (``package.json``, lockfiles) unless the task owns them by name.
  Reads are never gated. The shipcrew server injects ``root`` and
  ``owned_paths`` into the bundle when it starts a task; without them the
  policy abstains.
* :func:`test_writes_only` -- verify roles (qa, security) write test files
  and their report only; any other write is DENY.
* :func:`push_guard` -- the orchestrator may push only task branches named
  ``shipcrew/<8 hex>-<slug>``, every push segment explicitly.

These policies only return ALLOW or ASK (the push guard and the test-writes
guard also DENY); the
catastrophic DENY set stays in omnigent's ``blast_radius`` and the shipcrew CEL
fragments. Evaluation is most-restrictive-wins across policies.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
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
)

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
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
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
    """

    argv: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)

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


def _normalize_program(argv: list[str]) -> list[str]:
    argv = _unwrap(argv)
    if not argv:
        return argv
    head = argv[0]
    for prefix in _BIN_PREFIXES:
        if head.startswith(prefix) and "/" not in head[len(prefix) :]:
            argv = [head[len(prefix) :], *argv[1:]]
            break
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


def _has_expansion(text: str) -> bool:
    """Whether the shell would rewrite a word of *text* before running it.

    Parameter / ANSI-C expansion (``$X``, ``${X}``, ``$'..'``) outside single
    quotes, unquoted brace expansion (``--{ha,}rd``) and an unquoted glob in an
    option word (``--ha?d``) all turn a word the allowlist saw into another
    one (``CI=--output=f; git diff $CI``), so such a command is unanalyzable.
    """
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
            i += 1
            continue
        if c == "\\":
            i += 2
            word_start = False
            continue
        if c == "$" and i + 1 < len(text) and not text[i + 1].isspace() and text[i + 1] != '"':
            return True
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
                    return True
                brace -= 1
            elif brace and (c == "," or text.startswith("..", i)):
                brace_list = True
            elif option_word and c in _GLOB_CHARS:
                return True
        i += 1
    return False


def parse_command(command: str, *, strict: bool = True) -> list[Segment] | None:
    """Split *command* into simple commands, or ``None`` when it cannot be analyzed.

    Command substitution, process substitution, backticks and heredocs make a
    command unanalyzable (their inner commands would run unseen), except the
    ``$(cat <<'EOF' ... EOF)`` literal-string idiom; so do parameter, brace
    and option-glob expansions (see :func:`_has_expansion`) unless
    ``strict=False`` (the owned-paths check, which refuses any target holding
    a ``$`` itself).
    """
    text = command.replace("\\\n", " ")
    text = _HEREDOC_STRING.sub("HEREDOC", text)
    if any(marker in text for marker in ("$(", "`", "<(", ">(")):
        return None
    if strict and _has_expansion(text):
        return None
    text = _FD_DUP.sub(r"\1", text)
    text = _FD_NUMBER.sub(r"\1", text)
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
_CURL_VALUE_FLAGS = frozenset(
    {
        "-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
        "--json", "-X", "--request", "-A", "--user-agent", "-e", "--referer", "-b", "--cookie",
        "-F", "--form", "-w", "--write-out", "-m", "--max-time", "--connect-timeout",
        "--retry", "--retry-delay", "--retry-max-time", "-u", "--user", "-r", "--range",
    }
)  # fmt: skip
_CURL_BANNED = re.compile(
    r"^(-o|--output|-O|--remote-name|--remote-name-all|-T|--upload-file|-K|--config|"
    r"-c|--cookie-jar|-D|--dump-header|--trace|--trace-ascii|--stderr|-x|--proxy|"
    r"--unix-socket|--abstract-unix-socket|--resolve|--connect-to)(=|$)"
)
_URL = re.compile(r"^(?:https?://)?(?P<host>\[[^\]]+\]|[^/:?#]+)(?::\d+)?(?:[/?#].*)?$")


def _curl_localhost(args: list[str]) -> bool:
    """``curl`` whose every URL is a local host and that writes no file."""
    urls = 0
    i = 0
    while i < len(args):
        arg = args[i]
        if _CURL_BANNED.match(arg) or (
            arg.startswith("-")
            and not arg.startswith("--")
            and len(arg) > 2
            and any(c in "oOTKcDx" for c in arg[1:])
        ):
            return False
        if arg in _CURL_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        match = _URL.match(arg)
        if match is None or match.group("host").lower() not in _LOCAL_HOSTS:
            return False
        urls += 1
        i += 1
    return urls > 0


_BUILTIN_MATCHERS: dict[str, Callable[[list[str]], bool]] = {"curl_localhost": _curl_localhost}


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
    * ``"@curl:curl_localhost"`` -- a built-in matcher for the program.
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
) -> _Evaluator:
    """Factory: ALLOW a shell command only when every part is allowlisted, else ASK.

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
    :returns: An evaluator ``fn(event, config)``; non-shell tool calls ALLOW.
    """
    patterns = [compile_pattern(entry) for entry in allow]
    envs = frozenset(DEFAULT_ENV_ALLOW if env_allow is None else env_allow)
    suffix = f" {reason}" if reason else ""

    def _ask(what: str) -> _Json:
        return {
            "result": "ASK",
            "reason": f"{what} is not on the {role} shell allowlist.{suffix}",
        }

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        segments = parse_command(command)
        if segments is None:
            return _ask("A command with substitutions, heredocs or unbalanced quotes")
        for seg in segments:
            bad_env = [name for name in seg.env if name not in envs]
            if bad_env:
                return _ask(f"`{seg.text}` (env {', '.join(bad_env)})")
            if not shell_writes and not all(
                _scratch_target(t) for t in [*seg.writes, *shell_write_targets(seg.argv)]
            ):
                return _ask(f"`{seg.text}` (writes a file)")
            if not seg.argv:
                if seg.writes:
                    return _ask(f"`{seg.text}`")
                continue
            if not any(p.matches(seg.argv) for p in patterns):
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
    if prog == "sed" and any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
        pos = _positionals(args, _VALUE_FLAGS["sed"])
        has_script_flag = any(a in ("-e", "--expression", "-f", "--file") for a in args)
        return pos if has_script_flag else pos[1:]
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
    if prog in _DEP_SUBCOMMANDS and args and args[0] in _DEP_SUBCOMMANDS[prog]:
        if _FROZEN_FLAGS.intersection(args):
            return []
        return list(_MANIFESTS[prog])
    return []


def owned_paths(
    *,
    owned_paths: Sequence[str] | None = None,
    root: str | None = None,
    shared_paths: Sequence[str] | None = None,
    free_paths: Sequence[str] | None = None,
    extra_free_paths: Sequence[str] | None = None,
    reason: str | None = None,
) -> _Evaluator:
    """Factory: ASK when a write leaves the task's owned paths.

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
    :returns: An evaluator ``fn(event, config)``; reads always ALLOW.
    """
    owned_list = [p for p in (owned_paths or []) if isinstance(p, str) and p.strip()]
    if not owned_list:

        def _abstain(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
            return _ALLOW

        return _abstain

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
    owned_text = ", ".join(owned_list)
    suffix = f" {reason}" if reason else ""
    shared_names = [
        p.rsplit("/", 1)[-1] for p in shared.patterns if not _WILDCARD.search(p.rsplit("/", 1)[-1])
    ]

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
        if shared.match(rel) and rel not in exact_owned:
            return f"`{rel}` is a shared contract file (own it by name to change it)"
        if rel and owned.match(rel):
            return None
        return f"`{rel or '.'}` is outside this task's owned paths ({owned_text})"

    def _ask(why: str) -> _Json:
        return {"result": "ASK", "reason": f"Write needs approval: {why}.{suffix}"}

    def _evaluate(event: _Json, config: _Json | None = None) -> _Json:  # noqa: ARG001
        paths = _write_tool_paths(event)
        if paths is not None:
            for path in paths:
                why = _check(path, root_abs)
                if why:
                    return _ask(why)
            return _ALLOW
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        targets = _shell_targets(command, root_abs)
        if targets is None:
            return _ALLOW  # unanalyzable: shell_allowlist asks for it
        for target, seg_cwd in targets:
            why = _check(target, seg_cwd)
            if why:
                return _ask(why)
        return _ALLOW

    return _evaluate


def _shell_targets(command: str, root: str | None) -> list[tuple[str, str | None]] | None:
    """``(target, cwd)`` for every file a shell command writes; ``None`` if unanalyzable.

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
        out += [(t, seg_cwd) for t in [*seg.writes, *shell_write_targets(argv)]]
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
) -> _Evaluator:
    """Factory: DENY every write that is not a test file (verify roles: qa, security).

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
            return _ALLOW
        command = _shell_command(event)
        if command is None:
            return _ALLOW
        targets = _shell_targets(command, root_abs)
        for target, cwd in targets or []:
            if not _ok(target, cwd):
                return _deny(target)
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
    owned = _GlobSet(owned_list)
    exact_owned = {_norm_glob(p) for p in owned_list if not _WILDCARD.search(_norm_glob(p))}
    shared = _GlobSet(DEFAULT_SHARED_PATHS if shared_paths is None else shared_paths)
    outside: list[str] = []
    for path in changed:
        if (shared.match(path) and path not in exact_owned) or not owned.match(path):
            outside.append(path)
    return outside


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
