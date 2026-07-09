#!/usr/bin/env python3
"""openai-compatible-seat — a collab-kit autopilot backend for ANY OpenAI-compatible chat API.

Works with xAI/Grok, OpenAI/Codex, Gemini's OpenAI-compat endpoint, local Ollama, etc. — anything that
speaks POST /chat/completions. Stdlib only (no pip install), so it drops straight into seats.json.

The autopilot backend contract is exactly: read the prompt on STDIN, write the reply to STDOUT, exit 0.
This wrapper does that; on any failure it writes to stderr and exits non-zero, so the driver's CollabError
path fires and the round fails safely (inbound stays claimed).

Per-seat config comes from argv (so two seats can use different models); only the secret comes from env:
  --base <url>       API base, e.g. https://api.x.ai/v1  (default env SEAT_API_BASE, else xAI)
  --model <name>     e.g. grok-4 / gpt-5-codex / gemini-2.5-pro
  --key-env <VAR>    name of the env var holding the bearer token (default: SEAT_API_KEY)
  --timeout <secs>   HTTP timeout (default 120)

Example seats.json cmd (one entry per seat):
  ["python", "C:\\\\Users\\\\Nalakram\\\\Documents\\\\GitHub\\\\collab-kit\\\\tools\\\\adapters\\\\openai-compatible-seat.py",
   "--base", "https://api.x.ai/v1", "--model", "grok-4", "--key-env", "XAI_API_KEY"]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv() -> None:
    """Load API keys from a ``.env`` file into the environment (stdlib, no python-dotenv).

    Search order: ``$SEAT_ENV_FILE`` if set, else ``.env`` at the collab-kit root (two dirs up from this
    adapter). ``KEY=value`` per line; ``#`` comments and blank lines ignored; optional surrounding quotes
    and a leading ``export`` are stripped. A value already present in the real environment WINS (so a
    shell-exported key overrides the file), which is why we use ``setdefault``.
    """
    override = os.environ.get("SEAT_ENV_FILE")
    path = Path(override) if override else Path(__file__).resolve().parents[2] / ".env"
    try:
        text = path.read_text("utf-8")
    except OSError:
        return  # no .env — rely on the real environment
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def _post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    """POST JSON with a bearer token; return the parsed response. Raises urllib.error.HTTPError on 4xx/5xx."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _chat(base: str, model: str, key: str, prompt: str, timeout: float) -> str:
    """Chat Completions API (Grok, Gemini's OpenAI-compat, older OpenAI models, Ollama, ...)."""
    data = _post_json(f"{base}/chat/completions", key,
                      {"model": model, "messages": [{"role": "user", "content": prompt}]}, timeout)
    return data["choices"][0]["message"]["content"] or ""


def _responses(base: str, model: str, key: str, prompt: str, timeout: float) -> str:
    """Responses API (newer OpenAI models like gpt-5-codex that reject /chat/completions)."""
    data = _post_json(f"{base}/responses", key, {"model": model, "input": prompt}, timeout)
    return _extract_responses_text(data)


def _extract_responses_text(data: dict) -> str:
    """Pull assistant text out of a Responses payload: prefer the ``output_text`` convenience field, else
    concatenate ``output_text`` chunks from ``message`` items (skipping reasoning/tool items)."""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                chunks.append(c["text"])
    return "".join(chunks)


def main(argv=None) -> int:
    # Force UTF-8 on the std streams. On Windows the console/pipe defaults to cp1252,
    # so a non-Latin-1 char in the prompt (stdin) or the model's reply (stdout) would
    # raise UnicodeEncodeError and crash the seat (exited-1 backend failure). errors=
    # "replace" keeps a stray un-encodable byte from ever failing the round.
    for _stream, _mode in ((sys.stdin, "r"), (sys.stdout, "w"), (sys.stderr, "w")):
        _reconf = getattr(_stream, "reconfigure", None)
        if _reconf is not None:
            _reconf(encoding="utf-8", errors="replace")
    _load_dotenv()
    p = argparse.ArgumentParser(prog="openai-compatible-seat")
    p.add_argument("--base", default=os.environ.get("SEAT_API_BASE", "https://api.x.ai/v1"))
    p.add_argument("--model", default=os.environ.get("SEAT_API_MODEL", "grok-4"))
    p.add_argument("--key-env", default="SEAT_API_KEY")
    p.add_argument("--api", choices=("auto", "chat", "responses"), default="auto",
                   help="which OpenAI-shaped API to call; 'auto' tries chat then falls back to responses on a 404")
    p.add_argument("--timeout", type=float, default=float(os.environ.get("SEAT_API_TIMEOUT", "120")))
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    key = os.environ.get(args.key_env)
    if not key:
        sys.stderr.write(f"openai-compatible-seat: env var {args.key_env} (API key) is not set\n")
        return 2

    prompt = sys.stdin.read()  # the whole prompt (seat system + inbound handoff content) arrives here
    base = args.base.rstrip("/")
    try:
        if args.api == "responses":
            out = _responses(base, args.model, key, prompt, args.timeout)
        elif args.api == "chat":
            out = _chat(base, args.model, key, prompt, args.timeout)
        else:  # auto: chat, but fall back to the Responses API if the model demands it
            try:
                out = _chat(base, args.model, key, prompt, args.timeout)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 404 and "responses" in body.lower():
                    sys.stderr.write("openai-compatible-seat: model requires the Responses API; retrying /responses\n")
                    out = _responses(base, args.model, key, prompt, args.timeout)
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

    sys.stdout.write(out or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
