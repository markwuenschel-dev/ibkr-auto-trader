"""dashboard_web — a stdlib ``http.server`` live web view of an autopilot run.

Serves a single self-contained page (inline CSS/JS, zero external assets) that polls a small JSON API
and renders run vitals + per-seat stats, the handoff board, a live event feed, and a reply viewer that
shows what each agent actually wrote. Buttons pause/resume/stop the loop and approve (advance) a handoff
— the driver never advances state ([C36]); a human, through this page, does. Light/dark theme follows the
OS and can be toggled.

Security posture (this is a single-operator LOCAL tool, not a multi-user server):
  * binds ``127.0.0.1`` only — never reachable off-box.
  * rejects any request whose ``Host`` header is not localhost — blocks DNS-rebinding.
  * state-changing POSTs require a per-run random token (embedded in the served page, sent as
    ``X-Dash-Token``) so another local page/process cannot silently drive pause/approve/nudge.
  * ``GET /api/handoff`` is read-only + host-checked; its ``hid`` is validated (``\\d{1,9}``) and resolved
    through the state machine, never joined into a path. Agent reply text is returned as data and the page
    renders it as text (``<pre>``/``textContent``), never as HTML ([C38]).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HID_RE = re.compile(r"\d{1,9}")  # a handoff id is a short zero-padded integer — never a path fragment
_SEAT_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")  # a seat name — never a path fragment / key-lookup only
_MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,60}")  # a catalog model id (e.g. gpt-5.5, grok-4.5-textonly)
_RUN_RE = re.compile(r"[0-9A-Za-z._-]{1,64}")  # a run_uid — never joined into a path (key-lookup only)

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402

_DEFAULT_PORT = 8787


def _material_snapshot(snapshot: dict) -> dict:
    """Remove render-clock fields so the stream advances only for source changes."""
    material = copy.deepcopy(snapshot)
    material.pop("ts", None)
    material.pop("freshness", None)
    material.pop("stream", None)
    for record in (material.get("health") or {}).values():
        if isinstance(record, dict):
            record.pop("updated_ts", None)
    for item in material.get("items") or []:
        if isinstance(item, dict):
            item.pop("freshness", None)
    for rows in (material.get("board") or {}).values():
        for item in rows if isinstance(rows, list) else []:
            if isinstance(item, dict):
                item.pop("freshness", None)
    for item in material.get("open") or []:
        if isinstance(item, dict):
            item.pop("freshness", None)
    return material


class _SnapshotBroker:
    """Bounded, resumable stream of authoritative full snapshots."""

    def __init__(self, snapshot_fn, *, instance_id: str | None = None, capacity: int = 512):
        self._snapshot_fn = snapshot_fn
        self.instance_id = instance_id or uuid.uuid4().hex
        self._events: deque[dict] = deque(maxlen=capacity)
        self._sequence = 0
        self._digest: str | None = None
        self._lock = threading.Lock()

    def refresh(self) -> dict:
        snapshot = self._snapshot_fn()
        encoded = json.dumps(_material_snapshot(snapshot), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock:
            if digest == self._digest and self._events:
                return self._events[-1]
            self._digest = digest
            self._sequence += 1
            generated_ts = snapshot.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            snapshot["stream"] = {
                "status": "connected",
                "instance_id": self.instance_id,
                "sequence": self._sequence,
                "generated_ts": generated_ts,
            }
            snapshot.setdefault("health", {})["stream"] = {
                "status": "healthy",
                "updated_ts": generated_ts,
                "reason": None,
            }
            event = {
                "id": f"{self.instance_id}:{self._sequence}",
                "instance_id": self.instance_id,
                "sequence": self._sequence,
                "type": "snapshot",
                "data": snapshot,
            }
            self._events.append(event)
            return event

    def events_after(self, cursor: str | None) -> list[dict]:
        with self._lock:
            if not self._events:
                return []
            current = self._events[-1]
            if not cursor:
                return [current]
            try:
                instance, raw_sequence = cursor.rsplit(":", 1)
                sequence = int(raw_sequence)
            except (ValueError, AttributeError):
                sequence = -1
                instance = ""
            oldest = self._events[0]["sequence"]
            if instance != self.instance_id or sequence > current["sequence"] or sequence < oldest - 1:
                reconciled = dict(current)
                reconciled["type"] = "reconcile"
                return [reconciled]
            return [event for event in self._events if event["sequence"] > sequence]


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "collab-dash/0.1"

    # --- infra ------------------------------------------------------------- #
    def log_message(self, *a):  # silence the default per-request stderr spam
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionError):
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _broker(self) -> _SnapshotBroker:
        broker = getattr(self.server, "broker", None)
        if broker is None:
            collab = self.server.collab  # type: ignore[attr-defined]
            home = self.server.home  # type: ignore[attr-defined]
            broker = _SnapshotBroker(lambda: dc.snapshot(collab, home))
            self.server.broker = broker  # type: ignore[attr-defined]
        return broker

    def _stream(self, cursor: str | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            keepalive_at = time.monotonic()
            while True:
                broker = self._broker()
                broker.refresh()
                events = broker.events_after(cursor)
                for event in events:
                    payload = json.dumps(event["data"], separators=(",", ":"))
                    frame = (
                        f"id: {event['id']}\n"
                        f"event: {event['type']}\n"
                        f"data: {payload}\n\n"
                    ).encode()
                    self.wfile.write(frame)
                    self.wfile.flush()
                    cursor = event["id"]
                    keepalive_at = time.monotonic()
                if not events and time.monotonic() - keepalive_at >= 10.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    keepalive_at = time.monotonic()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    # --- routes ------------------------------------------------------------ #
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parts = urllib.parse.urlsplit(self.path)
        collab = self.server.collab  # type: ignore[attr-defined]
        if parts.path == "/":
            page = _PAGE.replace("__TOKEN__", self.server.token)  # type: ignore[attr-defined]
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if parts.path == "/api/state":
            return self._json(200, dc.snapshot(collab, self.server.home))  # type: ignore[attr-defined]
        if parts.path == "/api/stream":
            query_cursor = (urllib.parse.parse_qs(parts.query).get("cursor") or [None])[0]
            cursor = self.headers.get("Last-Event-ID") or query_cursor
            return self._stream(cursor)
        if parts.path == "/api/operational":
            query = urllib.parse.parse_qs(parts.query)
            hid = (query.get("hid") or [""])[0].strip()
            if not _HID_RE.fullmatch(hid):
                return self._json(400, {"error": "bad hid"})
            try:
                cursor_raw = (query.get("cursor") or [None])[0]
                limit_raw = (query.get("limit") or ["100"])[0]
                cursor = int(cursor_raw) if cursor_raw not in (None, "") else None
                limit = int(limit_raw)
                return self._json(200, dc.operational_detail(collab, hid, cursor=cursor, limit=limit))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except hc.HandoffNotFound as e:
                return self._json(404, {"error": str(e)})
        if parts.path == "/api/handoff":
            # read-only + host-checked (no token, like /api/state). hid is validated and resolved via
            # the state machine — never joined into a path.
            hid = (urllib.parse.parse_qs(parts.query).get("hid") or [""])[0].strip()
            if not _HID_RE.fullmatch(hid):
                return self._json(400, {"error": "bad hid"})
            try:
                return self._json(200, dc.handoff_view(collab, hid))
            except hc.HandoffNotFound as e:
                return self._json(404, {"error": str(e)})
            except cc.CollabError as e:
                return self._json(500, {"error": str(e)})
        if parts.path == "/api/narrative":
            # read-only + host-checked (like /api/handoff). hid validated + resolved via the state machine.
            hid = (urllib.parse.parse_qs(parts.query).get("hid") or [""])[0].strip()
            if not _HID_RE.fullmatch(hid):
                return self._json(400, {"error": "bad hid"})
            try:
                return self._json(200, dc.narrative_view(collab, hid))
            except hc.HandoffNotFound as e:
                return self._json(404, {"error": str(e)})
            except Exception:  # a summary failure must never 500 the whole dashboard
                return self._json(500, {"error": "narrative unavailable"})
        if parts.path == "/api/runs":
            # read-only run history (newest first); entries may carry current:true for the live run.
            return self._json(200, dc.list_runs(collab))
        if parts.path == "/api/run":
            run_query = urllib.parse.parse_qs(parts.query)
            rid = (run_query.get("id") or [""])[0].strip()
            windowed = (run_query.get("window") or ["0"])[0] == "1"
            if not _RUN_RE.fullmatch(rid):
                return self._json(400, {"error": "bad run id"})
            try:  # unknown/malformed uid -> 400; a run that no longer exists on disk -> 404.
                return self._json(200, dc.run_detail(collab, rid, windowed=windowed))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
        if parts.path == "/api/compare":
            q = urllib.parse.parse_qs(parts.query)
            a = (q.get("a") or [""])[0].strip()
            b = (q.get("b") or [""])[0].strip()
            if not (_RUN_RE.fullmatch(a) and _RUN_RE.fullmatch(b)):
                return self._json(400, {"error": "bad run id"})
            try:
                return self._json(200, dc.compare_runs(collab, a, b))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if self.headers.get("X-Dash-Token") != self.server.token:  # type: ignore[attr-defined]
            return self._json(403, {"error": "bad token"})
        body = {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError, OSError:
            body = {}
        collab = self.server.collab  # type: ignore[attr-defined]
        try:
            if self.path == "/api/pause":
                return self._json(200, dc.set_paused(collab, True, by="dashboard-web"))
            if self.path == "/api/resume":
                return self._json(200, dc.set_paused(collab, False, by="dashboard-web"))
            if self.path == "/api/stop":
                return self._json(200, dc.set_stop(collab, True, by="dashboard-web"))
            if self.path == "/api/approve":
                # A HUMAN OVERRIDE, and the API says so. It checks no evidence, so the operator must
                # name themselves and say why; the route will not invent either on their behalf.
                hid = str(body.get("hid") or "").strip()
                if not _HID_RE.fullmatch(hid):
                    return self._json(400, {"error": "bad hid"})
                actor = str(body.get("actor") or "").strip()
                reason = str(body.get("reason") or "").strip()
                if not actor:
                    return self._json(400, {"error": "actor is required for a human override"})
                if not reason:
                    return self._json(400, {"error": "reason is required for a human override"})
                return self._json(200, dc.advance_handoff(collab, hid, actor=actor, reason=reason))
            if self.path == "/api/nudge":
                hid = str(body.get("hid") or "").strip()
                if not _HID_RE.fullmatch(hid):
                    return self._json(400, {"error": "bad hid"})
                return self._json(200, dc.nudge(collab, hid))
            if self.path == "/api/reopen":
                hid = str(body.get("hid") or "").strip()
                if not _HID_RE.fullmatch(hid):
                    return self._json(400, {"error": "bad hid"})
                action = str(body.get("action") or "retry")
                if action not in ("retry", "adopt"):
                    return self._json(400, {"error": "bad action"})
                # RETRY/ADOPT a paused candidate: files a durable operator request the driver consumes
                # (HandoffNotFound -> 404; already-closed -> HandoffConflict -> 409).
                return self._json(200, dc.reopen_handoff(collab, hid, action=action, by="dashboard-web"))
            if self.path == "/api/seat-model":
                seat, model = body.get("seat"), body.get("model")
                if not (
                    isinstance(seat, str)
                    and isinstance(model, str)
                    and _SEAT_RE.fullmatch(seat)
                    and _MODEL_RE.fullmatch(model)
                ):
                    return self._json(400, {"error": "bad seat/model"})
                try:  # validation failures (unknown seat/model) are BAD INPUT -> 400, not a 500
                    result = dc.set_seat_model(self.server.home, seat, model, by="dashboard-web")  # type: ignore[attr-defined]
                except cc.CollabError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, result)
            if self.path == "/api/max-turns":
                n = body.get("n")
                # bool is an int subclass — reject it; validate type + range BEFORE calling core.
                if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 50):
                    return self._json(400, {"error": "bad n"})
                try:  # core re-validates; a ValueError there is BAD INPUT -> 400, not a 500.
                    return self._json(200, dc.set_max_rounds(collab, n, by="dashboard-web"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            if self.path == "/api/start":
                mr = body.get("max_rounds")
                if mr is not None and (
                    not isinstance(mr, int) or isinstance(mr, bool) or not (1 <= mr <= 50)
                ):
                    return self._json(400, {"error": "bad max_rounds"})
                try:  # "already running" / "not found" are CONFLICTs, not server faults -> 409.
                    return self._json(
                        200, dc.start_driver(collab, self.server.home, max_rounds=mr, by="dashboard-web")
                    )  # type: ignore[attr-defined]
                except cc.CollabError as e:
                    return self._json(409, {"error": str(e)})
        except hc.HandoffNotFound as e:
            return self._json(404, {"error": str(e)})
        except hc.HandoffConflict as e:
            return self._json(409, {"error": str(e)})
        except cc.CollabError as e:
            return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found"})


def serve(collab, home=None, port: int = _DEFAULT_PORT) -> int:
    """Start the local dashboard server (blocking). Ctrl-C to stop."""
    httpd = _DashboardServer(("127.0.0.1", port), _Handler)
    httpd.collab = str(collab)  # type: ignore[attr-defined]
    httpd.home = home  # type: ignore[attr-defined]
    httpd.token = secrets.token_urlsafe(16)  # type: ignore[attr-defined]
    httpd.broker = _SnapshotBroker(lambda: dc.snapshot(httpd.collab, httpd.home))  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{port}/"
    print(f"[dashboard] serving {Path(str(collab)).name} at {url}", flush=True)
    print(f"[dashboard] control token: {httpd.token}  (embedded in the page; POSTs require it)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# the page — self-contained (inline CSS/JS, no external assets, CSP-friendly)
# --------------------------------------------------------------------------- #

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>autopilot · mission control</title>
<link id="favicon" rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7' fill='%2322b8a6'/%3E%3C/svg%3E">
<script>
  (function(){ try{ var t=localStorage.getItem('ap-theme'); if(t==='light'||t==='dark')
    document.documentElement.setAttribute('data-theme',t);}catch(e){} })();
</script>
<style>
  :root{
    --bg:#0b0e14; --surface:#12161f; --raised:#171d28; --raised2:#1c2430;
    --border:#242c3a; --border-soft:#1b222d;
    --text:#e7ebf2; --muted:#8c96a8; --faint:#5c6675;
    --claude:#ec8c60; --claude-dim:rgba(236,140,96,.13); --claude-line:rgba(236,140,96,.38);
    --gpt:#22b8a6; --gpt-dim:rgba(34,184,166,.13); --gpt-line:rgba(34,184,166,.38);
    --grok:#313131; --grok-dim:rgba(49,49,49,.50); --grok-line:rgba(120,120,120,.50);
    --gemini:#4796E3; --gemini-dim:rgba(71,150,227,.13); --gemini-line:rgba(71,150,227,.38);
    --ok:#57c46b; --warn:#e6ac48; --crit:#f0616d; --violet:#b98af0;
    --ok-dim:rgba(87,196,107,.14); --crit-dim:rgba(240,97,109,.13); --violet-dim:rgba(185,138,240,.13);
    --accent:#6f9bf6;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
    --r:12px; --r-sm:8px; color-scheme:dark;
  }
  @media (prefers-color-scheme:light){ :root{
    --bg:#f5f7fa; --surface:#ffffff; --raised:#f4f6f9; --raised2:#eceff4;
    --border:#e0e5ee; --border-soft:#eaedf3; --text:#171c26; --muted:#5a6575; --faint:#8b95a7;
    --claude:#c8663a; --claude-dim:rgba(200,102,58,.10); --claude-line:rgba(200,102,58,.34);
    --gpt:#0d9488; --gpt-dim:rgba(13,148,136,.10); --gpt-line:rgba(13,148,136,.34);
    --grok:#313131; --grok-dim:rgba(49,49,49,.08); --grok-line:rgba(120,120,120,.45);
    --gemini:#4796E3; --gemini-dim:rgba(71,150,227,.10); --gemini-line:rgba(71,150,227,.34);
    --ok:#2f9e44; --warn:#b7791f; --crit:#e03e52; --violet:#7c53d6;
    --ok-dim:rgba(47,158,68,.12); --crit-dim:rgba(224,62,82,.10); --violet-dim:rgba(124,83,214,.10);
    --accent:#3b6fe0; --shadow:0 1px 2px rgba(20,30,50,.06), 0 8px 22px rgba(20,30,50,.08); color-scheme:light;
  }}
  :root[data-theme="dark"]{
    --bg:#0b0e14; --surface:#12161f; --raised:#171d28; --raised2:#1c2430;
    --border:#242c3a; --border-soft:#1b222d; --text:#e7ebf2; --muted:#8c96a8; --faint:#5c6675;
    --claude:#ec8c60; --claude-dim:rgba(236,140,96,.13); --claude-line:rgba(236,140,96,.38);
    --gpt:#22b8a6; --gpt-dim:rgba(34,184,166,.13); --gpt-line:rgba(34,184,166,.38);
    --grok:#313131; --grok-dim:rgba(49,49,49,.50); --grok-line:rgba(120,120,120,.50);
    --gemini:#4796E3; --gemini-dim:rgba(71,150,227,.13); --gemini-line:rgba(71,150,227,.38);
    --ok:#57c46b; --warn:#e6ac48; --crit:#f0616d; --violet:#b98af0;
    --ok-dim:rgba(87,196,107,.14); --crit-dim:rgba(240,97,109,.13); --violet-dim:rgba(185,138,240,.13);
    --accent:#6f9bf6; --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28); color-scheme:dark;
  }
  :root[data-theme="light"]{
    --bg:#f5f7fa; --surface:#ffffff; --raised:#f4f6f9; --raised2:#eceff4;
    --border:#e0e5ee; --border-soft:#eaedf3; --text:#171c26; --muted:#5a6575; --faint:#8b95a7;
    --claude:#c8663a; --claude-dim:rgba(200,102,58,.10); --claude-line:rgba(200,102,58,.34);
    --gpt:#0d9488; --gpt-dim:rgba(13,148,136,.10); --gpt-line:rgba(13,148,136,.34);
    --grok:#313131; --grok-dim:rgba(49,49,49,.08); --grok-line:rgba(120,120,120,.45);
    --gemini:#4796E3; --gemini-dim:rgba(71,150,227,.10); --gemini-line:rgba(71,150,227,.34);
    --ok:#2f9e44; --warn:#b7791f; --crit:#e03e52; --violet:#7c53d6;
    --ok-dim:rgba(47,158,68,.12); --crit-dim:rgba(224,62,82,.10); --violet-dim:rgba(124,83,214,.10);
    --accent:#3b6fe0; --shadow:0 1px 2px rgba(20,30,50,.06), 0 8px 22px rgba(20,30,50,.08); color-scheme:light;
  }
  *{ box-sizing:border-box; }
  body{ margin:0; background:
      radial-gradient(1200px 500px at 78% -12%, color-mix(in srgb, var(--gpt) 8%, transparent), transparent 60%),
      radial-gradient(1000px 460px at 8% -8%, color-mix(in srgb, var(--claude) 8%, transparent), transparent 58%),
      var(--bg);
    color:var(--text); font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased; min-height:100vh; }
  .mono{ font-family:ui-monospace,"Cascadia Code","SF Mono","JetBrains Mono",Consolas,monospace; font-variant-numeric:tabular-nums; }
  .num{ font-variant-numeric:tabular-nums; }
  .eyebrow{ font-size:10.5px; text-transform:uppercase; letter-spacing:.14em; color:var(--muted); font-weight:600; }
  .muted{ color:var(--muted); }
  a{ color:var(--accent); }
  :focus-visible{ outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }
  .wrap{ padding:0 clamp(16px,2.4vw,56px) 44px; }

  header.cmd{ position:sticky; top:0; z-index:10; display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    padding:12px clamp(16px,2.4vw,56px); background:color-mix(in srgb, var(--surface) 82%, transparent);
    backdrop-filter:blur(10px); border-bottom:1px solid var(--border); }
  .brand{ display:flex; align-items:center; gap:10px; }
  .beacon{ width:10px; height:10px; border-radius:50%; background:var(--faint); position:relative; flex:none; }
  .beacon.on{ background:var(--ok); box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 22%, transparent); }
  .beacon.on::after{ content:""; position:absolute; inset:-4px; border-radius:50%;
    border:1px solid var(--ok); animation:ring 2.6s ease-out infinite; }
  @keyframes ring{ 0%{ transform:scale(.6); opacity:.9 } 100%{ transform:scale(2.1); opacity:0 } }
  .brand .t{ font-weight:650; letter-spacing:.02em; }
  .brand .repo{ color:var(--muted); font-size:12.5px; }
  .brand .repo b{ color:var(--text); font-weight:600; }
  .spacer{ flex:1; }
  .statepill{ display:inline-flex; align-items:center; gap:7px; padding:5px 12px; border-radius:999px;
    font-size:12px; font-weight:600; border:1px solid var(--muted); color:var(--muted); }
  .statepill .d{ width:7px; height:7px; border-radius:50%; background:currentColor; }
  /* Disconnected: the poll is dead, so every panel below is a FROZEN snapshot. Say so loudly and grey the
     stale content — an untouched-looking page is indistinguishable from a live one and lies by omission. */
  #offline{ display:none; position:sticky; top:0; z-index:50; background:var(--bad,#b3261e); color:#fff;
    font-size:13px; font-weight:600; padding:8px 14px; text-align:center; letter-spacing:.01em; }
  body.disconnected #offline{ display:block; }
  body.disconnected .wrap{ opacity:.45; filter:grayscale(1); pointer-events:none; }
  .live{ display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }
  .live .dot{ width:7px; height:7px; border-radius:50%; background:var(--faint); }
  .live.ok .dot{ background:var(--ok); } .live.warn .dot{ background:var(--warn); }
  button{ font:inherit; font-size:13px; cursor:pointer; border-radius:var(--r-sm); padding:6px 13px;
    background:var(--raised); color:var(--text); border:1px solid var(--border); transition:.15s; }
  button:hover{ border-color:var(--accent); transform:translateY(-1px); }
  button:disabled{ opacity:.4; cursor:default; transform:none; }
  button.ghost{ background:transparent; }
  button.primary{ background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button.primary:hover{ filter:brightness(1.06); }
  button.warn{ color:var(--warn); border-color:color-mix(in srgb,var(--warn) 45%, var(--border)); }
  button.danger{ color:var(--crit); border-color:color-mix(in srgb,var(--crit) 45%, var(--border)); }
  .iconbtn{ width:34px; height:34px; padding:0; display:grid; place-items:center; font-size:15px; }

  .hero{ margin:22px 0 20px; display:flex; border:1px solid var(--border); border-radius:var(--r);
    background:linear-gradient(180deg, color-mix(in srgb,var(--violet) 5%, var(--surface)), var(--surface));
    box-shadow:var(--shadow); overflow:hidden; }
  .hero .stripe{ width:5px; flex:none; background:var(--faint); }
  .hero .body{ padding:20px 22px; flex:1; display:flex; gap:26px; flex-wrap:wrap; align-items:flex-start; }
  .hero .lead{ min-width:min(100%,320px); flex:1; }
  .hero h1{ margin:6px 0 8px; font-size:30px; line-height:1.06; letter-spacing:-.01em; text-wrap:balance; font-weight:700; }
  .hero p{ margin:0; color:var(--muted); max-width:62ch; }
  .hero .cta{ display:flex; gap:9px; flex-wrap:wrap; margin-top:16px; }
  .hero .cta:empty{ display:none; }
  .metrics{ display:flex; gap:22px; flex-wrap:wrap; align-content:flex-start; padding-top:4px; }
  .metric .v{ font-size:22px; font-weight:680; letter-spacing:-.01em; }
  .metric .v.mono{ font-size:20px; }
  .metric .k{ font-size:10.5px; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); margin-top:1px; }

  .grid{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:900px){ .grid{ grid-template-columns:1fr; } }
  .viewtabs{ display:flex; gap:6px; margin:0 0 16px; overflow-x:auto; }
  .viewtabs button[role="tab"]{ white-space:nowrap; }
  .viewtabs button[aria-selected="true"]{ background:var(--accent); border-color:var(--accent); color:#fff; }
  .viewpanel[hidden]{ display:none!important; }
  .viewpanel[hidden]{ display:none!important; }
  .card{ background:var(--surface); border:1px solid var(--border); border-radius:var(--r); padding:16px 17px; box-shadow:var(--shadow); }
  .card > h2{ margin:0 0 14px; font-size:12px; text-transform:uppercase; letter-spacing:.13em; color:var(--muted);
    display:flex; align-items:center; justify-content:space-between; gap:10px; font-weight:650; }
  .card > h2 .r{ display:inline-flex; gap:10px; align-items:center; font-size:11px; letter-spacing:.02em; text-transform:none; font-weight:500; }
  .full{ grid-column:1/-1; }

  .vendor{ margin-bottom:8px; } .vendor:last-child{ margin-bottom:0; }
  .vendor .vh{ display:flex; align-items:center; gap:8px; margin:6px 0 8px; flex-wrap:wrap; }
  .vendor .vh .tag{ font-size:11px; font-weight:700; padding:2px 9px; border-radius:999px; }
  .vendor.claude .vh .tag{ color:var(--claude); background:var(--claude-dim); border:1px solid var(--claude-line); }
  .vendor.gpt .vh .tag{ color:var(--gpt); background:var(--gpt-dim); border:1px solid var(--gpt-line); }
  .vendor.grok .vh .tag{ color:#fff; background:#313131; border:1px solid #313131; }
  .vendor.gemini .vh .tag{ color:var(--gemini); background:var(--gemini-dim); border:1px solid var(--gemini-line); }
  .vendor .vh .role{ font-size:11px; color:var(--faint); }
  .seat{ display:grid; grid-template-columns:auto 1fr auto; gap:12px; align-items:start;
    padding:10px 11px; border-radius:var(--r-sm); border:1px solid var(--border-soft); background:var(--raised); margin-bottom:7px; }
  .seat:last-child{ margin-bottom:0; }
  .seat.active{ border-color:var(--gpt-line); box-shadow:0 0 0 1px var(--gpt-line) inset; }
  .seat.c.active{ border-color:var(--claude-line); box-shadow:0 0 0 1px var(--claude-line) inset; }
  .seat.k.active{ border-color:var(--grok-line); box-shadow:0 0 0 1px var(--grok-line) inset; }
  .seat.m.active{ border-color:var(--gemini-line); box-shadow:0 0 0 1px var(--gemini-line) inset; }
  .seat .av{ width:30px; height:30px; border-radius:9px; display:grid; place-items:center; font-weight:700; font-size:13px; flex:none; }
  .seat.c .av{ color:var(--claude); background:var(--claude-dim); border:1px solid var(--claude-line); }
  .seat.g .av{ color:var(--gpt); background:var(--gpt-dim); border:1px solid var(--gpt-line); }
  .seat.k .av{ color:#fff; background:#313131; border:1px solid #4a4a4a; }
  .seat.m .av{ color:var(--gemini); background:var(--gemini-dim); border:1px solid var(--gemini-line); }
  .seat .nm{ font-weight:640; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .seat .nm .model{ font-size:11px; color:var(--muted); padding:1px 7px; border-radius:999px; border:1px solid var(--border); }
  .seat .sub{ font-size:11.5px; color:var(--muted); margin-top:2px; }
  .seat .now{ font-size:11.5px; margin-top:4px; }
  .seatmodel{ margin-top:7px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .modelsel{ font:inherit; font-size:11.5px; color:var(--text); background:var(--raised2);
    border:1px solid var(--border); border-radius:var(--r-sm); padding:3px 8px; max-width:100%; }
  .modelsel:hover{ border-color:var(--accent); } .modelsel:focus-visible{ outline:2px solid var(--accent); }
  .modelnote{ font-size:10.5px; color:var(--faint); }
  .seat .st{ text-align:right; font-size:11px; color:var(--muted); white-space:nowrap; }
  .seat .st .b{ color:var(--text); font-weight:600; }
  .lbar{ height:4px; border-radius:3px; background:var(--raised2); overflow:hidden; margin-top:7px; }
  .lbar>i{ display:block; height:100%; border-radius:3px; }
  .badge-auth{ font-size:9.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--gpt);
    border:1px solid var(--gpt-line); border-radius:5px; padding:1px 5px; }

  .lanehead{ display:flex; gap:8px; align-items:center; }
  .chip{ display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600; border:1px solid var(--border); }
  .chip.ok{ color:var(--ok); border-color:color-mix(in srgb,var(--ok) 40%,var(--border)); background:var(--ok-dim); }
  .chip.crit{ color:var(--crit); border-color:color-mix(in srgb,var(--crit) 40%,var(--border)); background:var(--crit-dim); }
  .lane{ display:flex; gap:11px; align-items:flex-start; padding:10px 6px 10px 12px; border-radius:var(--r-sm);
    border:1px solid var(--border-soft); background:var(--raised); border-left:3px solid var(--faint); margin-bottom:7px; }
  .lane:last-child{ margin-bottom:0; }
  .lane.hit{ border-left-color:var(--crit); } .lane.clean{ border-left-color:var(--ok); }
  .lane .verdict{ width:22px; height:22px; border-radius:6px; display:grid; place-items:center; font-size:12px; font-weight:700; flex:none; margin-top:1px; }
  .lane.hit .verdict{ color:var(--crit); background:var(--crit-dim); }
  .lane.clean .verdict{ color:var(--ok); background:var(--ok-dim); }
  .lane .ln{ flex:1; } .lane .ln .name{ font-weight:600; }
  .lane .ln .flow{ font-size:11px; color:var(--muted); margin-top:3px; }
  .lane .ln .flow .g{ color:var(--gpt); } .lane .ln .flow .arrow{ color:var(--faint); }
  .lane .tally{ text-align:right; font-size:11px; color:var(--muted); white-space:nowrap; }
  .lane .tally .c{ color:var(--crit); font-weight:600; } .lane .tally .r{ color:var(--ok); font-weight:600; }

  .pipe{ display:flex; align-items:stretch; gap:0; flex-wrap:wrap; }
  .stage{ flex:1; min-width:160px; padding:2px 16px; position:relative; }
  .stage:not(:last-child)::after{ content:"\203a"; position:absolute; right:-4px; top:26px; color:var(--faint); font-size:20px; }
  .stage .sh{ display:flex; align-items:baseline; gap:8px; margin-bottom:8px; }
  .stage .sh .n{ font-size:24px; font-weight:700; letter-spacing:-.02em; }
  .stage .sh .l{ font-size:10.5px; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); }
  .hchip{ display:flex; align-items:center; gap:8px; padding:6px 9px; border-radius:7px; background:var(--raised);
    border:1px solid var(--border-soft); font-size:12px; margin-bottom:6px; cursor:pointer; }
  .hchip:hover{ border-color:var(--accent); }
  .hchip .id{ font-weight:640; } .hchip .rt{ color:var(--muted); font-size:11px; margin-left:auto; white-space:nowrap; }
  .hchip .rt .c{ color:var(--claude); } .hchip .rt .g{ color:var(--gpt); }
  .hchip .rt .k{ color:#c8c8c8; } .hchip .rt .m{ color:var(--gemini); }
  .stage .more{ font-size:11.5px; color:var(--faint); padding:2px; }

  .feed .ev{ display:grid; grid-template-columns:64px 18px 1fr auto; gap:10px; align-items:center;
    padding:6px 4px; border-bottom:1px solid var(--border-soft); font-size:12.5px; }
  .feed .ev:last-child{ border-bottom:0; }
  .feed .ev.clk{ cursor:pointer; border-radius:6px; }
  .feed .ev.clk:hover{ background:var(--raised); }
  .feed .ev .ts{ color:var(--faint); font-size:11.5px; } .feed .ev .ic{ text-align:center; }
  .feed .ev .msg b{ font-weight:640; } .feed .ev .lat{ color:var(--muted); font-size:11px; }
  .role-c{ color:var(--claude); } .role-g{ color:var(--gpt); }
  .role-k{ color:#c8c8c8; } .role-m{ color:var(--gemini); }
  .v-ok{ color:var(--ok); } .v-crit{ color:var(--crit); } .v-violet{ color:var(--violet); } .v-warn{ color:var(--warn); }
  #empty{ padding:40px 16px; text-align:center; color:var(--muted); }

  footer{ margin-top:22px; color:var(--faint); font-size:11.5px; display:flex; gap:16px; flex-wrap:wrap; align-items:center; }
  .legdot{ display:inline-flex; align-items:center; gap:6px; }
  .legdot i{ width:9px; height:9px; border-radius:3px; display:inline-block; }

  .turnbox{ display:inline-flex; align-items:center; gap:6px; padding:2px 4px 2px 9px; border-radius:999px;
    border:1px solid var(--border); background:var(--raised); }
  .turnbox .tlbl{ font-size:10px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); font-weight:600; }
  .turnbox input{ font:inherit; font-size:12.5px; width:52px; color:var(--text); background:var(--raised2);
    border:1px solid var(--border); border-radius:var(--r-sm); padding:3px 6px; }
  .turnbox input:hover,.turnbox input:focus-visible{ border-color:var(--accent); }
  .turnbox button{ padding:4px 10px; }

  .runs{ display:flex; flex-direction:column; gap:0; }
  .runrow{ display:grid; grid-template-columns:auto 1fr auto; gap:10px 14px; align-items:center;
    padding:8px 6px 8px 10px; border-bottom:1px solid var(--border-soft); border-left:3px solid transparent; cursor:pointer; }
  .runrow:last-child{ border-bottom:0; }
  .runrow:hover{ background:var(--raised); }
  .runrow.current{ border-left-color:var(--gpt); background:var(--gpt-dim); }
  .runrow .rchk{ display:grid; place-items:center; }
  .runrow .rmeta{ display:flex; flex-wrap:wrap; gap:4px 12px; align-items:baseline; min-width:0; }
  .runrow .rmeta .when{ font-weight:640; } .runrow .rmeta .uid{ color:var(--faint); font-size:11px; }
  .runrow .rmeta .sub{ color:var(--muted); font-size:11.5px; }
  .runrow .rtags{ display:flex; gap:6px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
  .rtag{ font-size:10.5px; padding:1px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); white-space:nowrap; }
  .rtag.livepin{ color:var(--gpt); border-color:var(--gpt-line); background:var(--gpt-dim); font-weight:600; }
  .runbar{ display:flex; align-items:center; gap:10px; margin-top:10px; flex-wrap:wrap; }
  .cmptbl{ width:100%; border-collapse:collapse; font-size:12.5px; }
  .cmptbl th,.cmptbl td{ text-align:left; padding:6px 12px; border-bottom:1px solid var(--border-soft); vertical-align:top; }
  .cmptbl th{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; font-weight:600; }
  .cmptbl td.k{ color:var(--muted); white-space:nowrap; } .cmptbl td.v{ font-variant-numeric:tabular-nums; }
  .cmptbl td.d.up{ color:var(--crit); } .cmptbl td.d.down{ color:var(--ok); } .cmptbl td.d.same{ color:var(--faint); }
  .narr{ max-width:100%; } .narr.collapsed{ display:none; }
  .narr .nh1{ font-size:17px; font-weight:700; letter-spacing:-.01em; margin:2px 0 4px; text-wrap:balance; }
  .narr .nh2{ font-size:10.5px; text-transform:uppercase; letter-spacing:.13em; color:var(--muted);
    font-weight:650; margin:0 0 8px; }
  .narr .np{ margin:0 0 9px; color:var(--text); }
  .narr .np.lead{ font-size:14.5px; max-width:92ch; margin-bottom:14px; }
  .narr .nlist{ margin:0; display:flex; flex-direction:column; gap:6px; }
  .narr .nli{ position:relative; padding-left:17px; }
  .narr .nli::before{ content:"\203a"; position:absolute; left:3px; top:-1px; color:var(--faint); font-weight:700; }
  .narr .nli.blk{ color:var(--warn); } .narr .nli.blk::before{ content:"\26a0"; color:var(--warn); }
  .narr code{ background:var(--raised2); border:1px solid var(--border-soft); border-radius:5px;
    padding:0 5px; font-size:12px; font-family:ui-monospace,Consolas,monospace; }
  .narr .none{ color:var(--muted); }
  /* fill the card width: context sections tile into auto-fit columns; the timeline spans full width. */
  .narr .ncols{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
    gap:18px 32px; align-items:start; margin-top:4px; }
  .narr .nsec{ min-width:0; }
  .narr .nsec.wide{ grid-column:1/-1; padding-top:14px; border-top:1px solid var(--border-soft); }
  .narr .nsec.wide + .nsec{ padding-top:14px; border-top:1px solid var(--border-soft); }
  .narr .nsec.turns .nlist{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
    gap:8px 30px; }
  #narrToggle{ transition:transform .15s; } #narrCard.folded #narrToggle{ transform:rotate(-90deg); }
  #viewer{ position:fixed; inset:0; background:rgba(0,0,0,.55); display:none; align-items:center; justify-content:center; z-index:40; padding:16px; }
  #viewer .box{ background:var(--surface); border:1px solid var(--border); border-radius:14px; max-width:800px; width:100%; max-height:86vh; display:flex; flex-direction:column; box-shadow:var(--shadow); }
  #viewer .top{ display:flex; justify-content:space-between; align-items:center; padding:13px 17px; border-bottom:1px solid var(--border); }
  #viewer .fm{ display:flex; flex-wrap:wrap; gap:6px 16px; padding:10px 17px; font-size:12px; color:var(--muted); border-bottom:1px solid var(--border); }
  #viewer pre{ margin:0; padding:17px; overflow:auto; white-space:pre-wrap; word-break:break-word;
    font:12.5px/1.55 ui-monospace,Consolas,monospace; color:var(--text); }
  .rbadge{ font-size:10px; color:#fff; background:var(--accent); border-radius:5px; padding:1px 6px; }
  #toasts{ position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:50; }
  .toast{ background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--accent);
    border-radius:8px; padding:9px 13px; font-size:12.5px; max-width:340px; box-shadow:var(--shadow); }
  .toast.ok{ border-left-color:var(--ok); } .toast.err{ border-left-color:var(--crit); }
  .opcounts,.healthgrid{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:7px; }
  .opchip,.healthitem{ border:1px solid var(--border); border-radius:8px; padding:7px 9px; background:rgba(128,128,128,.04); }
  .opchip button{ width:100%; text-align:left; background:none; border:0; color:inherit; padding:0; }
  .healthitem b,.opchip b{ display:block; font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
  .healthitem span,.opchip span{ display:block; margin-top:3px; font-size:11px; color:var(--muted); }
  .opmeta{ display:block; margin-top:5px; font-size:10.5px; color:var(--muted); line-height:1.35; }
  .filterbar{ display:flex; gap:8px; align-items:center; margin:0 0 10px; color:var(--muted); font-size:11px; }
  .filterbar input{ min-width:180px; flex:1; border:1px solid var(--border); border-radius:6px; padding:6px 8px; background:var(--panel); color:var(--text); }
  @media (prefers-reduced-motion:reduce){ *{ animation:none!important; transition:none!important; } }
</style></head>
<body>
<div id="offline" role="alert">⚠ DISCONNECTED — the dashboard server is not responding. Everything below is a frozen snapshot, not live.</div>
<header class="cmd">
  <div class="brand">
    <span class="beacon" id="beacon" aria-hidden="true"></span>
    <span class="t">autopilot</span>
    <span class="repo">· <b id="repo">—</b></span>
  </div>
  <span class="spacer"></span>
  <span class="statepill" id="statePill"><span class="d"></span><span id="stateTxt">—</span></span>
  <span class="live" id="live"><span class="dot"></span><span id="liveTxt">—</span></span>
  <button class="iconbtn ghost" id="theme" aria-label="Toggle theme" onclick="toggleTheme()">&#9790;</button>
  <span class="turnbox" title="Work attempt limit — human sign-off gate">
    <span class="tlbl">attempts</span>
    <input type="number" min="1" max="50" id="maxturns" class="mono" aria-label="Max work attempts">
    <button class="ghost" id="btnTurns" onclick="setTurns()">Set</button>
  </span>
  <button class="primary" id="btnStart" onclick="startRun()" title="Spawn the driver process against this collab (uses the cap at left)">▶ Start</button>
  <button class="ghost" id="btnPause" onclick="ctl('pause')">Pause</button>
  <button class="ghost" id="btnResume" onclick="ctl('resume')">Resume</button>
  <button class="danger ghost" id="btnStop" onclick="doStop()">Stop</button>
</header>

<main class="wrap">
  <section class="hero" id="hero">
    <div class="stripe" id="heroStripe" aria-hidden="true"></div>
    <div class="body">
      <div class="lead">
        <div class="eyebrow" id="heroEyebrow"></div>
        <h1 id="heroTitle">connecting…</h1>
        <p id="heroSub"></p>
        <div class="cta" id="heroCta"></div>
      </div>
      <div class="metrics" id="heroMetrics"></div>
    </div>
  </section>

  <div id="empty" style="display:none">Driver not running — start autopilot to see live activity.</div>

  <nav class="viewtabs" role="tablist" aria-label="Dashboard views">
    <button role="tab" id="tab-operator" aria-controls="view-operator" aria-selected="true" tabindex="0">Operator timeline</button>
    <button role="tab" id="tab-models" aria-controls="view-models" aria-selected="false" tabindex="-1">Model activity</button>
    <button role="tab" id="tab-quality" aria-controls="view-quality" aria-selected="false" tabindex="-1">Validation &amp; quality</button>
    <button role="tab" id="tab-diagnostics" aria-controls="view-diagnostics" aria-selected="false" tabindex="-1">Diagnostics</button>
  </nav>

  <div class="grid viewpanel" id="view-operator" role="tabpanel" aria-labelledby="tab-operator">
    <section class="card full"><h2>Operator timeline <span class="r">what · why · consequence · next action</span></h2>
      <div id="operatorTimeline"></div></section>
    <section class="card full" id="narrCard" style="display:none"><h2>What happened
      <span class="r"><span id="narrMeta"></span>
      <button class="iconbtn ghost" id="narrToggle" aria-label="Collapse" aria-expanded="true"
        onclick="toggleNarr()" title="Collapse / expand">&#9662;</button></span></h2>
      <div id="narr" class="narr"></div></section>

    <section class="card full"><h2>Handoff pipeline <span class="r">state machine · newest first</span></h2>
      <div class="pipe" id="pipe"></div></section>

    <section class="card full"><h2>Activity <span class="r">recent actor turns &amp; validation lanes</span></h2>
      <div class="feed" id="feed"></div></section>
  </div>

  <div class="grid viewpanel" id="view-models" role="tabpanel" aria-labelledby="tab-models" hidden>
    <section class="card"><h2>Agents
      <span class="r"><span class="legdot"><i style="background:var(--claude)"></i>Claude</span>
      <span class="legdot"><i style="background:var(--gpt)"></i>OpenAI</span>
      <span class="legdot"><i style="background:#313131;outline:1px solid #6a6a6a"></i>Grok</span>
      <span class="legdot"><i style="background:var(--gemini)"></i>Gemini</span></span></h2>
      <div id="agents"></div></section>
    <section class="card"><h2>Execution roster <span class="r">planned → transport → telemetry</span></h2>
      <label class="filterbar">Filter current retained window
        <input id="modelFilter" aria-label="Filter execution roster" type="search" oninput="modelShowAll=false;renderModelActivity(last)">
      </label>
      <div id="modelActivity"></div></section>
  </div>

  <div class="grid viewpanel" id="view-quality" role="tabpanel" aria-labelledby="tab-quality" hidden>
    <section class="card" id="lanesCard"><h2>Adversarial lanes <span class="r lanehead" id="lanehead"></span></h2>
      <div id="lanes"></div></section>
    <section class="card"><h2>Quality workspace <span class="r">requirements · validations · dispositions</span></h2>
      <label class="filterbar">Filter current retained window
        <input id="qualityFilter" aria-label="Filter quality evidence window" type="search" oninput="renderQuality(last)">
      </label>
      <div id="qualityWorkspace"></div></section>
  </div>

  <div class="grid viewpanel" id="view-diagnostics" role="tabpanel" aria-labelledby="tab-diagnostics" hidden>
    <section class="card full" id="operationsCard"><h2>Operational state
      <span class="r" id="transportState">RECONNECTING</span></h2>
      <div class="opcounts" id="opcounts"></div>
      <h2 style="margin-top:14px">Health <span class="r">separate evidence dimensions</span></h2>
      <div class="healthgrid" id="healthgrid"></div></section>
    <section class="card full" id="runsCard"><h2>Run history <span class="r">newest first · click a row for detail</span></h2>
      <div class="runs" id="runs"></div>
      <div class="runbar" id="runbar" style="display:none">
        <span class="muted" id="cmpHint">Select two runs to compare.</span>
        <span class="spacer" style="flex:1"></span>
        <button class="primary" id="btnCompare" onclick="doCompare()" disabled>Compare</button>
      </div></section>
  </div>

  <footer id="foot"></footer>
</main>

<div id="viewer" role="dialog" aria-modal="true" aria-labelledby="v-title" onclick="if(event.target===this)closeViewer()">
  <div class="box"><div class="top"><b id="v-title">handoff</b>
    <span><span class="rbadge" id="v-reply" style="display:none">reply</span>
    <button onclick="closeViewer()" aria-label="Close">&times;</button></span></div>
    <div class="fm" id="v-fm"></div><pre id="v-body"></pre></div>
</div>

<div id="rviewer" role="dialog" aria-modal="true" aria-labelledby="rv-title" onclick="if(event.target===this)closeRViewer()">
  <div class="box"><div class="top"><b id="rv-title">run</b>
    <button onclick="closeRViewer()" aria-label="Close">&times;</button></div>
    <div class="fm" id="rv-fm"></div>
    <div style="overflow:auto; padding:6px 17px 17px" id="rv-body"></div></div>
</div>
<div id="toasts" aria-live="polite"></div>

<script>
const TOKEN="__TOKEN__";
let last=null, connected=true;
let stream=null, streamState="RECONNECTING", streamInstance=null, streamSequence=0, streamCursor="";
let reconnectAttempt=0, reconcileBusy=false;
const RECONNECT_DELAYS=[1000,2000,4000,8000,16000,30000];
let narrHid=null, narrData=null;   // the handoff the "What happened" card is showing + its fetched narrative
let narrSigLast=null;              // last work-signature we fetched for -> refetch when the story actually moves
const $=id=>document.getElementById(id);
const esc=s=>(s==null?"":String(s));
function el(tag,cls,txt){const e=document.createElement(tag); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e;}
const MODEL_STATE_LABELS={
  queued:"Queued",starting:"Starting",connecting:"Connecting to gateway",
  gateway_accepted:"Gateway accepted request",generating:"Generating",streaming:"Streaming",
  waiting_for_tool:"Waiting for tool",waiting_for_evaluator:"Waiting for evaluator",
  completed:"Completed",failed:"Failed",timed_out:"Timed out",cancelled:"Cancelled",
  retrying:"Retrying",skipped:"Skipped",rejected:"Rejected",accepted:"Accepted",
  superseded:"Superseded",telemetry_verified:"Telemetry verified",
  telemetry_failed:"Telemetry export failed",telemetry_reconciled:"Telemetry reconciled",
  provider_returned:"Provider returned",gateway_reached:"Gateway reached",
  failed_before_invocation:"Failed before invocation",invoked:"Invoked",selected:"Selected",
  configured:"Configured",disabled:"Disabled"
};
function modelStateLabel(state){ return MODEL_STATE_LABELS[state]||String(state||"Unknown state").replace(/_/g," "); }
function toolActivitySummary(items){
  const events=Array.isArray(items)?items:[]; if(!events.length) return "";
  const latest=events[events.length-1]||{}, phase=latest.phase==="tool_started"?"started":latest.phase==="tool_completed"?"completed":modelStateLabel(latest.state);
  return events.length+" retained tool lifecycle event"+(events.length===1?"":"s")+" · latest "+(latest.tool_name||"tool")+" "+phase+(latest.result_status?" · "+latest.result_status:"")+(latest.event_ts?" · "+(fmtET(latest.event_ts)||latest.event_ts):"");
}
const VIEW_IDS=["operator","models","quality","diagnostics"];
function activateView(view,focus){
  if(!VIEW_IDS.includes(view)) view="operator";
  VIEW_IDS.forEach(name=>{ const tab=$("tab-"+name), panel=$("view-"+name), on=name===view;
    tab.setAttribute("aria-selected",on?"true":"false"); tab.tabIndex=on?0:-1; panel.hidden=!on; });
  try{ localStorage.setItem("ap-view",view); }catch(e){}
  if(focus) $("tab-"+view).focus();
}
function tabKeydown(e){ const current=VIEW_IDS.indexOf(e.currentTarget.id.replace("tab-","")); let next=null;
  if(e.key==="ArrowRight") next=(current+1)%VIEW_IDS.length;
  else if(e.key==="ArrowLeft") next=(current-1+VIEW_IDS.length)%VIEW_IDS.length;
  else if(e.key==="Home") next=0;
  else if(e.key==="End") next=VIEW_IDS.length-1;
  if(next!=null){ e.preventDefault(); activateView(VIEW_IDS[next],true); }
}
VIEW_IDS.forEach(view=>{ const tab=$("tab-"+view); tab.onclick=()=>activateView(view,false); tab.onkeydown=tabKeydown; });
try{ activateView(localStorage.getItem("ap-view")||"operator",false); }catch(e){ activateView("operator",false); }
function fmtms(ms){ if(ms==null) return "-"; return ms<1000? Math.round(ms)+"ms" : (ms/1000).toFixed(1)+"s"; }
function fmtage(s){ if(s==null) return ""; s=Math.floor(s); return s<60? s+"s" : s<3600? Math.floor(s/60)+"m" : Math.floor(s/3600)+"h"; }
// Timestamps are stored UTC; the dashboard shows them in US Eastern (America/New_York — handles EST/EDT).
// Time-only for TODAY; date-stamped otherwise. A bare "18:20" on a run from a previous day reads as "just
// now" and turns stale history into an apparent live failure — always say the day when it isn't today.
function fmtET(iso){ if(!iso) return ""; try{
    const d=new Date(iso), TZ="America/New_York";
    const day=x=>x.toLocaleDateString("en-US",{timeZone:TZ});
    const t=d.toLocaleTimeString("en-US",{timeZone:TZ,hour12:false});
    return day(d)===day(new Date())? t : d.toLocaleDateString("en-US",{timeZone:TZ,month:"short",day:"numeric"})+" "+t;
  }catch(e){ return (iso||"").slice(11,19); } }
function fmtdur(s){ if(s==null||s<0) return "-"; s=Math.floor(s); const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
  return h? h+"h"+m+"m" : m? m+"m"+ss+"s" : ss+"s"; }
function fmtbytes(b){ if(b==null) return ""; return b<1024? b+"B" : (b/1024).toFixed(1)+"KB"; }
function maxWorkAttempts(s){ const st=(s&&s.status)||{}, plan=(s&&s.run_plan)||{}, rounds=plan.rounds||{};
  const work=(((st.budget||{}).budgets||{}).work_attempts)||{};
  return st.max_rounds!=null?st.max_rounds:(rounds.maximum!=null?rounds.maximum:(work.limit!=null?work.limit:null)); }

const SEAT_ORDER=["builder","reviewer","breaker","verifier"];
function seatSlot(n){ let i=SEAT_ORDER.indexOf(n); if(i<0){ SEAT_ORDER.push(n); i=SEAT_ORDER.length-1; } return i; }
// Vendor code from a seat's launcher/model (keyword match), falling back to the old builder=claude split.
// c=Claude, g=OpenAI, k=Grok/xAI, m=Gemini/Google. Detection is by substring so it works whether the
// vendor shows up in the launcher, the model id, or the seat name.
function vendorOf(m,name){ const t=(((m&&m.launcher)||"")+" "+((m&&m.model)||"")+" "+(name||"")).toLowerCase();
  if(t.indexOf("claude")>=0) return "c";
  if(t.indexOf("grok")>=0) return "k";
  if(t.indexOf("gemini")>=0) return "m";
  if(t.indexOf("gpt")>=0||t.indexOf("openai")>=0||t.indexOf("chatgpt")>=0) return "g";
  return name==="builder"?"c":"g"; }
function seatVendor(n){ return vendorOf((last&&last.seats&&last.seats[n])||null, n); }
function vColor(n){ return {c:"var(--claude)",g:"var(--gpt)",k:"#c8c8c8",m:"var(--gemini)"}[seatVendor(n)]||"var(--gpt)"; }
// Activity/pipeline role color class — all four vendors, not the old Claude-vs-OpenAI binary.
function roleCls(n){ return {c:"role-c",g:"role-g",k:"role-k",m:"role-m"}[seatVendor(n)]||"role-g"; }
function seatVCls(n){ return seatVendor(n); }  // c|g|k|m — for hchip spans
function seatModel(n){ const m=(last&&last.seats&&last.seats[n])||null;
  // While a run is LIVE, prefer the model the driver actually composed at start (status.run_seats — ids only,
  // held in the driver's memory) over the on-disk seats.json value, which a mid-run edit would desync. Keep
  // the launcher from _seat_models (run_seats carries no launcher). After a run ends, fall back to disk.
  const st=(last&&last.status)||null;
  const running = st && st.phase && st.phase!=="done" && st.phase!=="capped";
  const live = running && st.run_seats && st.run_seats[n];
  const mo=live||(m&&m.model), la=m&&m.launcher; if(!mo&&!la) return null;
  if(mo&&la&&la!=="python") return la+" · "+mo; return mo||la||null; }
// A finding string (breaker "FINDING: <path> -> <trigger>", verifier "VERDICT: CONFIRMED <path> <trigger>")
// split into segments: [what/where, what triggers it, what breaks]. Tolerant — no arrow -> single segment.
const _PATH_RE=/([A-Za-z0-9_./\\-]+\.[A-Za-z]{1,6}(?::\d+(?:-\d+)?)?)/g;
function parseFinding(txt){ let t=String(txt||"").trim();
  t=t.replace(/^\s*(FINDING|VERDICT)\s*:?\s*/i,"").replace(/^\s*(CONFIRMED|REFUTED)\s+/i,"");
  const parts=t.split(/\s*(?:->|→)\s*/).map(x=>x.trim()).filter(Boolean); return parts.length?parts:[t]; }
// The newest adversarial-lane event as a one-line play-by-play (null if none) — the live closeout narration.
function latestLaneLine(s){ const evs=s.events||[];
  for(let i=evs.length-1;i>=0;i--){ const ev=evs[i]; if((ev.stage||"")!=="autopilot.lane") continue;
    const dec=ev.decision||{}, act=dec.action||"", rc=dec.reason_codes||[];
    const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    if(act==="breaker"){ const nf=(rc.find(x=>x.indexOf("findings:")===0)||"findings:0").slice(9);
      return "breaker probing "+ln+" — "+nf+" finding"+(nf==="1"?"":"s"); }
    if(act==="verdict"){ const vd=(rc.find(x=>x.indexOf("verdict:")===0)||"verdict:?").slice(8);
      return "verifier adjudicating "+ln+" → "+vd; }
    const cf=(rc.find(x=>x.indexOf("confirmed:")===0)||"confirmed:0").slice(10);
    const rf=(rc.find(x=>x.indexOf("refuted:")===0)||"refuted:0").slice(8);
    return "lane "+ln+" done · "+cf+" confirmed / "+rf+" refuted"; }
  return null; }
// Header label for a vendor group: "<label> · <first model of that vendor>" (e.g. "OpenAI · gpt-5.5").
function vendorTag(models,vendor){ const label={c:"Claude",g:"OpenAI",k:"xAI",m:"Google"}[vendor]||vendor;
  for(const k in models){ const m=models[k]; if(m&&vendorOf(m,k)===vendor&&m.model) return label+" · "+m.model; } return label; }
const SEATMETA={ builder:{av:"B",role:"writes the code"}, reviewer:{av:"R",role:"sign-off authority · repo-aware"},
  breaker:{av:"K",role:"adversarial lane · tries to break it"}, verifier:{av:"V",role:"independent · confirms or refutes"} };
const PHASECOL={thinking:"#22b8a6",capped:"#b98af0",paused:"#e6ac48",done:"#57c46b",idle:"#8c96a8",sleeping:"#6f9bf6"};
function phaseVar(ph){ return ph==="capped"?"var(--violet)":ph==="thinking"?"var(--gpt)":ph==="paused"?"var(--warn)":
  ph==="done"?"var(--ok)":"var(--muted)"; }
function phaseLabel(ph){ return {thinking:"Running",capped:"Capped · awaiting you",paused:"Paused",done:"Done",
  sleeping:"Sleeping",idle:"Idle"}[ph]||ph||"—"; }

function toast(msg,kind){ const t=el("div","toast "+(kind||""),msg); $("toasts").appendChild(t); setTimeout(()=>t.remove(),4000); }
function curTheme(){ return document.documentElement.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'); }
function toggleTheme(){ const n=curTheme()==='light'?'dark':'light'; document.documentElement.setAttribute('data-theme',n);
  try{ localStorage.setItem('ap-theme',n);}catch(e){} syncThemeIcon(); }
function syncThemeIcon(){ $("theme").textContent = curTheme()==='light'?'☀':'☾'; }

async function ctl(kind, extra){
  try{ const r=await fetch("/api/"+kind,{method:"POST",headers:{"X-Dash-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(extra||{})});
    // A fresh control token is minted every time the dashboard server (re)starts and baked into the page.
    // If this tab was loaded before a restart its token is stale -> 403. GET polling still works (no token),
    // so the page looks alive while every button silently fails. Self-heal: reload to pull the new token.
    if(r.status===403){ toast("dashboard was restarted — reconnecting…","err"); setTimeout(()=>location.reload(),900); return; }
    const j=await r.json().catch(()=>({}));
    if(!r.ok) toast(j.error||("error "+r.status),"err"); else toast(kind+(extra&&extra.hid?" "+extra.hid:"")+" ok","ok");
  }catch(e){ toast(String(e),"err"); } refresh();
}
function startRun(){ const n=parseInt(($("maxturns")||{}).value,10);
  const mr=(Number.isInteger(n)&&n>=1&&n<=50)?n:null;
  if(confirm("Start the autopilot driver against this collab now"+(mr?(" (max work attempts "+mr+")"):"")+"?")) ctl("start",mr?{max_rounds:mr}:{}); }
function doStop(){ if(confirm("Stop the autopilot loop (graceful, reversible)?")) ctl("stop"); }
// A human override, named as one. The old copy said "Approve (advance) ... to done?", which reads like
// countersigning a verified result; this path checks no evidence at all. Both fields are required by
// /api/approve, so collect them here rather than let the request 400.
function approve(hid){
  const reason=prompt("HUMAN OVERRIDE — "+hid+" will be closed WITHOUT authoritative verification.\n\nWhy are you overriding the gate?");
  if(!reason||!reason.trim()) return;
  const actor=prompt("Your name — recorded as the actor on this override:");
  if(!actor||!actor.trim()) return;
  ctl("approve",{hid,actor:actor.trim(),reason:reason.trim()});
}
function nudge(hid){ if(confirm("Re-queue "+hid+" as a NEW pending handoff?")) ctl("nudge",{hid}); }
function reopen(hid){ if(confirm("Re-open "+hid+" — send it back to pending so the driver re-runs it (send-back loop retries the defects)?")) ctl("reopen",{hid}); }

let viewerOpener=null;
async function openHandoff(hid){
  viewerOpener=document.activeElement;
  $("v-title").textContent="handoff "+hid; $("v-fm").textContent=""; $("v-reply").style.display="none";
  $("v-body").textContent="loading…"; $("viewer").style.display="flex"; $("viewer").querySelector("button").focus();
  try{ const [handoffResponse,operationalResponse]=await Promise.all([
      fetch("/api/handoff?hid="+encodeURIComponent(hid)),
      fetch("/api/operational?hid="+encodeURIComponent(hid)+"&limit=100")]);
    const j=await handoffResponse.json(), op=await operationalResponse.json();
    if(!handoffResponse.ok){ $("v-body").textContent=j.error||("error "+handoffResponse.status); return; }
    const item=op.item||{};
    $("v-title").textContent="handoff "+j.id+" · "+esc(item.operational_state||j.state); $("v-reply").style.display=j.is_reply?"inline-block":"none";
    const fm=j.frontmatter||{}, fmbox=$("v-fm"); fmbox.textContent="";
    ["from","to","title","priority","date","status"].forEach(k=>{ if(fm[k]!=null){ const s=el("span");
      s.appendChild(el("b",null,k+": ")); s.appendChild(document.createTextNode(esc(fm[k]))); fmbox.appendChild(s); }});
    ["operational_state","state_reason","state_source","state_ts","owner","actor","required_action","run_id","correlation_id","trace_id"].forEach(k=>{
      if(item[k]!=null){ const s=el("span"); s.appendChild(el("b",null,k+": ")); s.appendChild(document.createTextNode(esc(item[k]))); fmbox.appendChild(s); }});
    const escalation=item.escalation||{}, history=(op.history||{}).events||[];
    let detail="";
    if(escalation.reason) detail+="ESCALATION\nseverity: "+esc(escalation.severity)+"\nreason: "+esc(escalation.reason)+"\ntimestamp: "+esc(escalation.timestamp)+"\nrequired action: "+esc(escalation.required_action)+"\n\n";
    if((item.conflicts||[]).length) detail+="CONFLICTS\n"+(item.conflicts||[]).map(c=>"- "+c.kind+": "+c.message).join("\n")+"\n\n";
    detail+="LIFECYCLE HISTORY\n"+history.map(e=>"- "+esc(e.event_ts)+" · "+esc(e.new_state)+" · "+esc(e.reason)+" · "+esc(e.source)).join("\n")+"\n\n";
    detail+="SOURCE EVIDENCE (redacted)\n"+JSON.stringify(op.source_evidence||{},null,2)+"\n\nHANDOFF BODY\n";
    $("v-body").textContent = detail+(j.body_text&&j.body_text.trim()? j.body_text : "(no body)");
  }catch(e){ $("v-body").textContent="failed to load: "+e; }
}
function closeViewer(){ $("viewer").style.display="none"; if(viewerOpener&&viewerOpener.focus) viewerOpener.focus(); }
$("viewer").addEventListener("keydown",e=>{ if(e.key==="Escape"){ closeViewer(); return; }
  if(e.key!=="Tab") return; const f=$("viewer").querySelectorAll("button, pre[tabindex]"); if(!f.length) return;
  const first=f[0], lastEl=f[f.length-1];
  if(e.shiftKey&&document.activeElement===first){ e.preventDefault(); lastEl.focus(); }
  else if(!e.shiftKey&&document.activeElement===lastEl){ e.preventDefault(); first.focus(); } });

function staleness(s){
  if(!s.status) return {cls:"warn",txt:"driver not running"};
  const age=Date.now()-Date.parse(s.status.updated_ts); const iv=(s.status.interval||1)*1000; const ph=s.status.phase;
  if(ph==="done"||ph==="capped") return {cls:"",txt:phaseLabel(ph).split(" ")[0].toLowerCase()};
  const staleAfter=Math.max(30000, 3*Math.max(10000, iv));
  const clk=fmtET(s.status.updated_ts)+" ET";
  // Show a per-second age that RESETS on every heartbeat — a climbing-then-resetting number is unmistakable
  // proof the driver is alive even mid-turn (no events for minutes is normal during a long builder call).
  // If this number climbs past the stale threshold and stops resetting, the driver is actually dead.
  const secs=Math.max(0,Math.floor(age/1000));
  return age<staleAfter? {cls:"ok",txt:"live · ♥ "+secs+"s · "+clk} : {cls:"warn",txt:"STALLED "+fmtage(age/1000)+" · "+clk};
}
function setFav(hex){ if(setFav._c===hex) return; setFav._c=hex;
  $("favicon").href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7' fill='"+encodeURIComponent(hex)+"'/%3E%3C/svg%3E"; }

function render(s){
  if(!s) return;
  const st=s.status||{}; const ph=s.paused?"paused":(st.phase||"");
  $("repo").textContent=(s.collab||"").split(/[\\/]/).pop()||"—";
  // command bar
  const pill=$("statePill"), c=st.phase||s.status?phaseVar(ph):"var(--muted)";
  pill.style.color=phaseVar(ph); pill.style.borderColor=phaseVar(ph);
  $("stateTxt").textContent = s.status? phaseLabel(ph) : "driver not running";
  const stl=connected? staleness(s):{cls:"warn",txt:"disconnected"};
  $("live").className="live "+stl.cls; $("liveTxt").textContent=stl.txt;
  const alive = stl.cls==="ok" && ph==="thinking";
  $("beacon").className="beacon"+(alive?" on":"");
  $("btnPause").disabled=!!s.paused||!s.status; $("btnResume").disabled=!s.paused;
  $("btnStart").disabled = stl.cls==="ok";  // a live driver is heartbeating -> block a second spawn
  setFav(PHASECOL[ph]||"#5c6675"); document.title=(s.status?"● ":"")+phaseLabel(ph)+" · autopilot";
  $("empty").style.display = (!s.status && !(s.events&&s.events.length))? "block":"none";

  renderOperational(s); renderOperatorTimeline(s); renderNarrative(s); renderHero(s); renderSeats(s);
  renderModelActivity(s); renderQuality(s); renderLanes(s); renderPipe(s); renderFeed(s); renderRuns(s); syncTurns(s);
  const cn=s.counts||{};
  const foot=$("foot"); foot.textContent="";
  [["pending",cn.pending],["claimed",cn.claimed],["done",cn.done],["archive",cn.archive]].forEach(([k,v])=>{
    const w=el("span"); w.appendChild(document.createTextNode(k+" ")); w.appendChild(el("b",null,String(v||0))); foot.appendChild(w); });
  foot.appendChild(el("span","spacer")); foot.lastChild.style.flex="1";
  [["var(--claude)","Claude"],["var(--gpt)","OpenAI"],["#313131","Grok"],["var(--gemini)","Gemini"]].forEach(([bg,lab])=>{
    const ld=el("span","legdot"); const i=el("i"); i.style.background=bg;
    if(lab==="Grok") i.style.outline="1px solid #6a6a6a";
    ld.appendChild(i); ld.appendChild(el("span",null,lab)); foot.appendChild(ld);
  });
}

function renderOperatorTimeline(s){
  const box=$("operatorTimeline"); box.textContent=""; const rows=(s.operator_summary||[]).slice(-100).reverse();
  const plan=s.run_plan||{};
  if(plan.plain_language_strategy){ const planRow=el("div","hchip");
    planRow.appendChild(el("div","b","Declared run strategy"));
    planRow.appendChild(el("div",null,plan.plain_language_strategy));
    planRow.appendChild(el("div","muted","Objective: "+(plan.objective||"not recorded"))); box.appendChild(planRow); }
  if(!rows.length){ box.appendChild(el("div","muted","No structured operator events recorded for this run.")); return; }
  rows.forEach(item=>{ const row=el("div","hchip"); const body=el("div");
    body.appendChild(el("div","b",(item.actor||"unknown actor")+" · "+(item.action||"unknown action")));
    body.appendChild(el("div","muted",(item.reason||"reason unknown")+" — "+(item.consequence||"consequence unknown")));
    const next=item.operator_action&&item.operator_action!=="none"?item.operator_action:item.next_action;
    body.appendChild(el("div",null,"Next: "+(next||"not recorded"))); row.appendChild(body); box.appendChild(row); });
}

let modelShowAll=false;
function renderModelActivity(s){
  const box=$("modelActivity"); box.textContent=""; const rawRoster=s.execution_roster||[], attempts=s.model_activity||[];
  const query=(($("modelFilter")||{}).value||"").trim().toLowerCase();
  const roster=rawRoster.filter(item=>!query||[item.role,item.model,item.state].some(value=>String(value||"").toLowerCase().includes(query)));
  if(!roster.length){ box.appendChild(el("div","muted","No execution roster is retained for this run.")); return; }
  const visible=modelShowAll?roster:roster.slice(0,100);
  visible.forEach(item=>{ const row=el("div","hchip model-roster"); const model=item.model||"model not configured";
    row.appendChild(el("div","b",(item.role||"unknown role")+" · "+model));
    const milestones=[modelStateLabel(item.state||"configured"), (item.orchestration_execution_count||0)+" orchestration executions", (item.provider_attempt_count||0)+" provider calls"];
    if(item.gateway_reached) milestones.push("gateway reached");
    if(item.provider_returned) milestones.push("provider returned");
    if(item.telemetry_reconciled) milestones.push("telemetry reconciled");
    row.appendChild(el("div","muted",milestones.join(" · ")));
    row.appendChild(el("div",null,"Terminal disposition: "+(item.terminal_disposition||"not yet reconciled")+" · telemetry "+(item.telemetry_outcome||"not applicable")+(item.terminal_reason?" · reason "+item.terminal_reason:"")));
    row.appendChild(el("div",null,"Task: "+(item.assigned_task||"not recorded")));
    row.appendChild(el("div","muted","Selected because: "+(item.selection_reason||"not recorded"))); box.appendChild(row); });
  if(!modelShowAll&&roster.length>visible.length){ const more=el("button","ghost","Show all "+roster.length+" matching roster entries");
    more.onclick=()=>{modelShowAll=true;renderModelActivity(s);}; box.appendChild(more); }
  const window=((s.collection_windows||{}).model_activity)||{}; const total=window.total!=null?window.total:attempts.length;
  if(total){ const note=window.truncated?("Showing latest "+window.returned+" of "+total+" model attempts."):(total+" retained model attempt record"+(total===1?"":"s"));
    box.appendChild(el("div","muted",note)); }
  attempts.slice(-100).reverse().forEach(item=>{ const row=el("div","hchip model-attempt"); const activity=item.last_activity||{}, token=item.tokens||{};
    row.appendChild(el("div","b",(item.requested_model||"unknown model")+" · "+modelStateLabel(item.state)+" · "+(item.source||"source unknown")+" · "+(item.attempt_id||"attempt unknown")));
    const elapsed=item.total_duration_ms!=null?fmtms(item.total_duration_ms):(item.started_ts?fmtdur((Date.now()-Date.parse(item.started_ts))/1000):"not recorded");
    row.appendChild(el("div",null,"Started "+(fmtET(item.started_ts)||"not recorded")+" · completed "+(fmtET(item.completed_ts)||"not recorded")+" · last activity "+(fmtET(item.updated_ts)||"not recorded")+" · elapsed "+elapsed));
    row.appendChild(el("div","muted","Route "+(item.gateway_route||"not recorded")+" · resolved "+(item.actual_model||"not recorded")+" · provider "+(item.provider||"not recorded")+" · "+(item.streaming?"streaming":"non-streaming")));
    row.appendChild(el("div","muted","LiteLLM request "+(item.gateway_request_id||"not recorded")+" · provider request "+(item.provider_request_id||"not recorded")+" · Langfuse trace "+(item.trace_id||"not recorded")));
    row.appendChild(el("div","muted","Parent execution "+(item.parent_attempt_id||"none")+" · request "+(item.request_id||"not recorded")));
    row.appendChild(el("div","muted","First token "+fmtms(item.first_token_latency_ms)+" · tokens in/out/cached "+(token.input??"?")+"/"+(token.output??"?")+"/"+(token.cached??"?")+" · cost "+(item.cost!=null?item.cost:"not recorded")+" · retries "+(item.retry_count||0)));
    if(Object.keys(activity).length) row.appendChild(el("div",null,"Safe activity: "+Object.entries(activity).map(([k,v])=>{
      const shown=(k==="phase"||k.endsWith("_status"))?String(v).replace(/_/g," "):v;
      return k.replace(/_/g," ")+" "+shown;
    }).join(" · ")));
    const toolSummary=toolActivitySummary(item.tool_activity); if(toolSummary) row.appendChild(el("div",null,"Tool activity: "+toolSummary));
    box.appendChild(row); });
  box.appendChild(el("div","muted","Private model reasoning and response bodies are not displayed."));
}

function renderQuality(s){
  const box=$("qualityWorkspace"); box.textContent="";
  const query=(($("qualityFilter")||{}).value||"").trim().toLowerCase();
  const matches=item=>!query||JSON.stringify(item).toLowerCase().includes(query);
  const candidates=(s.candidates||[]).filter(matches), validations=(s.validations||[]).filter(matches), requirements=(s.requirements||[]).filter(matches), dispositions=(s.dispositions||[]).filter(matches);
  const windows=s.collection_windows||{};
  const heading=(label,key,values)=>{ const meta=windows[key]||{}, count=query?values.length:(meta.total!=null?meta.total:values.length);
    const row=el("div","hchip"); row.appendChild(el("div","b",label+" · "+count));
    if(!count) row.appendChild(el("div","muted",query?"No records in the current retained window match this filter.":"Not recorded for this run.")); box.appendChild(row); };
  const windowNote=key=>{ const meta=windows[key]||{}; if(meta.truncated) box.appendChild(el("div","muted","Showing latest "+meta.returned+" of "+meta.total+" in the current retained window.")); };
  heading("Candidates","candidates",candidates); windowNote("candidates");
  candidates.slice(-25).forEach(item=>{ const producer=item.producer||{}; const row=el("div","hchip");
    row.appendChild(el("div","b",(item.candidate_id||"unknown candidate")+" · "+(item.current_disposition||"unknown disposition")));
    row.appendChild(el("div",null,"Produced by "+(producer.role||"unknown producer")+" / "+(producer.model||"model unknown")+" · parent "+(item.parent_candidate_id||"none")));
    row.appendChild(el("div","muted","Files: "+((item.files||[]).join(", ")||"not recorded")+" · patch "+(item.patch_digest||"not recorded")+" · artifact "+(item.final_artifact_ref||"not recorded")+" · commit "+(item.final_commit||"not recorded")+" · incorporated "+((item.incorporated_candidate_ids||[]).join(", ")||"none")));
    row.appendChild(el("div","muted","Revision evidence: "+((item.revision_evidence_refs||[]).join(", ")||"none recorded"))); box.appendChild(row); });
  heading("Validations","validations",validations); windowNote("validations");
  validations.slice(-50).forEach(item=>{ const row=el("div","hchip"), dimensions=item.dimensions||{}, testQuality=item.test_quality||{};
    row.appendChild(el("div","b",(item.validation_id||"validation")+" · "+(item.status||"unknown")+" · "+(item.source_kind||"source unknown")));
    row.appendChild(el("div",null,"Producer "+(item.producer||"unknown")+" "+(item.producer_version||"version unknown")+" · evidence "+(item.artifact_ref||"not recorded")));
    row.appendChild(el("div","muted","Dimensions: "+(Object.entries(dimensions).map(([k,v])=>k.replace(/_/g," ")+" "+v).join(" · ")||"not recorded")+" · uncertainty "+(item.uncertainty||"not recorded")));
    if(item.baseline_delta) row.appendChild(el("div","muted","Baseline change: "+Object.entries(item.baseline_delta).map(([k,v])=>k.replace(/_/g," ")+" "+v).join(" · ")));
    if((item.gaps||[]).length) row.appendChild(el("div","v-warn","Known gaps: "+item.gaps.join(" · ")));
    if(Object.keys(testQuality).length) row.appendChild(el("div","muted","Test quality: "+Object.entries(testQuality).map(([k,v])=>k.replace(/_/g," ")+" "+(v==null?"unknown":v?"yes":"no")).join(" · ")));
    box.appendChild(row); });
  heading("Requirements","requirements",requirements); windowNote("requirements");
  requirements.slice(-50).forEach(item=>{ const row=el("div","hchip");
    row.appendChild(el("div","b",(item.requirement_id||"requirement")+" · "+(item.effective_status||item.status||"unknown")+(item.critical?" · critical":" · non-critical")));
    row.appendChild(el("div",null,item.description||"Requirement text not recorded."));
    row.appendChild(el("div","muted",(item.source_kind||"source unknown")+" · producer "+(item.producer||"unknown")+" · evidence "+((item.evidence_refs||[]).join(", ")||"not recorded"))); box.appendChild(row); });
  heading("Dispositions","dispositions",dispositions); windowNote("dispositions");
  dispositions.slice(-25).forEach(item=>{ const row=el("div","hchip");
    row.appendChild(el("div","b",(item.disposition||"unknown")+" · "+(item.candidate_id||"candidate unknown")));
    row.appendChild(el("div",null,item.executive_explanation||"Explanation not recorded."));
    row.appendChild(el("div","muted","Reason: "+(item.primary_reason||"not applicable")+" · categories "+((item.reason_categories||[]).join(", ")||"none")+" · decision maker "+(item.decision_maker||"not recorded")+" · failed checks "+((item.failed_checks||[]).join(", ")||"none")+" · superseded by "+(item.superseded_by_candidate_id||"not applicable")));
    row.appendChild(el("div","muted","Impact: "+(item.impact||"unknown")+" Remediation: "+(item.remediation||"not recorded")));
    if((item.disagreements||[]).length) row.appendChild(el("div","v-warn","Evaluator disagreement recorded · "+(item.resolution||"unresolved")));
    if((item.weaknesses||[]).length) row.appendChild(el("div","muted","Visible weaknesses: "+item.weaknesses.join(", ")));
    box.appendChild(row); });
  const evidence=s.evidence_health||s.health||{};
  if(evidence.archive_integrity) box.appendChild(el("div","muted","Archive integrity: "+evidence.archive_integrity));
}

// ---- "What happened" narrative card ------------------------------------- //
// Picks the handoff worth narrating (the one being worked, else the newest done/claimed) and lazy-loads its
// human-readable story from /api/narrative. Re-fetched when the focus handoff changes OR when the run's
// work-signature moves (a round lands, the stage/phase/candidate turns over) — NOT on every poll, so an idle
// heartbeat costs nothing but a live run's story keeps up on its own.
function narrSig(s,hid){ const st=s.status||{}; const b=s.board||{};
  return [hid, st.round, st.stage, st.phase, st.candidate, (b.claimed||[]).length, (b.done||[]).length].join("|"); }
function focusHid(s){
  // No live run -> no live story. /api/narrative knowingly falls back to ARCHIVED runs' event logs and
  // labels those turns with whatever seats status.json currently names, so narrating while idle prints a
  // previous run's work under the current models. History has the per-run detail; this card stays empty.
  if(!s.live) return null;
  const st=s.status||{};
  if(st.current_hid) return String(st.current_hid);
  const b=s.board||{};
  // a CLAIMED handoff (in-progress, or stuck/unshipped after a capped run) is what's happening NOW — it
  // outranks the newest DONE handoff (finished history). Otherwise fall back to the last done.
  const cl=b.claimed||[]; if(cl.length) return String(cl[cl.length-1].id);
  const done=b.done||[]; if(done.length) return String(done[done.length-1].id);
  return null;
}
function renderNarrative(s){
  const card=$("narrCard"); const hid=focusHid(s);
  if(!hid){ card.style.display="none"; narrHid=null; narrData=null; narrSigLast=null; return; }
  card.style.display=""; applyNarrCollapse();
  const sig=narrSig(s,hid);
  if(hid!==narrHid){  // focus moved -> blank + reload (a spinner here is honest: it's a different story)
    narrHid=hid; narrData=null; narrSigLast=sig; paintNarrative(); fetchNarrative(hid); return;
  }
  if(sig!==narrSigLast){  // same handoff, work moved -> refetch in place, no "loading…" flash
    narrSigLast=sig; fetchNarrative(hid);
  }
}
function narrCollapsed(){ try{ return localStorage.getItem("ap-narr-collapsed")==="1"; }catch(e){ return false; } }
function applyNarrCollapse(){ const c=narrCollapsed();
  $("narr").classList.toggle("collapsed",c); $("narrCard").classList.toggle("folded",c);
  $("narrToggle").setAttribute("aria-expanded", c?"false":"true");
  $("narrToggle").setAttribute("aria-label", c?"Expand":"Collapse"); }
function toggleNarr(){ try{ localStorage.setItem("ap-narr-collapsed", narrCollapsed()?"0":"1"); }catch(e){}
  applyNarrCollapse(); }
async function fetchNarrative(hid){
  try{ const r=await fetch("/api/narrative?hid="+encodeURIComponent(hid)); const j=await r.json();
    if(narrHid!==hid) return;   // focus moved on while we were loading — drop this stale response
    narrData = r.ok? j : {error:(j&&j.error)||("error "+r.status)}; paintNarrative();
  }catch(e){ if(narrHid===hid){ narrData={error:String(e)}; paintNarrative(); } }
}
function paintNarrative(){
  const box=$("narr"), meta=$("narrMeta"); if(!box) return; box.textContent="";
  meta.textContent = narrHid? ("handoff "+narrHid+(narrData&&narrData.state?" · "+narrData.state:"")) : "";
  if(!narrData){ box.appendChild(el("div","none","loading…")); return; }
  if(narrData.error){ box.appendChild(el("div","none",narrData.error)); return; }
  narrMarkdown(box, narrData.markdown||"");
}
// SAFE structural markdown -> DOM ([C38]): EVERY text node is set via textContent, so untrusted agent prose
// can never become markup. Only #/##/bullets/**bold**/`code` are recognised — none can inject HTML. We parse
// into a title + lead + per-## sections first, THEN lay the sections into a width-filling grid.
function narrParse(md){
  const out={title:null, lead:[], secs:[]}; let cur=null;
  String(md).split("\n").forEach(raw=>{
    const ln=raw.replace(/<!--[\s\S]*?-->/g,"").replace(/\s+$/,"");
    if(!ln.trim()) return;
    if(ln.startsWith("# ")){ out.title=ln.slice(2).trim(); return; }
    if(ln.startsWith("## ")){ cur={head:ln.slice(3).trim(), items:[]}; out.secs.push(cur); return; }
    const mLi=ln.match(/^\s*(?:\d+\.|[-*])\s+(.*)$/);
    const bucket = cur? cur.items : out.lead;
    if(mLi){ bucket.push({kind:"li", text:mLi[1], blk:/⚠|blocker/i.test(mLi[1])}); return; }
    if(/^_.*_$/.test(ln)) bucket.push({kind:"p", text:ln.replace(/^_|_$/g,""), none:true});
    else bucket.push({kind:"p", text:ln});
  });
  return out;
}
function narrItems(parent, items){   // render a flat item list, grouping consecutive bullets into one .nlist
  let list=null;
  items.forEach(it=>{
    if(it.kind==="li"){ if(!list){ list=el("div","nlist"); parent.appendChild(list); }
      const li=el("div","nli"+(it.blk?" blk":"")); narrInline(li,it.text); list.appendChild(li); }
    else{ list=null; const p=el("div","np"+(it.none?" none":"")); narrInline(p,it.text); parent.appendChild(p); }
  });
}
function narrMarkdown(box, md){
  const doc=narrParse(md);
  if(doc.title) box.appendChild(el("div","nh1",doc.title));
  doc.lead.forEach((it,i)=>{ const p=el("div","np"+(i===0?" lead":"")+(it.none?" none":""));
    narrInline(p,it.text); box.appendChild(p); });
  const grid=el("div","ncols");
  doc.secs.forEach(sec=>{
    const turns=/how it unfolded/i.test(sec.head);   // the timeline — span full width, flow into columns
    const block=el("div","nsec"+(turns?" wide turns":""));
    block.appendChild(el("div","nh2",sec.head));
    narrItems(block, sec.items);
    grid.appendChild(block);
  });
  box.appendChild(grid);
}
function narrInline(node, text){
  String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach(pt=>{ if(!pt) return;
    if(pt.length>4&&pt.startsWith("**")&&pt.endsWith("**")) node.appendChild(el("b",null,pt.slice(2,-2)));
    else if(pt.length>2&&pt.startsWith("`")&&pt.endsWith("`")) node.appendChild(el("code",null,pt.slice(1,-1)));
    else node.appendChild(document.createTextNode(pt));
  });
}

function metric(box,v,k,color){ const m=el("div","metric"); const vv=el("div","v num",v); if(color)vv.style.color=color;
  m.appendChild(vv); m.appendChild(el("div","k",k)); box.appendChild(m); }

function renderHero(s){
  const st=s.status||{}, ov=(s.stats||{}).overall||{};
  const eb=$("heroEyebrow"), ti=$("heroTitle"), sub=$("heroSub"), cta=$("heroCta"), mx=$("heroMetrics"), stripe=$("heroStripe");
  eb.textContent=""; ti.textContent=""; sub.textContent=""; cta.textContent=""; mx.textContent="";
  const ph=s.paused?"paused":(st.phase||"");
  // NO RUN ACTIVE. The server sends status=null whenever no driver holds the board lease, so there is
  // nothing live to draw and we must not invent it from the last run's leftovers. Say what ended, say
  // what is queued, and offer the one useful action — Start. The board below is durable and stays.
  if(!s.status){
    const lr=s.last_run||null, pend=((s.board||{}).pending||[]), claimed=((s.board||{}).claimed||[]);
    // PARKED behind an escalation: a driver refuses to auto-run it, so pressing ▶ Start spawns a watcher
    // that finds nothing and idles — looking like "Start did nothing". Say it loudly instead of blank.
    const escRow=claimed.filter(h=>h.escalated).slice(-1)[0]||(pend.filter(h=>h.escalated)[0])||null;
    if(escRow){
      stripe.style.background="var(--warn)";
      eb.textContent="⚠ escalation · awaiting you";
      ti.textContent=escRow.id+" is parked in escalation"+(escRow.escalation_reason?(" ("+escRow.escalation_reason+")"):"");
      sub.textContent="A driver will NOT auto-run an escalated handoff — ▶ Start alone spawns a watcher that idles. Open it to read why it stopped, or Re-run/adopt to clear the escalation and drive it.";
      const bo=el("button","primary","Open "+escRow.id); bo.onclick=()=>openHandoff(escRow.id); cta.appendChild(bo);
      const br=el("button","warn ghost","↻ Re-run / adopt "+escRow.id); br.onclick=()=>reopen(escRow.id); cta.appendChild(br);
      return;
    }
    stripe.style.background="var(--faint)";
    eb.textContent="no run active";
    const next=pend.length?String(pend[0].id):(claimed.length?String(claimed[claimed.length-1].id):null);
    if(pend.length){ ti.textContent=next+" is queued — press ▶ Start to run it"; }
    else if(claimed.length){ ti.textContent=next+" is claimed but nothing is running it"; }
    else { ti.textContent="No run active"; }
    if(lr && lr.run_uid){
      const bits=["last: "+(lr.hid?lr.hid+" · ":"")+"ended "+(fmtET(lr.ended_ts)||"?")];
      if(lr.phase_final) bits.push(lr.phase_final+(lr.pause_reason?(" ("+lr.pause_reason+")"):""));
      sub.textContent=bits.join(" · ")+" — see History for its detail.";
    } else {
      sub.textContent="Start autopilot against this collab to see live activity.";
    }
    const b=el("button","primary","▶ Start"); b.onclick=()=>startRun(); cta.appendChild(b);
    if(claimed.length && !pend.length){
      const r=el("button","warn ghost","↻ Re-run "+next); r.onclick=()=>reopen(next); cta.appendChild(r);
    }
    return;
  }
  const active = st.active_seat && st.current_hid && !s.paused && ph==="thinking";
  const pendingCt=((s.board||{}).pending||[]).length;
  let color="var(--faint)";
  if(active){
    const seat=st.active_seat, isLanes=(seat==="lanes"||seat==="assess"), isVerify=(seat==="verify"||st.stage==="verify");
    color=isVerify?"var(--ok)":isLanes?vColor("breaker"):vColor(seat);
    eb.textContent="work attempt "+(st.round||0)+" of max "+(maxWorkAttempts(s)??"not recorded")+" · "+(st.stage||"working");
    const verb = isVerify?"is running the authoritative gate on":
      seat==="reviewer"?"is reviewing":seat==="builder"?"is building on":
      (seat==="breaker"||seat==="verifier"||isLanes)?"is probing":"is working on";
    const label=isVerify?"verify.py":isLanes?"adversarial lanes":seat;
    const t1=el("span",null,label); t1.style.color=color; ti.appendChild(t1);
    ti.appendChild(document.createTextNode(" "+verb+" "+st.current_hid));
    const model=isLanes?(seatModel("breaker")||seatModel("verifier")||""):(isVerify?"":(seatModel(seat)||""));
    const since=Date.parse(st.active_since||st.updated_ts); const elp=(Date.now()-since)/1000, to=st.timeout||0;
    // During the multi-minute closeout the sub-line reads the newest lane event (live play-by-play) instead of
    // the static "breaker→verifier"; falls back to the static label until the first lane event lands.
    const laneLine=isLanes?latestLaneLine(s):null;
    const cand=st.candidate?("candidate "+st.candidate+" · "):"";
    sub.textContent=cand+(isVerify?("scripts/verify.py · "):(isLanes?(laneLine||("reviewer ∥ breaker→verifier · "+model)):model))
      +" · "+fmtdur(elp)+(to?" / "+fmtdur(to):"")+" elapsed";
  } else if(ph==="thinking" && !s.paused){
    // Mid-assess / mid-stage with no active_seat mark (or a cleared one): still tell the operator what
    // the driver is doing. The old fall-through painted a blank "Autopilot" for multi-minute gaps.
    const hid=st.current_hid||((((s.board||{}).claimed||[])[0]||{}).id)||null;
    const stage=st.stage||"working";
    const cand=st.candidate?("candidate "+st.candidate):"";
    color="var(--accent)";
    eb.textContent="work attempt "+(st.round||0)+" of max "+(maxWorkAttempts(s)??"not recorded")+" · "+stage;
    ti.textContent=(hid?(stage+" on "+hid):("Autopilot · "+stage));
    const since=Date.parse(st.active_since||st.updated_ts); const elp=since?(Date.now()-since)/1000:null;
    const laneLine=latestLaneLine(s);
    const bits=[cand, laneLine, elp!=null?(fmtdur(elp)+" since last seat mark"):null].filter(Boolean);
    sub.textContent=bits.length?bits.join(" · "):"Driver is live — waiting for the next seat mark (tests, lanes, or conformance).";
  } else if(!s.paused && pendingCt>0){
    // Queued work with NO driver actively running it — show this REGARDLESS of a stale status.phase (a
    // prior run that already exited leaves phase "done"/"capped"). Actionable: press Start.
    const qhid=String(((s.board||{}).pending||[])[0].id);
    color="var(--gpt)"; eb.textContent=pendingCt+" queued · no driver running";
    ti.textContent=qhid+" is queued — press ▶ Start to run it";
    sub.textContent="Start launches a watching driver that claims and runs it. (Re-run sends a stuck handoff back to this queue.)";
  } else if(ph==="capped"){
    const err=st.last_error||""; const hm=err.match(/^(\d{1,9})/); const hid=hm?hm[1]:(st.current_hid||"");
    const doneRows=((s.board||{}).done||[]).concat((s.board||{}).archive||[]);
    const doneIds=new Set(doneRows.map(h=>h.id));
    if(hid && doneIds.has(hid)){
      // "Resolved — shipped" in green was shown for ANY handoff sitting in done/, including one a human
      // overrode after the gate refused it. That is the capped case, so an override is the LIKELY way it
      // got here — the one place the green chip must not be reflexive. Split on the recorded kind.
      const row=doneRows.find(h=>String(h.id)===String(hid))||{};
      if(row.closed_autonomously){
        color="var(--ok)"; eb.textContent="last run"; ti.textContent="Resolved — "+hid+" shipped";
        sub.textContent="Closed on an authoritative verification receipt. Loop idle; start a run to continue.";
      } else {
        color="var(--violet)"; eb.textContent="last run · human override";
        ti.textContent="Closed by hand — "+hid+" was NOT verified";
        sub.textContent=(row.closed_label||"HUMAN OVERRIDE — closed by a person; NOT authoritatively verified")
          +(row.closed_actor?(" · by "+row.closed_actor):"")+(row.closed_reason?(" · “"+row.closed_reason+"”"):"");
      }
    }
    else{ const decision=s.latest_decision||null; const reason=decision&&decision.reason;
      color="var(--violet)"; eb.textContent=reason?("stopped · "+reason.replaceAll("_"," ")):"stopped · reason not recorded";
      ti.textContent="The gate held. Your call.";
      const blk=(s.lanes||{}).blockers||0; const laneLine=latestLaneLine(s);
      sub.textContent=(hid?("Handoff "+hid+" was not signed off. "):"")+(blk?(blk+" adversarial finding"+(blk===1?"":"s")+" unresolved. "):"")+(decision?("Decision after completed round "+decision.completed_round+" of max "+decision.maximum_rounds+". "):"")+"Nothing shipped autonomously — exactly as designed."+(laneLine?(" Last lane: "+laneLine+"."):"");
      if(hid){ const b1=el("button","primary","Human override "+hid+" → done"); b1.onclick=()=>approve(hid); cta.appendChild(b1);
        const b2=el("button","warn ghost","↻ Re-run "+hid); b2.onclick=()=>reopen(hid); cta.appendChild(b2); } }
  } else if(ph==="paused"){ color="var(--warn)"; eb.textContent="held"; ti.textContent="Paused";
    sub.textContent="The loop is frozen and fully reversible. Resume when you're ready.";
    const b=el("button","primary","Resume"); b.onclick=()=>ctl("resume"); cta.appendChild(b);
  } else if(ph==="done"){
    const claimed=(s.board||{}).claimed||[];
    if(claimed.length){  // "drained" but a handoff is STUCK in claimed (worked, never signed off) — not success
      const hid=String(claimed[claimed.length-1].id); const blk=(s.lanes||{}).blockers||0;
      color="var(--violet)"; eb.textContent="idle · "+claimed.length+" stuck";
      ti.textContent=hid+" is stuck in claimed — NOT shipped";
      sub.textContent="The run drained but "+hid+" never signed off"+(blk?(" ("+blk+" unresolved finding"+(blk===1?"":"s")+")"):"")+". Re-run it (send-back loop retries) or fix it by hand.";
      const b=el("button","primary","↻ Re-run "+hid); b.onclick=()=>reopen(hid); cta.appendChild(b);
    } else { color="var(--ok)"; eb.textContent="complete"; ti.textContent="Thread complete";
      sub.textContent="The board is drained and autopilot is idle."; }
  } else if(ph==="sleeping"||ph==="idle"){ color="var(--faint)"; eb.textContent=ph; ti.textContent="Idle — watching for work";
    sub.textContent="No pending handoff addressed to a CLI seat right now.";
  } else { color="var(--faint)"; eb.textContent=ph||"—"; ti.textContent="Autopilot"; sub.textContent=""; }
  stripe.style.background=color;
  if(st.started_ts) metric(mx,fmtdur((Date.now()-Date.parse(st.started_ts))/1000),"uptime");
  const budgets=((st.budget||{}).budgets)||{};
  const work=budgets.work_attempts||{};
  const turns=budgets.actor_turns||{};
  const verification=budgets.verification_calls||{};
  const providerWindow=((s.collection_windows||{}).provider_attempts)||{};
  const providerAttempts=providerWindow.total!=null?providerWindow.total:(s.model_activity||[]).filter(a=>a.source==="gateway_client").length;
  const latestDecision=s.latest_decision||null;
  metric(mx,String(work.consumed!=null?work.consumed:(st.round||0))+(work.limit!=null?(" / "+work.limit):""),"work attempts");
  metric(mx,String(turns.consumed!=null?turns.consumed:(ov.rounds||0)),"actor turns");
  metric(mx,String(verification.consumed!=null?verification.consumed:0),"verification calls");
  metric(mx,String(providerAttempts),"provider attempts");
  metric(mx,String(latestDecision?latestDecision.completed_round:0),"completed round boundaries");
  metric(mx,String(ov.fails||0),"failures",ov.fails?"var(--crit)":"var(--ok)");
  if(ov.avg_ms){ const m=el("div","metric"); m.appendChild(el("div","v mono",fmtms(ov.avg_ms))); m.appendChild(el("div","k","avg actor turn")); mx.appendChild(m); }
  const blk=(s.lanes||{}).blockers||0; if(blk) metric(mx,String(blk),"blockers","var(--crit)");
}

function renderSeats(s){
  const box=$("agents");
  // While the user has a model dropdown focused/open, skip the rebuild: re-rendering the panel on every
  // 1s/2s poll would tear the <select> out of the DOM and slam it shut mid-selection.
  const af=document.activeElement;
  if(af && af.tagName==="SELECT" && box.contains(af)) return;
  box.textContent="";
  const models=s.seats||{}, sstats=(s.stats||{}).seats||{}, st=s.status||{}, L=s.lanes||{};
  const catalog=s.models_catalog||[];
  const names=[...new Set([...Object.keys(models),...Object.keys(sstats)])].sort((a,b)=>seatSlot(a)-seatSlot(b));
  if(!names.length){ box.appendChild(el("div","muted","(no seats.json for --home)")); return; }
  const maxAvg=Math.max(1,...names.map(n=>(sstats[n]||{}).avg_ms||0));
  const groups={c:[],g:[],k:[],m:[]}; names.forEach(n=>(groups[seatVendor(n)]||groups.g).push(n));
  [["c","claude","the builder"],["k","grok","adversary — probes · breaks · verifies"],
   ["m","gemini","adversary — probes · breaks · verifies"],["g","gpt","the adversary — reviews · breaks · verifies"]].forEach(([v,cls,cap])=>{
    if(!groups[v].length) return;
    const vd=el("div","vendor "+cls); const vh=el("div","vh");
    vh.appendChild(el("span","tag",vendorTag(models,v))); vh.appendChild(el("span","role",cap)); vd.appendChild(vh);
    groups[v].forEach(n=>{
      const meta=SEATMETA[n]||{av:(n[0]||"?").toUpperCase(),role:""};
      const row=el("div","seat "+v+(n===st.active_seat?" active":""));
      row.appendChild(el("div","av",meta.av));
      const mid=el("div");
      const nm=el("div","nm"); nm.appendChild(el("span",null,n));
      const ml=seatModel(n); if(ml) nm.appendChild(el("span","model mono",ml));
      if(n==="reviewer") nm.appendChild(el("span","badge-auth","sign-off"));
      mid.appendChild(nm); mid.appendChild(el("div","sub",meta.role));
      const seatCfg=models[n]||{};
      if(catalog.length && seatCfg.backend==="cli"){
        const pick=el("div","seatmodel"); const sel=el("select","modelsel mono");
        let cur=seatCfg.model, has=false;
        catalog.forEach(id=>{ const o=el("option",null,id); o.value=id; if(id===cur){o.selected=true; has=true;} sel.appendChild(o); });
        if(cur && !has){ const o=el("option",null,cur+" (custom)"); o.value=cur; o.selected=true; sel.insertBefore(o,sel.firstChild); }
        sel.onchange=()=>ctl("seat-model",{seat:n,model:sel.value});
        pick.appendChild(sel);
        pick.appendChild(el("span","modelnote","model changes apply on the next driver start."));
        mid.appendChild(pick);
      }
      if(n===st.active_seat && st.current_hid){ const since=Date.parse(st.active_since||st.updated_ts);
        const elp=(Date.now()-since)/1000, to=st.timeout||0;
        const now=el("div","now"); const dot=el("span",null,"▶ "); dot.style.color=vColor(n);
        now.appendChild(dot); now.appendChild(document.createTextNode("on "+st.current_hid+" · "+fmtdur(elp)+(to?" / "+fmtdur(to):"")));
        if(to&&elp>0.8*to) now.style.color="var(--warn)"; mid.appendChild(now); }
      const d=sstats[n]||{}; const avg=d.avg_ms||0;
      if(avg>0){ const bar=el("div","lbar"); const bi=el("i"); bi.style.width=Math.round(100*avg/maxAvg)+"%";
        bi.style.background=vColor(n); bar.appendChild(bi); mid.appendChild(bar); }
      row.appendChild(mid);
      const stt=el("div","st");
      if(d.rounds){ const r1=el("div"); r1.appendChild(el("span","b",String(d.rounds))); r1.appendChild(document.createTextNode(" actor turns")); stt.appendChild(r1);
        stt.appendChild(el("div","mono","avg "+fmtms(d.avg_ms)));
        if(d.fails) stt.appendChild(el("div","mono",d.fails+" fails")); else stt.appendChild(el("div","mono","0 fails")); }
      else if((L.lanes||[]).length && (n==="breaker"||n==="verifier")){ const r1=el("div");
        r1.appendChild(el("span","b",String((L.lanes||[]).length))); r1.appendChild(document.createTextNode(" lanes")); stt.appendChild(r1);
        stt.appendChild(el("div","mono",(L.blockers||0)+" findings")); }
      else stt.appendChild(el("div","muted","standby"));
      row.appendChild(stt); vd.appendChild(row);
    });
    box.appendChild(vd);
  });
}

let laneOpen=new Set();  // lane names the user expanded — persists across live snapshot renders
function renderLanes(s){
  const L=s.lanes; const card=$("lanesCard"), box=$("lanes"), head=$("lanehead");
  const st=s.status||{}, ph=s.paused?"paused":(st.phase||"");
  const assessing = ph==="thinking" && (st.stage==="assess"||st.stage==="verify"||st.active_seat==="lanes"||st.active_seat==="verify");
  // CLEAR before hiding. Hiding alone left the previous run's lane DOM in the document, so it reappeared
  // intact the moment anything un-hid the card. The server now only ever sends lanes belonging to the
  // live run (scoped by run_uid), so "no lanes" genuinely means there is nothing to show — except while
  // assess is in flight, when the ledger is not written yet and the card used to vanish for minutes.
  if(!L||!L.lanes||!L.lanes.length){
    box.textContent=""; head.textContent="";
    if(assessing){
      card.style.display="";
      head.appendChild(el("span","chip","in progress"));
      const line=latestLaneLine(s);
      box.appendChild(el("div","muted",
        st.stage==="verify"
          ? "Running scripts/verify.py (authoritative gate) before reviewer ∥ lanes fan-out…"
          : (line||"Reviewer ∥ adversarial lanes ∥ spec-conformance running — ledger appears when the pair finishes.")));
      return;
    }
    card.style.display="none"; return;
  }
  card.style.display=""; head.textContent="";
  // Only an AUTHORITATIVE pass earns the green chip. A pytest-only run also reports passed=true;
  // showing that as "tests ✓" is how a lint/type-broken checkout reads as verified.
  const tp=el("span","chip "+(L.verification_green?"ok":"crit"),L.verification_label||(L.tests_passed?"tests ✓":"tests ✗"));
  tp.title=L.verification_label||""; head.appendChild(tp);
  head.appendChild(el("span","chip "+((L.blockers||0)?"crit":""),(L.blockers||0)+" blocker"+((L.blockers||0)===1?"":"s")));
  box.textContent="";
  L.lanes.forEach(ln=>{
    const hit=(ln.confirmed||0)>0;
    const incomplete=!!ln.incomplete;
    const cf=ln.confirmed_findings||[], rf=ln.refuted_findings||[]; const hasDetail=(cf.length+rf.length)>0;
    const wrap=el("div","lanewrap");
    const d=el("div","lane "+(ln.ran?(hit?"hit":"clean"):""));
    d.appendChild(el("div","verdict",hit?"!":(incomplete?"?":"✓")));
    const pass=ln.pass||ln.lane||"?";
    const c=el("div","ln"); c.appendChild(el("div","name",pass+(hasDetail?"  ▸":"")));
    const flow=el("div","flow"); flow.appendChild(el("span","g",ln.breaker||"breaker"));
    flow.appendChild(el("span","arrow"," → ")); flow.appendChild(el("span","g",ln.verifier||"verifier")); c.appendChild(flow);
    const tags=[ln.profile,ln.composite?"composite":null,(ln.contracts||[]).length?((ln.contracts||[]).length+" contract"+((ln.contracts||[]).length===1?"":"s")):null].filter(Boolean);
    if(tags.length){ const meta=el("div","flow",tags.join(" · ")); meta.style.fontSize="10px"; c.appendChild(meta); }
    d.appendChild(c);
    const t=el("div","tally"); t.appendChild(el("span","c",(ln.confirmed||0)+" confirmed")); t.appendChild(el("br"));
    t.appendChild(el("span","r",(ln.refuted||0)+" refuted")); d.appendChild(t);
    wrap.appendChild(d);
    if(hasDetail){
      const key=ln.lane||"?"; const caret=c.querySelector(".name"); const isOpen=laneOpen.has(key);
      const det=el("div"); det.style.cssText="margin:2px 0 10px;padding:8px 10px;border-radius:6px;background:rgba(128,128,128,.09);";
      det.style.display=isOpen?"block":"none";
      // Render a finding as: a colored CONFIRMED/refuted chip, then its segments (file:line → triggers →
      // breaks) with code paths monospaced. All agent text goes through textContent — never innerHTML.
      const monoInto=(line,seg)=>{ let last2=0, m2; const re=new RegExp(_PATH_RE.source,"g");
        while((m2=re.exec(seg))){ if(m2.index>last2) line.appendChild(document.createTextNode(seg.slice(last2,m2.index)));
          const code=el("code",null,m2[0]); code.style.cssText="font-family:ui-monospace,SFMono-Regular,monospace;background:rgba(128,128,128,.16);padding:0 4px;border-radius:3px;font-size:11px;";
          line.appendChild(code); last2=m2.index+m2[0].length; }
        if(last2<seg.length) line.appendChild(document.createTextNode(seg.slice(last2))); };
      const addF=(txt,confirmed)=>{ const row=el("div");
        row.style.cssText="margin:0 0 10px;padding:7px 9px;border-radius:6px;background:rgba(128,128,128,.06);";
        const chip=el("span",null,confirmed?"CONFIRMED":"REFUTED");
        chip.style.cssText="display:inline-block;font-weight:700;font-size:9.5px;letter-spacing:.06em;padding:1px 7px;border-radius:9px;color:#fff;background:"+(confirmed?"var(--crit,#e5484d)":"var(--ok,#57c46b)")+";";
        row.appendChild(chip);
        parseFinding(txt).forEach((seg,i)=>{ const line=el("div"); line.style.cssText="margin:5px 0 0;font-size:11.5px;line-height:1.45;";
          if(i>0){ const lab=el("span",null,(i===1?"triggers":"breaks")+": "); lab.style.cssText="color:var(--muted,#8c96a8);font-size:10px;"; line.appendChild(lab); }
          monoInto(line,seg); row.appendChild(line); });
        det.appendChild(row); };
      cf.forEach(f=>addF(f,true)); rf.forEach(f=>addF(f,false));
      if(caret) caret.textContent=key+(isOpen?"  ▾":"  ▸");
      d.style.cursor="pointer";
      d.onclick=()=>{ const open=!laneOpen.has(key); if(open) laneOpen.add(key); else laneOpen.delete(key);
        det.style.display=open?"block":"none"; if(caret) caret.textContent=key+(open?"  ▾":"  ▸"); };
      wrap.appendChild(det);
    }
    box.appendChild(wrap);
  });
}

const OPERATIONAL_STATES=["queued","claimed","running","awaiting","paused","capped","blocked","parked","escalated","retrying","failed","cancelled","superseded","completed"];
const HEALTH_DIMENSIONS=["source_reads","reconciliation","history_persistence","run_archive_persistence","attempt_persistence","call_ledger_persistence","run_evidence_persistence","schema_compatibility","freshness","stream","gateway","langfuse"];
let activeOpFilter=null;
function renderOperational(s){
  const counts=s.state_counts||{}, countBox=$("opcounts"); countBox.textContent="";
  OPERATIONAL_STATES.forEach(state=>{ const cell=el("div","opchip"+(activeOpFilter===state?" selected":""));
    const button=el("button",null); button.setAttribute("aria-pressed",activeOpFilter===state?"true":"false");
    button.appendChild(el("b",null,state)); button.appendChild(el("span",null,String(counts[state]||0)+" item"+((counts[state]||0)===1?"":"s")));
    button.onclick=()=>{ activeOpFilter=activeOpFilter===state?null:state; renderOperational(last); renderPipe(last); };
    cell.appendChild(button); countBox.appendChild(cell); });
  const health=s.health||{}, healthBox=$("healthgrid"); healthBox.textContent="";
  HEALTH_DIMENSIONS.forEach(name=>{ const record=health[name]||{status:"unknown",reason:"not reported"};
    const cell=el("div","healthitem health-"+esc(record.status));
    cell.appendChild(el("b",null,name.replace(/_/g," ")+" · "+String(record.status||"unknown").toUpperCase()));
    cell.appendChild(el("span",null,record.reason||("updated "+fmtET(record.updated_ts)))); healthBox.appendChild(cell); });
  $("transportState").textContent=streamState;
}

function renderPipe(s){
  const board=s.board||{}, box=$("pipe"); box.textContent="";
  const meta={pending:{},claimed:{},done:{color:"var(--ok)"},archive:{}};
  ["pending","claimed","done","archive"].forEach(state=>{
    const rows=(board[state]||[]).filter(h=>!activeOpFilter||h.operational_state===activeOpFilter); const st=el("div","stage");
    const sh=el("div","sh"); const n=el("span","n num",String(rows.length)); if(meta[state].color)n.style.color=meta[state].color;
    sh.appendChild(n); sh.appendChild(el("span","l",state)); st.appendChild(sh);
    const showChips = state==="pending"||state==="claimed";
    if(showChips){ rows.slice(0,4).forEach(h=>{ const ch=el("div","hchip"); ch.onclick=()=>openHandoff(h.id);
      ch.appendChild(el("span","id mono",h.id));
      ch.appendChild(el("span",null,(h.slug||"").replace(/-/g," ").slice(0,22)));
      ch.appendChild(el("span","opmeta",String(h.operational_state||h.state)+" · "+String(h.state_reason||"reason unknown")));
      ch.appendChild(el("span","opmeta","owner "+String(h.owner||"unknown")+" · "+(h.state_ts?fmtET(h.state_ts):"time unknown")));
      if(h.required_action) ch.appendChild(el("span","opmeta","action: "+h.required_action));
      if(h.escalation) ch.appendChild(el("span","opmeta","escalation "+String(h.escalation.severity||"unknown")+" · "+String(h.escalation.reason||"reason unknown")));
      if((h.conflicts||[]).length) ch.appendChild(el("span","opmeta","WARNING: "+h.conflicts.length+" retained conflict(s)"));
      const rt=el("span","rt"); const f=el("span",seatVCls(h.from),h.from||"?");
      rt.appendChild(f); rt.appendChild(document.createTextNode("→")); rt.appendChild(el("span",seatVCls(h.to),h.to||"?"));
      ch.appendChild(rt); st.appendChild(ch); });
      if(!rows.length) st.appendChild(el("div","more","empty"));
    } else { if(rows.length){ const h=rows[rows.length-1]; const ch=el("div","hchip"); ch.onclick=()=>openHandoff(h.id);
        ch.appendChild(el("span","id mono",h.id)); ch.appendChild(el("span",null,(h.slug||"").replace(/-/g," ").slice(0,20)));
        ch.appendChild(el("span","opmeta",String(h.operational_state||h.state)+" · "+String(h.state_reason||"reason unknown")));
        st.appendChild(ch); if(rows.length>1) st.appendChild(el("div","more","+ "+(rows.length-1)+" earlier")); }
      else st.appendChild(el("div","more","—")); }
    box.appendChild(st);
  });
}

function evIcon(ev){ const dec=ev.decision||{}, act=dec.action||"", stage=ev.stage||"";
  if(stage==="autopilot.round"&&act==="fail") return ["✖","v-crit"];
  if(stage==="autopilot.round"&&act==="reply") return ["↳",roleCls(ev.role)];
  if(stage==="autopilot.round"&&act==="start") return ["▶",roleCls(ev.role)];
  if(stage==="autopilot.lane"&&act==="verdict") return ["⚖",(dec.reason_codes||[]).some(x=>x.indexOf("CONFIRMED")>=0)?"v-crit":"v-ok"];
  if(stage==="autopilot.lane"&&act==="breaker") return ["🔨",roleCls(ev.role||"breaker")];
  if(stage==="autopilot.lane") return ["⚖","v-violet"];
  if(stage==="autopilot.autonomous_done"||stage==="handoff.done") return ["✓✓","v-ok"];
  if(stage==="autopilot.sendback") return ["🔁","v-crit"];
  if(stage==="autopilot.signoff_blocked") return ["⛔","v-crit"];
  if(stage==="autopilot.pause") return ["⏸","v-violet"];
  if(stage==="autopilot.control") return ["·","v-warn"];
  if(stage==="handoff.create") return ["+","muted"];
  if(stage==="review") return ["←","muted"];
  return ["·","muted"]; }

function evMsg(ev){ const dec=ev.decision||{}, act=dec.action||"", stage=ev.stage||"", rc=dec.reason_codes||[];
  const art=(ev.artifact||"").replace("handoff:",""); const role=ev.role||"";
  const b=(t,cls)=>{ const s=el("b",cls,t); return s; };
  const wrap=(...parts)=>{ const s=el("span","msg"); parts.forEach(p=> s.appendChild(typeof p==="string"?document.createTextNode(p):p)); return s; };
  const rcls=roleCls(role);
  if(stage==="autopilot.round"&&act==="reply"){ const nid=(rc.find(x=>x.indexOf("new:")===0)||"new:?").slice(4);
    return wrap(b(role,rcls)," answered "+art+" → "+nid); }
  if(stage==="autopilot.round"&&act==="fail") return wrap(b(role,rcls)," FAILED "+art+": ", el("span","v-crit",esc((ev.failure||{}).message).slice(0,50)));
  if(stage==="autopilot.round"&&act==="start") return wrap(b(role,rcls)," thinking on "+art);
  if(stage==="autopilot.round"&&act==="turn") return wrap(b(role,rcls)," finished turn on "+art);
  if(stage==="autopilot.lane"&&act==="breaker"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const nf=(rc.find(x=>x.indexOf("findings:")===0)||"findings:0").slice(9); return wrap(b(role,roleCls(role||"breaker"))," probed "+ln+" — "+nf+" finding"+(nf==="1"?"":"s")); }
  if(stage==="autopilot.lane"&&act==="verdict"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const fi=(rc.find(x=>x.indexOf("finding:")===0)||"").slice(8); const vd=(rc.find(x=>x.indexOf("verdict:")===0)||"verdict:?").slice(8);
    return wrap(b(role,roleCls(role||"verifier"))," "+ln+" "+fi+" → ", el("b",vd==="CONFIRMED"?"v-crit":"v-ok",vd)); }
  if(stage==="autopilot.lane"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const cf=(rc.find(x=>x.indexOf("confirmed:")===0)||"confirmed:0").slice(10); const rf=(rc.find(x=>x.indexOf("refuted:")===0)||"refuted:0").slice(8);
    return wrap("lane "+ln+" done · ", el("span","v-crit",cf+" confirmed"), " / ", el("span","v-ok",rf+" refuted")); }
  if(stage==="autopilot.autonomous_done") return wrap(el("b","v-ok",art+" SIGNED OFF → done"));
  if(stage==="autopilot.sendback"){ const n=(rc.find(x=>x.indexOf("defects:")===0)||"defects:?").slice(8);
    const sb=(rc.find(x=>x.indexOf("sendback:")===0)||"sendback:?").slice(9);
    return wrap(el("b","v-crit","sent back to builder")," — send-back #"+sb+", "+n+" defect"+(n==="1"?"":"s")); }
  if(stage==="autopilot.signoff_blocked") return wrap(b(art)," sign-off blocked: ", el("span","v-crit",rc.join(", ").slice(0,60)));
  if(stage==="autopilot.pause") return wrap(el("b","v-violet","autopilot stopped")," — awaiting human"+(art?" on "+art:""));
  if(stage==="autopilot.control") return wrap("· "+act+" ("+role+")");
  if(stage==="handoff.done") return wrap(el("b","v-ok",art+" → done"));
  if(stage==="handoff.create") return wrap("created "+art);
  if(stage==="review") return wrap("claim "+art);
  return wrap(stage+" "+act+" "+art); }

function buildFeed(box, evs, clickable){
  box.textContent="";
  if(!evs||!evs.length){ box.appendChild(el("div","muted","no events yet.")); return; }
  evs.forEach(ev=>{ const art=(ev.artifact||"").replace("handoff:","");
    const clk=clickable&&art;
    const row=el("div","ev"+(clk?" clk":"")); if(clk) row.onclick=()=>openHandoff(art);
    row.appendChild(el("span","ts mono",fmtET(ev.ts)));
    const [ic,icc]=evIcon(ev); row.appendChild(el("span","ic "+icc,ic));
    row.appendChild(evMsg(ev));
    const m=ev.metrics||{}; if(m.latency_ms!=null){ row.appendChild(el("span","lat mono",fmtms(m.latency_ms))); }
    else row.appendChild(el("span","lat",""));
    box.appendChild(row); });
}
function renderFeed(s){
  const box=$("feed"); const atBottom=(box.scrollHeight-box.scrollTop-box.clientHeight)<24;
  buildFeed(box,(s.events||[]),true);
  if(atBottom) box.scrollTop=box.scrollHeight;
}

// ---- max work-attempt control ------------------------------------------- //
function fmtDurMs(ms){ if(ms==null) return "-"; return fmtdur(ms/1000); }
function shortId(u){ if(!u) return "—"; const m=String(u).match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z?-(\d+)$/);
  if(m){ const mo=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][(+m[2])-1]||m[2];
    return mo+" "+m[3]+" "+m[4]+":"+m[5]+" · "+m[6]; }   // "20260711T175411Z-76072" -> "Jul 11 17:54 · 76072"
  return String(u).slice(0,10); }
function syncTurns(s){
  const inp=$("maxturns");
  // CRITICAL focus guard (mirrors the renderSeats <select> guard): if the operator is typing in the
  // cap field, DON'T overwrite it on the 2s poll — that would eat their keystrokes. Only sync when idle.
  if(document.activeElement!==inp){
    const mx=maxWorkAttempts(s);
    if(mx!=null) inp.value=mx; else if(!inp.value) inp.placeholder="cap";
  }
  $("btnTurns").disabled=!s.status;
}
function setTurns(){ const v=Number($("maxturns").value);
  if(!Number.isInteger(v)||v<1||v>50){ toast("cap must be an integer 1-50","err"); return; }
  ctl("max-turns",{n:v});   // ctl handles token/JSON/toast/refresh
}

// ---- run history + compare ---------------------------------------------- //
let cmpSel=[];  // up to two run_uids selected for compare (persists across polls)
function runPhaseCls(ph){ return ph==="done"?"v-ok":ph==="capped"?"v-violet":ph==="paused"?"v-warn":
  ph==="thinking"?"role-g":ph==="failed"?"v-crit":"muted"; }
function renderRuns(s){
  const box=$("runs"), bar=$("runbar"), runs=(s.runs||[]);
  box.textContent="";
  if(!runs.length){ box.appendChild(el("div","muted","no completed runs yet.")); bar.style.display="none"; return; }
  const present=new Set(runs.map(r=>r.run_uid)); cmpSel=cmpSel.filter(u=>present.has(u));
  runs.forEach(r=>{
    const uid=r.run_uid||"";
    const row=el("div","runrow"+(r.current?" current":""));
    const chkw=el("label","rchk"); chkw.onclick=e=>e.stopPropagation();
    const chk=el("input"); chk.type="checkbox"; chk.checked=cmpSel.includes(uid);
    chk.setAttribute("aria-label","select run for compare");
    chk.onclick=e=>{ e.stopPropagation(); toggleCmp(uid,chk); };
    chkw.appendChild(chk); row.appendChild(chkw);
    const meta=el("div","rmeta");
    meta.appendChild(el("span","when",fmtET(r.started_ts)||"—"));
    meta.appendChild(el("span",runPhaseCls(r.phase_final),r.phase_final||"—"));
    const sub=el("span","sub");
    sub.textContent=(r.rounds_total!=null?r.rounds_total:"?")+" actor turns · "+fmtDurMs(r.duration_ms);
    meta.appendChild(sub);
    if(uid) meta.appendChild(el("span","uid mono",shortId(uid)));
    row.appendChild(meta);
    const tags=el("div","rtags");
    if(r.current) tags.appendChild(el("span","rtag livepin","● live"));
    const L=r.lanes||{}; tags.appendChild(el("span","rtag","C"+(L.confirmed||0)+"/R"+(L.refuted||0)));
    const so=(r.signoff||{}).result; if(so) tags.appendChild(el("span","rtag","signoff "+so));
    row.appendChild(tags);
    row.onclick=()=>openRun(uid);
    box.appendChild(row);
  });
  bar.style.display="flex"; updateCmpUI();
}
function toggleCmp(uid,chk){
  const i=cmpSel.indexOf(uid);
  if(i>=0) cmpSel.splice(i,1);
  else { if(cmpSel.length>=2){ chk.checked=false; toast("pick exactly two runs to compare","err"); return; } cmpSel.push(uid); }
  updateCmpUI();
}
function updateCmpUI(){ $("btnCompare").disabled=cmpSel.length!==2;
  $("cmpHint").textContent=cmpSel.length===0?"Select two runs to compare.":
    cmpSel.length===1?"Select one more run.":"Ready to compare."; }

let rviewerOpener=null;
function openRViewer(title){ rviewerOpener=document.activeElement;
  $("rv-title").textContent=title; $("rv-fm").textContent=""; $("rv-body").textContent="loading…";
  $("rviewer").style.display="flex"; $("rviewer").querySelector("button").focus(); }
function closeRViewer(){ $("rviewer").style.display="none"; if(rviewerOpener&&rviewerOpener.focus) rviewerOpener.focus(); }
$("rviewer").addEventListener("keydown",e=>{ if(e.key==="Escape") closeRViewer(); });

function laneBreakdown(box,byLane){
  if(!byLane||typeof byLane!=="object") return;
  const keys=Object.keys(byLane); if(!keys.length) return;
  const h=el("div","eyebrow"); h.textContent="lanes"; h.style.margin="14px 0 6px"; box.appendChild(h);
  keys.forEach(k=>{ const v=byLane[k]||{};
    const cf=typeof v==="object"?(v.confirmed||0):v, rf=typeof v==="object"?(v.refuted||0):0;
    const row=el("div","lane "+(cf>0?"hit":"clean")); row.appendChild(el("div","verdict",cf>0?"!":"✓"));
    const c=el("div","ln"); c.appendChild(el("div","name",k)); row.appendChild(c);
    const t=el("div","tally"); t.appendChild(el("span","c",cf+" confirmed")); t.appendChild(el("br"));
    t.appendChild(el("span","r",rf+" refuted")); row.appendChild(t); box.appendChild(row); });
}
function replaySection(box,title,subtitle){ const section=el("section","replay-section");
  const h=el("div","eyebrow",title); h.style.margin="14px 0 6px"; section.appendChild(h);
  if(subtitle) section.appendChild(el("div","muted",subtitle)); box.appendChild(section); return section; }
function replayRow(section,title,detail,cls){ const row=el("div","hchip");
  row.appendChild(el("div","b",title)); if(detail) row.appendChild(el("div",cls||"muted",detail)); section.appendChild(row); }
function replayWindow(section,j,key){ const meta=((j.collection_windows||{})[key])||{};
  if(meta.truncated) section.appendChild(el("div","muted","Showing latest "+meta.returned+" of "+meta.total+" retained records. Full evidence remains available from the unwindowed run API.")); }
function renderHistoricalEvidence(body,j){
  const human=j.operator_run_summary||null;
  if(!human){ const missing=replaySection(body,"Human run summary");
    replayRow(missing,"Historical human summary was not recorded","This legacy archive is not promoted to complete; use only the retained sections below.","v-warn");
  } else {
    const outcome=human.outcome||{}, facts=human.proven_facts||{};
    const summary=replaySection(body,"Human run summary","Evidence status · "+(human.truth_status||"unknown"));
    replayRow(summary,human.objective||"Objective not recorded","Run objective");
    replayRow(summary,
      "Stopped after completed round "+(outcome.completed_rounds??"not recorded")+" of maximum "+(outcome.maximum_rounds??"not recorded"),
      "Reason: "+(outcome.stop_reason||"not recorded"), human.truth_status==="complete"?"v-ok":"v-warn");
    replayRow(summary,"Models expected · "+(facts.models_expected||[]).join(", "),"Attempted · "+(facts.models_attempted||[]).join(", "));
    replayRow(summary,"Gateway and telemetry",
      (facts.calls_reaching_litellm??0)+" reached LiteLLM · "+(facts.provider_responses??0)+" provider responses · "+(facts.calls_reaching_langfuse??0)+" reached Langfuse · "+(facts.explicit_telemetry_failures??0)+" explicit export failures");
    const missingEvidence=human.missing_evidence||[];
    replayRow(summary,"Missing evidence · "+missingEvidence.length,missingEvidence.length?missingEvidence.join(" · "):"None recorded",missingEvidence.length?"v-warn":"v-ok");
    replayRow(summary,"Known risks · "+(human.known_risks||[]).length,(human.known_risks||[]).join(" · ")||"None recorded",(human.known_risks||[]).length?"v-warn":"v-ok");
    replayRow(summary,"Human actions · "+(human.human_actions||[]).length,(human.human_actions||[]).join(" · ")||"No operator action recorded");
    const judgments=replaySection(body,"Evaluator judgments","Independent decisions remain distinct from model claims.");
    (human.evaluator_judgments||[]).forEach(item=>replayRow(judgments,(item.disposition||"unknown")+" · "+(item.candidate_id||"candidate unknown"),(item.explanation||"Explanation not recorded")+" Impact: "+(item.impact||"unknown")));
    (human.model_claims||[]).forEach(item=>replayRow(judgments,"MODEL CLAIM · "+(item.validation_id||"claim"),(item.status||"unknown")+" · not an acceptance oracle","v-warn"));
  }
  const roster=replaySection(body,"Execution roster","Every planned model and its terminal evidence state.");
  (j.roster||[]).forEach(item=>replayRow(roster,(item.role||"unknown role")+" · "+(item.model||"model not recorded"),modelStateLabel(item.state)+" · terminal "+(item.terminal_disposition||"not reconciled")+" · telemetry "+(item.telemetry_outcome||"not applicable")+" · "+(item.orchestration_execution_count||0)+" orchestration execution(s) · "+(item.provider_attempt_count||0)+" provider call(s)"+(item.terminal_reason?" · reason "+item.terminal_reason:"")));
  if(!(j.roster||[]).length) replayRow(roster,"Not recorded","No execution roster was retained.","v-warn");
  const attempts=replaySection(body,"Model attempts","Lifecycle and telemetry outcomes; identifiers are secondary evidence.");
  (j.attempts||[]).forEach(item=>replayRow(attempts,(item.requested_model||"unknown model")+" · "+modelStateLabel(item.state)+" · "+(item.source||"source unknown"),(item.seat||"unknown role")+" · parent execution "+(item.parent_attempt_id||"none")+" · route "+(item.gateway_route||"not recorded")+" · resolved "+(item.actual_model||"not recorded")+" · provider "+(item.provider||"not recorded")+" · first token "+fmtms(item.first_token_latency_ms)+" · duration "+fmtms(item.total_duration_ms)+" · telemetry "+(item.telemetry_result||item.telemetry_state||"not recorded")+" · LiteLLM "+(item.gateway_request_id||"not recorded")+" · trace "+(item.trace_id||"not recorded")+" · tool lifecycle "+(toolActivitySummary(item.tool_activity)||"not retained")+" · attempt "+(item.attempt_id||"unknown")));
  replayWindow(attempts,j,"model_activity");
  const candidates=replaySection(body,"Candidates and lineage");
  (j.candidates||[]).forEach(item=>replayRow(candidates,(item.candidate_id||"unknown candidate")+" · "+(item.current_disposition||"unknown"),"Parent "+(item.parent_candidate_id||"none")+" · files "+((item.files||[]).join(", ")||"not recorded")+" · artifact "+(item.final_artifact_ref||"not recorded")+" · commit "+(item.final_commit||"not recorded")+" · revision evidence "+((item.revision_evidence_refs||[]).join(", ")||"none")));
  replayWindow(candidates,j,"candidates");
  const quality=replaySection(body,"Validation and requirements","Source authority remains visible.");
  (j.validations||[]).forEach(item=>replayRow(quality,(item.validation_id||"validation")+" · "+(item.status||"unknown"),(item.source_kind||"source unknown")+" · producer "+(item.producer||"unknown")+" "+(item.producer_version||"version unknown")+" · artifact "+(item.artifact_ref||"not recorded")+" · dimensions "+(JSON.stringify(item.dimensions||{}))+" · baseline "+(JSON.stringify(item.baseline_delta||{}))+" · uncertainty "+(item.uncertainty||"not recorded")+" · gaps "+((item.gaps||[]).join(", ")||"none")+" · test quality "+JSON.stringify(item.test_quality||{})));
  (j.requirements||[]).forEach(item=>replayRow(quality,(item.requirement_id||"requirement")+" · "+(item.effective_status||item.status||"unknown"),(item.critical?"critical":"non-critical")+" · "+(item.description||"text not recorded")+" · "+(item.source_kind||"source unknown")+" · evidence "+((item.evidence_refs||[]).join(", ")||"not recorded")));
  replayWindow(quality,j,"validations"); replayWindow(quality,j,"requirements");
  const dispositions=replaySection(body,"Candidate dispositions","Reasons, impact, remediation, and disagreement are retained.");
  (j.dispositions||[]).forEach(item=>replayRow(dispositions,(item.disposition||"unknown")+" · "+(item.candidate_id||"candidate unknown"),(item.executive_explanation||"Explanation not recorded")+" Reason: "+(item.primary_reason||"not applicable")+" / "+((item.reason_categories||[]).join(", ")||"none")+". Failed checks: "+((item.failed_checks||[]).join(", ")||"none")+". Impact: "+(item.impact||"unknown")+" Remediation: "+(item.remediation||"not recorded")+" Weaknesses: "+((item.weaknesses||[]).join(", ")||"none")+" Unavailable evidence: "+((item.unavailable_evidence||[]).join(", ")||"none")+((item.disagreements||[]).length?(" Evaluator disagreement: "+(item.resolution||"unresolved")):"")));
  replayWindow(dispositions,j,"dispositions");
}
async function openRun(uid){
  if(!uid) return; openRViewer("run "+shortId(uid));
  const body=$("rv-body");
  try{ const r=await fetch("/api/run?id="+encodeURIComponent(uid)+"&window=1"); const j=await r.json();
    if(!r.ok){ body.textContent=j.error||("error "+r.status); return; }
    const sum=j.summary||{};
    $("rv-title").textContent="run "+shortId(sum.run_uid||uid)+(sum.phase_final?" · "+sum.phase_final:"");
    const fm=$("rv-fm"); fm.textContent="";
    const addfm=(k,v)=>{ if(v==null||v==="") return; const sp=el("span");
      sp.appendChild(el("b",null,k+": ")); sp.appendChild(document.createTextNode(String(v))); fm.appendChild(sp); };
    addfm("started",fmtET(sum.started_ts)); addfm("actor turns",sum.rounds_total); addfm("max work attempts",sum.max_rounds);
    addfm("duration",fmtDurMs(sum.duration_ms)); addfm("legacy turn count",sum.calls);
    const lz=sum.lanes||{}; addfm("lanes","C"+(lz.confirmed||0)+"/R"+(lz.refuted||0));
    addfm("signoff",(sum.signoff||{}).result);
    body.textContent=""; renderHistoricalEvidence(body,j);
    const ah=el("div","eyebrow"); ah.textContent="technical activity tail"; ah.style.margin="14px 0 6px"; body.appendChild(ah);
    const feedbox=el("div","feed"); buildFeed(feedbox,(j.events||[]),false); body.appendChild(feedbox);
    laneBreakdown(body, lz.by_lane || (j.lanes||{}).by_lane || j.lanes);
  }catch(e){ body.textContent="failed to load: "+e; }
}

function cellStr(v){ if(v==null) return "—"; if(typeof v==="object"){ try{ return JSON.stringify(v); }catch(e){ return String(v); } } return String(v); }
function buildCompare(box,j){
  const A=j.a||{}, B=j.b||{}, D=j.deltas||{};
  const keys=[...new Set([...Object.keys(A),...Object.keys(B),...Object.keys(D)])].sort();
  const wrap=el("div"); wrap.style.overflowX="auto";
  const tbl=el("table","cmptbl");
  const hr=el("tr"); ["field","A · "+shortId(A.run_uid),"B · "+shortId(B.run_uid),"Δ"].forEach(h=>hr.appendChild(el("th",null,h)));
  tbl.appendChild(hr);
  keys.forEach(k=>{ const tr=el("tr");
    tr.appendChild(el("td","k",k));
    tr.appendChild(el("td","v",cellStr(A[k])));
    tr.appendChild(el("td","v",cellStr(B[k])));
    let cls="d same", txt="—", dv=D[k];
    if(dv!=null){ if(typeof dv==="number"){ cls="d "+(dv>0?"up":dv<0?"down":"same"); txt=(dv>0?"+":"")+dv; } else txt=cellStr(dv); }
    else if(typeof A[k]==="number"&&typeof B[k]==="number"){ const d=B[k]-A[k]; cls="d "+(d>0?"up":d<0?"down":"same"); txt=(d>0?"+":"")+d; }
    tr.appendChild(el("td","v "+cls,txt)); tbl.appendChild(tr); });
  wrap.appendChild(tbl); box.appendChild(wrap);
}
async function doCompare(){
  if(cmpSel.length!==2) return; const [a,b]=cmpSel;
  openRViewer("compare runs");
  const body=$("rv-body");
  try{ const r=await fetch("/api/compare?a="+encodeURIComponent(a)+"&b="+encodeURIComponent(b)); const j=await r.json();
    if(!r.ok){ body.textContent=j.error||("error "+r.status); return; }
    body.textContent=""; buildCompare(body,j);
  }catch(e){ body.textContent="failed to load: "+e; }
}

function setTransport(state){
  streamState=state; connected=state==="CONNECTED";
  document.body.classList.toggle("disconnected",state==="DISCONNECTED");
  if($("transportState")) $("transportState").textContent=state;
  if(last) render(last);
}
function applyStreamEvent(event,kind){
  let s; try{s=JSON.parse(event.data);}catch(e){return;}
  const info=s.stream||{}, instance=info.instance_id||String(event.lastEventId||"").split(":")[0];
  const sequence=Number(info.sequence||String(event.lastEventId||"").split(":")[1]||0);
  if(kind!=="reconcile"&&streamInstance===instance&&sequence<=streamSequence) return;
  if(kind!=="reconcile"&&streamInstance===instance&&streamSequence&&sequence>streamSequence+1){ reconcileState(); return; }
  streamInstance=instance||streamInstance; streamSequence=sequence||streamSequence;
  streamCursor=event.lastEventId||streamCursor; last=s; reconnectAttempt=0;
  setTransport("CONNECTED"); render(s);
}
function connectStream(){
  if(stream) stream.close();
  const suffix=streamCursor?("?cursor="+encodeURIComponent(streamCursor)):"";
  stream=new EventSource("/api/stream"+suffix);
  stream.onopen=()=>{ reconnectAttempt=0; setTransport("CONNECTED"); };
  stream.addEventListener("snapshot",event=>applyStreamEvent(event,"snapshot"));
  stream.addEventListener("reconcile",event=>applyStreamEvent(event,"reconcile"));
  stream.onerror=()=>{ stream.close(); stream=null;
    const delay=RECONNECT_DELAYS[Math.min(reconnectAttempt,RECONNECT_DELAYS.length-1)];
    setTransport(reconnectAttempt>=RECONNECT_DELAYS.length?"DISCONNECTED":last?"STALE":"RECONNECTING");
    reconnectAttempt++; setTimeout(connectStream,delay); };
}
async function reconcileState(){
  if(reconcileBusy) return; reconcileBusy=true;
  try{ const response=await fetch("/api/state"); if(!response.ok) throw new Error("state "+response.status);
    const s=await response.json(); last=s; if(streamState!=="DISCONNECTED") connected=true; render(s);
  }catch(e){ if(!stream) setTransport(last?"STALE":"DISCONNECTED"); }
  finally{ reconcileBusy=false; }
}
async function refresh(){ return reconcileState(); }
document.addEventListener("keydown",e=>{ const tag=(e.target.tagName||"").toLowerCase();
  if(tag==="input"||tag==="textarea"||tag==="select") return;
  if($("viewer").style.display==="flex"||$("rviewer").style.display==="flex") return;
  if(e.key==="p"){ if(!$("btnPause").disabled) ctl("pause"); }
  else if(e.key==="r"){ if(!$("btnResume").disabled) ctl("resume"); }
  else if(e.key==="s"){ doStop(); } });
syncThemeIcon();
matchMedia('(prefers-color-scheme: light)').addEventListener('change',syncThemeIcon);
reconcileState(); connectStream(); setInterval(reconcileState,30000);
setInterval(()=>{ if(last) render(last); },1000);
</script>
</body></html>
"""
