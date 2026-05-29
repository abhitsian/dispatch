"""HTTP server hosting:

  POST /approve            — Claude Code PreToolUse hook entry point (blocks
                             until the user grants or denies via radio/menu/web).
  GET  /health             — liveness check (also used by the .app launcher's
                             single-instance guard).
  GET  /ui                 — serves dashboard.html (the visible window).
  GET  /state              — JSON snapshot the dashboard polls every ~1s.
  POST /api/<action>[/arg] — dashboard-driven actions (grant_hook, deny_unit,
                             toggle_elevated, transmit, test_*, etc).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paths import RESOURCE_DIR
DEFAULT_PORT = 8765
DASHBOARD_PATH = RESOURCE_DIR / "dashboard.html"

_DISPATCH = None
_CHANNEL = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # silence

    # ---------------- GET ----------------
    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/ui" or self.path == "/":
            self._send_file(DASHBOARD_PATH, "text/html; charset=utf-8")
            return
        if self.path == "/state":
            self._send_json(200, _build_state_snapshot())
            return
        self._send_json(404, {"error": "not found"})

    # ---------------- POST ----------------
    def do_POST(self):
        path = self.path
        if path == "/approve":
            self._handle_approve()
            return
        if path.startswith("/api/"):
            self._handle_api(path[len("/api/"):])
            return
        self._send_json(404, {"error": "not found"})

    # ---- approve (the hook entry) ----
    def _handle_approve(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads((self.rfile.read(length) or b"{}").decode())
        except Exception as exc:
            self._send_json(400, {"error": f"bad json: {exc}"})
            return
        if _DISPATCH is None:
            self._send_json(503, {"error": "dispatch not initialised"})
            return
        try:
            decision = _DISPATCH.request_hook_approval(
                session_id=payload.get("session_id", ""),
                tool_name=payload.get("tool_name", ""),
                tool_input=payload.get("tool_input", {}),
            )
        except Exception as exc:
            self._send_json(500, {"error": f"dispatch failed: {exc}"})
            return
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": "via dispatch",
            }
        }
        self._send_json(200, out)

    # ---- /api/* (dashboard-driven actions) ----
    def _handle_api(self, rest: str):
        if _DISPATCH is None:
            self._send_json(503, {"error": "dispatch not initialised"})
            return
        # split off the optional /<arg>
        if "/" in rest:
            action, arg = rest.split("/", 1)
        else:
            action, arg = rest, ""

        # body (for /api/transmit)
        body = {}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                body = json.loads((self.rfile.read(length) or b"{}").decode())
        except Exception:
            body = {}

        result = _dispatch_action(action, arg, body)
        if result is None:
            self._send_json(404, {"error": f"unknown action {action}"})
            return
        self._send_json(200, result)

    # ---- helpers ----
    def _send_json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _send_file(self, path: Path, content_type: str):
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(500, {"error": f"could not read {path}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------- state snapshot for /state ----------------

def _build_state_snapshot() -> dict:
    if _DISPATCH is None:
        return {"roster": [], "log": [], "channel": {}, "alert_count": 0,
                "complete_count": 0, "permission_count": 0}
    s = _DISPATCH.state
    roster = _DISPATCH.roster(force=False)

    units = []
    for cs, agent in roster.items():
        hook = _DISPATCH.hook_for_callsign(cs)
        unit = {
            "callsign": cs,
            "project_label": agent.project_label,
            "title": agent.title,
            "session_id": agent.session_id,
            "pid": agent.pid,
            "elevated": _DISPATCH.is_unit_elevated(cs),
            "muted": _DISPATCH.is_unit_muted(cs),
            "hook_pending": None,
            "pending": None,
            "awaiting": None,
            "completed": s.completed.get(cs),
            "last_assistant_text": (agent.last_assistant_text or "")[:400],
        }
        if hook:
            unit["hook_pending"] = {
                "id": hook.request_id,
                "tool_name": hook.tool_name,
                "tool_summary": hook.tool_summary,
            }
        if s.pending and s.pending.callsign == cs:
            unit["pending"] = {"text": s.pending.request_text}
        if cs in s.awaiting:
            aa = s.awaiting[cs]
            unit["awaiting"] = {"question": aa.question}
        units.append(unit)

    alert_count = (
        len(s.hook_pending) + (1 if s.pending else 0) + len(s.awaiting)
    )
    permission_count = len(s.hook_pending) + (1 if s.pending else 0)

    channel = {
        "busy": _CHANNEL.is_busy() if _CHANNEL else False,
        "queue": _CHANNEL.queue_depth() if _CHANNEL else 0,
        "label": _CHANNEL.current_label() if _CHANNEL else "",
        "muted": _CHANNEL.muted if _CHANNEL else False,
    }

    log = [
        {"ts": t.ts, "speaker": t.speaker, "text": t.text}
        for t in s.log[-200:]
    ]

    return {
        "roster": units,
        "log": log,
        "channel": channel,
        "alert_count": alert_count,
        "complete_count": len(s.completed),
        "permission_count": permission_count,
    }


# ---------------- action router for /api/* ----------------

def _dispatch_action(action: str, arg: str, body: dict):
    d = _DISPATCH
    if action == "grant_hook":
        d.grant_hook(arg); return {"ok": True}
    if action == "deny_hook":
        d.deny_hook(arg); return {"ok": True}
    if action == "grant_unit":
        d.grant_unit(arg); return {"ok": True}
    if action == "deny_unit":
        d.deny_unit(arg); return {"ok": True}
    if action == "grant_all":
        return {"ok": True, "granted": d.grant_all_pending()}
    if action == "deny_all":
        return {"ok": True, "denied": d.deny_all_pending()}
    if action == "dismiss_awaiting":
        d.dismiss_awaiting(arg); return {"ok": True}
    if action == "dismiss_complete":
        d.dismiss_complete(arg); return {"ok": True}
    if action == "toggle_elevated":
        return {"ok": True, "elevated": d.toggle_unit_elevated(arg)}
    if action == "toggle_mute_unit":
        return {"ok": True, "muted": d.toggle_unit_mute(arg)}
    if action == "toggle_mute_all_units":
        if d.all_units_muted():
            d.unmute_all_units()
            return {"ok": True, "all_muted": False}
        d.mute_all_units()
        return {"ok": True, "all_muted": True}
    if action == "toggle_channel_mute":
        if _CHANNEL is None:
            return {"ok": False, "error": "no channel"}
        new = not _CHANNEL.muted
        _CHANNEL.set_muted(new)
        return {"ok": True, "muted": new}
    if action == "status_check":
        d.post_user("All units, status check, over."); return {"ok": True}
    if action == "transmit":
        cs = (body or {}).get("callsign", "")
        msg = (body or {}).get("message", "")
        if not msg:
            return {"ok": False, "error": "empty message"}
        d.post_user(msg, direct_callsign=cs or None)
        return {"ok": True}
    if action == "test_hook":
        d.test_trigger_hook(); return {"ok": True}
    if action == "test_complete":
        d.test_trigger_complete(); return {"ok": True}
    if action == "test_awaiting":
        d.test_trigger_awaiting(); return {"ok": True}
    # Voice signals — dashboard asks the menu-bar app to start the mic.
    if action == "voice_to_all":
        d.signal_voice_target("ALL"); return {"ok": True}
    if action == "voice_to":
        d.signal_voice_target(arg or None); return {"ok": True, "target": arg}
    return None


def start_server(dispatch, port: int = DEFAULT_PORT, channel=None):
    """Start the approval + dashboard server. Returns the server object."""
    global _DISPATCH, _CHANNEL
    _DISPATCH = dispatch
    if channel is None:
        from audio import CHANNEL as default_channel
        _CHANNEL = default_channel
    else:
        _CHANNEL = channel
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
