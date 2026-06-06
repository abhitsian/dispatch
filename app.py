"""Dispatch — Mac menu bar app.

The Roster submenu lists every live `claude` session as UNIT-N with its own
submenu (preview, reply by voice, grant/deny, full-text, mute, copy session id).
The radio queue plays one transmission at a time; icon shows 🟡 while busy and
🚨 when something needs your attention. 🔇 mutes the channel.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

# Launch Services strips PATH for bundled apps — restore it BEFORE we import
# anything that shells out (audio.py runs ffmpeg/say, sessions.py runs claude,
# terminal_inject.py runs osascript). Without this, the bundled app can't find
# /opt/homebrew/bin/whisper or ~/.local/bin/claude at all.
_DEFAULT_PATH = ":".join([
    "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    "/opt/homebrew/bin", "/opt/homebrew/sbin",
    str(Path.home() / ".local" / "bin"),
])
os.environ["PATH"] = _DEFAULT_PATH + ":" + os.environ.get("PATH", "")
# Launch Services hands us LC_ALL=C — that makes every text-mode subprocess
# raise UnicodeDecodeError the moment whisper / ffmpeg emit a progress bar.
# Force UTF-8 globally so anything we spawn reads/writes text safely.
os.environ.setdefault("LANG", "en_US.UTF-8")
os.environ.setdefault("LC_ALL", "en_US.UTF-8")

# The bundle launcher execs Homebrew's python, so macOS resolves the main bundle
# to Python.app — the Dock, ⌘-Tab, the frontmost-app menu, and System Events all
# label us "Python". Override the main bundle's info dictionary BEFORE AppKit is
# imported (rumps pulls in NSApplication below), so every surface reads "Dispatch"
# instead. Pure-runtime fix — keeps the thin-launcher / `git pull` build model.
try:
    from Foundation import NSBundle
    _bundle = NSBundle.mainBundle()
    if _bundle is not None:
        _info = _bundle.localizedInfoDictionary() or _bundle.infoDictionary()
        if _info is not None:
            _info["CFBundleName"] = "Dispatch"
            _info["CFBundleDisplayName"] = "Dispatch"
            _info["CFBundleIdentifier"] = "com.vaibhav.dispatch"
except Exception:
    pass

import rumps

from audio import CHANNEL, Recorder, transcribe
from dispatch import Dispatch
from dispatch_server import start_server as start_approval_server
import quota

from paths import RESOURCE_DIR
APP_DIR = RESOURCE_DIR  # legacy alias used throughout the file

IDLE_ICON = "🔘"
LIVE_ICON = "🔴"
BUSY_ICON = "🟡"
ALERT_ICON = "🚨"
MUTED_ICON = "🔇"
# Pulse frames for the always-on attention animation.
PULSE_ALERT = ("🚨", "🟥")
PULSE_COMPLETE = ("🟢", "💚")
COMPLETE_STEADY = "🟢"
# Recording pulse — louder voice gets a bolder dot; quiet "listening" pulses
# between two faded glyphs so you can see we're still hearing you.
PULSE_RECORDING_QUIET = ("🟡", "🟠")
RECORDING_QUIET = "🟡"
RECORDING_MED = "🟠"
RECORDING_LOUD = "🔴"
# Processing pulse while whisper is grinding.
PULSE_PROCESSING = ("🟡", "🟤", "🟠", "🟤")

ROSTER_REFRESH_SEC = 0.25  # 4 Hz — fast enough for level-meter and pulse animations


def _notify(title: str, subtitle: str, message: str) -> bool:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}" '
        f'sound name "Submarine"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        return True
    except Exception:
        return False


def _open_window(title: str, body: str):
    """Show a read-only-ish text window with the full assistant message."""
    win = rumps.Window(
        message=body if len(body) <= 4000 else body[:4000] + "\n…(truncated)",
        title=title,
        dimensions=(620, 360),
        ok="Close",
        cancel=None,
        default_text="",
    )
    win.run()


class DispatchApp(rumps.App):
    def __init__(self):
        super().__init__("Dispatch", title=IDLE_ICON, quit_button=None)
        self.recorder = Recorder()
        # NOTE: no on_change wiring — UI updates only happen on the main thread
        # via @rumps.timer (PyObjC will segfault if NSMenu/title is touched off-main).
        self.dispatch = Dispatch(on_change=None)
        self._busy_recording = False
        self._processing = False  # True while whisper is transcribing
        self._was_pending = False
        self._was_awaiting_keys: set[str] = set()
        self._reply_target: Optional[str] = None
        self._glow_phase = 0  # increments each tick — drives all pulse animations

        # top-level controls
        self.transmit_item = rumps.MenuItem(
            "▶  Start transmit", callback=self.toggle_transmit
        )
        self.talk_all_item = rumps.MenuItem(
            "🎙  Talk to ALL units (voice)", callback=self.start_voice_all
        )
        self.show_window_item = rumps.MenuItem(
            "🪟  Show dashboard window  (⌥⌘D)", callback=self.show_dashboard_window
        )
        self.status_item = rumps.MenuItem(
            "📊  Status check", callback=self.status_check
        )
        self.mute_item = rumps.MenuItem(
            "🔇  Mute channel (audio off)", callback=self.toggle_channel_mute
        )
        self.mute_all_units_item = rumps.MenuItem(
            "🔇  Mute all units", callback=self.toggle_mute_all_units
        )
        self.queue_item = rumps.MenuItem("Channel: idle")
        self.queue_item.set_callback(None)
        # Token quota pill — color-coded by usage tier, refreshed each tick.
        self.quota_item = rumps.MenuItem("Quota: …")
        self.quota_item.set_callback(self.show_quota_detail)
        self.quota_detail_item = rumps.MenuItem("  · scanning…")
        self.quota_detail_item.set_callback(None)
        self._last_quota_snap: dict = {}
        self.hook_item = rumps.MenuItem(
            "Hook: …", callback=self.toggle_hook_install
        )
        self.settings_menu = rumps.MenuItem("⚙  Settings")
        self.settings_menu.add(self.hook_item)

        self.roster_menu = rumps.MenuItem("Roster (live sessions)")
        self.log_menu = rumps.MenuItem("Channel log (last 8)")
        self.quit_item = rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application())

        # the menu is rebuilt by _rebuild_menu() whenever the pending signature
        # changes — see _tick. We do an initial build here.
        self._menu_signature: tuple | None = None
        self._rebuild_menu(force=True)
        self._refresh_log()
        self._refresh_queue_label()
        self._refresh_hook_label()

    # ---------- title (icon) ----------
    def _apply_title(self):
        # Recording — live level-meter dot. Louder voice = brighter glyph.
        if self.recorder.recording:
            lvl = self.recorder.level()
            tag = f"  {self._reply_target}" if self._reply_target else ""
            if lvl > 0.18:
                self.title = f"{RECORDING_LOUD}{tag}"
            elif lvl > 0.06:
                self.title = f"{RECORDING_MED}{tag}"
            else:
                # quiet — pulse between two faded glyphs so user sees we're listening
                self.title = f"{PULSE_RECORDING_QUIET[self._glow_phase % 2]}{tag}"
            return
        # Transcribing — show a distinct processing pulse so user knows the
        # system is doing work, not stuck.
        if self._processing:
            self.title = PULSE_PROCESSING[self._glow_phase % len(PULSE_PROCESSING)]
            return
        if self._busy_recording:
            self.title = BUSY_ICON
            return

        s = self.dispatch.state
        has_alert = bool(s.pending or s.awaiting or s.hook_pending)
        has_complete = bool(s.completed)
        muted = CHANNEL.muted

        # Pulse ALWAYS when something needs attention — visual is silent and
        # works whether audio is on or off. The pulse is what catches the eye
        # at a glance; audio is just a bonus when unmuted.
        if has_alert:
            self.title = PULSE_ALERT[self._glow_phase % 2]
            return
        if has_complete:
            self.title = PULSE_COMPLETE[self._glow_phase % 2]
            return
        if CHANNEL.is_busy() or CHANNEL.queue_depth() > 0:
            self.title = BUSY_ICON
            return
        if muted:
            self.title = MUTED_ICON
            return
        self.title = IDLE_ICON

    # ---------- timer (main thread — ALL UI updates happen here) ----------
    @rumps.timer(ROSTER_REFRESH_SEC)
    def _tick(self, _):
        self._glow_phase += 1

        # Dashboard-driven voice triggers: the WKWebView can't open the mic,
        # so it sets dispatch._voice_signal; we pick it up here on the main
        # thread (which is where the Recorder must be driven from).
        sig = self.dispatch.consume_voice_signal()
        if sig and not self._busy_recording:
            if self.recorder.recording:
                # Second press from ANY voice button = stop & transcribe, whatever
                # the current target is. The dashboard has no menu-bar toggle, so
                # without this a recording started from the dashboard can never be
                # stopped — it wedges in recording=True and every later click is
                # dropped by the guard. (Menu-bar Stop is unreachable on the
                # multi-display setup where the status item gets mis-placed.)
                self.toggle_transmit(None)
            elif sig == "ALL":
                self.start_voice_all(None)
            else:
                self._start_directed_reply(sig)
        # Rebuild the whole menu only when something user-visible changed.
        new_sig = self._compute_menu_signature()
        if new_sig != self._menu_signature:
            self._rebuild_menu()
            self._menu_signature = new_sig
        else:
            # cheap refreshes that don't restructure the menu
            self._refresh_log()
            self._refresh_queue_label()
            self._refresh_hook_label()
            self._refresh_mute_all_label()
            self._refresh_quota_label()
        self._fire_notifications_if_new()
        self._apply_title()

        # Publish voice state so the dashboard window can show recording /
        # transcribing feedback. The menu-bar icon pulse that used to signal this
        # is invisible when the status item is hidden (multi-display), so the
        # dashboard is now the primary place this state shows up.
        self.dispatch.voice_state = {
            "recording": self.recorder.recording,
            "processing": self._processing,
            "level": round(self.recorder.level(), 3),
            "target": self._reply_target,
        }

    def _compute_menu_signature(self) -> tuple:
        s = self.dispatch.state
        return (
            tuple(sorted(s.hook_pending.keys())),
            (s.pending.callsign if s.pending else None),
            tuple(sorted(s.awaiting.keys())),
            tuple(sorted(s.completed.keys())),
            tuple(self.dispatch.roster().keys()),
            tuple(sorted(s.elevated_units)),
            tuple(sorted(s.muted_units)),
            CHANNEL.muted,
        )

    def _rebuild_menu(self, *, force: bool = False):
        # Clear all existing items via rumps's public dict API. Titles include
        # short request-id tags now (see below), so per-pending-hook duplicates
        # don't collide.
        for k in list(self.menu.keys()):
            try:
                del self.menu[k]
            except Exception:
                pass

        # 1. TOP — one-click pending actions (rebuilt every change)
        wrote_top = False
        s = self.dispatch.state

        # Bulk actions: only show when 2+ permission asks are stacked up.
        permission_count = len(s.hook_pending) + (1 if s.pending else 0)
        if permission_count >= 2:
            self.menu.add(rumps.MenuItem(
                f"✅  ALLOW ALL  ({permission_count} backed up)",
                callback=lambda _i: self.dispatch.grant_all_pending(),
            ))
            self.menu.add(rumps.MenuItem(
                f"⛔  DENY ALL  ({permission_count} backed up)",
                callback=lambda _i: self.dispatch.deny_all_pending(),
            ))
            self.menu.add(None)
            wrote_top = True

        # Hook approvals (Claude is literally blocked waiting). Titles include
        # a short req_id slice so multiple pending hooks for the same unit
        # don't collide in rumps's title-keyed dict.
        roster_lookup = self.dispatch.roster()
        for req_id, hook in list(s.hook_pending.items()):
            tag = req_id[:6]
            ag = roster_lookup.get(hook.callsign)
            unit_id = ag.display_label if ag else hook.callsign
            label = f"🚨 {unit_id} wants: {hook.tool_summary[:42]} [{tag}]"
            row = rumps.MenuItem(label); row.set_callback(None)
            self.menu.add(row)
            self.menu.add(rumps.MenuItem(
                f"   ✅  Allow  ({hook.callsign})  [{tag}]",
                callback=lambda _i, rid=req_id: self.dispatch.grant_hook(rid),
            ))
            self.menu.add(rumps.MenuItem(
                f"   ⛔  Deny  ({hook.callsign})  [{tag}]",
                callback=lambda _i, rid=req_id: self.dispatch.deny_hook(rid),
            ))
            wrote_top = True

        # Text-style permission (agent reply said "requesting permission")
        if s.pending is not None:
            label = f"🚨 {s.pending.callsign} permission ask"
            row = rumps.MenuItem(label); row.set_callback(None)
            self.menu.add(row)
            cs = s.pending.callsign
            self.menu.add(rumps.MenuItem(
                f"   ✅  Grant  ({cs})",
                callback=lambda _i, c=cs: self.dispatch.grant_unit(c),
            ))
            self.menu.add(rumps.MenuItem(
                f"   ⛔  Deny  ({cs})",
                callback=lambda _i, c=cs: self.dispatch.deny_unit(c),
            ))
            wrote_top = True

        # Open questions (need a real answer)
        for cs, aa in list(s.awaiting.items()):
            label = f"🚨 {cs}: {aa.question[:48]}"
            row = rumps.MenuItem(label); row.set_callback(None)
            self.menu.add(row)
            self.menu.add(rumps.MenuItem(
                f"   🎙  Reply by voice → {cs}",
                callback=lambda _i, c=cs: self._start_directed_reply(c),
            ))
            self.menu.add(rumps.MenuItem(
                f"   ✓  Mark as read  ({cs})",
                callback=lambda _i, c=cs: self.dispatch.dismiss_awaiting(c),
            ))
            wrote_top = True

        # Task complete
        for cs, summary in list(s.completed.items()):
            label = f"🟢 {cs} done: {summary[:48]}"
            row = rumps.MenuItem(label); row.set_callback(None)
            self.menu.add(row)
            self.menu.add(rumps.MenuItem(
                f"   ✓  Acknowledge  ({cs})",
                callback=lambda _i, c=cs: self.dispatch.dismiss_complete(c),
            ))
            wrote_top = True

        if wrote_top:
            self.menu.add(None)

        # 2. STATIC CONTROLS
        # Quota pill — always visible so the user sees burn at a glance.
        self.menu.add(self.quota_item)
        self.menu.add(self.quota_detail_item)
        self.menu.add(None)
        self.menu.add(self.show_window_item)
        self.menu.add(None)
        self.menu.add(self.transmit_item)
        self.menu.add(self.talk_all_item)
        self.menu.add(None)
        self.menu.add(self.status_item)
        self.menu.add(self.mute_item)
        self.menu.add(self.mute_all_units_item)
        self.menu.add(self.queue_item)
        self.menu.add(self.settings_menu)
        self.menu.add(None)
        self.menu.add(self.roster_menu)
        self.menu.add(self.log_menu)
        self.menu.add(None)
        self.menu.add(self.quit_item)

        # The roster + log submenus need their contents (also rebuilt fresh)
        self._refresh_roster()
        self._refresh_log()
        self._refresh_queue_label()
        self._refresh_hook_label()
        self._refresh_quota_label()

    def _fire_notifications_if_new(self):
        # Permission None -> set
        pending = self.dispatch.state.pending
        if pending and not self._was_pending:
            self._was_pending = True
            _notify(
                "DISPATCH — input required",
                f"{pending.callsign} requesting permission",
                pending.request_text,
            )
        elif not pending:
            self._was_pending = False

        # New awaiting (open question)
        awaiting = self.dispatch.state.awaiting
        new_keys = set(awaiting.keys()) - self._was_awaiting_keys
        for cs in new_keys:
            aa = awaiting[cs]
            _notify(
                "DISPATCH — awaiting your call",
                f"{cs}: {aa.question[:60]}",
                aa.question,
            )
        self._was_awaiting_keys = set(awaiting.keys())

    # ---------- queue label ----------
    def _refresh_queue_label(self):
        depth = CHANNEL.queue_depth()
        cur = CHANNEL.current_label()
        if cur and depth:
            self.queue_item.title = f"Channel: ▶ {cur[:34]} (+{depth} queued)"
        elif cur:
            self.queue_item.title = f"Channel: ▶ {cur[:50]}"
        elif depth:
            self.queue_item.title = f"Channel: {depth} queued"
        else:
            self.queue_item.title = f"Channel: idle{' (muted)' if CHANNEL.muted else ''}"

    # ---------- roster (with per-unit submenus) ----------
    def _refresh_roster(self):
        roster = self.dispatch.roster(force=True)
        try:
            self.roster_menu.clear()
        except AttributeError:
            pass
        if not roster:
            self.roster_menu.add(rumps.MenuItem("(no live claude sessions)"))
            return

        n_pending = sum(1 for cs in roster
                        if self.dispatch.state.pending
                        and self.dispatch.state.pending.callsign == cs)
        n_awaiting = sum(1 for cs in roster if cs in self.dispatch.state.awaiting)
        header = rumps.MenuItem(
            f"  {len(roster)} units · {n_awaiting} awaiting · {n_pending} permission ask"
        )
        header.set_callback(None)
        self.roster_menu.add(header)

        for callsign, agent in roster.items():
            self.roster_menu.add(self._build_unit_submenu(callsign, agent))

    def _build_unit_submenu(self, callsign: str, agent) -> rumps.MenuItem:
        # Determine state flags
        is_pending = bool(
            self.dispatch.state.pending
            and self.dispatch.state.pending.callsign == callsign
        )
        is_awaiting = callsign in self.dispatch.state.awaiting
        is_muted = self.dispatch.is_unit_muted(callsign)
        hook = self.dispatch.hook_for_callsign(callsign)  # may be None
        has_hook = hook is not None

        is_elev = self.dispatch.is_unit_elevated(callsign)
        flag = ""
        if is_pending or is_awaiting or has_hook:
            flag = "🚨 "
        elif is_elev:
            flag = "🔓 "  # elevated — auto-allow
        elif is_muted:
            flag = "🔇 "

        # Show the auto-derived short name prominently: "UNIT-3 · Add Memory"
        # with the full title (Claude's ai-title) and project as secondary.
        label = (
            f"{flag}{agent.display_label}  ·  {agent.project_label}  ·  {agent.title}"
        )
        submenu = rumps.MenuItem(label)

        # Preview line (hook > pending > awaiting > recent traffic)
        preview = ""
        if has_hook:
            preview = f"[hook] {hook.tool_summary}"
        elif is_pending:
            preview = self.dispatch.state.pending.request_text
        elif is_awaiting:
            preview = self.dispatch.state.awaiting[callsign].question
        else:
            preview = (agent.last_assistant_text or "").strip().splitlines()[0:1]
            preview = preview[0] if preview else "(no recent traffic)"
        preview_item = rumps.MenuItem(f"❝ {preview[:80]} ❞")
        preview_item.set_callback(None)
        submenu.add(preview_item)
        submenu.add(None)

        # Hook approval (highest priority — Claude is literally waiting)
        if has_hook:
            submenu.add(rumps.MenuItem(
                "✅  Allow this tool call",
                callback=lambda _i, rid=hook.request_id: self.dispatch.grant_hook(rid),
            ))
            submenu.add(rumps.MenuItem(
                "⛔️  Deny this tool call",
                callback=lambda _i, rid=hook.request_id: self.dispatch.deny_hook(rid),
            ))

        # Reply by voice (always available)
        submenu.add(rumps.MenuItem(
            "🎙  Reply by voice",
            callback=lambda _i, cs=callsign: self._start_directed_reply(cs),
        ))

        # Grant / Deny — only when permission was explicitly requested via text
        if is_pending:
            submenu.add(rumps.MenuItem(
                "✅  Grant",
                callback=lambda _i, cs=callsign: self.dispatch.grant_unit(cs),
            ))
            submenu.add(rumps.MenuItem(
                "⛔️  Deny",
                callback=lambda _i, cs=callsign: self.dispatch.deny_unit(cs),
            ))
        # Open-question dismiss
        if is_awaiting and not is_pending:
            submenu.add(rumps.MenuItem(
                "✓  Mark as read",
                callback=lambda _i, cs=callsign: self.dispatch.dismiss_awaiting(cs),
            ))

        # Show full message
        full_text = (
            (self.dispatch.state.pending.request_text if is_pending else "")
            or (self.dispatch.state.awaiting[callsign].last_assistant_text
                if is_awaiting else "")
            or agent.last_assistant_text
            or "(no message)"
        )
        show_title = f"{callsign}  ·  {agent.title}"
        submenu.add(rumps.MenuItem(
            "👁  Show full message…",
            callback=lambda _i, body=full_text, t=show_title: _open_window(t, body),
        ))

        # Mute toggle
        mute_label = "🔊  Unmute this unit" if is_muted else "🔇  Mute this unit"
        submenu.add(rumps.MenuItem(
            mute_label,
            callback=lambda _i, cs=callsign: self.dispatch.toggle_unit_mute(cs),
        ))

        # Elevated permissions toggle (is_elev computed at top of function)
        elev_label = (
            "🔒  Revoke elevation (gate tool calls)" if is_elev
            else "🔓  Elevate (auto-allow tool calls)"
        )
        submenu.add(rumps.MenuItem(
            elev_label,
            callback=lambda _i, cs=callsign: self.dispatch.toggle_unit_elevated(cs),
        ))

        # Copy session id (handy for piping into other tools)
        sid = agent.session_id
        submenu.add(rumps.MenuItem(
            f"📋  Copy session id ({sid[:8]}…)",
            callback=lambda _i, s=sid: self._copy_clipboard(s),
        ))
        return submenu

    def _copy_clipboard(self, text: str):
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode())
        except Exception:
            pass

    # ---------- channel log ----------
    def _refresh_log(self):
        try:
            self.log_menu.clear()
        except AttributeError:
            pass
        recent = self.dispatch.state.log[-8:]
        if not recent:
            self.log_menu.add(rumps.MenuItem("(channel quiet)"))
            return
        for t in recent:
            label = f"[{time.strftime('%H:%M', time.localtime(t.ts))}] {t.speaker}: {t.text[:70]}"
            self.log_menu.add(rumps.MenuItem(label))

    # ---------- transmit (free-form, callsign-parsed) ----------
    def toggle_transmit(self, _):
        if self._busy_recording:
            return
        if not self.recorder.recording:
            self._reply_target = None
            self.recorder.start()
            self.transmit_item.title = "■  Stop transmit"
            self._apply_title()
            return
        self.transmit_item.title = "▶  Start transmit"
        self._busy_recording = True
        self._apply_title()
        threading.Thread(target=self._finish_recording, args=(None,), daemon=True).start()

    # ---------- transmit (directed to a specific unit OR ALL) ----------
    def _start_directed_reply(self, callsign: str):
        if self._busy_recording or self.recorder.recording:
            return
        self._reply_target = callsign
        self.recorder.start()
        self.transmit_item.title = f"■  Stop reply → {callsign}"
        self._apply_title()

    def start_voice_all(self, _):
        """Toggle voice → broadcast: first click starts recording; second click
        stops, transcribes, injects into every live unit's terminal."""
        if self._busy_recording:
            return
        # Click while already recording = stop (route through the normal
        # toggle_transmit path so the title/state stays in sync).
        if self.recorder.recording:
            self.toggle_transmit(None)
            return
        self._reply_target = "ALL"
        self.recorder.start()
        self.transmit_item.title = "■  Stop transmit (→ ALL)"
        self.talk_all_item.title = "■  Stop voice → ALL"
        self._apply_title()

    def _finish_recording(self, _unused):
        try:
            wav = self.recorder.stop()
            target = self._reply_target
            self._reply_target = None
            if wav is None:
                return
            # Mark processing so the icon pulses 🟡 while whisper runs.
            self._processing = True
            text = transcribe(wav)
            self._processing = False
            if not text:
                _notify("Dispatch", "No speech detected", "Say again, over.")
                return
            _notify(
                "Dispatch  • heard you",
                f"→ {target or 'auto-routed'}",
                text[:200],
            )
            if target == "ALL":
                self.dispatch.broadcast_to_all(text)
            else:
                self.dispatch.post_user(text, direct_callsign=target)
        finally:
            self._processing = False
            time.sleep(0.3)
            self._busy_recording = False
            self.transmit_item.title = "▶  Start transmit"
            self.talk_all_item.title = "🎙  Talk to ALL units (voice)"
            self._apply_title()

    def status_check(self, _):
        self.dispatch.post_user("All units, status check, over.")

    def show_dashboard_window(self, _):
        try:
            from native_window import open_window
            open_window("http://127.0.0.1:8765/ui")
        except Exception:
            subprocess.Popen(["open", "http://127.0.0.1:8765/ui"])

    def toggle_channel_mute(self, _):
        new = not CHANNEL.muted
        CHANNEL.set_muted(new)
        self.mute_item.title = (
            "🔊  Unmute channel" if new else "🔇  Mute channel (audio off)"
        )
        self._apply_title()

    def toggle_mute_all_units(self, _):
        if self.dispatch.all_units_muted():
            self.dispatch.unmute_all_units()
        else:
            self.dispatch.mute_all_units()
        # the timer tick will refresh the label via _refresh_mute_all_label()
        self._refresh_mute_all_label()

    def _refresh_mute_all_label(self):
        all_muted = self.dispatch.all_units_muted()
        self.mute_all_units_item.title = (
            "🔊  Unmute all units" if all_muted else "🔇  Mute all units"
        )

    def _refresh_quota_label(self):
        # M1 feature flag — when off, hide pill and skip all scanning.
        if not quota.feature_enabled("m1_quota_meter"):
            self.quota_item.title = "Quota: disabled"
            self.quota_detail_item.title = "  (m1_quota_meter = false in config)"
            return
        try:
            snap = quota.snapshot()
        except Exception as exc:
            self.quota_item.title = f"Quota: error ({exc})"
            return
        self._last_quota_snap = snap
        self.quota_item.title = quota.format_pill(snap)
        self.quota_detail_item.title = "  " + quota.format_detail(snap)
        # M1 is passive — just refresh the pill/detail. No tier transitions,
        # no voice, no banner, no enforcement (M2 removed).

    def show_quota_detail(self, _):
        snap = self._last_quota_snap or quota.snapshot()
        cfg = snap.get("config", {})
        per_session = snap.get("per_session") or {}
        top = sorted(per_session.items(), key=lambda x: -x[1])[:8]
        lines = [
            f"Plan tier: {cfg.get('tier', 'unknown')}",
            "",
            f"5h window:  {snap['tokens_5h']:>14,}  /  {snap['ceiling_5h']:>12,}  ({snap['pct_5h']*100:.1f}%)",
            f"Week:       {snap['tokens_7d']:>14,}  /  {snap['ceiling_7d']:>12,}  ({snap['pct_7d']*100:.1f}%)",
            "",
            f"Binding: {snap['pct']*100:.1f}% of estimated ceiling",
            f"5h resets in: {snap['reset_5h_in_sec']//60} min",
            "",
            "Top sessions in current 5h window:",
        ]
        for sid, tok in top:
            lines.append(f"  {sid[:18]}…   {tok:>10,} cost-equiv tokens")
        if not top:
            lines.append("  (no recent traffic)")
        lines += [
            "",
            "Cost-equiv = input + output×5 + cache_create×1.25 + cache_read×0.1",
            "Edit ~/Library/Application Support/Dispatch/quota-config.json to tune.",
        ]
        _open_window("Dispatch — token quota", "\n".join(lines))

    # ---------- Claude Code hook install/uninstall ----------
    def _hook_installed(self) -> bool:
        try:
            settings = Path.home() / ".claude" / "settings.local.json"
            if not settings.exists():
                return False
            data = __import__("json").loads(settings.read_text())
        except Exception:
            return False
        marker = str(APP_DIR / "hooks" / "pretooluse.sh")
        for entry in data.get("hooks", {}).get("PreToolUse", []) or []:
            for h in entry.get("hooks", []) or []:
                if marker in (h.get("command") or ""):
                    return True
        return False

    def toggle_hook_install(self, _):
        py = str(APP_DIR / ".venv" / "bin" / "python")
        if not Path(py).exists():
            py = "python3"
        script = str(APP_DIR / "install_hook.py")
        cmd = "uninstall" if self._hook_installed() else "install"
        try:
            subprocess.run([py, script, cmd], check=False, timeout=10)
        except Exception:
            pass
        # the @rumps.timer will update the label on the next tick

    def _refresh_hook_label(self):
        installed = self._hook_installed()
        if installed:
            self.hook_item.title = "🪝  Hook: installed  (click to uninstall)"
        else:
            self.hook_item.title = "🪝  Hook: not installed  (click to install)"


def _another_instance_alive(port: int = 8765) -> bool:
    """Return True if another dispatch process is already serving /health.
    Prevents two bundled .apps racing for the port (which leaves the loser
    silently broken — UI clicks go to a process whose HTTP server died)."""
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=0.5
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _install_dock_reopen_handler():
    """Make clicking the Dock icon raise the dashboard window.

    rumps' NSApp delegate ships no reopen handler, so we inject one onto its
    class before run(): AppKit instantiates that class as the delegate
    (rumps.py: NSApp.alloc().init()), and the new method then fires on every
    Dock-icon click / app reopen.

    We deliberately do NOT pop the menu-bar dropdown here: on a notched Mac
    the status item is hidden behind the notch, so macOS has no slot to anchor
    its menu to and dumps it at the screen origin (top-left). The dashboard
    window IS the menu instead — it carries every action, plus the gear popover
    for settings — so raising the window is the reliable move. (Quit lives on
    the standard right-click Dock menu.)

    Best-effort: any failure here just means the Dock click does nothing extra.
    """
    try:
        import objc
        from rumps.rumps import NSApp as _RumpsAppDelegate

        def _reopen(self, ns_app, has_visible_windows):
            try:
                from native_window import open_window
                open_window("http://127.0.0.1:8765/ui")
            except Exception:
                pass
            return True

        objc.classAddMethods(_RumpsAppDelegate, [
            objc.selector(
                _reopen,
                selector=b'applicationShouldHandleReopen:hasVisibleWindows:',
                signature=b'c@:@c',
            ),
        ])
    except Exception:
        pass


def main():
    if _another_instance_alive():
        subprocess.run([
            "osascript", "-e",
            'display notification "Dispatch is already running — look for its '
            'icon or the dashboard window." with title "Dispatch"',
        ], check=False)
        return

    _install_dock_reopen_handler()
    app = DispatchApp()
    start_approval_server(app.dispatch, port=8765)

    # Open the native dashboard window once the HTTP server + NSApp event loop
    # are ready. We schedule it via PyObjCTools.AppHelper.callLater so it runs
    # on the main thread after rumps boots NSApplication.
    def _open_native_window():
        try:
            from native_window import open_window
            open_window("http://127.0.0.1:8765/ui")
        except Exception as exc:
            # Fall back to default browser if WKWebView import fails for any reason
            subprocess.Popen(["open", "http://127.0.0.1:8765/ui"])

    try:
        from PyObjCTools.AppHelper import callLater
        callLater(1.5, _open_native_window)
    except Exception:
        threading.Thread(target=lambda: (time.sleep(1.5), _open_native_window()),
                          daemon=True).start()

    # Global hotkey ⌥⌘D → toggle the dashboard window from anywhere. This is the
    # reliable way in: it works on any display and sidesteps the multi-display
    # status-item placement quirk that can hide the menu-bar icon entirely.
    try:
        import hotkey
        def _toggle_dashboard():
            try:
                from native_window import toggle_window
                toggle_window("http://127.0.0.1:8765/ui")
            except Exception:
                subprocess.Popen(["open", "http://127.0.0.1:8765/ui"])
        if hotkey.register(_toggle_dashboard):
            print("hotkey ⌥⌘D registered → toggle dashboard")
        else:
            print("hotkey ⌥⌘D registration rejected (combo may be taken)")
    except Exception as exc:
        print(f"hotkey unavailable: {exc}")

    CHANNEL.enqueue_tx(
        "Dispatch online. Scanning open units. Channel open, over.", "DISPATCH",
        label="boot hail",
    )
    app.run()


if __name__ == "__main__":
    main()
