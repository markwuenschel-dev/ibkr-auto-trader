#!/usr/bin/env python3
"""openai-repo-seat — a REPO-AWARE collab-kit autopilot backend for an OpenAI-compatible chat API.

Same backend contract as ``openai-compatible-seat.py`` (prompt on STDIN -> reply on STDOUT, non-zero on
failure), and the model still runs remotely (e.g. gpt-5.5 on OpenAI) so the two-vendor / cross-vendor
independence of the seat is preserved. The difference: this adapter runs an **agentic tool-calling loop**
so the remote model can actually SEE THE REPO. A remote model has no filesystem — but THIS SCRIPT does, so
we expose three read-only filesystem tools (``list_dir``, ``read_file``, ``search``) as OpenAI function
tools. When the model calls one, we read the real repository off local disk (scoped to ``--repo-root``,
no escaping the root) and hand the bytes back, looping until the model stops calling tools and returns its
final review. That is how a datacenter model "sees the repo": it asks, we serve.

Read-only by default (list_dir/read_file/search) — a review seat inspects but never mutates. Pass
``--write`` and it also gains ``write_file`` + ``run_command`` (allow-listed: pytest/ruff/python/uv), so a
BUILDER seat on ANY model (not just Claude) can implement a handoff and run the checks. Stdlib only (no pip
/ no openai SDK) — function-calling is done over raw POST /chat/completions.

  --base <url>        API base, e.g. https://api.openai.com/v1
  --model <name>      e.g. gpt-5.5
  --key-env <VAR>     env var holding the bearer token (e.g. OPENAI_API_KEY)
  --repo-root <dir>   repository the model may read (REQUIRED — enables tool mode)
  --timeout <secs>    per-HTTP-call timeout (default 180); the driver's seat timeout bounds the whole loop
  --max-steps <n>     max tool round-trips before we force a final answer (default 50)
  --max-bytes <n>     per read_file / per search-hit byte cap (default 100000)
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

# Directories that are never worth serving to a code reviewer — noise, vendored, or VCS internals.
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}
_TEXT_SNIFF_BYTES = 2048  # read this many bytes to decide if a file is text (NUL byte => binary)

# INT-037b: a read_test (--run-checks) seat has no write_file tool, but its allow-listed interpreters
# (python/uv) CAN write, so it runs against an EPHEMERAL COPY of the working tree — a `git worktree` of
# HEAD would miss the uncommitted builder edits the judge must review. Writes land in the throwaway; the
# judged source is untouched. Excludes _SKIP_DIRS (VCS internals, caches, vendored, dist/build) plus a
# few extra build-artifact dirs. This raises the bar against ordinary/accidental writes (ADR-0006);
# perfect containment is a container (the deferred end state), and a model that writes a symlink out of
# the copy is a documented residual, not something this copy stops.
_COPY_SKIP = _SKIP_DIRS | {".next", ".hypothesis", ".tox", "target", "coverage"}


def _isolate_check_root(root: Path) -> tuple[Path, Callable[[], None]]:
    """Copy ``root``'s working tree to a fresh temp dir; return ``(copy_root, cleanup)``.

    ``cleanup()`` removes the copy unless ``COLLAB_KEEP_CHECK_ROOT=1`` (retain for debugging). The temp
    dir is created via ``tempfile.mkdtemp`` OUTSIDE ``root`` so the copy is never nested under the judged
    tree, and uncommitted edits are carried (it is a copy, not a git worktree).
    """
    dest = Path(tempfile.mkdtemp(prefix="collab-check-root-"))
    shutil.copytree(root, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*_COPY_SKIP))

    def _cleanup() -> None:
        if os.environ.get("COLLAB_KEEP_CHECK_ROOT") == "1":
            sys.stderr.write(f"openai-repo-seat: keeping isolated check root {dest}\n")
            return
        shutil.rmtree(dest, ignore_errors=True)

    return dest, _cleanup


def _load_dotenv() -> None:
    """Load API keys from ``.env`` (stdlib). See openai-compatible-seat for the full contract; identical."""
    override = os.environ.get("SEAT_ENV_FILE")
    path = Path(override) if override else Path(__file__).resolve().parents[2] / ".env"
    try:
        text = path.read_text("utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# filesystem tools — every path is resolved and CONTAINED within repo_root
# --------------------------------------------------------------------------- #


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse to leave it. Guards against ``..``, absolute paths, and
    symlinks that point outside the tree (realpath is followed, then containment is re-checked). Raises
    ValueError on any escape so the caller returns an error string to the model instead of crashing."""
    rel = (rel or ".").strip().lstrip("/\\")
    candidate = (root / rel).resolve()
    root_real = root.resolve()
    if candidate != root_real and root_real not in candidate.parents:
        raise ValueError(f"path {rel!r} escapes the repo root — refused")
    return candidate


def _is_text(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\x00" not in f.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return False


def _tool_list_dir(root: Path, path: str = ".") -> str:
    d = _safe_path(root, path)
    if not d.is_dir():
        return f"error: {path!r} is not a directory"
    entries = []
    for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.name in _SKIP_DIRS:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
    rel = d.relative_to(root.resolve())
    header = f"{rel.as_posix() or '.'}/ ({len(entries)} entries):"
    return header + "\n" + "\n".join(entries) if entries else header + "\n(empty)"


def _tool_read_file(
    root: Path, path: str, start_line: int = 1, max_lines: int = 600, max_bytes: int = 100_000
) -> str:
    p = _safe_path(root, path)
    if not p.is_file():
        return f"error: {path!r} is not a file"
    if not _is_text(p):
        return f"error: {path!r} looks binary — not shown"
    try:
        raw = p.read_text("utf-8", errors="replace")
    except OSError as e:
        return f"error reading {path!r}: {e}"
    lines = raw.splitlines()
    start = max(1, int(start_line or 1))
    end = start + max(1, int(max_lines or 600)) - 1
    out, size = [], 0
    for i in range(start, min(end, len(lines)) + 1):
        row = f"{i:>6}\t{lines[i - 1]}"
        size += len(row) + 1
        if size > max_bytes:
            out.append(f"… [truncated at {max_bytes} bytes; call read_file with start_line={i} for more]")
            break
        out.append(row)
    tail = "" if end >= len(lines) else f"\n… [{len(lines) - end} more lines below]"
    return f"{path} (lines {start}-{min(end, len(lines))} of {len(lines)}):\n" + "\n".join(out) + tail


def _tool_search(
    root: Path, query: str, glob: str = "**/*.py", max_results: int = 60, max_bytes: int = 100_000
) -> str:
    if not query:
        return "error: empty query"
    root_real = root.resolve()
    hits, scanned = [], 0
    for p in root_real.glob(glob or "**/*"):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.relative_to(root_real).parts):
            continue
        if not _is_text(p):
            continue
        scanned += 1
        try:
            for n, line in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1):
                if query in line:
                    rel = p.relative_to(root_real).as_posix()
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        hits.append(f"… [stopped at {max_results} hits]")
                        return f"search {query!r} (glob {glob!r}):\n" + "\n".join(hits)
        except OSError:
            continue
    if not hits:
        return f"search {query!r} (glob {glob!r}): no matches in {scanned} files"
    return f"search {query!r} (glob {glob!r}), {len(hits)} hits:\n" + "\n".join(hits)


def _tool_write_file(root: Path, path: str, content: str = "") -> str:
    """Create or overwrite a UTF-8 text file under the repo root (parent dirs auto-created)."""
    try:
        p = _safe_path(root, path)
    except ValueError as e:
        return f"error: {e}"
    if p.is_dir():
        return f"error: {path!r} is a directory, not a file"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"error writing {path!r}: {e}"
    return f"wrote {path} ({len(content)} bytes, {content.count(chr(10)) + 1} lines)"


_RUN_ALLOW = {"pytest", "ruff", "python", "python3", "py", "uv"}


def _tool_run_command(root: Path, command: str, max_bytes: int, timeout: float) -> str:
    """Run an allow-listed check command (pytest/ruff/python/uv) in the repo root; return exit code +
    captured output. Not a general shell — the first token must be in the allow-list."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as e:
        return f"error: cannot parse command {command!r}: {e}"
    if not argv:
        return "error: empty command"
    if argv[0] not in _RUN_ALLOW:
        return f"error: command {argv[0]!r} not allowed (allowed: {sorted(_RUN_ALLOW)})"
    try:
        proc = subprocess.run(
            argv, cwd=str(root), capture_output=True, text=True, timeout=timeout, shell=False
        )
    except subprocess.TimeoutExpired:
        return f"error: {command!r} timed out after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as e:
        return f"error running {command!r}: {e}"
    body = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return f"$ {command}\n(exit {proc.returncode})\n{body[:max_bytes]}"


_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories in the repository, relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to repo root; '.' for root.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from the repository, returned with 1-based line "
                "numbers so you can cite file:line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root."},
                    "start_line": {"type": "integer", "description": "First line to read (default 1)."},
                    "max_lines": {"type": "integer", "description": "How many lines (default 600)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search the repository for a literal substring (e.g. a function or symbol "
                "name); returns file:line matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal substring to find."},
                    "glob": {
                        "type": "string",
                        "description": "Path glob to scope the search (default '**/*.py').",
                    },
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
]


_RUN_COMMAND_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run an allow-listed check command (pytest, ruff, python, uv) in the repo "
                "root; returns exit code + output. Use it to run the tests/linters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "e.g. 'python -m pytest -q' or 'ruff check'.",
                    }
                },
                "required": ["command"],
            },
        },
    },
]

_WRITE_FILE_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a UTF-8 text file in the repository (parent dirs "
                "auto-created). Provide the FULL new file contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root."},
                    "content": {"type": "string", "description": "The full file contents to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
]

# A builder (--write) gets both write_file + run_command; a read_test lane (--run-checks) gets
# run_command ONLY (no write_file). That is TOOL-SURFACE policy, not filesystem containment: the
# allow-listed interpreters (python/uv/pytest) can still mutate the tree (e.g. `python -c`, a
# `uv run` script, a fixture). Write-containment for check seats is NOT enforced yet — an ephemeral
# isolated root is the required next hardening (collab/docs/adr/0006, INT-037).
_WRITE_TOOLS_SPEC = _WRITE_FILE_SPEC + _RUN_COMMAND_SPEC


_SYSTEM_BUILDER_NOTE = (
    "You are the BUILDER, working directly on the ACTUAL repository. You have tools to inspect it "
    "(list_dir, read_file, search) AND to change it: write_file creates/overwrites a file with the full "
    "contents you provide, and run_command runs allow-listed checks (pytest, ruff, python, uv) in the repo "
    "root. IMPLEMENT the handoff by WRITING the real source and test files with write_file — do not merely "
    "describe them. After writing, RUN the checks with run_command (e.g. 'python -m pytest -q') and report "
    "the ACTUAL output — never claim green until a run_command shows it. When done, write a short final "
    "summary as an ordinary message with no further tool calls."
)


def _dispatch_tool(root: Path, name: str, args: dict, max_bytes: int, run_timeout: float = 600.0) -> str:
    """Execute a tool call against the real filesystem. Any error becomes a returned string (never an
    exception) so one bad model-supplied argument fails that call, not the whole round."""
    try:
        if name == "list_dir":
            return _tool_list_dir(root, args.get("path", "."))
        if name == "read_file":
            return _tool_read_file(
                root, args["path"], args.get("start_line", 1), args.get("max_lines", 600), max_bytes
            )
        if name == "search":
            return _tool_search(
                root, args["query"], args.get("glob", "**/*.py"), args.get("max_results", 60), max_bytes
            )
        if name == "write_file":
            return _tool_write_file(root, args["path"], args.get("content", ""))
        if name == "run_command":
            return _tool_run_command(root, args["command"], max_bytes, run_timeout)
        return f"error: unknown tool {name!r}"
    except (ValueError, KeyError, TypeError) as e:
        return f"error: {e}"


# --------------------------------------------------------------------------- #
# HTTP + agentic loop
# --------------------------------------------------------------------------- #


def _refuse_live_llm_if_hermetic() -> None:
    """Fail closed under pytest / explicit hermetic flags — never bill providers in tests."""
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        or (os.environ.get("LLG_HERMETIC") or "").strip().lower() in {"1", "true", "yes", "on"}
        or (os.environ.get("COLLAB_HERMETIC") or "").strip().lower() in {"1", "true", "yes", "on"}
    ):
        raise RuntimeError(
            "openai-repo-seat: refusing live LLM call under hermetic/pytest "
            "(LLG_HERMETIC / COLLAB_HERMETIC / PYTEST_CURRENT_TEST). "
            "Unset those only for intentional live seat runs."
        )


def _request_headers(url: str, key: str) -> dict:
    """Auth + content-type, plus xAI's sticky-routing header on *.x.ai hosts only: per xAI's
    caching best practices, ``x-grok-conv-id`` routes same-conversation requests to the same
    server (their cache is per-server). Keyed with the same per-process value as
    ``prompt_cache_key``; other providers never see the header."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "x.ai" or host.endswith(".x.ai"):
        headers["x-grok-conv-id"] = _PROMPT_CACHE_KEY
    return headers


def _post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    _refuse_live_llm_if_hermetic()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=_request_headers(url, key),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _attribution_metadata(model: str, *, feature: str = "repo-seat") -> dict | None:
    """Origin fields for LiteLLM → Langfuse when SERVICE_NAME is set or gateway is used."""
    service = (os.environ.get("SERVICE_NAME") or os.environ.get("LLG_SERVICE") or "").strip()
    if not service and (os.environ.get("LITELLM_VIRTUAL_KEY") or "").strip().startswith("sk-"):
        service = "ibkr-auto-trader"
    if not service:
        return None
    environment = (
        os.environ.get("ENVIRONMENT") or os.environ.get("LLG_ENVIRONMENT") or "development"
    ).strip()
    release = (
        os.environ.get("GIT_SHA")
        or os.environ.get("RELEASE")
        or os.environ.get("LLG_RELEASE")
        or "dev"
    ).strip()
    return {
        "request_id": str(uuid.uuid4()),
        "service": service,
        "feature": feature,
        "environment": environment or "development",
        "release": release or "dev",
        "model_alias": model,
    }


def _with_attribution(payload: dict, model: str, *, feature: str = "repo-seat") -> dict:
    meta = _attribution_metadata(model, feature=feature)
    if meta:
        payload = {**payload, "metadata": meta}
    return payload


# One stable cache key per seat process: every POST of one run shares it so the provider routes all
# steps to the same cache (OpenAI: documented as required for reliable matching on gpt-5.6+; xAI:
# same semantics as the x-grok-conv-id sticky-routing header; Gemini's OpenAI-compat layer ignores
# unknown params). Must NEVER vary within a run — a per-step value would defeat prefix caching.
_PROMPT_CACHE_KEY = "seat-" + uuid.uuid4().hex


def _usage_log_path() -> Path | None:
    """Where per-POST usage telemetry JSONL goes. ``COLLAB_USAGE_LOG`` wins (empty/off/0/false/none
    disables); unset defaults to ``<kit-root>/logs/seat-usage.jsonl`` (gitignored) — except under
    pytest, where the default stays OFF so hermetic tests never write into the repo tree unless
    they point ``COLLAB_USAGE_LOG`` at a tmp_path."""
    raw = os.environ.get("COLLAB_USAGE_LOG")
    if raw is not None:
        val = raw.strip()
        if val.lower() in {"", "off", "0", "false", "none"}:
            return None
        return Path(val)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return Path(__file__).resolve().parents[2] / "logs" / "seat-usage.jsonl"


def _log_usage(api: str, model: str, step: int, data: dict, *, adapter: str = "repo-seat") -> None:
    """Per-POST cache telemetry: one short stderr line + one JSONL record. Never raises.

    ``cached_tokens`` is optional-per-provider (Gemini's OpenAI-compat layer may omit the details
    object entirely) — absence is logged as ``n/a``, never coerced to 0.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    cached = details.get("cached_tokens")
    written = details.get("cache_write_tokens")
    sys.stderr.write(
        f"usage api={api} step={step} "
        f"prompt_tokens={'n/a' if prompt_tokens is None else prompt_tokens} "
        f"cached_tokens={'n/a' if cached is None else cached} "
        f"cache_write_tokens={'n/a' if written is None else written}\n"
    )
    path = _usage_log_path()
    if path is None:
        return
    rec = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "adapter": adapter,
        "api": api,
        "model": model,
        "step": step,
        "prompt_cache_key": _PROMPT_CACHE_KEY,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached,
        "cache_write_tokens": written,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        sys.stderr.write(f"usage log write failed: {e}\n")


def _is_gpt54_plus(model: str) -> bool:
    """True for gpt-5.4, gpt-5.5, gpt-5.6-luna, … — the family that rejects tools on /chat/completions."""
    m = re.match(r"^gpt-5\.(\d+)", (model or "").strip().lower())
    return bool(m and int(m.group(1)) >= 4)


def _should_bridge_to_responses(
    model: str,
    *,
    has_tools: bool,
    reasoning_effort=None,
    reasoning_summary=None,
) -> bool:
    """Choose /responses over /chat/completions for the agentic tool loop.

    Bridges when:

    * ``reasoning_effort`` AND ``reasoning_summary`` are both explicitly set (original older-model
      path), OR
    * model is gpt-5.4+ AND tools are present — **unconditional** for that family.

    The second arm used to also require ``reasoning_effort is not None``. That gate was wrong: the
    platform injects reasoning for gpt-5.6-luna even when the client never sets the field, and chat
    then 400s with *Function tools with reasoning_effort are not supported … use /v1/responses*.
    Detect the family + tools up front instead of burning a failed chat call (2026-07-17).
    """
    if reasoning_effort is not None and reasoning_summary is not None:
        return True
    return bool(has_tools and _is_gpt54_plus(model))


def _chat_rejects_tools_for_responses(code: int, body: str) -> bool:
    """HTTP errors from /chat that mean "retry on /responses", not "hard fail"."""
    b = (body or "").lower()
    if code == 404 and "responses" in b:
        return True
    # 400 invalid_request: tools + reasoning_effort on chat (luna / gpt-5.4+ family).
    return code == 400 and (
        "reasoning_effort" in b or "use /v1/responses" in b or "use the v1/responses" in b
    )


_SYSTEM_TOOL_NOTE = (
    "You have read-only tools to inspect the ACTUAL repository under review: list_dir, read_file, and "
    "search. Before you raise a blocker or sign off, USE them to verify the builder's claims against the "
    "real source — open the files, grep for the symbols the builder names, and confirm a fix exists at a "
    "specific file:line rather than reasoning about the prose. When you have seen enough, write your final "
    "review as an ordinary message with no further tool calls."
)


_SYSTEM_READTEST_NOTE = (
    "You are an ADVERSARIAL VERIFICATION agent (breaker/verifier). You have read-only tools to inspect the "
    "ACTUAL repository (list_dir, read_file, search) AND run_command to run allow-listed checks "
    "(pytest, ruff, python, uv) in the repo root. You are NOT granted a write tool; do NOT intentionally "
    "modify the source you are judging (this is instruction, not a sandbox — the interpreters could write, "
    "so honour it). Run the tests/linters to gather evidence, cite exact file:line "
    "paths, and write your finding/verdict as an ordinary message with no further tool calls."
)


def _run_agentic(
    base: str,
    model: str,
    key: str,
    prompt: str,
    root: Path,
    *,
    timeout: float,
    max_steps: int,
    max_bytes: int,
    tools_spec=None,
    system_note: str = _SYSTEM_TOOL_NOTE,
    run_timeout: float = 600.0,
) -> str:
    """Drive a chat/completions tool-calling loop until the model returns a tool-free message."""
    tools_spec = tools_spec if tools_spec is not None else _TOOLS_SPEC
    messages = [
        {"role": "system", "content": system_note},
        {"role": "user", "content": prompt},
    ]
    url = f"{base}/chat/completions"
    for step in range(1, max_steps + 1):
        payload = _with_attribution(
            {
                "model": model,
                "messages": messages,
                "tools": tools_spec,
                "tool_choice": "auto",
                "prompt_cache_key": _PROMPT_CACHE_KEY,
            },
            model,
        )
        data = _post_json(url, key, payload, timeout)
        _log_usage("chat", model, step, data)
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or ""
        # Echo the assistant turn (with its tool_calls) then answer each call with real file bytes.
        assistant_turn: dict = {"role": "assistant", "content": msg.get("content"), "tool_calls": calls}
        if msg.get("reasoning_content") is not None:
            # xAI reasoning models (grok-4.5) REQUIRE reasoning_content replayed with its turn —
            # omitting it is their documented top cause of prompt-cache misses.
            assistant_turn["reasoning_content"] = msg["reasoning_content"]
        messages.append(assistant_turn)
        for call in calls:
            fn = call.get("function", {})
            try:
                cargs = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                cargs = {}
            result = _dispatch_tool(root, fn.get("name", ""), cargs, max_bytes, run_timeout)
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result[:max_bytes]})
    # Ran out of steps: ask once more, tools OFF, forcing a final written review from what it has seen.
    messages.append(
        {
            "role": "user",
            "content": "You have reached the tool-call budget. Stop inspecting and write "
            "your final review now.",
        }
    )
    data = _post_json(
        url,
        key,
        _with_attribution(
            {
                "model": model,
                "messages": messages,
                # Keep tools in the payload (tool_choice "none" forbids calling them) so this last
                # request still prefix-matches the loop's cached steps — dropping tools changes the
                # provider's cache hash (OpenAI hashes the tools list).
                "tools": tools_spec,
                "tool_choice": "none",
                "prompt_cache_key": _PROMPT_CACHE_KEY,
            },
            model,
        ),
        timeout,
    )
    _log_usage("chat", model, max_steps + 1, data)
    return data["choices"][0]["message"].get("content") or ""


def _extract_responses_text(data: dict) -> str:
    """Pull assistant text out of a /responses payload (output_text convenience field, else message text)."""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if (
                isinstance(c, dict)
                and c.get("type") in ("output_text", "text")
                and isinstance(c.get("text"), str)
            ):
                chunks.append(c["text"])
    return "".join(chunks)


def _to_responses_tools(chat_tools: list) -> list:
    """Convert /chat/completions tool specs ({type,function:{name,...}}) to the flat /responses shape."""
    out = []
    for t in chat_tools:
        f = t.get("function", {})
        out.append(
            {
                "type": "function",
                "name": f.get("name"),
                "description": f.get("description", ""),
                "parameters": f.get("parameters", {}),
            }
        )
    return out


def _run_agentic_responses(
    base: str,
    model: str,
    key: str,
    prompt: str,
    root: Path,
    *,
    timeout: float,
    max_steps: int,
    max_bytes: int,
    tools_spec=None,
    system_note: str = _SYSTEM_TOOL_NOTE,
    run_timeout: float = 600.0,
) -> str:
    """The same agentic tool loop, but over the OpenAI **/responses** API (models that reject
    /chat/completions, e.g. gpt-5.6-terra). Tool calls come back as ``function_call`` output items and are
    answered with ``function_call_output`` input items keyed by ``call_id``; we pass the full growing input
    each turn (no server-side state)."""
    tools = _to_responses_tools(tools_spec if tools_spec is not None else _TOOLS_SPEC)
    url = f"{base}/responses"
    input_items = [{"role": "user", "content": prompt}]
    for step in range(1, max_steps + 1):
        data = _post_json(
            url,
            key,
            _with_attribution(
                {
                    "model": model,
                    "instructions": system_note,
                    "input": input_items,
                    "tools": tools,
                    "tool_choice": "auto",
                    "prompt_cache_key": _PROMPT_CACHE_KEY,
                },
                model,
            ),
            timeout,
        )
        _log_usage("responses", model, step, data)
        output = data.get("output") or []
        calls = [it for it in output if isinstance(it, dict) and it.get("type") == "function_call"]
        if not calls:
            return _extract_responses_text(data)
        # Echo the model's output items back IN ORDER, keeping the `reasoning` items — NOT just the
        # function_calls. A reasoning model (e.g. gpt-5.6-terra) REJECTS a replayed function_call that
        # arrives without its paired reasoning item ("function_call ... was provided without its required
        # 'reasoning' item"). We keep only reasoning + function_call (dropping any assistant message text,
        # which must not sit between the calls and their outputs) so each call's reasoning travels with it.
        input_items.extend(
            it for it in output if isinstance(it, dict) and it.get("type") in ("reasoning", "function_call")
        )
        for call in calls:
            cid = call.get("call_id") or call.get("id")
            try:
                cargs = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                cargs = {}
            result = _dispatch_tool(root, call.get("name", ""), cargs, max_bytes, run_timeout)
            input_items.append({"type": "function_call_output", "call_id": cid, "output": result[:max_bytes]})
    input_items.append(
        {
            "role": "user",
            "content": "You have reached the tool-call budget. Stop and write your final answer now.",
        }
    )
    data = _post_json(
        url,
        key,
        _with_attribution(
            {
                "model": model,
                "instructions": system_note,
                "input": input_items,
                # Keep tools (tool_choice "none") so this last request still prefix-matches the
                # loop's cached steps — see the chat-loop budget-final note.
                "tools": tools,
                "tool_choice": "none",
                "prompt_cache_key": _PROMPT_CACHE_KEY,
            },
            model,
        ),
        timeout,
    )
    _log_usage("responses", model, max_steps + 1, data)
    return _extract_responses_text(data)


def main(argv=None) -> int:
    for _stream in (sys.stdin, sys.stdout, sys.stderr):  # UTF-8 std streams (Windows cp1252 guard)
        _reconf = getattr(_stream, "reconfigure", None)
        if _reconf is not None:
            _reconf(encoding="utf-8", errors="replace")
    _load_dotenv()
    # LiteLLM gateway defaults when LITELLM_VIRTUAL_KEY is set (see openai-compatible-seat).
    _gw = (os.environ.get("LITELLM_VIRTUAL_KEY") or "").strip().startswith("sk-")
    _default_base = (
        os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("SEAT_API_BASE")
        or ("http://localhost:4000/v1" if _gw else "https://api.openai.com/v1")
    )
    _default_model = (
        os.environ.get("LITELLM_MODEL")
        or os.environ.get("SEAT_API_MODEL")
        or ("llm-general" if _gw else "gpt-5.5")
    )
    _default_key_env = "LITELLM_VIRTUAL_KEY" if _gw else "OPENAI_API_KEY"
    p = argparse.ArgumentParser(prog="openai-repo-seat")
    p.add_argument("--base", default=_default_base)
    p.add_argument("--model", default=_default_model)
    p.add_argument("--key-env", default=_default_key_env)
    p.add_argument(
        "--repo-root", required=True, help="repository the model may read (and write with --write)"
    )
    p.add_argument("--timeout", type=float, default=float(os.environ.get("SEAT_API_TIMEOUT", "180")))
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--max-bytes", type=int, default=100_000)
    p.add_argument(
        "--write",
        action="store_true",
        help="enable write_file + run_command (a BUILDER seat); default is read-only",
    )
    p.add_argument(
        "--run-checks",
        action="store_true",
        help="enable run_command ONLY — allow-listed checks, NO write_file tool (a read_test "
        "breaker/verifier seat); NOT a sandbox — interpreters can still write (see ADR-0006). "
        "Ignored if --write is also given.",
    )
    p.add_argument(
        "--run-timeout",
        type=float,
        default=600.0,
        help="per run_command timeout in seconds (only with --write / --run-checks)",
    )
    p.add_argument(
        "--api",
        choices=("auto", "chat", "responses"),
        default="auto",
        help=(
            "which OpenAI-shaped API to drive the tool loop over; 'auto' uses /responses for "
            "gpt-5.4+ with tools (and when reasoning fields are both set), else tries chat and "
            "falls back to responses on 404 / reasoning_effort 400"
        ),
    )
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    key = os.environ.get(args.key_env)
    if not key:
        sys.stderr.write(f"openai-repo-seat: env var {args.key_env} (API key) is not set\n")
        return 2
    root = Path(args.repo_root)
    if not root.is_dir():
        sys.stderr.write(f"openai-repo-seat: --repo-root {args.repo_root!r} is not a directory\n")
        return 2

    # INT-037b: a read_test seat (--run-checks, not --write) runs against an ephemeral copy of the
    # working tree so an allow-listed interpreter write cannot mutate the source it is judging (ADR-0006).
    # --write (builder) seats deliberately operate on the real root.
    _check_cleanup: Callable[[], None] | None = None
    if args.run_checks and not args.write:
        root, _check_cleanup = _isolate_check_root(root)

    prompt = sys.stdin.read()
    base = args.base.rstrip("/")
    try:
        if args.write:
            tools_spec = _TOOLS_SPEC + _WRITE_TOOLS_SPEC  # write_file + run_command
            system_note = _SYSTEM_BUILDER_NOTE
        elif args.run_checks:
            tools_spec = _TOOLS_SPEC + _RUN_COMMAND_SPEC  # run_command only — no write_file
            system_note = _SYSTEM_READTEST_NOTE
        else:
            tools_spec = _TOOLS_SPEC
            system_note = _SYSTEM_TOOL_NOTE
        kw = dict(
            timeout=args.timeout,
            max_steps=args.max_steps,
            max_bytes=args.max_bytes,
            tools_spec=tools_spec,
            system_note=system_note,
            run_timeout=args.run_timeout,
        )
        has_tools = bool(tools_spec)
        bridge = _should_bridge_to_responses(args.model, has_tools=has_tools)
        if args.api == "responses" or (args.api == "auto" and bridge):
            if args.api == "auto" and bridge:
                sys.stderr.write(
                    f"openai-repo-seat: bridging {args.model!r}+tools to /responses "
                    f"(gpt-5.4+ family or explicit reasoning fields)\n"
                )
            out = _run_agentic_responses(base, args.model, key, prompt, root, **kw)
        elif args.api == "chat":
            out = _run_agentic(base, args.model, key, prompt, root, **kw)
        else:  # auto, no bridge: try chat; fall back when the model demands /responses
            try:
                out = _run_agentic(base, args.model, key, prompt, root, **kw)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if _chat_rejects_tools_for_responses(e.code, body):
                    sys.stderr.write(
                        "openai-repo-seat: model rejects /chat/completions; retrying /responses\n"
                    )
                    out = _run_agentic_responses(base, args.model, key, prompt, root, **kw)
                else:
                    sys.stderr.write(f"api error {e.code}: {body[:500]}\n")
                    return 1
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"api error {e.code}: {e.read().decode('utf-8', 'replace')[:500]}\n")
        return 1
    except (KeyError, IndexError, TypeError) as e:
        sys.stderr.write(f"unexpected response shape: {e}\n")
        return 1
    except Exception as e:  # network/timeout/JSON — fail the round, don't emit a partial reply
        sys.stderr.write(f"api call failed: {e}\n")
        return 1
    finally:
        if _check_cleanup is not None:
            _check_cleanup()  # remove the ephemeral check-root copy (INT-037b)

    sys.stdout.write(out or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
