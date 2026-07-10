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

import json
import re
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HID_RE = re.compile(r"\d{1,9}")  # a handoff id is a short zero-padded integer — never a path fragment
_SEAT_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")   # a seat name — never a path fragment / key-lookup only
_MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,60}")  # a catalog model id (e.g. gpt-5.5, grok-4.5-textonly)
_RUN_RE = re.compile(r"[0-9A-Za-z._-]{1,64}")   # a run_uid — never joined into a path (key-lookup only)

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402
import collab_common as cc  # noqa: E402

_DEFAULT_PORT = 8787


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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

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
            rid = (urllib.parse.parse_qs(parts.query).get("id") or [""])[0].strip()
            if not _RUN_RE.fullmatch(rid):
                return self._json(400, {"error": "bad run id"})
            try:  # unknown/malformed uid -> 400; a run that no longer exists on disk -> 404.
                return self._json(200, dc.run_detail(collab, rid))
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
        except (ValueError, OSError):
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
                hid = str(body.get("hid") or "").strip()
                if not _HID_RE.fullmatch(hid):
                    return self._json(400, {"error": "bad hid"})
                return self._json(200, dc.advance_handoff(collab, hid))
            if self.path == "/api/nudge":
                hid = str(body.get("hid") or "").strip()
                if not _HID_RE.fullmatch(hid):
                    return self._json(400, {"error": "bad hid"})
                return self._json(200, dc.nudge(collab, hid))
            if self.path == "/api/seat-model":
                seat, model = body.get("seat"), body.get("model")
                if not (isinstance(seat, str) and isinstance(model, str)
                        and _SEAT_RE.fullmatch(seat) and _MODEL_RE.fullmatch(model)):
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
        except hc.HandoffNotFound as e:
            return self._json(404, {"error": str(e)})
        except hc.HandoffConflict as e:
            return self._json(409, {"error": str(e)})
        except cc.CollabError as e:
            return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found"})


def serve(collab, home=None, port: int = _DEFAULT_PORT) -> int:
    """Start the local dashboard server (blocking). Ctrl-C to stop."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.collab = str(collab)      # type: ignore[attr-defined]
    httpd.home = home               # type: ignore[attr-defined]
    httpd.token = secrets.token_urlsafe(16)  # type: ignore[attr-defined]
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
  .stage .more{ font-size:11.5px; color:var(--faint); padding:2px; }

  .feed .ev{ display:grid; grid-template-columns:64px 18px 1fr auto; gap:10px; align-items:center;
    padding:6px 4px; border-bottom:1px solid var(--border-soft); font-size:12.5px; }
  .feed .ev:last-child{ border-bottom:0; }
  .feed .ev.clk{ cursor:pointer; border-radius:6px; }
  .feed .ev.clk:hover{ background:var(--raised); }
  .feed .ev .ts{ color:var(--faint); font-size:11.5px; } .feed .ev .ic{ text-align:center; }
  .feed .ev .msg b{ font-weight:640; } .feed .ev .lat{ color:var(--muted); font-size:11px; }
  .role-c{ color:var(--claude); } .role-g{ color:var(--gpt); }
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
  @media (prefers-reduced-motion:reduce){ *{ animation:none!important; transition:none!important; } }
</style></head>
<body>
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
  <span class="turnbox" title="Round budget — human sign-off gate">
    <span class="tlbl">cap</span>
    <input type="number" min="1" max="50" id="maxturns" class="mono" aria-label="Max rounds (round budget)">
    <button class="ghost" id="btnTurns" onclick="setTurns()">Set</button>
  </span>
  <button class="ghost" id="btnPause" onclick="ctl('pause')">Pause</button>
  <button class="ghost" id="btnResume" onclick="ctl('resume')">Resume</button>
  <button class="danger ghost" id="btnStop" onclick="doStop()">Stop</button>
</header>

<div class="wrap">
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

  <div class="grid">
    <section class="card full" id="narrCard" style="display:none"><h2>What happened
      <span class="r"><span id="narrMeta"></span>
      <button class="iconbtn ghost" id="narrToggle" aria-label="Collapse" aria-expanded="true"
        onclick="toggleNarr()" title="Collapse / expand">&#9662;</button></span></h2>
      <div id="narr" class="narr"></div></section>

    <section class="card"><h2>Agents
      <span class="r"><span class="legdot"><i style="background:var(--claude)"></i>Claude</span>
      <span class="legdot"><i style="background:var(--gpt)"></i>OpenAI</span></span></h2>
      <div id="agents"></div></section>

    <section class="card" id="lanesCard"><h2>Adversarial lanes <span class="r lanehead" id="lanehead"></span></h2>
      <div id="lanes"></div></section>

    <section class="card full"><h2>Handoff pipeline <span class="r">state machine · newest first</span></h2>
      <div class="pipe" id="pipe"></div></section>

    <section class="card full"><h2>Activity <span class="r">recent rounds &amp; lanes</span></h2>
      <div class="feed" id="feed"></div></section>

    <section class="card full" id="runsCard"><h2>Run history <span class="r">newest first · click a row for detail</span></h2>
      <div class="runs" id="runs"></div>
      <div class="runbar" id="runbar" style="display:none">
        <span class="muted" id="cmpHint">Select two runs to compare.</span>
        <span class="spacer" style="flex:1"></span>
        <button class="primary" id="btnCompare" onclick="doCompare()" disabled>Compare</button>
      </div></section>
  </div>

  <footer id="foot"></footer>
</div>

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
let narrHid=null, narrData=null;   // the handoff the "What happened" card is showing + its fetched narrative
const $=id=>document.getElementById(id);
const esc=s=>(s==null?"":String(s));
function el(tag,cls,txt){const e=document.createElement(tag); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e;}
function fmtms(ms){ if(ms==null) return "-"; return ms<1000? Math.round(ms)+"ms" : (ms/1000).toFixed(1)+"s"; }
function fmtage(s){ if(s==null) return ""; s=Math.floor(s); return s<60? s+"s" : s<3600? Math.floor(s/60)+"m" : Math.floor(s/3600)+"h"; }
// Timestamps are stored UTC; the dashboard shows them in US Eastern (America/New_York — handles EST/EDT).
function fmtET(iso){ if(!iso) return ""; try{ return new Date(iso).toLocaleTimeString("en-US",{timeZone:"America/New_York",hour12:false}); }catch(e){ return (iso||"").slice(11,19); } }
function fmtdur(s){ if(s==null||s<0) return "-"; s=Math.floor(s); const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
  return h? h+"h"+m+"m" : m? m+"m"+ss+"s" : ss+"s"; }
function fmtbytes(b){ if(b==null) return ""; return b<1024? b+"B" : (b/1024).toFixed(1)+"KB"; }

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
function vColor(n){ return {c:"var(--claude)",g:"var(--gpt)",k:"var(--grok)",m:"var(--gemini)"}[seatVendor(n)]||"var(--gpt)"; }
function seatModel(n){ const m=(last&&last.seats&&last.seats[n])||null; if(!m) return null;
  const mo=m.model, la=m.launcher; if(mo&&la&&la!=="python") return la+" · "+mo; return mo||la||null; }
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
function doStop(){ if(confirm("Stop the autopilot loop (graceful, reversible)?")) ctl("stop"); }
function approve(hid){ if(confirm("Approve (advance) "+hid+" to done?")) ctl("approve",{hid}); }
function nudge(hid){ if(confirm("Re-queue "+hid+" as a NEW pending handoff?")) ctl("nudge",{hid}); }

let viewerOpener=null;
async function openHandoff(hid){
  viewerOpener=document.activeElement;
  $("v-title").textContent="handoff "+hid; $("v-fm").textContent=""; $("v-reply").style.display="none";
  $("v-body").textContent="loading…"; $("viewer").style.display="flex"; $("viewer").querySelector("button").focus();
  try{ const r=await fetch("/api/handoff?hid="+encodeURIComponent(hid)); const j=await r.json();
    if(!r.ok){ $("v-body").textContent=j.error||("error "+r.status); return; }
    $("v-title").textContent="handoff "+j.id+" · "+esc(j.state); $("v-reply").style.display=j.is_reply?"inline-block":"none";
    const fm=j.frontmatter||{}, fmbox=$("v-fm"); fmbox.textContent="";
    ["from","to","title","priority","date","status"].forEach(k=>{ if(fm[k]!=null){ const s=el("span");
      s.appendChild(el("b",null,k+": ")); s.appendChild(document.createTextNode(esc(fm[k]))); fmbox.appendChild(s); }});
    $("v-body").textContent = j.body_text&&j.body_text.trim()? j.body_text : "(no body)";
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
  setFav(PHASECOL[ph]||"#5c6675"); document.title=(s.status?"● ":"")+phaseLabel(ph)+" · autopilot";
  $("empty").style.display = (!s.status && !(s.events&&s.events.length))? "block":"none";

  renderNarrative(s); renderHero(s); renderSeats(s); renderLanes(s); renderPipe(s); renderFeed(s); renderRuns(s); syncTurns(s);
  const cn=s.counts||{};
  const foot=$("foot"); foot.textContent="";
  [["pending",cn.pending],["claimed",cn.claimed],["done",cn.done],["archive",cn.archive]].forEach(([k,v])=>{
    const w=el("span"); w.appendChild(document.createTextNode(k+" ")); w.appendChild(el("b",null,String(v||0))); foot.appendChild(w); });
  foot.appendChild(el("span","spacer")); foot.lastChild.style.flex="1";
  const l1=el("span","legdot"); l1.appendChild(el("i")); l1.lastChild.style.background="var(--claude)"; l1.appendChild(el("span",null,"Claude builds")); foot.appendChild(l1);
  const l2=el("span","legdot"); l2.appendChild(el("i")); l2.lastChild.style.background="var(--gpt)"; l2.appendChild(el("span",null,"ChatGPT reviews · breaks · verifies")); foot.appendChild(l2);
}

// ---- "What happened" narrative card ------------------------------------- //
// Picks the handoff worth narrating (the one being worked, else the newest done/claimed) and lazy-loads its
// human-readable story from /api/narrative — re-fetched only when the focus handoff changes, not every poll.
function focusHid(s){
  const st=s.status||{};
  if(st.current_hid) return String(st.current_hid);
  const b=s.board||{};
  const done=b.done||[]; if(done.length) return String(done[done.length-1].id);
  const cl=b.claimed||[]; if(cl.length) return String(cl[cl.length-1].id);
  return null;
}
function renderNarrative(s){
  const card=$("narrCard"); const hid=focusHid(s);
  if(!hid){ card.style.display="none"; narrHid=null; narrData=null; return; }
  card.style.display=""; applyNarrCollapse();
  if(hid!==narrHid){ narrHid=hid; narrData=null; paintNarrative(); fetchNarrative(hid); }
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
  if(!s.status){ stripe.style.background="var(--faint)"; eb.textContent="offline"; ti.textContent="Driver not running";
    sub.textContent="Start autopilot against this collab to see live activity."; return; }
  const active = st.active_seat && st.current_hid && !s.paused && ph==="thinking";
  let color="var(--faint)";
  if(active){
    color=vColor(st.active_seat);
    eb.textContent="round "+(st.round||0)+" / "+(st.max_rounds||0)+" · working";
    const verb = st.active_seat==="reviewer"?"is reviewing":st.active_seat==="builder"?"is building on":
      (st.active_seat==="breaker"||st.active_seat==="verifier")?"is probing":"is working on";
    const t1=el("span",null,st.active_seat); t1.style.color=color; ti.appendChild(t1);
    ti.appendChild(document.createTextNode(" "+verb+" "+st.current_hid));
    const since=Date.parse(st.active_since||st.updated_ts); const elp=(Date.now()-since)/1000, to=st.timeout||0;
    sub.textContent=(seatModel(st.active_seat)||"")+" · "+fmtdur(elp)+(to?" / "+fmtdur(to):"")+" elapsed";
  } else if(ph==="capped"){
    const err=st.last_error||""; const hm=err.match(/^(\d{1,9})/); const hid=hm?hm[1]:(st.current_hid||"");
    const doneIds=new Set(((s.board||{}).done||[]).concat((s.board||{}).archive||[]).map(h=>h.id));
    if(hid && doneIds.has(hid)){ color="var(--ok)"; eb.textContent="last run"; ti.textContent="Resolved — "+hid+" shipped";
      sub.textContent="The capped handoff was approved or finished out of band. Loop idle; start a run to continue."; }
    else{ color="var(--violet)"; eb.textContent="round budget reached · "+(st.round||0)+" / "+(st.max_rounds||0);
      ti.textContent="The gate held. Your call.";
      const blk=(s.lanes||{}).blockers||0;
      sub.textContent=(hid?("Handoff "+hid+" was not signed off. "):"")+(blk?(blk+" adversarial finding"+(blk===1?"":"s")+" unresolved. "):"")+"Nothing shipped autonomously — exactly as designed.";
      if(hid){ const b1=el("button","primary","Approve "+hid+" → done"); b1.onclick=()=>approve(hid); cta.appendChild(b1);
        const b2=el("button","warn ghost","Re-queue "+hid); b2.onclick=()=>nudge(hid); cta.appendChild(b2); } }
  } else if(ph==="paused"){ color="var(--warn)"; eb.textContent="held"; ti.textContent="Paused";
    sub.textContent="The loop is frozen and fully reversible. Resume when you're ready.";
    const b=el("button","primary","Resume"); b.onclick=()=>ctl("resume"); cta.appendChild(b);
  } else if(ph==="done"){ color="var(--ok)"; eb.textContent="complete"; ti.textContent="Thread complete";
    sub.textContent="The board is drained and autopilot is idle.";
  } else if(ph==="sleeping"||ph==="idle"){ color="var(--faint)"; eb.textContent=ph; ti.textContent="Idle — watching for work";
    sub.textContent="No pending handoff addressed to a CLI seat right now.";
  } else { color="var(--faint)"; eb.textContent=ph||"—"; ti.textContent="Autopilot"; sub.textContent=""; }
  stripe.style.background=color;
  if(st.started_ts) metric(mx,fmtdur((Date.now()-Date.parse(st.started_ts))/1000),"uptime");
  metric(mx,String(ov.rounds||0),"rounds");
  metric(mx,String(ov.fails||0),"failures",ov.fails?"var(--crit)":"var(--ok)");
  if(ov.avg_ms){ const m=el("div","metric"); m.appendChild(el("div","v mono",fmtms(ov.avg_ms))); m.appendChild(el("div","k","avg round")); mx.appendChild(m); }
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
      if(d.rounds){ const r1=el("div"); r1.appendChild(el("span","b",String(d.rounds))); r1.appendChild(document.createTextNode(" rounds")); stt.appendChild(r1);
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

function renderLanes(s){
  const L=s.lanes; const card=$("lanesCard"), box=$("lanes"), head=$("lanehead");
  if(!L||!L.lanes||!L.lanes.length){ card.style.display="none"; return; }
  card.style.display=""; head.textContent="";
  const tp=el("span","chip "+(L.tests_passed?"ok":"crit"),L.tests_passed?"tests ✓":"tests ✗"); head.appendChild(tp);
  head.appendChild(el("span","chip "+((L.blockers||0)?"crit":""),(L.blockers||0)+" blocker"+((L.blockers||0)===1?"":"s")));
  box.textContent="";
  L.lanes.forEach(ln=>{
    const hit=(ln.confirmed||0)>0; const d=el("div","lane "+(ln.ran?(hit?"hit":"clean"):""));
    d.appendChild(el("div","verdict",hit?"!":"✓"));
    const c=el("div","ln"); c.appendChild(el("div","name",ln.lane||"?"));
    const flow=el("div","flow"); const b=el("span","g",ln.breaker||"breaker"); flow.appendChild(b);
    flow.appendChild(el("span","arrow"," → ")); flow.appendChild(el("span","g",ln.verifier||"verifier")); c.appendChild(flow);
    d.appendChild(c);
    const t=el("div","tally"); t.appendChild(el("span","c",(ln.confirmed||0)+" confirmed")); t.appendChild(el("br"));
    t.appendChild(el("span","r",(ln.refuted||0)+" refuted")); d.appendChild(t);
    box.appendChild(d);
  });
}

function renderPipe(s){
  const board=s.board||{}, box=$("pipe"); box.textContent="";
  const meta={pending:{},claimed:{},done:{color:"var(--ok)"},archive:{}};
  ["pending","claimed","done","archive"].forEach(state=>{
    const rows=board[state]||[]; const st=el("div","stage");
    const sh=el("div","sh"); const n=el("span","n num",String(rows.length)); if(meta[state].color)n.style.color=meta[state].color;
    sh.appendChild(n); sh.appendChild(el("span","l",state)); st.appendChild(sh);
    const showChips = state==="pending"||state==="claimed";
    if(showChips){ rows.slice(0,4).forEach(h=>{ const ch=el("div","hchip"); ch.onclick=()=>openHandoff(h.id);
      ch.appendChild(el("span","id mono",h.id));
      ch.appendChild(el("span",null,(h.slug||"").replace(/-/g," ").slice(0,22)));
      const rt=el("span","rt"); const f=el("span",seatVendor(h.from)==="c"?"c":"g",h.from||"?");
      rt.appendChild(f); rt.appendChild(document.createTextNode("→")); rt.appendChild(el("span",seatVendor(h.to)==="c"?"c":"g",h.to||"?"));
      ch.appendChild(rt); st.appendChild(ch); });
      if(!rows.length) st.appendChild(el("div","more","empty"));
    } else { if(rows.length){ const h=rows[rows.length-1]; const ch=el("div","hchip"); ch.onclick=()=>openHandoff(h.id);
        ch.appendChild(el("span","id mono",h.id)); ch.appendChild(el("span",null,(h.slug||"").replace(/-/g," ").slice(0,20)));
        st.appendChild(ch); if(rows.length>1) st.appendChild(el("div","more","+ "+(rows.length-1)+" earlier")); }
      else st.appendChild(el("div","more","—")); }
    box.appendChild(st);
  });
}

function evIcon(ev){ const dec=ev.decision||{}, act=dec.action||"", stage=ev.stage||"";
  if(stage==="autopilot.round"&&act==="fail") return ["✖","v-crit"];
  if(stage==="autopilot.round"&&act==="reply") return ["↳",seatVendor(ev.role)==="c"?"role-c":"role-g"];
  if(stage==="autopilot.round"&&act==="start") return ["▶",seatVendor(ev.role)==="c"?"role-c":"role-g"];
  if(stage==="autopilot.lane"&&act==="verdict") return ["⚖",(dec.reason_codes||[]).some(x=>x.indexOf("CONFIRMED")>=0)?"v-crit":"v-ok"];
  if(stage==="autopilot.lane"&&act==="breaker") return ["🔨","role-g"];
  if(stage==="autopilot.lane") return ["⚖","v-violet"];
  if(stage==="autopilot.autonomous_done"||stage==="handoff.done") return ["✓✓","v-ok"];
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
  const rcls=seatVendor(role)==="c"?"role-c":"role-g";
  if(stage==="autopilot.round"&&act==="reply"){ const nid=(rc.find(x=>x.indexOf("new:")===0)||"new:?").slice(4);
    return wrap(b(role,rcls)," answered "+art+" → "+nid); }
  if(stage==="autopilot.round"&&act==="fail") return wrap(b(role,rcls)," FAILED "+art+": ", el("span","v-crit",esc((ev.failure||{}).message).slice(0,50)));
  if(stage==="autopilot.round"&&act==="start") return wrap(b(role,rcls)," thinking on "+art);
  if(stage==="autopilot.lane"&&act==="breaker"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const nf=(rc.find(x=>x.indexOf("findings:")===0)||"findings:0").slice(9); return wrap(b(role,"role-g")," probed "+ln+" — "+nf+" finding"+(nf==="1"?"":"s")); }
  if(stage==="autopilot.lane"&&act==="verdict"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const fi=(rc.find(x=>x.indexOf("finding:")===0)||"").slice(8); const vd=(rc.find(x=>x.indexOf("verdict:")===0)||"verdict:?").slice(8);
    return wrap(b(role,"role-g")," "+ln+" "+fi+" → ", el("b",vd==="CONFIRMED"?"v-crit":"v-ok",vd)); }
  if(stage==="autopilot.lane"){ const ln=(rc.find(x=>x.indexOf("lane:")===0)||"lane:?").slice(5);
    const cf=(rc.find(x=>x.indexOf("confirmed:")===0)||"confirmed:0").slice(10); const rf=(rc.find(x=>x.indexOf("refuted:")===0)||"refuted:0").slice(8);
    return wrap("lane "+ln+" done · ", el("span","v-crit",cf+" confirmed"), " / ", el("span","v-ok",rf+" refuted")); }
  if(stage==="autopilot.autonomous_done") return wrap(el("b","v-ok",art+" SIGNED OFF → done"));
  if(stage==="autopilot.signoff_blocked") return wrap(b(art)," sign-off blocked: ", el("span","v-crit",rc.join(", ").slice(0,60)));
  if(stage==="autopilot.pause") return wrap(el("b","v-violet","round budget reached")," — awaiting human"+(art?" on "+art:""));
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

// ---- max-turns (round budget) ------------------------------------------- //
function fmtDurMs(ms){ if(ms==null) return "-"; return fmtdur(ms/1000); }
function shortId(u){ return u? String(u).slice(0,10) : "—"; }
function syncTurns(s){
  const inp=$("maxturns");
  // CRITICAL focus guard (mirrors the renderSeats <select> guard): if the operator is typing in the
  // cap field, DON'T overwrite it on the 2s poll — that would eat their keystrokes. Only sync when idle.
  if(document.activeElement!==inp){
    const mx=(s.status||{}).max_rounds;
    if(mx!=null) inp.value=mx; else if(!inp.value) inp.placeholder="cap";
  }
  $("btnTurns").disabled=!s.status;
}
function setTurns(){ const v=Number($("maxturns").value);
  if(!Number.isInteger(v)||v<1||v>50){ toast("cap must be an integer 1–50","err"); return; }
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
    sub.textContent=(r.rounds_total!=null?r.rounds_total:"?")+" rounds · "+fmtDurMs(r.duration_ms);
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
async function openRun(uid){
  if(!uid) return; openRViewer("run "+shortId(uid));
  const body=$("rv-body");
  try{ const r=await fetch("/api/run?id="+encodeURIComponent(uid)); const j=await r.json();
    if(!r.ok){ body.textContent=j.error||("error "+r.status); return; }
    const sum=j.summary||{};
    $("rv-title").textContent="run "+shortId(sum.run_uid||uid)+(sum.phase_final?" · "+sum.phase_final:"");
    const fm=$("rv-fm"); fm.textContent="";
    const addfm=(k,v)=>{ if(v==null||v==="") return; const sp=el("span");
      sp.appendChild(el("b",null,k+": ")); sp.appendChild(document.createTextNode(String(v))); fm.appendChild(sp); };
    addfm("started",fmtET(sum.started_ts)); addfm("rounds",sum.rounds_total); addfm("cap",sum.max_rounds);
    addfm("duration",fmtDurMs(sum.duration_ms)); addfm("calls",sum.calls);
    const lz=sum.lanes||{}; addfm("lanes","C"+(lz.confirmed||0)+"/R"+(lz.refuted||0));
    addfm("signoff",(sum.signoff||{}).result);
    body.textContent="";
    const ah=el("div","eyebrow"); ah.textContent="activity"; ah.style.margin="4px 0 6px"; body.appendChild(ah);
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

async function refresh(){ let s;
  try{ s=await(await fetch("/api/state")).json(); connected=true; last=s; }
  catch(e){ connected=false; if(last) render(last); return; }
  render(s);
}
document.addEventListener("keydown",e=>{ const tag=(e.target.tagName||"").toLowerCase();
  if(tag==="input"||tag==="textarea"||tag==="select") return;
  if($("viewer").style.display==="flex"||$("rviewer").style.display==="flex") return;
  if(e.key==="p"){ if(!$("btnPause").disabled) ctl("pause"); }
  else if(e.key==="r"){ if(!$("btnResume").disabled) ctl("resume"); }
  else if(e.key==="s"){ doStop(); } });
syncThemeIcon();
matchMedia('(prefers-color-scheme: light)').addEventListener('change',syncThemeIcon);
refresh(); setInterval(refresh,2000);
setInterval(()=>{ if(last) render(last); },1000);
</script>
</body></html>
"""
