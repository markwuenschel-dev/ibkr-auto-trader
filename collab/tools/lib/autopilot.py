"""autopilot — bounded, agent-agnostic driver that closes the builder<->reviewer loop (collab-kit slice 6).

The transport (file protocol) and trigger (watcher) already exist; this is the missing *drive* piece. It
takes a ``pending`` handoff addressed to a seat, hands the seat's agent the prompt, captures the response,
and posts it back as a new handoff to the other seat — with no human shuttling files by hand.

Agent-agnostic by design ([C34]): a backend is a **command template** — an argv that reads the prompt on
**stdin** and returns text on **stdout**. One mechanism covers headless ``claude -p -``, Grok, Codex,
Gemini, Cursor; adding one is a ``seats.json`` entry, not code. A seat with **no** CLI backend is the
human/web seat — the driver leaves its handoffs alone so they go out over the Telegram bridge.

Safety posture:
  * [C35] **bounded** — every attempt is charged against a named ``RunBudget`` counter (work attempts,
    review decisions per candidate, verification passes, total model calls, wall-clock; ADR-0002); the loop
    cannot ping-pong forever. On exhaustion — or a fix that makes no progress — it writes a durable
    escalation record + a pause note to the outbox and stops. (``--max-rounds`` is a deprecated alias for
    the work-attempt budget.) A board-level ``ActiveHandoffLease`` (ADR-0003 D2) enforces one live driver.
  * [C36] **no irreversible step without a satisfied evidence contract** (§18, autonomous rev) — the driver
    ``claim``s (reversible) and ``create``s freely; it reaches ``done/`` ONLY via an autonomous reviewer
    sign-off whose §18.3 Autonomous Done-Transition Contract is satisfied (independent approver + clean
    verification ledger + source==tested). It still never archives, commits, or writes outside the collab.
    Separation of authority — not a human gate — is the boundary: no seat may approve its own work.
  * [C38] **agent output is untrusted DATA, never control-plane** — stdout is size-capped and
    control-char-stripped, then stored as a *separate artifact file*; the typed reply handoff body is only
    a safe ``AUTOPILOT_REPLY <path>`` pointer. Agent output can never forge a ``## Constraints`` section.
  * [C39] **backend isolation** — invoked via argv (no ``shell=True`` -> no shell injection) with the prompt
    on stdin (no arg injection) under a timeout; a hung/failing agent fails that one round safely.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from dataclasses import replace
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
sys.path.insert(0, _LIB)
import adapter_profiles  # noqa: E402
import candidate_assessment as ca  # noqa: E402
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import escalation as esc  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402
import operator_requests as opreq  # noqa: E402
import run_budget as rb  # noqa: E402
import transitions as _transitions  # noqa: E402
from verification import AUTHORITATIVE_ARGV as _AUTHORITATIVE_ARGV  # noqa: E402


def _load_local(alias: str, filename: str):
    """Load a sibling module by path — immune to stdlib name-shadowing (our ``trace.py`` vs the
    stdlib ``trace``; a plain ``import trace`` would bind whichever is already in sys.modules)."""
    spec = importlib.util.spec_from_file_location(alias, Path(_LIB) / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_local("collab_trace", "trace.py")

_MAX_RESP_BYTES = 256 * 1024  # process-boundary cap on an agent's stdout ([C38] — a runaway can't OOM us)
_MAX_STDERR_BYTES = 64 * 1024  # process-boundary cap on an agent's stderr
_DEFAULT_TIMEOUT = 300.0  # per-backend-invocation wall clock ([C39])
_POINTER_RE = re.compile(r"^AUTOPILOT_REPLY (\S+)\s*$", re.M)

# Reviewer sign-off token (§18). A seat marked "can_sign_off": true in seats.json ASSERTS the §18.3 evidence
# contract holds for the handoff it reviewed by ending its reply with this token alone on a line. The token
# is NECESSARY BUT NOT SUFFICIENT ([C36]/[C38]): the machine (done_contract.evaluate), not the token,
# advances state — done/ requires an independent approver + a clean verification ledger + source==tested.
# The token is deliberately distinctive so it can't be tripped by prose ("I won't sign off" never matches).
_SIGNOFF_RE = re.compile(r"^\s*\[\[\s*SIGN-?OFF\s*\]\]\s*$", re.IGNORECASE | re.M)
_SIGNED_OFF = object()  # run_round sentinel: a reviewing seat approved -> run() ends the loop cleanly


# --------------------------------------------------------------------------- #
# observability: event log + live status + human control ([C15]/[C36])
# --------------------------------------------------------------------------- #
#
# The driver is otherwise invisible: it prints one line per round and — because it calls hc.claim/
# hc.create directly, not via the CLI — emits NOTHING to the audit log. These helpers close that gap
# so a decoupled dashboard can watch a run. They are STRICTLY observability: every emit/status write
# is best-effort (``_emit_safe``), so a contended log lock or a busy status file can never fail a
# committed round ([C15]). The one thing that can influence the loop, ``control.json``, only ever
# makes it *pause/stop* (reversible, [C36]) — it never advances or deletes a handoff.


def _run_id(collab) -> str:
    """Slug of the collab dir — the ``run_id`` grouping key, shared with the manual CLI's events."""
    try:
        return cc.slugify(Path(collab).name or "collab")
    except ValueError:
        return "collab"


def _log_default(collab) -> str:
    """The append-only event log a dashboard tails: ``<collab>/logs/events.jsonl``."""
    return str(Path(collab) / "logs" / "events.jsonl")


def _emit_safe(fn, *args, **kwargs) -> None:
    """Telemetry is observability, not correctness ([C15]). A committed state change must NEVER be
    reported as failed because an append-only audit write (or its lock) failed. Warn and continue."""
    try:
        fn(*args, **kwargs)
    except Exception as e:  # deliberately broad: emit is fire-and-forget, state already committed
        print(f"[autopilot] telemetry emit failed (state change already committed): {e}", file=sys.stderr)


def _status_path(collab) -> Path:
    return Path(collab) / "autopilot" / "status.json"


def _control_path(collab) -> Path:
    return Path(collab) / "autopilot" / "control.json"


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_status(collab, **fields) -> None:
    """Merge ``fields`` into ``<collab>/autopilot/status.json`` and atomically re-publish it — the
    cheap 'what is the driver doing RIGHT NOW' surface. Best-effort: any failure is swallowed so a
    locked/busy status file never breaks a round."""
    try:
        p = _status_path(collab)
        p.parent.mkdir(parents=True, exist_ok=True)  # atomic_write does not create parents
        cur: dict = {}
        try:
            cur = json.loads(p.read_text("utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except OSError, ValueError:
            cur = {}
        cur.setdefault("schema_version", "0.1")
        cur.update(fields)
        cur["collab"] = str(collab)
        cur["updated_ts"] = _now_utc()
        cc.safe_write(p, json.dumps(cur, separators=(",", ":")) + "\n")
    except Exception as e:  # observability must never break the loop ([C15])
        print(f"[autopilot] status write failed: {e}", file=sys.stderr)


_HEARTBEAT_S = (
    5.0  # while a backend call is in flight, refresh the status heartbeat this often (visible liveness)
)


class _Heartbeat:
    """Refresh ``status.updated_ts`` every ``_HEARTBEAT_S`` from a daemon thread while a backend call is in
    flight. This lets the dashboard tell a driver that is *alive and working through a long agentic call*
    (fresh heartbeat) from one that *crashed mid-call* (heartbeat stopped) — independent of how long the
    call legitimately runs. The refresh writes no fields, so ``_write_status``'s merge preserves the round's
    ``phase``/``active_seat``/``active_since``/``timeout``. Elapsed-vs-timeout is read from ``active_since``,
    NOT ``updated_ts`` (which the heartbeat resets). Best-effort ([C15]); a hung child is still bounded by
    the backend timeout, not by this liveness signal."""

    def __init__(self, collab, interval: float = _HEARTBEAT_S):
        self._collab = collab
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self._interval):
            _write_status(self._collab)  # updated_ts only; phase/active_seat/timeout preserved

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False


_LEASE_RENEW_S = 30.0  # comfortably inside handoff_core._LEASE_TTL_S (90s), so a tick can be missed safely


class _LeaseRenewer:
    """Keep the board lease's heartbeat fresh for the WHOLE run, from one daemon thread.

    ``handoff_core`` documents the lease as "renewed by the driver heartbeat", but nothing renewed it: a
    healthy driver's lease went stale ``_LEASE_TTL_S`` (90 s) after ``acquire`` — i.e. during the first
    agentic call of every real run. Two things then break, and the second is the dangerous one:
      1. :func:`dashboard_core.driver_running` reports "no driver running" while the driver is working.
      2. :func:`dashboard_core.start_driver` only refuses while a LIVE lease is held, so pressing Start
         spawns a SECOND driver onto the same board — voiding ADR-0003 D2 exclusivity — and any other run
         may reclaim the "stale" lease and claim the same handoff.

    Run-lifetime rather than per-call: the gaps *between* calls count against the same TTL. ``renew()`` is
    a no-op returning False whenever this run doesn't currently hold the lease (before ``acquire``, after
    ``release``), so this is safe to run for the entire loop. Best-effort ([C15]) — a renew failure must
    never take the run down; the loop's own lease checks remain authoritative.
    """

    def __init__(self, lease, interval: float = _LEASE_RENEW_S):
        self._lease = lease
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self._lease is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self._interval):
            with suppress(Exception):
                self._lease.renew()

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False


def _read_control(collab) -> dict:
    """Read the dashboard-written control file. Missing/corrupt -> the safe default (running, not
    stopped). The driver only ever *obeys a pause/stop* from here; it never takes an action from it.

    ``max_rounds`` is the one live-tunable knob: a positive int here raises/lowers the per-thread cap
    mid-run (the dashboard's "give it more turns" affordance). A missing/non-positive/non-int value is
    ``None`` -> the driver keeps its launch-time ``max_rounds`` fallback. Still reversible/bounded ([C35]):
    it only ever changes the ceiling on an already-bounded loop; it can never advance or delete a handoff."""
    default = {"paused": False, "stop": False, "max_rounds": None}
    try:
        doc = json.loads(_control_path(collab).read_text("utf-8"))
        if not isinstance(doc, dict):
            return default
        mr = doc.get("max_rounds")
        mr = mr if (isinstance(mr, int) and not isinstance(mr, bool) and mr > 0) else None
        return {
            "paused": bool(doc.get("paused", False)),
            "stop": bool(doc.get("stop", False)),
            "max_rounds": mr,
        }
    except OSError, ValueError:
        return default


# --------------------------------------------------------------------------- #
# seats config
# --------------------------------------------------------------------------- #


def _seats_file(home) -> Path:
    return Path(home) / "seats.json"


def load_seats(home) -> dict:
    """Load ``seats.json`` into ``{seat: {backend, cmd, system, timeout}}``.

    Refuse corrupt configuration ([data-integrity]).

    A missing file is an empty config (no seat is automated — every handoff waits for a human). A file that
    exists but is unparseable/mis-shaped raises rather than silently driving nothing or the wrong thing.

    Model catalog: if a seat declares ``"model": "<id>"``, its runnable ``cmd`` is composed from the
    top-level ``"models"`` catalog entry (``cmd`` template + optional ``unset_env``) plus the seat's own
    ``"model_args"`` (role-specific tail, e.g. ``--repo-root`` or the builder's permission flags). This is
    what makes "any model in any seat" a one-field edit — the dashboard just rewrites a seat's ``model``.
    A seat with an explicit ``cmd`` and no ``model`` is used verbatim (backward compatible).
    """
    f = _seats_file(home)
    try:
        raw = f.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise cc.CollabError(f"cannot read seats config {f}: {e}") from e
    try:
        doc = json.loads(raw)
        seats = doc["seats"] if isinstance(doc, dict) else None
        if not isinstance(seats, dict):
            raise ValueError("missing 'seats' object")
    except (ValueError, KeyError, TypeError) as e:
        raise cc.CollabError(f"corrupt seats config {f}: {e}") from e
    models = doc.get("models") if isinstance(doc.get("models"), dict) else {}
    for name, cfg in seats.items():
        if not isinstance(cfg, dict):
            continue
        # compile_seat (ADR-0003 D3) resolves the model's adapter and renders a capability-checked
        # argv: a managed seat (declares role/access) gets adapter-generated flags; a legacy
        # model_args seat is composed as before but REFUSED if it carries a flag the adapter would
        # choke on (the 030 --permission-mode crash). Enforced again here at run start, not only at
        # dashboard-save time.
        compiled = adapter_profiles.compile_seat(name, cfg, models)
        if compiled.get("cmd") is not None:
            cfg["cmd"] = compiled["cmd"]  # composed runnable argv
        if compiled.get("unset_env") is not None and "unset_env" not in cfg:
            cfg["unset_env"] = compiled["unset_env"]  # inherit the model's env drops (e.g. subscription)
        cfg["adapter"] = compiled["adapter"]
        cfg["switchable"] = compiled["switchable"]
        if compiled.get("policy") is not None:
            cfg["policy"] = compiled["policy"]
    return seats


def load_models(home) -> dict:
    """The top-level ``"models"`` catalog from ``seats.json`` -> ``{id: {cmd, unset_env?}}`` (``{}`` if none).

    Read-only view for the dashboard's per-seat model picker. Never raises on a missing catalog — an
    absent/empty ``"models"`` block simply means no selectable models (seats then use explicit ``cmd``)."""
    f = _seats_file(home)
    try:
        doc = json.loads(f.read_text("utf-8"))
    except OSError, ValueError:
        return {}
    models = doc.get("models") if isinstance(doc, dict) else None
    return models if isinstance(models, dict) else {}


def _resolve_assurance_plan(home, guardrails):
    """Resolve the v2 assurance policy once, before candidate identity and dispatch.

    Legacy test fixtures and existing local catalogs without ``assessment_profiles`` deliberately
    retain the old runner until their operator migrates from ``seats.example.json``.  A catalog that
    opts into profiles, however, is fail-closed: malformed profiles never silently downgrade to the
    legacy generic fan-out.
    """
    try:
        raw = _seats_file(home).read_text("utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise cc.CollabError(f"cannot read assurance seats configuration: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise cc.CollabError(f"cannot parse assurance seats configuration: {exc}") from exc
    if not isinstance(doc, dict) or "assessment_profiles" not in doc:
        return None
    try:
        import verification_plan as vp

        lanes_file = Path(cc.resolve_kit_root()) / "telemetry" / "lanes.json"
        lanes_doc = json.loads(lanes_file.read_text("utf-8"))
        return vp.resolve_verification_plan(lanes_doc, doc, guardrails=guardrails)
    except (OSError, ValueError, cc.CollabError) as exc:
        raise cc.CollabError(f"invalid risk-tiered assurance configuration: {exc}") from exc


def _to_from(path: Path) -> tuple:
    """``(to, from)`` from a handoff's frontmatter (via ``contracts``), or ``(None, None)`` if unparseable.

    ``list_handoffs`` returns only ``id/slug/state/path``; routing lives in the frontmatter, so we parse
    the file (same as the watcher). An unreadable/malformed file is skipped, never fatal."""
    try:
        fm = contracts.parse_handoff(path).get("frontmatter") or {}
    except Exception:
        return None, None
    return (fm.get("to") or "").strip(), (fm.get("from") or "").strip()


def _cli_seat(seats: dict, seat: str) -> dict | None:
    """The seat's config iff it is an automatable CLI backend with a non-empty argv, else None."""
    cfg = seats.get(seat)
    if not isinstance(cfg, dict) or cfg.get("backend") != "cli":
        return None
    cmd = cfg.get("cmd")
    if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
        return None
    return cfg


# --------------------------------------------------------------------------- #
# untrusted agent output ([C38])
# --------------------------------------------------------------------------- #


def _sanitize(text: str) -> str:
    """Bound + de-control an agent's stdout before it is ever persisted. Keeps ``\\n``/``\\t`` (it's prose),
    drops other control chars (NUL, escapes), and caps size. This is DATA, so structure is allowed here —
    the safety comes from it living in an artifact file, never a parsed handoff body."""
    clean = "".join(c for c in text if c >= " " or c in "\n\t")
    return clean[:_MAX_RESP_BYTES]


def _replies_dir(collab) -> Path:
    return Path(collab) / "autopilot" / "replies"


def _write_reply(collab, seat: str, text: str) -> str:
    """Persist a sanitized agent response as an artifact; return its collab-relative path (POSIX)."""
    d = _replies_dir(collab)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{cc.slugify(seat)}.md"
    cc.safe_write(d / name, text if text.endswith("\n") else text + "\n")
    return f"autopilot/replies/{name}"


# --------------------------------------------------------------------------- #
# prompt assembly
# --------------------------------------------------------------------------- #


def _substance(collab, path: Path) -> str:
    """The content an agent should respond to: the referenced reply artifact if the handoff is an autopilot
    pointer, else the handoff file text. The artifact path is constrained to ``<collab>/autopilot/replies/``
    so a hand-crafted ``AUTOPILOT_REPLY ../../secret`` in a body cannot read outside the collab ([C28])."""
    try:
        with open(path, "rb") as fh:
            text = fh.read(_MAX_RESP_BYTES).decode("utf-8", errors="replace")  # bounded read
    except OSError:
        return ""
    m = _POINTER_RE.search(text)
    if not m:
        return text
    base = _replies_dir(collab).resolve()
    art = (Path(collab) / m.group(1)).resolve()
    if base not in art.parents:  # pointer escapes the replies dir -> ignore it, use the handoff text
        return text
    try:
        with open(art, "rb") as fh:
            return fh.read(_MAX_RESP_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return text


def _build_prompt(system: str | None, content: str) -> str:
    head = (system.strip() + "\n\n") if system else ""
    return f"{head}{content}"


def _reply_title(seat: str, round_no: int) -> str:
    return f"autopilot {cc.slugify(seat)} reply r{round_no}"


def _carry_guardrails(path, guardrails) -> None:
    """Propagate a slice's ``guardrails`` frontmatter onto a reply handoff, so when that reply is the one
    a reviewer signs off, the done-contract runs the SAME adversarial lanes. Without this the auto-generated
    reply has no guardrails, ``required_lanes`` is empty, and the lanes silently never run ([C42])."""
    if not guardrails:
        return
    try:
        txt = Path(path).read_text("utf-8")
        if re.search(r"(?m)^guardrails:", txt):  # already present — don't double-write
            return
        gl = "guardrails: [" + ", ".join(str(g) for g in guardrails) + "]\n"
        Path(path).write_text(re.sub(r"(?m)^(status:.*\n)", r"\1" + gl, txt, count=1), "utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# backend invocation ([C39])
# --------------------------------------------------------------------------- #


def _feed_stdin(proc, data: bytes) -> None:
    """Write the prompt to the child's stdin from a daemon thread, then close it. Threaded so a backend that
    never reads stdin (a hostile spewer) cannot block the driver, and a large prompt cannot deadlock."""
    try:
        if proc.stdin is not None:
            proc.stdin.write(data)
            proc.stdin.flush()
    except OSError:
        pass  # broken pipe: the child exited/ignored stdin — not our problem
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass


def _kill(proc) -> None:
    with suppress(OSError):
        proc.kill()
    with suppress(Exception):
        proc.wait(timeout=5)


def _fsize(f) -> int:
    """Return a capture file's on-disk size, reflecting the child's writes rather than this offset."""
    try:
        return os.fstat(f.fileno()).st_size
    except OSError:
        return 0


def _cli_runner(cmd: list, prompt: str, *, timeout: float, unset_env=None) -> str:
    """Run a backend agent CLI with **process-boundary** output caps ([C38]/[C39]).

    Deliberately NOT ``subprocess.run(capture_output=True)`` — that buffers the child's entire stdout in
    memory before any cap applies, so a hostile/broken backend could OOM the driver. Instead: argv only
    (``shell=False`` -> no shell injection), the prompt fed on stdin by a daemon thread, stdout/stderr
    redirected to temp *files* (so the child never blocks on a full pipe and the parent never holds the
    bytes in RAM), and a poll loop that KILLS the process the instant it exceeds the size cap or the
    timeout. Only the bounded prefix is ever read back. Fail-closed: a cap breach or timeout raises
    ``CollabError`` so the round fails safely rather than delivering attacker-shaped output.

    ``unset_env`` drops the named vars from the child's environment (the parent still has them). This is how
    a ``claude -p`` seat runs on the Max/Pro **subscription** instead of per-token API billing: the driver
    loads ``.env`` into its own environment, so without this the child would inherit ``ANTHROPIC_API_KEY``
    and Claude Code would bill the API; drop it and Claude Code falls back to the logged-in subscription.
    """
    name = cmd[0] if cmd else "<empty>"
    child_env = None
    if unset_env:
        drop = {str(k) for k in unset_env}
        child_env = {k: v for k, v in os.environ.items() if k not in drop}
    capture = ExitStack()
    try:
        fout = capture.enter_context(tempfile.TemporaryFile())  # noqa: SIM115 - ExitStack owns it below
        ferr = capture.enter_context(tempfile.TemporaryFile())  # noqa: SIM115 - ExitStack owns it below
    except OSError as e:
        capture.close()
        raise cc.CollabError(f"backend {name!r}: cannot open capture buffers: {e}") from e
    breach = None
    # Windows: the driver is spawned DETACHED_PROCESS, so it owns NO console. Seats are console-subsystem
    # binaries (python.exe / claude.exe), and Windows hands a console-less parent's console child a BRAND
    # NEW console -- i.e. a terminal window pops up per seat, per round, on top of whatever you are doing.
    # CREATE_NO_WINDOW suppresses that. stdout/stderr are already redirected to files, so nothing is lost.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with capture:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=fout,
                stderr=ferr,
                shell=False,
                env=child_env,
                creationflags=creationflags,
            )
        except OSError as e:
            raise cc.CollabError(f"backend {name!r} could not be launched: {e}") from e
        threading.Thread(target=_feed_stdin, args=(proc, prompt.encode("utf-8")), daemon=True).start()
        start = time.monotonic()
        slack = 65536  # allow one buffer over the cap before we declare a breach and kill
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                breach = "timeout"
                break
            if _fsize(fout) > _MAX_RESP_BYTES + slack or _fsize(ferr) > _MAX_STDERR_BYTES + slack:
                breach = "cap"
                break
            time.sleep(0.02)
        if breach is not None:
            _kill(proc)
        rc = proc.returncode
        fout.seek(0)
        out = fout.read(_MAX_RESP_BYTES)  # bounded read-back: never the full on-disk flood
        ferr.seek(0)
        err = ferr.read(_MAX_STDERR_BYTES)
    if breach == "timeout":
        raise cc.CollabError(f"backend {name!r} timed out after {timeout}s (process killed)")
    if breach == "cap":
        raise cc.CollabError(f"backend {name!r} exceeded the output cap and was killed")
    if rc != 0:
        raise cc.CollabError(f"backend {name!r} exited {rc}: {err.decode('utf-8', 'replace').strip()[:500]}")
    return out.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# closeout: auto-run the tests + adversarial lanes to build the evidence ledger
# --------------------------------------------------------------------------- #


def load_closeout(home) -> dict | None:
    """The optional top-level ``"closeout"`` block from seats.json — the breaker/verifier seats + source +
    test config for the auto-lane closeout. ``None`` disables it (sign-off then needs a manually-built
    ledger). Keys: ``breaker``, ``verifier`` (seat names), ``source_base`` (default: kit root),
    ``source_roots`` (globs), ``test_path`` (pytest target -- a PARTIAL result that cannot close a
    handoff on its own).

    ``verify_command`` is REJECTED, loudly, at load. It used to declare "the repo's authoritative
    whole-checkout gate", which meant seats.json could name any command and have its exit code stamped
    authoritative -- the 2026-07-15 fail-open. The authoritative gate is now discovered
    (``scripts/verify.py``) and its argv is fixed (``verification.AUTHORITATIVE_ARGV``). Raising beats
    ignoring: a silently-dropped key leaves an operator believing their gate is configured and running
    when it is neither."""
    try:
        doc = json.loads(_seats_file(home).read_text("utf-8"))
    except OSError, ValueError, cc.CollabError:
        return None
    c = doc.get("closeout") if isinstance(doc, dict) else None
    if not isinstance(c, dict):
        return None
    if "verify_command" in c:
        raise cc.CollabError(
            "closeout.verify_command is no longer honoured and must be removed from seats.json: the "
            "authoritative gate is not operator-configurable. Any command exiting 0 was previously "
            "stamped 'GREEN — scripts/verify.py exit 0'. The gate is discovered (scripts/verify.py) "
            f"and its argv is fixed: {' '.join(_AUTHORITATIVE_ARGV)}"
        )
    return c


def _run_test_suite(test_path, cwd) -> dict:
    """Capture verification evidence as a real subprocess ([C38]-style boundary); return the ledger
    ``tests`` record.

    The AUTHORITATIVE gate is DISCOVERED, never configured: if the tree under review has
    ``scripts/verify.py``, run the one canonical argv (``verification.AUTHORITATIVE_ARGV``). Otherwise
    fall back to a pytest-only run, recorded as an explicitly PARTIAL result -- ``done_contract``
    condition 5 refuses to close on it, so a repo with no discoverable gate cannot autonomously close.
    Fail-closed is the point: an unconfigured gate must not look like a passing one.

    ``closeout.verify_command`` USED to override this and was handed straight to ``subprocess`` with its
    exit code stamped ``authoritative`` — so any operator-configured command that exited 0 (a wrapper, a
    narrowed pytest, ``python -c "print('RESULT: PASS')"``) closed handoffs wearing the label
    "GREEN — scripts/verify.py exit 0". The key is now rejected outright (see :func:`load_closeout`)
    rather than honoured, because a gate whose identity is configurable by the thing it gates is not a
    gate. ``verification.run_authoritative`` re-validates the argv regardless.

    Before 2026-07-15 this ran pytest and nothing else, and the contract read its bare ``passed``
    boolean as though it attested the checkout. It never did: lint and type failures were invisible
    to the gate. ``verification.is_green`` is now the only reader allowed to conclude "this checkout
    passed", and no pytest-only record can satisfy it.
    """
    import verification as _v  # lazy: keep module import cost off the driver's hot path

    base = str(cwd) if cwd else None
    if base and _v.verify_script_present(base):
        return _v.run_authoritative(base)
    if not test_path:
        return _v.unverified("no scripts/verify.py under review, and no test_path")
    return _v.run_pytest_only(test_path, base, python=sys.executable)


def _capture_preflight(base, test_path, reviewer_seat, manifest) -> dict:
    """Trusted repo-awareness capture for the autonomous reviewer (done_contract condition 11, §18).

    Runs READ-ONLY repo commands as subprocesses under ``base`` and records their exit codes + output
    tails: the reviewer's proof of repo awareness (pwd / git toplevel / status / diff / pytest
    ``--collect-only``) plus the files under review (``inspected_files`` = the attested manifest paths).
    The done-contract refuses an autonomous close without a valid block — a text-only "reviewed it" is not
    repo awareness ([C42]). Captured by the DRIVER, never parsed from untrusted agent stdout ([C38])."""

    def _run(argv):
        try:
            p = subprocess.run(argv, cwd=str(base), capture_output=True, text=True, timeout=300)
            return {"exit_code": p.returncode, "stdout_tail": (p.stdout or "")[-2000:]}
        except (OSError, subprocess.SubprocessError) as e:
            return {"exit_code": -1, "stdout_tail": f"error: {str(e)[:200]}"}

    git_rev = _run(["git", "rev-parse", "--show-toplevel"])
    collect = (
        _run([sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_path)])
        if test_path
        else {"exit_code": -1, "stdout_tail": "no test_path configured"}
    )
    ok_root = git_rev["exit_code"] == 0
    top = git_rev["stdout_tail"].strip()
    repo_root = top.splitlines()[-1] if (ok_root and top) else str(base)
    return {
        "seat": reviewer_seat,
        "repo_access": ok_root,
        "repo_root": repo_root,
        "commands": {
            "pwd": {"exit_code": 0, "stdout_tail": str(base)},
            "git_rev_parse": git_rev,
            "git_status_short": _run(["git", "status", "--short"]),
            "git_diff_name_only": _run(["git", "diff", "--name-only"]),
            "pytest_collect_only": collect,
        },
        "inspected_files": sorted((manifest or {}).keys()),
    }


def _autoclose_ledger(collab, hid, builder_seat, closeout, *, seats, runner, log, reviewer_seat=None):
    """Build the verification ledger for a sign-off attempt: run the test suite, then the adversarial lanes
    (breaker -> independent verifier) for the handoff's risk class. Reuses :func:`lanes.run_lanes`, which
    captures the reviewer repo-preflight (condition 11) and writes
    ``<collab>/autopilot/verification/<hid>.ledger.json`` that the done-contract then reads."""
    import lanes as _lanes  # lazy: lanes imports autopilot; autopilot must not import lanes at module top

    base = closeout.get("source_base") or str(cc.resolve_kit_root())
    roots = closeout.get("source_roots") or ["**/*.py"]
    test_path = closeout.get("test_path")
    tests = _run_test_suite(test_path, base)
    return _lanes.run_lanes(
        collab,
        hid,
        seats=seats,
        breaker_seat=closeout.get("breaker"),
        verifier_seat=closeout.get("verifier"),
        builder_seat=builder_seat,
        reviewer_seat=reviewer_seat,
        source_roots=roots,
        source_base=base,
        test_path=test_path,
        tests=tests,
        runner=runner,
        log=log,
    )


# --------------------------------------------------------------------------- #
# one turn — pure dispatch (ADR-0001: a turn is conversation, never a board transition)
# --------------------------------------------------------------------------- #


def _dispatch_seat(
    collab,
    seat: str,
    *,
    seats: dict,
    runner,
    hid: str,
    transcript: str,
    log: str,
    rid: str,
    attempt: int,
    span_role: str,
) -> str | None:
    """Run ONE seat turn against the already-claimed handoff ``hid`` and return its raw stdout, or ``None``
    on a backend failure. A turn is *conversation*, not a handoff (ADR-0001): it is persisted only as an
    inert reply artifact + ``autopilot.round`` telemetry — it NEVER creates or transitions a board entry.

    The candidate lifecycle composes this pure dispatch; the sign-off/done JUDGEMENT lives in the loop
    (candidate_assessment classify + done_contract evaluate), not here. ``span_role`` (``builder``/
    ``reviewer``) labels the round for the dashboard's per-seat stats and run_history's call count."""
    cfg = _cli_seat(seats, seat)
    if cfg is None:
        return None  # human/web seat mid-exchange — the caller treats it as awaiting a person
    sp = f"{span_role}:{hid}:{attempt}"
    _emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.round",
        role=seat,
        artifact=f"handoff:{hid}",
        span_id=sp,
        decision={"action": "start", "reason_codes": [f"seat:{seat}"], "confidence": None},
        metrics={"round_no": attempt},
    )
    timeout_s = float(cfg.get("timeout", _DEFAULT_TIMEOUT))
    _write_status(
        collab,
        phase="thinking",
        active_seat=seat,
        current_hid=hid,
        round=attempt,
        active_since=_now_utc(),
        timeout=timeout_s,
    )  # active_since => elapsed; timeout => deadline
    prompt = _build_prompt(cfg.get("system"), transcript)
    t0 = time.monotonic()
    try:
        with _Heartbeat(collab):  # keep updated_ts fresh through a long agentic call (liveness, not deadline)
            raw = runner(list(cfg["cmd"]), prompt, timeout=timeout_s, unset_env=cfg.get("unset_env"))
    except cc.CollabError as e:
        lat_ms = round((time.monotonic() - t0) * 1000, 1)
        print(f"[autopilot] backend for seat {seat!r} failed on {hid}: {e}", file=sys.stderr)
        _emit_safe(
            _trace.emit,
            log,
            run_id=rid,
            stage="autopilot.round",
            role=seat,
            artifact=f"handoff:{hid}",
            span_id=f"{sp}:fail",
            parent_span_id=sp,
            decision={"action": "fail", "reason_codes": ["backend_error"], "confidence": None},
            failure={"kind": "backend", "message": str(e)[:500]},
            metrics={"latency_ms": lat_ms},
        )
        _write_status(
            collab, active_seat=None, current_hid=None, last_error=str(e)[:200], last_latency_ms=lat_ms
        )
        return None  # handoff stays claimed for a human; the drive stalls ([C39])
    lat_ms = round((time.monotonic() - t0) * 1000, 1)
    resp_bytes = len(raw.encode("utf-8", "replace"))
    relpath = _write_reply(collab, seat, _sanitize(raw))  # the turn, stored as an inert artifact
    _write_status(collab, active_seat=None, current_hid=None, last_latency_ms=lat_ms, last_error=None)
    _emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.round",
        role=seat,
        artifact=f"handoff:{hid}",
        span_id=f"{sp}:done",
        parent_span_id=sp,
        decision={"action": "turn", "reason_codes": [f"reply:{relpath}"], "confidence": None},
        metrics={"latency_ms": lat_ms, "resp_bytes": resp_bytes},
    )
    print(f"[autopilot] {seat} took a turn on {hid} (attempt {attempt})")
    return raw


# --------------------------------------------------------------------------- #
# candidate assessment (ADR-0003 D4): reviewer DECISION ∥ adversarial lanes EVIDENCE
# --------------------------------------------------------------------------- #
#
# The reviewer stays free-text + [[SIGNOFF]] (user-chosen 2026-07-13): the orchestrator synthesizes the
# structured whole-candidate ReviewerReport from whether the token appeared, and hands the reviewer +
# lane evidence to candidate_assessment, which owns the classify/merge policy. done_contract remains the
# machine gate ([C36]) — an APPROVED candidate still reaches done/ only through a satisfied evidence contract.


def _synth_reviewer_report(raw: str | None, candidate_id: str, *, can_sign_off: bool = True):
    """Reviewer = DECISION. A sign-off token from a ``can_sign_off`` seat ⇒ a clean report (no blocking
    findings); a withheld sign-off — or a token from a seat NOT authorized to sign off (the opt-in gate) —
    ⇒ ONE blocking ``contract`` finding carrying the reviewer's prose as its evidence; a FAILED reviewer
    call (``raw`` is None) ⇒ ``None`` ⇒ ``verification_incomplete`` (a pause, never a silent pass)."""
    if raw is None:
        return None
    if can_sign_off and _SIGNOFF_RE.search(raw):
        payload = {
            "requirement_coverage": {},
            "blocking_findings": [],
            "advisory_findings": [],
            "edited_code": False,
        }
    else:
        prose = _sanitize(raw).strip() or "reviewer withheld sign-off without stating a reason"
        payload = {
            "requirement_coverage": {},
            "advisory_findings": [],
            "edited_code": False,
            "blocking_findings": [
                {
                    "source": "reviewer",
                    "severity": ca.BLOCKING,
                    "category": "contract",
                    "title": "reviewer withheld sign-off",
                    "evidence": prose[:8000],
                    "remediation": "address the reviewer's blocking concerns, then re-request sign-off",
                }
            ],
        }
    return ca.ReviewerReport.parse(payload, candidate_id=candidate_id)


def _assessment_lane_ledger(led: dict | None) -> dict:
    """Adapt the ``run_lanes`` verification ledger (top-level ``blockers`` + truthful-terminal signals) into
    the shape ``candidate_assessment`` reads (top-level ``confirmed`` lane findings). The confirmed lane
    blockers become blocking correctness findings; the ``tool_error``/``incomplete``/``overflow`` signals
    pass through so an infrastructure/verification-incomplete run is never laundered into a clean pass."""
    led = led or {}
    confirmed = [
        {
            "fingerprint": b.get("id"),
            "lane": b.get("lane"),
            "evidence": (b.get("description") or "").strip(),
            "category": "correctness",
            "remediation": b.get("regression_test") or "",
        }
        for b in (led.get("blockers") or [])
    ]
    return {
        "confirmed": confirmed,
        "refuted": [],
        "tool_error": led.get("tool_error"),
        "incomplete": led.get("incomplete"),
        "overflow": int(led.get("overflow", 0) or 0),
        "unverified": led.get("unverified", []),
    }


def _compute_candidate(
    collab,
    hid,
    *,
    seats,
    builder_seat,
    reviewer_seat,
    source_roots,
    source_base,
    test_path,
    guardrails,
    builder_output=None,
    verification_plan=None,
):
    """Compute the candidate identity for the builder's current output. For CODE work the source manifest
    drives the id (so an unchanged fix mints the SAME id — the no-progress signal); the reviewer rubric +
    builder seat profile are folded in so a changed lens can never reuse evidence under the old one
    (ADR-0003 D4). For a TEXT-ONLY handoff (no source tree) there is nothing to hash on disk, so the
    builder's OUTPUT digest stands in — distinct output per attempt still mints a new candidate."""
    import gate_runner as gr  # lazy: gate_runner is heavy and only needed on the assess path

    manifest = gr.source_manifest(source_roots, source_base) if (source_roots and source_base) else {}
    if not manifest and builder_output is not None:  # text-only handoff: identity from the builder's output
        manifest = {"__builder_output__": _sha256(builder_output)}
    rcfg = seats.get(reviewer_seat) if isinstance(seats.get(reviewer_seat), dict) else {}
    try:
        profile_payload = {
            "builder": adapter_profiles.seat_profile_fingerprint(seats.get(builder_seat) or {}),
            "reviewer": adapter_profiles.seat_profile_fingerprint(seats.get(reviewer_seat) or {}),
            "verification_plan": verification_plan.identity_digest if verification_plan is not None else "",
        }
        fp = "assessment:" + _sha256(json.dumps(profile_payload, sort_keys=True))[:16]
    except Exception:
        fp = ""
    lane_config = (
        json.loads(verification_plan.identity_payload)
        if verification_plan is not None
        else {"guardrails": list(guardrails or [])}
    )
    return ca.Candidate.compute(
        hid,
        source_manifest=manifest,
        source_roots=list(source_roots or []),
        test_command=test_path,
        lane_config=lane_config,
        assessment_plan_revision=(
            verification_plan.lane_config_revision if verification_plan is not None else ""
        ),
        reviewer_rubric=str(rcfg.get("system") or ""),
        seat_profile_fingerprint=fp,
    )


def _sha256(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _assess_candidate(
    collab,
    hid,
    candidate,
    *,
    seats,
    closeout,
    builder_seat,
    reviewer_seat,
    reviewer_transcript,
    runner,
    budget,
    log,
    rid,
    source_base,
    source_roots,
    test_path,
    guardrails,
    verification_plan=None,
):
    """Gather one candidate's evidence: the reviewer DECISION ∥ the adversarial LANES, fanned out under a
    ThreadPoolExecutor and charged atomically through the shared ``RunBudget``. Returns
    ``(reviewer_report, lane_ledger)`` (the raw ``run_lanes`` ledger). A budget denial before fan-out, a
    lane exception, or a source edit DURING assessment (manifest drift) yields an infra/incomplete ledger —
    never a silent pass."""
    import gate_runner as gr  # lazy
    import lanes as _lanes  # lazy (lanes imports ap at module top)

    cid = candidate.candidate_id
    manifest_before = gr.source_manifest(source_roots, source_base) if (source_roots and source_base) else {}
    # Reserve the reviewer decision before concurrent dispatch. Resolved v2 lane pairs reserve their own
    # verification passes at dispatch time, so an unstarted high-risk pair is never pre-charged.
    try:
        budget.charge(rb.REVIEW_DECISION, candidate=cid)
        if verification_plan is None:
            budget.charge(rb.VERIFICATION_PASS)
    except rb.BudgetExceeded as e:
        return None, {
            "blockers": [],
            "tool_error": None,
            "overflow": 0,
            "unverified": [],
            "incomplete": {"reason": "budget", "which": e.which},
        }
    base = closeout or {}
    tests = _run_test_suite(test_path, source_base)

    def _review():
        return _dispatch_seat(
            collab,
            reviewer_seat,
            seats=seats,
            runner=runner,
            hid=hid,
            transcript=reviewer_transcript,
            log=log,
            rid=rid,
            attempt=int(budget.consumed().get("actor_turns", 0)),
            span_role="reviewer",
        )

    def _run_lanes():
        return _lanes.run_lanes(
            collab,
            hid,
            seats=seats,
            breaker_seat=base.get("breaker"),
            verifier_seat=base.get("verifier"),
            builder_seat=builder_seat,
            reviewer_seat=reviewer_seat,
            guardrails=guardrails,
            source_roots=source_roots,
            source_base=source_base,
            test_path=test_path,
            tests=tests,
            runner=runner,
            log=log,
            budget=budget,
            candidate_id=cid,
            verification_plan=verification_plan,
        )

    _write_status(
        collab, phase="thinking", active_seat="assess", current_hid=hid, active_since=_now_utc(), timeout=0
    )
    # Keep updated_ts fresh through the multi-minute reviewer ∥ lanes fan-out.
    with _Heartbeat(collab), ThreadPoolExecutor(max_workers=2) as ex:
        f_rev = ex.submit(_review)
        f_lanes = ex.submit(_run_lanes)
        rev_raw = f_rev.result()
        try:
            lane_ledger = f_lanes.result()
        except (cc.CollabError, hc.HandoffNotFound) as e:
            lane_ledger = {
                "blockers": [],
                "overflow": 0,
                "unverified": [],
                "incomplete": None,
                "tool_error": {"reason": "lane_exception", "error": str(e)[:200]},
            }
    _write_status(collab, active_seat=None)
    # source==tested integrity: a source edit during the assessment invalidates the evidence just gathered.
    manifest_after = gr.source_manifest(source_roots, source_base) if (source_roots and source_base) else {}
    if manifest_after != manifest_before:
        lane_ledger = dict(lane_ledger or {})
        lane_ledger["tool_error"] = {"reason": "source_drift_during_assessment"}
    can_sign_off = bool((seats.get(reviewer_seat) or {}).get("can_sign_off"))  # the opt-in approval gate
    return _synth_reviewer_report(rev_raw, cid, can_sign_off=can_sign_off), (lane_ledger or {})


def _next_root(collab, seats: dict, exclude: set) -> str | None:
    """The lowest-id ``pending`` handoff addressed to a CLI seat whose id is not in ``exclude`` — the next
    thread to drive. ``exclude`` holds roots already driven this run plus every reply the driver created, so
    a thread is driven once and driver-made replies are never mistaken for fresh work ([C35] — this is what
    bounds the fan-out to the actually-queued handoffs, one at a time, in id order)."""
    best = None
    for h in hc.list_handoffs(collab, "pending"):
        if h["id"] in exclude:
            continue
        to, _frm = _to_from(Path(h["path"]))
        if to and _cli_seat(seats, to.strip()) is not None and (best is None or h["id"] < best):
            best = h["id"]
    return best


def _reclaim_orphans(collab, *, log: str | None = None, rid: str | None = None) -> list:
    """Un-strand handoffs left in ``claimed/`` by a driver that died. Returns the reclaimed ids.

    ``claimed/`` means three different things and the board records no difference between them:

      * IN PROGRESS  -- a live board lease is held; someone is working it right now.
      * ORPHANED     -- the driver crashed / was killed / its terminal closed mid-work. Nobody is working
                        it, and NOTHING will ever select it again: ``_next_root`` scans ``pending`` only,
                        so the slice is stranded until a human files an operator request. This is the case
                        that silently wedged the board and made a later Start look like a no-op "done".
      * PARKED       -- the driver deliberately stopped and wrote an escalation: "awaiting human". Auto-
                        driving this re-opens a budget epoch with NO human authorization and re-drives a
                        known-blocked slice on every Start until the budget caps it.

    Only the middle case is ours. The discriminators are the live lease and the escalation record.

    Run at START, not at teardown: a ``finally`` never executes for a SIGKILL / closed terminal / power
    cut, which is exactly how the runs this fixes actually die. Start always executes.

    Best-effort ([C15]): a reclaim failure must never take the run down -- the slice simply stays stranded
    exactly as it is today, and the next start retries.
    """
    try:
        holder = hc.ActiveHandoffLease(collab, "reclaim-probe").holder()
    except Exception:
        return []  # cannot prove the board is free -> assume it is not, and touch nothing
    if isinstance(holder, dict):
        hb = holder.get("heartbeat_epoch")
        if hb is not None and (time.time() - float(hb)) < hc._LEASE_TTL_S:
            return []  # a live driver owns the board: its claimed handoff is IN PROGRESS, not orphaned
    reclaimed = []
    try:
        claimed = hc.list_handoffs(collab, "claimed")
    except Exception:
        return []
    for h in claimed:
        hid = h["id"]
        try:
            if esc.read(collab, hid) is not None:
                continue  # PARKED: a human was asked to decide. Not ours to restart.
        except Exception:
            continue  # cannot rule out an escalation -> fail closed, leave it parked
        try:
            hc.reclaim(collab, hid)
        except Exception as e:
            print(f"[autopilot] could not reclaim orphaned {hid}: {e}", file=sys.stderr)
            continue
        reclaimed.append(hid)
        print(f"[autopilot] reclaimed orphaned {hid}: claimed/ -> pending/ (no live driver, no escalation)")
        _emit_safe(
            _trace.emit,
            log,
            run_id=rid,
            stage="autopilot.reclaim",
            role="autopilot",
            artifact=f"handoff:{hid}",
            decision={
                "action": "reclaim",
                "reason_codes": [f"hid:{hid}", "orphan:no-lease", "orphan:no-escalation"],
                "confidence": None,
            },
        )
    return reclaimed


def _promote_next_draft(collab, *, log: str | None = None, rid: str | None = None) -> str | None:
    """A slice just shipped to ``done/`` — PULL the next staged one: move the lowest-id handoff from
    ``handoffs/draft/`` into ``handoffs/pending/``. Keeping exactly ONE slice queued makes the pipeline run
    in strict id order (029 -> 030 -> ...) with no skipping or out-of-order fan-out, even if more than one
    driver is polling. Returns the promoted numeric id, or ``None`` when ``draft/`` is empty."""
    draft = Path(collab) / "handoffs" / "draft"
    try:
        staged = sorted(
            (p for p in draft.glob("*.md") if re.match(r"^\d+-", p.name)),
            key=lambda p: int(re.match(r"^(\d+)", p.name).group(1)),
        )
    except OSError:
        return None
    if not staged:
        return None
    src = staged[0]
    dst = Path(collab) / "handoffs" / "pending" / src.name
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(src), str(dst))  # atomic within the one filesystem
    except OSError as e:
        print(f"[autopilot] could not promote next staged slice {src.name}: {e}", file=sys.stderr)
        return None
    hid = re.match(r"^(\d+)", src.name).group(1)
    if log:
        _emit_safe(
            _trace.emit,
            log,
            run_id=rid,
            stage="autopilot.promote",
            role="autopilot",
            artifact=f"handoff:{hid}",
            decision={"action": "promote", "reason_codes": [f"hid:{hid}", "from:draft"], "confidence": None},
        )
    print(f"[autopilot] promoted {src.name}: draft/ -> pending/ (next slice, in order)")
    return hid


def run(
    collab,
    *,
    seats: dict,
    max_rounds: int | None = None,
    limits=None,
    runner=_cli_runner,
    watch: bool = False,
    interval: float = 2.0,
    home=None,
    log: str | None = None,
) -> int:
    """Drive the CANDIDATE lifecycle ONE HANDOFF AT A TIME (ADR-0002/0003). Returns the total seat turns.

    The run holds a board-level ``ActiveHandoffLease`` (ADR-0003 D2) so exactly one driver ever selects or
    claims work. It takes the **lowest-id** pending handoff and drives it as a sequence of *candidates*: each
    builder attempt produces a candidate, which is assessed (reviewer DECISION ∥ adversarial-lane EVIDENCE)
    and classified (ADR-0003 D1). Only an ``approved`` candidate whose §18.3 evidence contract is satisfied
    reaches ``done/``; ``repair_required`` sends the builder the exact findings and loops; any other terminal
    (``infrastructure_blocked``/``verification_incomplete``/``no_progress``/budget-exhausted) writes a durable
    escalation record, pings the human, and STOPS — nothing moves past this handoff until it is resolved.

    Every bound is a named ``RunBudget`` counter (ADR-0002 D1): work attempts, review decisions per candidate
    (invariant 1), verification passes, total model calls, wall-clock. ``max_rounds`` is a DEPRECATED alias
    that overrides ``Limits.max_work_attempts``; prefer passing ``limits``. ``watch=True`` keeps the driver
    resident to poll for newly-queued handoffs once the board is drained.
    """
    log = log or _log_default(collab)
    rid = _run_id(collab)
    closeout = load_closeout(home) if home else None  # opt-in auto-lane closeout (breaker/verifier + tests)
    driven: set = set()  # roots started this run — excluded from future root selection ([C35])
    import run_history as _rh  # lazy: run_history imports autopilot; avoid a module-load cycle

    base_limits = limits or rb.Limits.balanced()
    if max_rounds is not None:  # deprecated alias: --max-rounds now tunes the work-attempt budget
        base_limits = replace(base_limits, max_work_attempts=int(max_rounds))

    # --- per-run identity (CONTRACT A). run_uid is minted ONCE here and is time-sortable: it names this
    # run's durable archive under autopilot/history/. status.json (shared across runs) is stamped with it,
    # plus the seats->model snapshot and git sha, so archive_run can rebuild run.json self-sufficiently.
    pid = os.getpid()
    started_ts = _now_utc()
    run_uid = re.sub(r"[^A-Za-z0-9]", "", started_ts) + f"-{pid}"  # e.g. 20260709T001844Z-6808
    run_git_sha = _rh.git_sha(collab)
    seats_snapshot = {
        name: (cfg.get("model") if isinstance(cfg, dict) else None) for name, cfg in (seats or {}).items()
    }
    lease = hc.ActiveHandoffLease(collab, run_uid, pid=pid)
    # ROTATE+WIPE the live feed so this run starts clean: the prior run archived ITS log at its own end
    # (finally, below), so truncating here only discards already-archived leftovers. Guard a fresh collab.
    try:
        _live_log = Path(_log_default(collab))
        if _live_log.exists():
            cc.safe_write(_live_log, "")
    except (OSError, cc.CollabError) as _e:
        print(f"[autopilot] could not rotate events log: {_e}", file=sys.stderr)
    # ``_write_status`` MERGES into the existing file, so every run-scoped field this run may never write
    # has to be cleared HERE or it survives from the previous run and renders as current. That is not
    # cosmetic: a stale ``pause_reason`` reads as this run's terminal cause, and a stale ``budget`` reads
    # as this run's consumption. Reset the whole run-scoped surface, not just the fields we happen to set.
    _write_status(
        collab,
        pid=pid,
        started_ts=started_ts,
        run_uid=run_uid,
        git_sha=run_git_sha,
        run_seats=seats_snapshot,
        phase="thinking",
        round=0,
        max_rounds=base_limits.max_work_attempts,
        watch=watch,
        interval=interval,
        active_seat=None,
        current_hid=None,
        last_latency_ms=None,
        last_error=None,
        stage=None,
        candidate=None,
        pause_reason=None,
        budget=None,
        active_since=None,
        timeout=None,
        ended_ts=None,
    )
    # Un-strand anything a dead driver left in claimed/ BEFORE selecting work: _next_root scans pending/
    # only, so an orphan is invisible to it and the run would idle to a false "done" with the board wedged.
    _reclaim_orphans(collab, log=log, rid=rid)
    try:
        # Renew the board lease for the whole loop: without this it goes stale 90 s in (mid first agentic
        # call), the dashboard reports "no driver running" while we work, and Start will spawn a SECOND
        # driver onto this board (ADR-0003 D2). Daemon thread; released by the `finally` below.
        with _LeaseRenewer(lease):
            return _run_loop(
                collab,
                seats=seats,
                base_limits=base_limits,
                runner=runner,
                watch=watch,
                interval=interval,
                home=home,
                log=log,
                rid=rid,
                closeout=closeout,
                driven=driven,
                lease=lease,
            )
    finally:
        # [C15] best-effort teardown on ANY exit (return, pause, stall, exception, kill): release the board
        # lease so the next run can start, then archive this run's evidence.
        # Never mask the real return/raise.
        try:
            lease.release()
        except Exception as _e:  # broad: lease release is teardown, never worth breaking the driver's exit
            print(f"[autopilot] lease release failed on exit: {_e}", file=sys.stderr)
        try:
            _rh.archive_run(collab, run_uid)
            _rh.prune(collab)
        except Exception as _e:  # broad: archival is telemetry, never worth breaking the driver's exit
            print(f"[autopilot] run archival failed (run already complete): {_e}", file=sys.stderr)


def _run_loop(
    collab, *, seats, base_limits, runner, watch, interval, home, log, rid, closeout, driven, lease
) -> int:
    """The bounded drive loop, factored out of :func:`run` so ``run`` can wrap it in one try/finally that
    guarantees lease release + archival on EVERY exit path. Returns the total seat turns executed."""
    total_calls = 0
    last_phase = None
    while True:
        ctrl = _read_control(collab)  # human control file — only ever pauses/stops the loop ([C36])
        if ctrl.get("stop"):
            _emit_safe(
                _trace.emit,
                log,
                run_id=rid,
                stage="autopilot.control",
                role="autopilot",
                decision={"action": "stop", "reason_codes": [], "confidence": None},
            )
            _write_status(collab, phase="done", active_seat=None, current_hid=None)
            return total_calls  # graceful, reversible exit: no done/, no commit — the human resumes later
        if ctrl.get("paused"):
            if last_phase != "paused":
                _emit_safe(
                    _trace.emit,
                    log,
                    run_id=rid,
                    stage="autopilot.control",
                    role="autopilot",
                    decision={"action": "pause", "reason_codes": [], "confidence": None},
                )
                _write_status(collab, phase="paused", active_seat=None, current_hid=None)
                last_phase = "paused"
            time.sleep(interval)
            continue
        if last_phase == "paused":
            _emit_safe(
                _trace.emit,
                log,
                run_id=rid,
                stage="autopilot.control",
                role="autopilot",
                decision={"action": "resume", "reason_codes": [], "confidence": None},
            )
            last_phase = None

        # Durable operator requests (ADR-0003) take priority over fresh work: a human filed a retry/adopt on
        # a PAUSED handoff (possibly while no driver was running), so honour it before selecting a new root.
        req = _next_request(collab, seats)
        if req is not None:
            last_phase = None
            outcome, calls = _drive_candidate(
                collab,
                req["hid"],
                seats=seats,
                closeout=closeout,
                runner=runner,
                home=home,
                log=log,
                rid=rid,
                lease=lease,
                base_limits=base_limits,
                reopen=req["action"],
            )
            opreq.consume(
                collab, req["hid"]
            )  # acted on -> the request is spent (the operator re-files to repeat)
            total_calls += calls
            if outcome == "closed":
                _promote_next_draft(collab, log=log, rid=rid)
                continue
            if outcome == "stopped" or _read_control(collab).get("stop"):
                continue
            if outcome in ("stalled", "human", "lease_held"):
                _write_status(collab, phase="capped", last_error=f"{req['hid']} not closed ({outcome})")
                _pause_note(collab, home, base_limits.max_work_attempts)
            return total_calls

        root = _next_root(collab, seats, driven)
        if root is None:  # board drained of fresh work (only human-seat handoffs remain)
            idle_phase = "sleeping" if watch else "idle"
            if last_phase != idle_phase:
                _emit_safe(
                    _trace.emit,
                    log,
                    run_id=rid,
                    stage="autopilot.idle",
                    role="autopilot",
                    decision={"action": "idle", "reason_codes": [], "confidence": None},
                )
                _write_status(collab, phase=idle_phase, active_seat=None, current_hid=None)
                last_phase = idle_phase
            if not watch:
                _write_status(collab, phase="done")
                return total_calls  # batch (default): the queue is drained -> we're done
            _write_status(collab)  # idle heartbeat: prove the daemon is still polling (crash detection)
            time.sleep(interval)  # daemon: keep polling for newly-queued handoffs
            continue

        last_phase = None
        driven.add(root)
        outcome, calls = _drive_candidate(
            collab,
            root,
            seats=seats,
            closeout=closeout,
            runner=runner,
            home=home,
            log=log,
            rid=rid,
            lease=lease,
            base_limits=base_limits,
        )
        total_calls += calls
        if outcome == "closed":
            _promote_next_draft(
                collab, log=log, rid=rid
            )  # a slice shipped -> pull the next staged slice in, in order
            continue  # ONLY an approved+contract-satisfied candidate advances -> select the next handoff
        if outcome == "stopped" or _read_control(collab).get("stop"):
            continue  # let the top of the loop emit the stop + return cleanly
        # Any other terminal (stalled/human/lease_held; or an escalation already written by _drive_candidate
        # for infra/incomplete/no_progress/budget/contract) -> ping the human and STOP: nothing moves past
        # this handoff until it is resolved.
        if outcome in ("stalled", "human", "lease_held"):
            _emit_safe(
                _trace.emit,
                log,
                run_id=rid,
                stage="autopilot.pause",
                role="autopilot",
                decision={
                    "action": "pause",
                    "reason_codes": [f"root:{root}", f"outcome:{outcome}"],
                    "confidence": None,
                },
            )
            _write_status(collab, phase="capped", last_error=f"{root} not closed ({outcome}); awaiting human")
            _pause_note(collab, home, base_limits.max_work_attempts)
        return total_calls


def _limits_from_control(collab, base_limits):
    """Apply the dashboard's live ``max_rounds`` control to the work-attempt ceiling.

    A positive control.json value changes this handoff's ``max_work_attempts``;
    a missing or invalid value keeps the launch-time budget. Reversible/bounded ([C35]).
    """
    mr = _read_control(collab).get("max_rounds")
    return replace(base_limits, max_work_attempts=int(mr)) if mr else base_limits


def _next_request(collab, seats):
    """Return the lowest-id consumable retry/adopt request, or ``None``.

    A missing, closed, or human-routed handoff cannot be re-driven, so its request
    is consumed here and skipped.
    """
    for req in opreq.pending(collab):
        hid = req["hid"]
        state, path = hc._reconcile(collab, hid)
        to, _frm = _to_from(Path(path)) if path else (None, None)
        if (
            path is None
            or state not in ("pending", "claimed")
            or _cli_seat(seats, (to or "").strip()) is None
        ):
            opreq.consume(collab, hid)  # nothing to re-drive -> discard the stale request
            continue
        return req
    return None


def _drive_candidate(
    collab, root, *, seats, closeout, runner, home, log, rid, lease, base_limits, reopen=None
):
    """Drive ONE handoff through the candidate lifecycle (ADR-0003 D1/D4). Returns ``(outcome, calls)``
    where ``calls`` is the number of seat turns dispatched (for run_history's per-run count).

    ``reopen`` (``retry``/``adopt``) re-drives a PAUSED handoff on a human-authorized fresh budget epoch:
    ``retry`` runs a fresh builder attempt; ``adopt`` skips the first builder attempt and assesses the
    current on-disk source as the candidate (the §18.3 contract still gates the close — adopt cannot force
    a done). A reopen clears the stale escalation record up front.

    Outcomes: ``closed`` (approved + contract satisfied -> the single ``hc.done``), ``stopped`` (control),
    ``stalled`` (backend fail / lost claim), ``human`` (next turn is a human seat), ``lease_held`` (a live
    foreign board lease), or a terminal pause (``repair`` exhausted to ``budget_exhausted``, ``no_progress``,
    ``infrastructure_blocked``, ``verification_incomplete``, ``contract_unsatisfied``) whose durable
    escalation record was written here."""
    calls = 0
    _rstate, rpath = hc._reconcile(collab, root)
    to_seat, from_seat = _to_from(Path(rpath)) if rpath else (None, None)
    builder_seat = (to_seat or "").strip()  # the handoff is addressed to the worker (acts first)
    reviewer_seat = (from_seat or "").strip()  # the counterpart authors the work and reviews the result
    request = _substance(collab, Path(rpath)) if rpath else ""  # read BEFORE claim moves it out of pending/
    guardrails = _root_guardrails(rpath)
    if rpath is None:
        return "stalled", calls  # the root vanished under us — a human needs to look
    drivable = ("pending", "claimed") if reopen else ("pending",)
    if _rstate not in drivable:
        return "stalled", calls  # a fresh drive needs pending; a reopen accepts the paused claimed state
    if _cli_seat(seats, builder_seat) is None:
        return "human", calls  # addressed to a human/web seat — left for the bridge

    # Acquire the board BEFORE claiming (ADR-0003 D2): a live foreign lease means another driver owns the
    # board — refuse to start a second concurrent run rather than race it onto a different handoff.
    try:
        lease.acquire(root)
    except hc.LeaseHeld:
        _write_status(
            collab, phase="capped", last_error=f"board lease held by another run; cannot drive {root}"
        )
        return "lease_held", calls
    if _rstate == "pending":
        try:
            hc.claim(collab, root)  # the SINGLE claim for the whole exchange ([C36])
        except cc.CollabError:
            return "stalled", calls  # lost the claim race
        _emit_safe(he.on_claim, log, rid, root, span_id=f"{root}:claim", parent_span_id=None, by=builder_seat)
    # else: a reopen of an already-claimed (paused) handoff keeps the existing claim.

    # closeout config drives the candidate's source manifest + adversarial lanes + test suite. Without a
    # source tree (a text-only handoff) there is nothing on disk to verify: the candidate's identity comes
    # from the builder's OUTPUT digest instead, and the assessment carries no source manifest (so an APPROVED
    # text handoff can never satisfy the evidence contract's builder-evidence condition — it escalates to a
    # human, the conservative outcome).
    base = closeout or {}
    source_base = base.get("source_base")  # None for a text-only handoff -> no manifest, no drift check
    source_roots = base.get("source_roots") or (["**/*.py"] if source_base else None)
    test_path = base.get("test_path")
    try:
        verification_plan = _resolve_assurance_plan(home, guardrails)
    except cc.CollabError as exc:
        budget = rb.RunBudget(collab, root, _limits_from_control(collab, base_limits))
        _escalate_pause(
            collab, root, home, budget, reason="infrastructure_blocked", log=log, rid=rid, cause=str(exc)
        )
        return "infrastructure_blocked", calls

    budget = rb.RunBudget(collab, root, _limits_from_control(collab, base_limits))
    if reopen:
        # A human-authorized fresh epoch (ADR-0002 D6): counters reset, the closed epoch stays immutable in
        # the record. Clear the stale escalation — this pause is being retried.
        budget.new_epoch(authorized_by=f"operator:{reopen}")
        esc.clear(collab, root)
        _emit_safe(
            _trace.emit,
            log,
            run_id=rid,
            stage="autopilot.reopen",
            role="human",
            artifact=f"handoff:{root}",
            decision={
                "action": reopen,
                "reason_codes": [f"epoch:{budget.epoch}", f"hid:{root}"],
                "confidence": None,
            },
        )
    transcript = request
    last_candidate_id = None
    # The most recent repair's blockers, embedded in a budget/no-progress escalation.
    last_blockers: list = []
    repaired = False
    adopt_pending = reopen == "adopt"  # adopt: skip the FIRST builder attempt, assess the current source
    attempt = 0
    while True:
        ctrl = _read_control(collab)
        if ctrl.get("stop"):
            return "stopped", calls
        # Honor a live control change to the work-attempt ceiling (reconstruct the budget: it reloads the
        # persisted counters from disk and re-applies the new limits — a lower cap can trip immediately).
        live_limits = _limits_from_control(collab, base_limits)
        if live_limits != budget.limits:
            budget = rb.RunBudget(collab, root, live_limits)
        _write_status(
            collab,
            max_rounds=live_limits.max_work_attempts,
            stage="builder",
            pause_reason=None,
            budget=budget.report(),
        )
        # Charge a work attempt BEFORE dispatch (ADR-0002 D6). A denial is a terminal budget pause.
        try:
            budget.charge(rb.WORK_ATTEMPT)
        except rb.BudgetExceeded:
            _escalate_pause(
                collab,
                root,
                home,
                budget,
                reason="budget_exhausted",
                log=log,
                rid=rid,
                blockers=last_blockers,
            )
            return "budget_exhausted", calls
        attempt += 1
        if adopt_pending:
            # adopt: the operator vouches the CURRENT on-disk source is the candidate — no builder dispatch,
            # assess it as-is. The evidence contract still gates any close.
            adopt_pending = False
            builder_raw = "[operator adopted the current on-disk source as this candidate]"
        else:
            builder_raw = _dispatch_seat(
                collab,
                builder_seat,
                seats=seats,
                runner=runner,
                hid=root,
                transcript=transcript,
                log=log,
                rid=rid,
                attempt=attempt,
                span_role="builder",
            )
            calls += 1
            if builder_raw is None:
                return "stalled", calls  # backend failed — handoff stays claimed; a human needs to look
        transcript = f"{transcript}\n\n----- {builder_seat} -----\n{_sanitize(builder_raw)}"

        candidate = _compute_candidate(
            collab,
            root,
            seats=seats,
            builder_seat=builder_seat,
            reviewer_seat=reviewer_seat,
            source_roots=source_roots,
            source_base=source_base,
            test_path=test_path,
            guardrails=guardrails,
            builder_output=builder_raw,
            verification_plan=verification_plan,
        )
        cid = candidate.candidate_id
        _write_status(collab, stage="assess", candidate=cid[5:17], current_hid=root)
        # No progress: a repair packet that produced a byte-identical candidate — the builder did not change
        # the source. Pause rather than reassess the identical work forever (ADR-0003).
        if repaired and cid == last_candidate_id:
            _escalate_pause(
                collab, root, home, budget, reason="no_progress", log=log, rid=rid, blockers=last_blockers
            )
            return "no_progress", calls

        # Cache: an already-completed identical candidate is reused verbatim (zero new reviewer/lane calls).
        prep = ca.prepare(collab, root, candidate=candidate)
        assessment = prep["cached"]
        if assessment is None:
            reviewer_transcript = (
                f"{transcript}\n\n----- for review -----\nReview the work above and decide sign-off."
            )
            report, lane_ledger = _assess_candidate(
                collab,
                root,
                candidate,
                seats=seats,
                closeout=closeout,
                builder_seat=builder_seat,
                reviewer_seat=reviewer_seat,
                reviewer_transcript=reviewer_transcript,
                runner=runner,
                budget=budget,
                log=log,
                rid=rid,
                source_base=source_base,
                source_roots=source_roots,
                test_path=test_path,
                guardrails=guardrails,
                verification_plan=verification_plan,
            )
            calls += (
                1  # the reviewer dispatch is one seat turn (lane breaker/verifier calls are not "rounds")
            )
            assessment = ca.complete(
                collab,
                root,
                candidate,
                reviewer_report=report,
                lane_ledger=_assessment_lane_ledger(lane_ledger),
                budget_snapshot=budget.report(),
            )

        outcome = assessment.outcome
        _emit_safe(
            _trace.emit,
            log,
            run_id=rid,
            stage="autopilot.assessment",
            role="autopilot",
            artifact=f"handoff:{root}",
            decision={
                "action": "assess",
                "reason_codes": [f"outcome:{outcome}", f"attempt:{attempt}", f"cand:{cid[5:17]}"],
                "confidence": None,
            },
        )

        if outcome == ca.APPROVED:
            verdict = _dc_evaluate(collab, root, seats, reviewer_seat, builder_seat, cid)
            if verdict["satisfied"]:
                # The ONLY AUTONOMOUS claimed->done transition ([C36]): approved + contract clean. Human
                # override paths exist (dashboard_core.advance_handoff, handoff_cli.cmd_done) and reach
                # the same CAS; they are distinguished by the persisted transition KIND, never by which
                # function was called. The contract hash is the receipt: it names the exact evidence.
                hc.done(
                    collab,
                    root,
                    kind=_transitions.KIND_AUTONOMOUS,
                    actor=reviewer_seat,
                    receipt=verdict["hash"],
                    candidate_id=cid,
                )
                _emit_safe(
                    he.on_autonomous_done,
                    log,
                    rid,
                    root,
                    span_id=f"{root}:signoff",
                    parent_span_id=None,
                    reviewer=reviewer_seat,
                    contract_hash=verdict["hash"],
                )
                _emit_safe(
                    _trace.emit,
                    log,
                    run_id=rid,
                    stage="autopilot.autonomous_done",
                    role=reviewer_seat,
                    artifact=f"handoff:{root}",
                    span_id=f"{root}:signoff",
                    decision={
                        "action": "autonomous_done",
                        "reason_codes": [
                            f"done:{root}",
                            f"by:{reviewer_seat}",
                            f"contract:{verdict['hash'][:12]}",
                        ],
                        "confidence": None,
                    },
                )
                print(f"[autopilot] {root} APPROVED + contract satisfied -> done; exchange complete")
                try:  # a human-readable narrative of the whole handoff — never let it break the closeout
                    import narrative as _narr

                    _narr.write(collab, root)
                except Exception as e:  # summary is a nicety; the sign-off already stands regardless
                    print(f"[autopilot] narrative for {root} skipped: {e}", file=sys.stderr)
                lease.release()  # done: drop the board so the loop can advance to the next handoff
                return "closed", calls
            # The evidence contract refused (e.g. red tests, missing preflight, source drift).
            # Never ship on an unsatisfied contract ([C36]); pause for a human.
            unmet = [c["name"] for c in verdict["conditions"] if c["status"] != "pass"]
            _write_status(collab, last_error=f"contract unsatisfied on {root}: {', '.join(unmet)}"[:200])
            _escalate_pause(
                collab, root, home, budget, reason="contract_unsatisfied", log=log, rid=rid, detail=unmet
            )
            return "contract_unsatisfied", calls

        if outcome == ca.REPAIR_REQUIRED:
            blockers = _assessment_blockers(assessment)
            transcript = f"{transcript}\n\n{_fix_directive(blockers)}"  # hand the builder the exact findings
            _record_sendback(collab, root, blockers, attempt=attempt, round_no=attempt, log=log, rid=rid)
            last_candidate_id = cid
            last_blockers = blockers
            repaired = True
            _write_status(collab, stage="repair", budget=budget.report())
            print(
                f"[autopilot] {root}: {len(blockers)} open blocker(s) -> repair packet to builder "
                f"(attempt {attempt}/{live_limits.max_work_attempts})"
            )
            continue  # loop -> another work attempt

        # infrastructure_blocked / verification_incomplete -> terminal pause + durable escalation record.
        _escalate_pause(
            collab,
            root,
            home,
            budget,
            reason=outcome,
            log=log,
            rid=rid,
            cause=assessment.cause,
            blockers=_assessment_blockers(assessment),
        )
        return outcome, calls


# The repair policy (ADR-0003): an open blocking finding (reviewer-withheld sign-off OR a lane-confirmed
# defect) sends the builder the exact findings and loops. Each builder re-attempt is one WORK_ATTEMPT charge,
# so the work-attempt budget is the bound; on exhaustion (or a no-progress fix) the driver writes a durable
# escalation record and pings the human. Every send-back is logged (see _record_sendback).


def _root_guardrails(rpath) -> list:
    """The ``guardrails`` frontmatter of the root handoff (``[]`` if absent/unparseable) — the list the lane
    runner derives the required adversarial lanes from, and part of the candidate's verification plan."""
    if rpath is None:
        return []
    try:
        fm = contracts.parse_handoff(Path(rpath)).get("frontmatter") or {}
        return fm.get("guardrails") or []
    except Exception:
        return []


def _dc_evaluate(collab, hid, seats, reviewer_seat, builder_seat, candidate_id):
    """Evaluate the §18.3 autonomous done-transition contract for a candidate (lazy import — autopilot must
    not import done_contract at module top). The machine gate an APPROVED candidate must satisfy ([C36])."""
    import done_contract as _dc

    return _dc.evaluate(
        collab,
        hid,
        seats=seats,
        reviewer_seat=reviewer_seat,
        builder_seat=builder_seat,
        candidate_id=candidate_id,
    )


def _assessment_blockers(assessment) -> list:
    """The assessment's open blocking findings rendered as ledger-shaped blocker dicts, so ``_fix_directive``/
    ``_record_sendback``/``escalation`` (which speak the ``{lane, description, regression_test}`` shape) can
    consume both reviewer-withheld and lane-confirmed findings uniformly."""
    out = []
    for f in getattr(assessment, "unresolved_findings", ()):  # already only the blocking, open findings
        source = getattr(f, "source", "reviewer")
        lane = source.split("lane:", 1)[1] if source.startswith("lane:") else source
        out.append(
            {
                "id": f.fingerprint,
                "lane": lane,
                "description": (f.evidence or f.remediation or "").strip(),
                "regression_test": None,
                "fixed": False,
            }
        )
    return out


def _escalate_pause(
    collab, hid, home, budget, *, reason, log, rid, cause=None, detail=None, blockers=None
) -> None:
    """Write the durable pause record for a terminal candidate outcome (ADR-0003): an ``escalation`` artifact
    (the human-readable "call to you"), an ``autopilot.escalation`` telemetry event, a capped status carrying
    the budget report, and an outbox ping. Read-only for everyone but a human, who clears it once resolved.

    ``blockers`` are reproduced findings to embed. The drive loop passes the
    last repair's blockers so a budget/no-progress escalation names the defect;
    a falsy value falls back to the ledger's blockers.
    """
    title = _handoff_title(collab, hid)
    label = f"{title} [{reason}]" if title else reason
    blockers = blockers if blockers else _assessment_blockers_from_ledger(collab, hid)
    run_uid = _status_run_uid(collab)
    try:
        esc.write(
            collab,
            hid,
            blockers,
            attempts=int(budget.consumed().get("work_attempts", 0)),
            title=label,
            run_uid=run_uid,
            # WHY it stopped, and what broke if it was the tooling. Without these the escalation reads
            # every stop as a confirmed defect and sends a human hunting a bug no lane ever found.
            reason=reason,
            cause=cause if isinstance(cause, dict) else None,
        )
    except Exception as e:  # the escalation artifact is best-effort; the pause + telemetry still stand
        print(f"[autopilot] could not write escalation for {hid}: {e}", file=sys.stderr)
    codes = [f"reason:{reason}", f"hid:{hid}", f"work_attempts:{budget.consumed().get('work_attempts', 0)}"]
    if cause and isinstance(cause, dict) and cause.get("reason"):
        codes.append(f"cause:{cause['reason']}")
    if detail:
        codes += [f"unmet:{n}" for n in detail]
    _emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.escalation",
        role="autopilot",
        artifact=f"handoff:{hid}",
        decision={"action": "escalate", "reason_codes": codes, "confidence": None},
        metrics={"budget": budget.report()},
    )
    _write_status(
        collab,
        phase="capped",
        pause_reason=reason,
        stage=None,
        current_hid=hid,
        last_error=f"{hid} paused ({reason}); awaiting human"[:200],
        budget=budget.report(),
    )
    _pause_note(collab, home, int(budget.limits.max_work_attempts))
    print(f"[autopilot] {hid} ESCALATED ({reason}) — durable record written; human review needed")


def _assessment_blockers_from_ledger(collab, hid: str) -> list:
    """Confirmed lane blockers from the verification ledger (best-effort) — the reproduced defects an
    escalation embeds. Falls back to ``[]`` when there is no ledger (e.g. a reviewer-only or budget pause)."""
    return _confirmed_blockers(collab, hid)


def _status_run_uid(collab) -> str | None:
    try:
        return json.loads(_status_path(collab).read_text("utf-8")).get("run_uid")
    except OSError, ValueError:
        return None


def _confirmed_blockers(collab, hid: str) -> list:
    """The CONFIRMED lane blockers for ``hid`` from its verification ledger (``[]`` if none / no ledger).
    These are the verified defects the sign-off contract refused on — the thing to fix or escalate."""
    try:
        import lanes as _lanes  # lazy: lanes imports autopilot

        led = _lanes.read_ledger(collab, hid) or {}
    except Exception:  # a missing/torn ledger must never break the drive loop
        return []
    return led.get("blockers") or []


def _fix_directive(blockers: list) -> str:
    """A builder-facing directive listing the CONFIRMED defects to fix — injected into the transcript so the
    one autonomous fix attempt is *targeted* (the builder is told exactly what the lanes broke, not left to
    guess from the reviewer's prose)."""
    lines = [
        "----- AUTOPILOT: VERIFIED DEFECTS — FIX THESE NOW -----",
        "The adversarial lanes CONFIRMED the defect(s) below in the code just shipped. Fix each at the "
        "cited location and hand back for re-review. Do NOT re-request sign-off until they are fixed.",
    ]
    for b in blockers:
        loc = str(b.get("description") or "").strip().replace("\n", " ")
        reg = b.get("regression_test")
        lines.append(f"- [{b.get('lane', 'lane')}] {loc}" + (f"  (regression test: {reg})" if reg else ""))
    return "\n".join(lines)


def _record_sendback(collab, hid, blockers, *, attempt, round_no, log, rid) -> None:
    """Log a SEND-BACK — the builder is handed the CONFIRMED defects and the exchange continues. Emits an
    ``autopilot.sendback`` telemetry event (surfaces in the dashboard activity feed) AND appends a durable
    human-readable line to ``<collab>/autopilot/sendbacks/<hid>.log`` — the audit trail of every bounce."""
    _emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.sendback",
        role="autopilot",
        artifact=f"handoff:{hid}",
        decision={
            "action": "sendback",
            "reason_codes": [f"sendback:{attempt}", f"defects:{len(blockers)}", f"round:{round_no}"]
            + [f"lane:{b.get('lane')}" for b in blockers],
            "confidence": None,
        },
    )
    try:
        d = Path(collab) / "autopilot" / "sendbacks"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{_now_utc()}  SEND-BACK #{attempt} (round {round_no}) — "
            f"{len(blockers)} confirmed defect(s) returned to the builder:"
        ]
        for b in blockers:
            desc = str(b.get("description") or "").strip().replace("\n", " ")
            reg = b.get("regression_test")
            lines.append(f"  - [{b.get('lane', 'lane')}] {desc}" + (f"  (regression: {reg})" if reg else ""))
        with open(d / f"{hid}.log", "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"[autopilot] could not write sendback log for {hid}: {e}", file=sys.stderr)


def _handoff_title(collab, hid: str) -> str | None:
    try:
        _s, p = hc._reconcile(collab, hid)
        if p:
            return (contracts.parse_handoff(Path(p)).get("frontmatter") or {}).get("title")
    except Exception:
        return None
    return None


def _pause_note(collab, home, max_rounds: int) -> None:
    """On the round cap, drop a message in the outbox so the human is pinged (via the bridge) to take over."""
    try:
        h = Path(home) if home is not None else cc.resolve_collab_home()
        ob = h / "outbox"
        ob.mkdir(parents=True, exist_ok=True)
        msg = (
            f"autopilot paused: reached the {max_rounds}-round cap for {Path(collab).name}. "
            f"Human review needed before it continues."
        )
        cc.safe_write(ob / f"{time.time_ns()}-autopilot-{cc.slugify(Path(collab).name)}.md", msg + "\n")
    except (OSError, cc.CollabError) as e:
        print(f"[autopilot] could not write pause note: {e}", file=sys.stderr)


def main(argv=None) -> int:
    cc.load_dotenv()  # load <kit root>/.env so every spawned backend (claude -p, adapters) inherits the keys
    p = argparse.ArgumentParser(prog="autopilot", description="collab-kit bounded agent-agnostic driver")
    p.add_argument("--collab", required=True, help="collab path to drive")
    p.add_argument("--home", help="$COLLAB_HOME (for seats.json + outbox); defaults to resolve_collab_home()")
    p.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        dest="max_rounds",
        help="DEPRECATED alias: overrides the work-attempt budget (Limits.max_work_attempts). "
        "Omit to use the calibrated balanced() budget (3 work attempts).",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="stay resident (daemon): after the queue goes idle, keep polling for new handoffs "
        "instead of exiting. Default is batch: run the exchange, then exit when it's idle.",
    )
    p.add_argument("--interval", type=float, default=2.0)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        home = args.home or str(cc.resolve_collab_home())
        seats = load_seats(home)
    except cc.CollabError as e:
        print(f"autopilot: {e}", file=sys.stderr)
        return 1
    if not seats:
        print("autopilot: no seats.json (or no seats) — nothing to drive", file=sys.stderr)
        return 1
    run(
        args.collab,
        seats=seats,
        max_rounds=args.max_rounds,
        watch=args.watch,
        interval=args.interval,
        home=home,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
