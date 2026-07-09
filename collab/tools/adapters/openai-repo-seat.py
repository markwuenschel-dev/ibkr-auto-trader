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

Read-only by design: there is no write/edit/exec tool, so a review seat can inspect the code but never
mutate it. Stdlib only (no pip / no openai SDK) — function-calling is done over raw POST /chat/completions.

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
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Directories that are never worth serving to a code reviewer — noise, vendored, or VCS internals.
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".idea", ".vscode"}
_TEXT_SNIFF_BYTES = 2048  # read this many bytes to decide if a file is text (NUL byte => binary)


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
            key = key[len("export "):].strip()
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


def _tool_read_file(root: Path, path: str, start_line: int = 1, max_lines: int = 600,
                    max_bytes: int = 100_000) -> str:
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
        row = f"{i:>6}\t{lines[i-1]}"
        size += len(row) + 1
        if size > max_bytes:
            out.append(f"… [truncated at {max_bytes} bytes; call read_file with start_line={i} for more]")
            break
        out.append(row)
    tail = "" if end >= len(lines) else f"\n… [{len(lines)-end} more lines below]"
    return f"{path} (lines {start}-{min(end, len(lines))} of {len(lines)}):\n" + "\n".join(out) + tail


def _tool_search(root: Path, query: str, glob: str = "**/*.py", max_results: int = 60,
                 max_bytes: int = 100_000) -> str:
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


_TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and subdirectories in the repository, relative to the repo root.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to repo root; '.' for root."}}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the repository, returned with 1-based line numbers so you can cite file:line.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "start_line": {"type": "integer", "description": "First line to read (default 1)."},
            "max_lines": {"type": "integer", "description": "How many lines (default 600)."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search the repository for a literal substring (e.g. a function or symbol name); returns file:line matches.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Literal substring to find."},
            "glob": {"type": "string", "description": "Path glob to scope the search (default '**/*.py')."},
            "max_results": {"type": "integer"}},
            "required": ["query"]}}},
]


def _dispatch_tool(root: Path, name: str, args: dict, max_bytes: int) -> str:
    """Execute a tool call against the real filesystem. Any error becomes a returned string (never an
    exception) so one bad model-supplied argument fails that call, not the whole round."""
    try:
        if name == "list_dir":
            return _tool_list_dir(root, args.get("path", "."))
        if name == "read_file":
            return _tool_read_file(root, args["path"], args.get("start_line", 1),
                                   args.get("max_lines", 600), max_bytes)
        if name == "search":
            return _tool_search(root, args["query"], args.get("glob", "**/*.py"),
                                args.get("max_results", 60), max_bytes)
        return f"error: unknown tool {name!r}"
    except (ValueError, KeyError, TypeError) as e:
        return f"error: {e}"


# --------------------------------------------------------------------------- #
# HTTP + agentic loop
# --------------------------------------------------------------------------- #

def _post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_SYSTEM_TOOL_NOTE = (
    "You have read-only tools to inspect the ACTUAL repository under review: list_dir, read_file, and "
    "search. Before you raise a blocker or sign off, USE them to verify the builder's claims against the "
    "real source — open the files, grep for the symbols the builder names, and confirm a fix exists at a "
    "specific file:line rather than reasoning about the prose. When you have seen enough, write your final "
    "review as an ordinary message with no further tool calls."
)


def _run_agentic(base: str, model: str, key: str, prompt: str, root: Path, *,
                 timeout: float, max_steps: int, max_bytes: int) -> str:
    """Drive a chat/completions tool-calling loop until the model returns a tool-free message."""
    messages = [
        {"role": "system", "content": _SYSTEM_TOOL_NOTE},
        {"role": "user", "content": prompt},
    ]
    url = f"{base}/chat/completions"
    for _ in range(max_steps):
        data = _post_json(url, key, {"model": model, "messages": messages,
                                     "tools": _TOOLS_SPEC, "tool_choice": "auto"}, timeout)
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or ""
        # Echo the assistant turn (with its tool_calls) then answer each call with real file bytes.
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            try:
                cargs = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                cargs = {}
            result = _dispatch_tool(root, fn.get("name", ""), cargs, max_bytes)
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": result[:max_bytes]})
    # Ran out of steps: ask once more, tools OFF, forcing a final written review from what it has seen.
    messages.append({"role": "user", "content":
                     "You have reached the tool-call budget. Stop inspecting and write your final review now."})
    data = _post_json(url, key, {"model": model, "messages": messages}, timeout)
    return data["choices"][0]["message"].get("content") or ""


def main(argv=None) -> int:
    for _stream in (sys.stdin, sys.stdout, sys.stderr):  # UTF-8 std streams (Windows cp1252 guard)
        _reconf = getattr(_stream, "reconfigure", None)
        if _reconf is not None:
            _reconf(encoding="utf-8", errors="replace")
    _load_dotenv()
    p = argparse.ArgumentParser(prog="openai-repo-seat")
    p.add_argument("--base", default=os.environ.get("SEAT_API_BASE", "https://api.openai.com/v1"))
    p.add_argument("--model", default=os.environ.get("SEAT_API_MODEL", "gpt-5.5"))
    p.add_argument("--key-env", default="OPENAI_API_KEY")
    p.add_argument("--repo-root", required=True, help="repository the model may read (read-only)")
    p.add_argument("--timeout", type=float, default=float(os.environ.get("SEAT_API_TIMEOUT", "180")))
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--max-bytes", type=int, default=100_000)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    key = os.environ.get(args.key_env)
    if not key:
        sys.stderr.write(f"openai-repo-seat: env var {args.key_env} (API key) is not set\n")
        return 2
    root = Path(args.repo_root)
    if not root.is_dir():
        sys.stderr.write(f"openai-repo-seat: --repo-root {args.repo_root!r} is not a directory\n")
        return 2

    prompt = sys.stdin.read()
    base = args.base.rstrip("/")
    try:
        out = _run_agentic(base, args.model, key, prompt, root,
                           timeout=args.timeout, max_steps=args.max_steps, max_bytes=args.max_bytes)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"api error {e.code}: {e.read().decode('utf-8', 'replace')[:500]}\n")
        return 1
    except (KeyError, IndexError, TypeError) as e:
        sys.stderr.write(f"unexpected response shape: {e}\n")
        return 1
    except Exception as e:  # network/timeout/JSON — fail the round, don't emit a partial reply
        sys.stderr.write(f"api call failed: {e}\n")
        return 1

    sys.stdout.write(out or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
