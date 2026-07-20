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
  ["python", "C:\\\\Users\\\\Nalakram\\\\Documents\\\\GitHub\\\\collab-kit\\\\tools\\\\adapters\\\\"
   "openai-compatible-seat.py",
   "--base", "https://api.x.ai/v1", "--model", "grok-4", "--key-env", "XAI_API_KEY"]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
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
            key = key[len("export ") :].strip()
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def _refuse_live_llm_if_hermetic() -> None:
    """Fail closed under pytest / explicit hermetic flags — never bill providers in tests."""
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        or (os.environ.get("LLG_HERMETIC") or "").strip().lower() in {"1", "true", "yes", "on"}
        or (os.environ.get("COLLAB_HERMETIC") or "").strip().lower() in {"1", "true", "yes", "on"}
    ):
        raise RuntimeError(
            "openai-compatible-seat: refusing live LLM call under hermetic/pytest "
            "(LLG_HERMETIC / COLLAB_HERMETIC / PYTEST_CURRENT_TEST). "
            "Unset those only for intentional live seat runs."
        )


def _request_headers(url: str, key: str) -> dict:
    """Auth + content-type, plus xAI's sticky-routing header on *.x.ai hosts only (see
    openai-repo-seat for the contract; identical). Keyed with the same per-process value as
    ``prompt_cache_key``."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "x.ai" or host.endswith(".x.ai"):
        headers["x-grok-conv-id"] = _PROMPT_CACHE_KEY
    return headers


def _post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    """POST JSON with a bearer token; return the parsed response. Raises urllib.error.HTTPError on 4xx/5xx."""
    _refuse_live_llm_if_hermetic()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=_request_headers(url, key),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _attribution_metadata(model: str, *, feature: str = "seat") -> dict | None:
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


# One stable cache key per seat process (same contract as openai-repo-seat: never varies within a
# run; OpenAI needs it for reliable matching on gpt-5.6+, xAI treats it as sticky routing, Gemini's
# OpenAI-compat layer ignores unknown params).
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


def _log_usage(api: str, model: str, step: int, data: dict, *, adapter: str = "compatible-seat") -> None:
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


def _chat(base: str, model: str, key: str, prompt: str, timeout: float) -> str:
    """Chat Completions API (Grok, Gemini's OpenAI-compat, older OpenAI models, Ollama, ...)."""
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "prompt_cache_key": _PROMPT_CACHE_KEY,
    }
    meta = _attribution_metadata(model, feature="compatible-seat")
    if meta:
        payload["metadata"] = meta
    data = _post_json(f"{base}/chat/completions", key, payload, timeout)
    _log_usage("chat", model, 1, data)
    return data["choices"][0]["message"]["content"] or ""


def _responses(base: str, model: str, key: str, prompt: str, timeout: float) -> str:
    """Responses API (newer OpenAI models like gpt-5-codex that reject /chat/completions)."""
    payload: dict = {"model": model, "input": prompt, "prompt_cache_key": _PROMPT_CACHE_KEY}
    meta = _attribution_metadata(model, feature="compatible-seat")
    if meta:
        payload["metadata"] = meta
    data = _post_json(f"{base}/responses", key, payload, timeout)
    _log_usage("responses", model, 1, data)
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
            if (
                isinstance(c, dict)
                and c.get("type") in ("output_text", "text")
                and isinstance(c.get("text"), str)
            ):
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
    # LiteLLM gateway: when LITELLM_VIRTUAL_KEY is a real sk-… key, default seats to the proxy
    # so collab can share one controlled endpoint without raw provider keys in seats.json.
    _gw = (os.environ.get("LITELLM_VIRTUAL_KEY") or "").strip().startswith("sk-")
    _default_base = (
        os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("SEAT_API_BASE")
        or ("http://localhost:4000/v1" if _gw else "https://api.x.ai/v1")
    )
    _default_model = (
        os.environ.get("LITELLM_MODEL")
        or os.environ.get("SEAT_API_MODEL")
        or ("llm-general" if _gw else "grok-4")
    )
    _default_key_env = "LITELLM_VIRTUAL_KEY" if _gw else "SEAT_API_KEY"
    p = argparse.ArgumentParser(prog="openai-compatible-seat")
    p.add_argument("--base", default=_default_base)
    p.add_argument("--model", default=_default_model)
    p.add_argument("--key-env", default=_default_key_env)
    p.add_argument(
        "--api",
        choices=("auto", "chat", "responses"),
        default="auto",
        help="which OpenAI-shaped API to call; 'auto' tries chat then falls back to responses on a 404",
    )
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
                    sys.stderr.write(
                        "openai-compatible-seat: model requires the Responses API; retrying /responses\n"
                    )
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
