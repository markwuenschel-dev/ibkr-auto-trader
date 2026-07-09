"""autopilot — bounded, agent-agnostic driver that closes the builder<->reviewer loop (collab-kit slice 6).

The transport (file protocol) and trigger (watcher) already exist; this is the missing *drive* piece. It
takes a ``pending`` handoff addressed to a seat, hands the seat's agent the prompt, captures the response,
and posts it back as a new handoff to the other seat — with no human shuttling files by hand.

Agent-agnostic by design ([C34]): a backend is a **command template** — an argv that reads the prompt on
**stdin** and returns text on **stdout**. One mechanism covers headless ``claude -p -``, Grok, Codex,
Gemini, Cursor; adding one is a ``seats.json`` entry, not code. A seat with **no** CLI backend is the
human/web seat — the driver leaves its handoffs alone so they go out over the Telegram bridge.

Safety posture:
  * [C35] **bounded** — a hard ``max_rounds`` cap; the loop cannot ping-pong forever. On the cap it writes
    a pause note to the outbox and stops.
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
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402


def _load_local(alias: str, filename: str):
    """Load a sibling module by path — immune to stdlib name-shadowing (our ``trace.py`` vs the
    stdlib ``trace``; a plain ``import trace`` would bind whichever is already in sys.modules)."""
    spec = importlib.util.spec_from_file_location(alias, Path(_LIB) / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_local("collab_trace", "trace.py")

_MAX_RESP_BYTES = 256 * 1024   # process-boundary cap on an agent's stdout ([C38] — a runaway can't OOM us)
_MAX_STDERR_BYTES = 64 * 1024  # process-boundary cap on an agent's stderr
_DEFAULT_TIMEOUT = 300.0       # per-backend-invocation wall clock ([C39])
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
        except (OSError, ValueError):
            cur = {}
        cur.setdefault("schema_version", "0.1")
        cur.update(fields)
        cur["collab"] = str(collab)
        cur["updated_ts"] = _now_utc()
        cc.safe_write(p, json.dumps(cur, separators=(",", ":")) + "\n")
    except Exception as e:  # observability must never break the loop ([C15])
        print(f"[autopilot] status write failed: {e}", file=sys.stderr)


_HEARTBEAT_S = 5.0  # while a backend call is in flight, refresh the status heartbeat this often (visible liveness)


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
        return {"paused": bool(doc.get("paused", False)), "stop": bool(doc.get("stop", False)),
                "max_rounds": mr}
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# seats config
# --------------------------------------------------------------------------- #


def _seats_file(home) -> Path:
    return Path(home) / "seats.json"


def load_seats(home) -> dict:
    """Load ``seats.json`` -> ``{seat: {backend, cmd, system, timeout}}``. Refuse-on-corrupt ([data-integrity]).

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
        if not (isinstance(cfg, dict) and cfg.get("model")):
            continue  # explicit-cmd seat (or human seat) — leave untouched
        spec = models.get(cfg["model"])
        if not (isinstance(spec, dict) and isinstance(spec.get("cmd"), list) and spec["cmd"]):
            raise cc.CollabError(
                f"seat {name!r} references model {cfg['model']!r} absent from the 'models' catalog in {f}")
        margs = cfg.get("model_args") or []
        if not isinstance(margs, list):
            raise cc.CollabError(f"seat {name!r}: 'model_args' must be a list")
        cfg["cmd"] = [str(a) for a in spec["cmd"]] + [str(a) for a in margs]  # composed runnable argv
        if spec.get("unset_env") and "unset_env" not in cfg:
            cfg["unset_env"] = list(spec["unset_env"])  # inherit the model's env drops (e.g. subscription)
    return seats


def load_models(home) -> dict:
    """The top-level ``"models"`` catalog from ``seats.json`` -> ``{id: {cmd, unset_env?}}`` (``{}`` if none).

    Read-only view for the dashboard's per-seat model picker. Never raises on a missing catalog — an
    absent/empty ``"models"`` block simply means no selectable models (seats then use explicit ``cmd``)."""
    f = _seats_file(home)
    try:
        doc = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    models = doc.get("models") if isinstance(doc, dict) else None
    return models if isinstance(models, dict) else {}


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
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _fsize(f) -> int:
    """On-disk size of a capture temp file — reflects the CHILD's writes (the parent's file offset does not)."""
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
    try:
        fout = tempfile.TemporaryFile()
        ferr = tempfile.TemporaryFile()
    except OSError as e:
        raise cc.CollabError(f"backend {name!r}: cannot open capture buffers: {e}") from e
    breach = None
    try:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fout, stderr=ferr, shell=False,
                                    env=child_env)
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
        out = fout.read(_MAX_RESP_BYTES)   # bounded read-back: never the full on-disk flood
        ferr.seek(0)
        err = ferr.read(_MAX_STDERR_BYTES)
    finally:
        fout.close()
        ferr.close()
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
    ``source_roots`` (globs), ``test_path`` (pytest target run before closeout)."""
    try:
        doc = json.loads(_seats_file(home).read_text("utf-8"))
    except (OSError, ValueError, cc.CollabError):
        return None
    c = doc.get("closeout") if isinstance(doc, dict) else None
    return c if isinstance(c, dict) else None


def _run_test_suite(test_path, cwd) -> dict:
    """Run the configured test suite as a real subprocess ([C38]-style boundary); return the ledger
    ``tests`` record. No ``test_path`` -> unknown (which the contract treats as not-passed)."""
    if not test_path:
        return {"passed": None, "run_id": None}
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", str(test_path), "-q"],
                              cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=1800)
        return {"passed": proc.returncode == 0, "run_id": f"pytest-{time.time_ns()}"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"passed": False, "run_id": f"pytest-error: {str(e)[:120]}"}


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
    collect = (_run([sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_path)])
               if test_path else {"exit_code": -1, "stdout_tail": "no test_path configured"})
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
    return _lanes.run_lanes(collab, hid, seats=seats, breaker_seat=closeout.get("breaker"),
                            verifier_seat=closeout.get("verifier"), builder_seat=builder_seat,
                            reviewer_seat=reviewer_seat, source_roots=roots, source_base=base,
                            test_path=test_path, tests=tests, runner=runner, log=log)


# --------------------------------------------------------------------------- #
# one round / the bounded loop
# --------------------------------------------------------------------------- #


def _run_turn(collab, seat: str, *, seats: dict, runner, hid: str, transcript: str, round_no: int,
              counterpart_seat: str, log: str, closeout: dict | None, rid: str) -> tuple[str, str | None]:
    """Run ONE builder/reviewer turn against the already-claimed handoff ``hid``.

    A turn is *conversation*, not a handoff: it is persisted only as a reply artifact + events — no board
    entry is created or transitioned here, so the single claimed handoff is all that ever sits in
    ``claimed/`` (the "one handoff at a time" invariant, by construction). ``transcript`` is the work
    request plus every prior turn; it is fed to the seat verbatim. Returns one of:

      ``("fail", None)``       — backend errored; the caller stalls the thread (handoff stays claimed).
      ``("signed_off", raw)``  — a ``can_sign_off`` seat approved AND the evidence contract is satisfied;
                                 the handoff has been advanced ``claimed -> done`` here.
      ``("turn", raw)``        — an ordinary turn (including a sign-off the contract BLOCKED); the caller
                                 appends ``raw`` to the transcript and hands the next turn to the other seat.
    """
    cfg = _cli_seat(seats, seat)
    if cfg is None:
        return ("fail", None)  # human/web seat mid-exchange — treated as a stall (awaits a person)
    sp = f"r{round_no}:{seat}"  # turn span; child spans (fail/signoff) hang off it
    _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.round", role=seat,
               artifact=f"handoff:{hid}", span_id=sp,
               decision={"action": "start", "reason_codes": [f"seat:{seat}"], "confidence": None},
               metrics={"round_no": round_no})
    timeout_s = float(cfg.get("timeout", _DEFAULT_TIMEOUT))
    _write_status(collab, phase="thinking", active_seat=seat, current_hid=hid, round=round_no,
                  active_since=_now_utc(), timeout=timeout_s)  # active_since => elapsed; timeout => deadline
    prompt = _build_prompt(cfg.get("system"), transcript)
    t0 = time.monotonic()
    try:
        with _Heartbeat(collab):  # keep updated_ts fresh through a long agentic call (liveness, not deadline)
            raw = runner(list(cfg["cmd"]), prompt, timeout=timeout_s, unset_env=cfg.get("unset_env"))
    except cc.CollabError as e:
        lat_ms = round((time.monotonic() - t0) * 1000, 1)
        print(f"[autopilot] backend for seat {seat!r} failed on {hid}: {e}", file=sys.stderr)
        _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.round", role=seat,
                   artifact=f"handoff:{hid}", span_id=f"{sp}:fail", parent_span_id=sp,
                   decision={"action": "fail", "reason_codes": ["backend_error"], "confidence": None},
                   failure={"kind": "backend", "message": str(e)[:500]}, metrics={"latency_ms": lat_ms})
        _write_status(collab, active_seat=None, current_hid=None, last_error=str(e)[:200],
                      last_latency_ms=lat_ms)
        return ("fail", None)  # handoff stays claimed for a human; loop stops ([C39])
    lat_ms = round((time.monotonic() - t0) * 1000, 1)
    resp_bytes = len(raw.encode("utf-8", "replace"))
    relpath = _write_reply(collab, seat, _sanitize(raw))  # the turn, stored as an inert artifact
    _write_status(collab, active_seat=None, current_hid=None, last_latency_ms=lat_ms, last_error=None)
    # Sign-off is judged on this turn iff a can_sign_off seat emitted the token. The token asserts the §18.3
    # evidence contract holds, but the MACHINE — not the token — advances state, and only on a *satisfied*
    # contract (independent approver reviewer!=builder, clean ledger, source==tested). ``counterpart_seat``
    # is the other participant — the builder whose work is under review.
    if cfg.get("can_sign_off") and _SIGNOFF_RE.search(raw):
        # Build the evidence ledger on demand: run the tests + adversarial lanes for THIS handoff before
        # judging. Opt-in via the seats.json `closeout` block; without it, sign-off falls to whatever ledger
        # already exists (usually none -> blocked). The breaker/verifier are independent of the builder.
        if closeout and closeout.get("breaker") and closeout.get("verifier"):
            print(f"[autopilot] {seat} requested sign-off on {hid} -> running tests + adversarial lanes…")
            # The closeout (tests + breaker->verifier lanes) runs for MINUTES with no seat call, so without
            # its own heartbeat the dashboard reads the frozen updated_ts as "stale" though the driver is
            # working. Surface a lane phase and keep the heartbeat alive for the whole closeout.
            _write_status(collab, phase="thinking", active_seat="lanes", current_hid=hid,
                          active_since=_now_utc(), timeout=0)
            try:
                with _Heartbeat(collab):  # refresh updated_ts through the multi-minute lane phase (liveness)
                    _autoclose_ledger(collab, hid, counterpart_seat, closeout, seats=seats, runner=runner,
                                      log=log, reviewer_seat=seat)
            except (cc.CollabError, hc.HandoffNotFound) as e:
                _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.lane", role=seat,
                           artifact=f"handoff:{hid}", span_id=f"{sp}:lanes", parent_span_id=sp,
                           decision={"action": "lane_error", "reason_codes": [str(e)[:60]], "confidence": None},
                           failure={"kind": "lanes", "message": str(e)[:200]})
                print(f"[autopilot] lanes for {hid} failed: {e}")
            _write_status(collab, active_seat=None)  # lanes done; the contract eval below is instant
        import done_contract as _dc  # lazy: autopilot must not import done_contract/lanes at module top
        verdict = _dc.evaluate(collab, hid, seats=seats, reviewer_seat=seat, builder_seat=counterpart_seat)
        if verdict["satisfied"]:
            hc.done(collab, hid)  # the ONLY claimed->done transition: an accepted, contract-satisfied sign-off
            _emit_safe(he.on_autonomous_done, log, rid, hid, span_id=f"{hid}:signoff", parent_span_id=sp,
                       reviewer=seat, contract_hash=verdict["hash"])  # condition 9: recorded
            _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.autonomous_done", role=seat,
                       artifact=f"handoff:{hid}", span_id=f"{sp}:signoff", parent_span_id=sp,
                       decision={"action": "autonomous_done",
                                 "reason_codes": [f"done:{hid}", f"by:{seat}", f"reply:{relpath}",
                                                  f"contract:{verdict['hash'][:12]}"], "confidence": None},
                       metrics={"latency_ms": lat_ms, "resp_bytes": resp_bytes})
            print(f"[autopilot] {seat} SIGNED OFF {hid} -> done (contract satisfied); exchange complete")
            return ("signed_off", raw)
        unmet = [c["name"] for c in verdict["conditions"] if c["status"] != "pass"]
        _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.signoff_blocked", role=seat,
                   artifact=f"handoff:{hid}", span_id=f"{sp}:signoff_blocked", parent_span_id=sp,
                   decision={"action": "signoff_blocked", "reason_codes": [f"unmet:{n}" for n in unmet],
                             "confidence": None},
                   failure={"kind": "contract", "message": "unmet: " + ", ".join(unmet)})
        _write_status(collab, last_error=f"signoff blocked on {hid}: {', '.join(unmet)}"[:200])
        print(f"[autopilot] {seat} sign-off on {hid} BLOCKED — unmet contract: {', '.join(unmet)}")
        # fall through: an ordinary turn so the exchange continues and the block stays visible
    _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.round", role=seat,
               artifact=f"handoff:{hid}", span_id=f"{sp}:done", parent_span_id=sp,
               decision={"action": "turn", "reason_codes": [f"reply:{relpath}"], "confidence": None},
               metrics={"latency_ms": lat_ms, "resp_bytes": resp_bytes})
    print(f"[autopilot] {seat} took a turn on {hid} (round {round_no})")
    return ("turn", raw)


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


def run(collab, *, seats: dict, max_rounds: int = 6, runner=_cli_runner, watch: bool = False,
        interval: float = 2.0, home=None, log: str | None = None) -> int:
    """Drive the builder<->reviewer loop ONE THREAD AT A TIME. Returns the total rounds executed.

    A *thread* is a queued handoff addressed to a CLI seat plus the reply chain it spawns. The loop takes
    the **lowest-id** pending handoff and drives that single thread — builder<->reviewer, following each
    reply — and **only a sign-off (-> ``done/``) advances to the next handoff.** Any other outcome — the
    per-thread ``max_rounds`` cap, a backend stall, or the next step being a human seat — pings the human
    (outbox) and STOPS the run: nothing moves past the current handoff until it is resolved (you approve it
    via the dashboard, or fix what stalled and restart). ``max_rounds`` is a PER-THREAD budget, not a global
    one: a slice that needs 8 rounds gets 8. Replies the driver creates are excluded from root selection, so
    a non-closing thread can never spawn unboundedly ([C35]). ``watch=True`` keeps it resident to poll for
    newly-queued handoffs only once the board is fully drained.
    """
    log = log or _log_default(collab)
    rid = _run_id(collab)
    closeout = load_closeout(home) if home else None  # opt-in auto-lane closeout (breaker/verifier + tests)
    driven: set = set()  # roots started + replies created this run — excluded from future root selection
    import run_history as _rh  # lazy: run_history imports autopilot; avoid a module-load cycle

    # --- per-run identity (CONTRACT A). run_uid is minted ONCE here and is time-sortable: it names this
    # run's durable archive under autopilot/history/. status.json (shared across runs) is stamped with it,
    # plus the seats->model snapshot and git sha, so archive_run can rebuild run.json self-sufficiently.
    pid = os.getpid()
    started_ts = _now_utc()
    run_uid = re.sub(r"[^A-Za-z0-9]", "", started_ts) + f"-{pid}"  # e.g. 20260709T001844Z-6808
    run_git_sha = _rh.git_sha(collab)
    seats_snapshot = {name: (cfg.get("model") if isinstance(cfg, dict) else None)
                      for name, cfg in (seats or {}).items()}
    # ROTATE+WIPE the live feed so this run starts clean: the prior run archived ITS log at its own end
    # (finally, below), so truncating here only discards already-archived leftovers. Guard a fresh collab.
    try:
        _live_log = Path(_log_default(collab))
        if _live_log.exists():
            cc.safe_write(_live_log, "")
    except (OSError, cc.CollabError) as _e:
        print(f"[autopilot] could not rotate events log: {_e}", file=sys.stderr)
    live_cap = max_rounds  # last-published effective cap (launch-time default; may be raised via control.json)
    _write_status(collab, pid=pid, started_ts=started_ts, run_uid=run_uid, git_sha=run_git_sha,
                  run_seats=seats_snapshot, phase="thinking", round=0, max_rounds=max_rounds,
                  watch=watch, interval=interval, active_seat=None, current_hid=None,
                  last_latency_ms=None, last_error=None)
    try:
        return _run_loop(collab, seats=seats, max_rounds=max_rounds, runner=runner, watch=watch,
                         interval=interval, home=home, log=log, rid=rid, closeout=closeout,
                         driven=driven, live_cap=live_cap)
    finally:
        # [C15] best-effort archival on ANY exit (return, cap, stall, exception, kill) — THIS is the fix
        # for lost run evidence: even a crashed/capped run is preserved. Never masks the real return/raise.
        try:
            _rh.archive_run(collab, run_uid)
            _rh.prune(collab)
        except Exception as _e:  # broad: archival is telemetry, never worth breaking the driver's exit
            print(f"[autopilot] run archival failed (run already complete): {_e}", file=sys.stderr)


def _run_loop(collab, *, seats, max_rounds, runner, watch, interval, home, log, rid, closeout,
              driven, live_cap) -> int:
    """The bounded drive loop, factored out of :func:`run` so ``run`` can wrap it in a single try/finally
    that guarantees the run is archived on EVERY exit path. Returns the total rounds executed."""
    total_rounds = 0
    last_phase = None
    while True:
        ctrl = _read_control(collab)  # human control file — only ever pauses/stops the loop ([C36])
        if ctrl.get("stop"):
            _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                       decision={"action": "stop", "reason_codes": [], "confidence": None})
            _write_status(collab, phase="done", active_seat=None, current_hid=None)
            return total_rounds  # graceful, reversible exit: no done/, no commit — the human resumes later
            # (run() still snapshots this run into history/ in its finally — telemetry, not a done/ move)
        if ctrl.get("paused"):
            if last_phase != "paused":
                _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                           decision={"action": "pause", "reason_codes": [], "confidence": None})
                _write_status(collab, phase="paused", active_seat=None, current_hid=None)
                last_phase = "paused"
            time.sleep(interval)
            continue
        if last_phase == "paused":
            _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                       decision={"action": "resume", "reason_codes": [], "confidence": None})
            last_phase = None

        root = _next_root(collab, seats, driven)
        if root is None:  # board drained of fresh work (only driver replies / human-seat handoffs remain)
            idle_phase = "sleeping" if watch else "idle"
            if last_phase != idle_phase:
                _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.idle", role="autopilot",
                           decision={"action": "idle", "reason_codes": [], "confidence": None})
                _write_status(collab, phase=idle_phase, active_seat=None, current_hid=None)
                last_phase = idle_phase
            if not watch:
                _write_status(collab, phase="done")
                return total_rounds  # batch (default): the queue is drained -> we're done
            _write_status(collab)  # idle heartbeat: prove the daemon is still polling (crash detection)
            time.sleep(interval)  # daemon: keep polling for newly-queued handoffs
            continue

        # --- drive this ONE handoff to completion: the two seats alternate IN-MEMORY while the single
        # handoff stays claimed. Turns are conversation, not board entries, so `claimed/` never holds more
        # than this one handoff ("one handoff at a time"). Only an accepted sign-off (outcome "closed")
        # advances it to done/ and lets us pick the next root; a cap, a backend stall, or a human-seat turn
        # STOPS the run and pings the human — nothing moves past this handoff until it is resolved.
        last_phase = None
        driven.add(root)
        outcome = "capped"  # default: ran out of the per-thread round budget
        _rstate, rpath = hc._reconcile(collab, root)
        first_seat, other_seat = _to_from(Path(rpath)) if rpath else (None, None)
        first_seat, other_seat = (first_seat or "").strip(), (other_seat or "").strip()
        request = _substance(collab, Path(rpath)) if rpath else ""  # read BEFORE claim moves it out of pending/
        claimed_ok = False
        if rpath is None or _rstate != "pending":
            outcome = "stalled"  # the root vanished/advanced under us — a human needs to look
        elif _cli_seat(seats, first_seat) is None:
            outcome = "human"  # the handoff is addressed to a human/web seat — awaiting a person
        else:
            try:
                hc.claim(collab, root)  # the SINGLE claim for the whole exchange ([C36])
                claimed_ok = True
            except cc.CollabError:
                outcome = "stalled"  # lost the claim race to a concurrent driver
        if claimed_ok:
            _emit_safe(he.on_claim, log, rid, root, span_id=f"{root}:claim", parent_span_id=None, by=first_seat)
            transcript = request               # the work request; each turn is appended below
            seat, counterpart = first_seat, other_seat  # `to` acts first; `from` is the counterpart
            thread_rounds = 0
            while True:
                tctrl = _read_control(collab)
                # Live per-thread cap: a positive control.json `max_rounds` raises/lowers the ceiling
                # mid-run; otherwise the launch-time `max_rounds` stands. Re-publish into status.json on a
                # change so the dashboard hero reflects the new budget. `outcome` stays "capped" on exit.
                live_max = tctrl.get("max_rounds")
                cap = live_max if live_max else max_rounds
                if cap != live_cap:
                    _write_status(collab, max_rounds=cap)
                    live_cap = cap
                if thread_rounds >= cap:
                    break  # per-thread budget exhausted -> outcome defaults to "capped"
                if tctrl.get("stop"):
                    outcome = "stopped"
                    break  # abandon the exchange; the top-level loop emits stop + returns
                if tctrl.get("paused"):  # hold ON this handoff until the human resumes (don't drop it)
                    if last_phase != "paused":
                        _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                                   decision={"action": "pause", "reason_codes": [], "confidence": None})
                        _write_status(collab, phase="paused", active_seat=None, current_hid=None)
                        last_phase = "paused"
                    time.sleep(interval)
                    continue
                if last_phase == "paused":
                    _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                               decision={"action": "resume", "reason_codes": [], "confidence": None})
                    last_phase = None
                if _cli_seat(seats, seat) is None:
                    outcome = "human"  # the next turn belongs to a human/web seat — awaiting a person
                    break
                status, raw = _run_turn(collab, seat, seats=seats, runner=runner, hid=root,
                                        transcript=transcript, round_no=total_rounds + 1,
                                        counterpart_seat=counterpart, log=log, closeout=closeout, rid=rid)
                total_rounds += 1
                thread_rounds += 1
                if status == "fail":
                    outcome = "stalled"  # backend failed — handoff stays claimed; a human needs to look
                    break
                if status == "signed_off":
                    _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.control", role="autopilot",
                               decision={"action": "signoff", "reason_codes": [f"root:{root}"], "confidence": None})
                    outcome = "closed"
                    break  # the handoff was accepted and advanced to done/
                transcript = f"{transcript}\n\n----- {seat} -----\n{_sanitize(raw)}"  # extend the conversation
                seat, counterpart = counterpart, seat  # hand the next turn to the other seat

        if outcome == "closed":
            continue  # ONLY a sign-off advances -> select the next handoff
        if outcome == "stopped" or _read_control(collab).get("stop"):
            continue  # let the top of the loop emit the stop + return cleanly
        # capped / stalled / human -> ping the human and STOP: nothing moves past this handoff until resolved
        _emit_safe(_trace.emit, log, run_id=rid, stage="autopilot.pause", role="autopilot",
                   decision={"action": "cap", "reason_codes": [f"root:{root}", f"outcome:{outcome}"],
                             "confidence": None})
        _write_status(collab, phase="capped",
                      last_error=f"{root} not signed off ({outcome}); awaiting human")
        _pause_note(collab, home, max_rounds)
        return total_rounds


def _pause_note(collab, home, max_rounds: int) -> None:
    """On the round cap, drop a message in the outbox so the human is pinged (via the bridge) to take over."""
    try:
        h = Path(home) if home is not None else cc.resolve_collab_home()
        ob = h / "outbox"
        ob.mkdir(parents=True, exist_ok=True)
        msg = (f"autopilot paused: reached the {max_rounds}-round cap for {Path(collab).name}. "
               f"Human review needed before it continues.")
        cc.safe_write(ob / f"{time.time_ns()}-autopilot-{cc.slugify(Path(collab).name)}.md", msg + "\n")
    except (OSError, cc.CollabError) as e:
        print(f"[autopilot] could not write pause note: {e}", file=sys.stderr)


def main(argv=None) -> int:
    cc.load_dotenv()  # load <kit root>/.env so every spawned backend (claude -p, adapters) inherits the keys
    p = argparse.ArgumentParser(prog="autopilot", description="collab-kit bounded agent-agnostic driver")
    p.add_argument("--collab", required=True, help="collab path to drive")
    p.add_argument("--home", help="$COLLAB_HOME (for seats.json + outbox); defaults to resolve_collab_home()")
    p.add_argument("--max-rounds", type=int, default=6, dest="max_rounds")
    p.add_argument("--watch", action="store_true",
                   help="stay resident (daemon): after the queue goes idle, keep polling for new handoffs "
                        "instead of exiting. Default is batch: run the exchange, then exit when it's idle.")
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
    run(args.collab, seats=seats, max_rounds=args.max_rounds, watch=args.watch,
        interval=args.interval, home=home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
