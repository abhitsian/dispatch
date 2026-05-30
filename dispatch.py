"""Dispatch — channel + router + session poller.

Each open Claude Code interactive session is a unit on the radio.
- The user transmits via mic; replies are routed by callsign and sent into
  that session via `claude -p --resume`.
- A background poller watches each open session for:
    * an assistant message that's asking the user something  (PENDING)
    * an assistant message that announces task completion    (COMPLETE)
  Both are surfaced as radio transmissions (alert envelope for PENDING),
  with dedupe per session + state so the same thing isn't announced twice.

All audio goes through audio.CHANNEL (a FIFO playback queue) — only one
transmission plays at a time.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agents import Agent, build_roster, get_agent, transmit_to
from allowlist import matches_allow_rules
from audio import CHANNEL
from protocol import Command, parse
from terminal_inject import inject as inject_terminal
from sessions import (
    STATE_COMPLETE,
    STATE_IDLE,
    STATE_PENDING,
    list_open_sessions,
)

from paths import LOG_DIR
LOG_FILE = LOG_DIR / "channel.log"

PERMISSION_RE = re.compile(r"\brequesting permission\b", re.IGNORECASE)

# Voice phrases that toggle elevated permissions for the addressed unit.
ELEVATE_RE = re.compile(
    r"\b(elevate(?:d)?|full authorization|auto[- ]?approve|carry on|"
    r"don'?t ask|stop asking)\b",
    re.IGNORECASE,
)
REVOKE_ELEVATE_RE = re.compile(
    r"\b(revoke|normal permissions?|gate|ask again|stop auto[- ]?approving)\b",
    re.IGNORECASE,
)

POLL_INTERVAL_SEC = 15
# Suppress poller pings when the session is actively being typed in.
ACTIVE_SUPPRESS_SEC = 60


@dataclass
class Transmission:
    ts: float
    speaker: str
    text: str

    def short(self) -> str:
        return f"[{time.strftime('%H:%M:%S', time.localtime(self.ts))}] {self.speaker}: {self.text}"


@dataclass
class PendingPermission:
    """Explicit grant/deny — an agent reply contained 'requesting permission'."""
    callsign: str
    request_text: str


@dataclass
class AwaitingAnswer:
    """Open question from a session — needs a real answer, not yes/no."""
    callsign: str
    question: str
    last_assistant_text: str   # dedupe key


@dataclass
class HookApproval:
    """A PreToolUse hook from a running session — needs allow/deny."""
    request_id: str
    callsign: str
    session_id: str
    tool_name: str
    tool_summary: str
    event: threading.Event
    decision: str = "ask"   # "allow" | "deny" | "ask" (timeout default)
    started_at: float = field(default_factory=time.time)


@dataclass
class ChannelState:
    log: list[Transmission] = field(default_factory=list)
    pending: PendingPermission | None = None
    # Open questions awaiting an answer, keyed by callsign.
    awaiting: dict[str, AwaitingAnswer] = field(default_factory=dict)
    # Pending hook approvals, keyed by request_id (multiple sessions may queue
    # at once; we serialize their announcements via the radio queue).
    hook_pending: dict[str, HookApproval] = field(default_factory=dict)
    # Completed-task announcements (kept until the user dismisses or the
    # session has new activity that reclassifies it).
    completed: dict[str, str] = field(default_factory=dict)  # callsign -> summary
    # Last assistant text we've already announced per session_id — dedupe key.
    announced: dict[str, str] = field(default_factory=dict)
    # callsigns the user has muted (suppresses announcements + outbound audio)
    muted_units: set[str] = field(default_factory=set)
    # callsigns granted elevated permissions — hook approvals auto-allow without
    # asking the user. Use sparingly.
    elevated_units: set[str] = field(default_factory=set)


class Dispatch:
    def __init__(self, on_change=None):
        self.state = ChannelState()
        # on_change is intentionally a no-op by default. The UI polls state via
        # @rumps.timer on the main thread — UI mutations from background threads
        # crash PyObjC (NSMenu/title must be touched on main).
        self.on_change = on_change or (lambda: None)
        self._lock = threading.Lock()
        self._roster: dict[str, Agent] = {}
        self._roster_ts: float = 0.0

        # Seed the in-memory log from the on-disk channel.log so the dashboard
        # and menu show recent history right after a restart (not blank).
        self._seed_log_from_disk()

        self._stop = threading.Event()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    def _seed_log_from_disk(self, max_lines: int = 80):
        """Read the last `max_lines` from channel.log and convert them back
        into Transmission entries so the UI isn't empty after restart."""
        if not LOG_FILE.exists():
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        now = time.time()
        # `[HH:MM:SS] speaker: text` — keep the original timestamp HH:MM:SS but
        # anchor today's date so the UI sorts/orders correctly.
        ts_re = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+([^:]+):\s+(.*)$")
        today0 = time.localtime(now)
        today_base = time.mktime(time.struct_time(
            (today0.tm_year, today0.tm_mon, today0.tm_mday, 0, 0, 0, 0, 0, -1)
        ))
        for raw in lines[-max_lines:]:
            m = ts_re.match(raw.rstrip("\n"))
            if not m:
                continue
            h, mi, s, speaker, text = m.groups()
            ts = today_base + int(h) * 3600 + int(mi) * 60 + int(s)
            # If the timestamp lands in the future we crossed midnight — back up
            # one day so it still sorts before "now".
            if ts > now:
                ts -= 86400
            self.state.log.append(Transmission(ts=ts, speaker=speaker.strip(),
                                                 text=text.strip()))

    # ---------- log ----------
    def _add(self, speaker: str, text: str):
        t = Transmission(ts=time.time(), speaker=speaker, text=text)
        self.state.log.append(t)
        try:
            # ALWAYS utf-8 — when the app is launched via Launch Services the
            # locale is C/ASCII by default, which crashes on emoji in agent
            # task-complete messages. Catch broadly so a log-write failure
            # never aborts the surrounding flow (e.g. grant_hook resolution).
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(t.short() + "\n")
        except Exception:
            pass
        try:
            self.on_change()
        except Exception:
            pass

    # ---------- roster ----------
    def roster(self, *, force: bool = False) -> dict[str, Agent]:
        now = time.time()
        if force or (now - self._roster_ts) > 3:
            self._roster = build_roster()
            self._roster_ts = now
        return self._roster

    def agent_by_callsign(self, callsign: str) -> Agent | None:
        return get_agent(callsign, self.roster())

    # ---------- mute ----------
    def toggle_unit_mute(self, callsign: str) -> bool:
        if callsign in self.state.muted_units:
            self.state.muted_units.discard(callsign)
            muted = False
        else:
            self.state.muted_units.add(callsign)
            muted = True
        self.on_change()
        return muted

    def is_unit_muted(self, callsign: str) -> bool:
        return callsign in self.state.muted_units

    def mute_all_units(self):
        for cs in self.roster(force=True):
            self.state.muted_units.add(cs)
        self._add("DISPATCH (system)", "muted all units")
        self.on_change()

    def unmute_all_units(self):
        self.state.muted_units.clear()
        self._add("DISPATCH (system)", "unmuted all units")
        self.on_change()

    def all_units_muted(self) -> bool:
        roster = self.roster()
        return bool(roster) and all(cs in self.state.muted_units for cs in roster)

    # ---------- elevated permissions ----------
    def toggle_unit_elevated(self, callsign: str, *, force: bool | None = None) -> bool:
        """Toggle (or set explicitly) whether this unit's hook approvals
        auto-allow. Returns the new state."""
        if force is None:
            new = callsign not in self.state.elevated_units
        else:
            new = bool(force)
        if new:
            self.state.elevated_units.add(callsign)
        else:
            self.state.elevated_units.discard(callsign)
        self._add(
            "DISPATCH (system)",
            f"{callsign} elevated permissions: {'ON' if new else 'OFF'}",
        )
        self.on_change()
        return new

    def is_unit_elevated(self, callsign: str) -> bool:
        return callsign in self.state.elevated_units

    # ---------- user transmissions ----------
    def post_user(self, raw_text: str, *, direct_callsign: str | None = None):
        """Post a user transmission. If direct_callsign is set, routing
        bypasses callsign parsing (used for the per-unit Reply-by-voice flow)."""
        speaker = f"DISPATCH (you{f' → {direct_callsign}' if direct_callsign else ''})"
        self._add(speaker, raw_text)
        threading.Thread(
            target=self._route_user,
            args=(raw_text, direct_callsign),
            daemon=True,
        ).start()

    def _route_user(self, raw_text: str, direct_callsign: str | None):
        if direct_callsign:
            agent = self.agent_by_callsign(direct_callsign)
            if not agent:
                CHANNEL.enqueue_tx(
                    f"Dispatch, {direct_callsign} no longer on channel, over.",
                    "DISPATCH",
                )
                return
            # if this answers an open question, clear it
            self.state.awaiting.pop(direct_callsign, None)
            self.on_change()
            self._transmit(agent, raw_text)
            return

        cmd = parse(raw_text)

        # Voice grant/deny resolves whichever ask is at the front of the
        # queue: hook approvals first (Claude is literally waiting), then
        # text-style PendingPermission.
        if cmd.kind == "PERMISSION":
            if self._resolve_oldest_hook(cmd.permission):
                return
            with self._lock:
                pending = self.state.pending
            if pending is not None:
                self._resolve_permission(pending, cmd.permission)
                return

        roster = self.roster(force=True)
        if not roster:
            CHANNEL.enqueue_tx(
                "Dispatch, no units on channel. Open a Claude session and try again, over.",
                "DISPATCH",
            )
            self._add("DISPATCH (system)", "no open sessions found")
            return

        if cmd.kind == "EMPTY":
            CHANNEL.enqueue_tx("Dispatch, no copy. Say again, over.", "DISPATCH")
            return

        if cmd.kind == "STATUS_CHECK":
            for agent in list(roster.values()):
                if self.is_unit_muted(agent.callsign):
                    continue
                self._transmit(agent, "Status check. Brief status, over.")
            return

        if cmd.addressee == "ALL":
            msg = cmd.body or raw_text
            for agent in list(roster.values()):
                if self.is_unit_muted(agent.callsign):
                    continue
                self._transmit(agent, msg)
            return

        if cmd.addressee:
            agent = get_agent(cmd.addressee, roster)
            if agent is None:
                CHANNEL.enqueue_tx(
                    f"Dispatch, {cmd.addressee} not on channel, say again, over.",
                    "DISPATCH",
                )
                return
            # Elevation toggle? (checked BEFORE answering an open question)
            body = cmd.body or ""
            if REVOKE_ELEVATE_RE.search(body):
                self.toggle_unit_elevated(agent.callsign, force=False)
                CHANNEL.enqueue_tx(
                    f"{agent.callsign}, elevated permissions revoked. Normal gating, over.",
                    "DISPATCH",
                )
                return
            if ELEVATE_RE.search(body):
                self.toggle_unit_elevated(agent.callsign, force=True)
                CHANNEL.enqueue_tx(
                    f"{agent.callsign}, elevated. Proceed without further checks, over.",
                    "DISPATCH",
                )
                return
            # Normal message — clears any pending open question for this unit.
            self.state.awaiting.pop(agent.callsign, None)
            self.on_change()
            self._transmit(agent, body or raw_text)
            return

        agent = next(iter(roster.values()))
        self._transmit(agent, raw_text)

    def _transmit(self, agent: Agent, message: str):
        """User-initiated transmit to a unit.

        Preferred path: type the message directly into the running session's
        terminal (iTerm/Terminal.app) so it appears in the user's chat window
        AND executes as if they typed it. The reply stays in the terminal;
        the dispatch poller will catch any awaiting/complete state on its
        next tick (~15s).

        Fallback: if the running session is in an un-driveable terminal (Warp,
        Alacritty, kitty, …) or has no tty, fall back to `claude -p --resume`,
        which writes to the session jsonl and speaks the reply over the radio.
        """
        if agent.pid:
            ok, reason = inject_terminal(agent.pid, message)
            if ok:
                self._add(agent.callsign, f"(typed into your terminal): {message}")
                # Force the poller to NOT dedupe the next assistant turn —
                # the user wants to hear about the reply if it's notable.
                self.state.announced.pop(agent.session_id, None)
                return
            # Otherwise, log why and fall through to the --resume path.
            self._add("DISPATCH (system)", f"terminal inject failed ({reason}) — falling back to --resume")

        reply = transmit_to(agent, message)
        self._add(agent.callsign, reply)
        self.state.announced[agent.session_id] = reply

        wants_permission = bool(PERMISSION_RE.search(reply))
        if wants_permission:
            with self._lock:
                self.state.pending = PendingPermission(
                    callsign=agent.callsign, request_text=reply,
                )
            self.on_change()
            CHANNEL.enqueue_alert("permission attention")
            CHANNEL.enqueue_tx(
                f"Attention dispatch — incoming permission request from "
                f"{agent.callsign}. Stand by, over.",
                "DISPATCH",
                label="attention intro",
            )

        if not self.is_unit_muted(agent.callsign):
            CHANNEL.enqueue_tx(reply, agent.callsign, voice=agent.voice,
                               label=f"{agent.callsign}: reply")

    def _resolve_permission(self, pending: PendingPermission, decision: str):
        with self._lock:
            self.state.pending = None
        self.on_change()
        # Cut any in-progress announcement first so the channel goes quiet.
        CHANNEL.interrupt()
        agent = self.agent_by_callsign(pending.callsign)
        if agent is None:
            self._add("DISPATCH (system)", f"{pending.callsign} no longer on channel")
            return
        # The session is genuinely waiting on the answer — type it in but
        # don't speak it (no need to "talk back" once the user has decided).
        msg = ("yes, proceed" if decision == "GRANTED" else "no, stand down")
        if agent.pid:
            ok, _reason = inject_terminal(agent.pid, msg)
            if ok:
                self._add(agent.callsign, f"(typed into your terminal): {msg}")
                return
        # Fallback: no tty / inject failed — fall through to the old
        # transmit-and-speak path so the agent still gets an answer.
        self._transmit(agent, msg)

    # ---------- poller (sessions ping the user) ----------
    def _poll_loop(self):
        time.sleep(2.0)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                self._add("DISPATCH (system)", f"poller error: {exc}")
            self._stop.wait(POLL_INTERVAL_SEC)

    def _poll_once(self):
        now = time.time()
        sessions = list_open_sessions()
        roster = self.roster(force=True)
        for sess in sessions:
            agent = next(
                (a for a in roster.values() if a.session_id == sess.session_id),
                None,
            )
            if agent is None:
                continue
            if self.is_unit_muted(agent.callsign):
                continue
            # skip sessions being actively typed in
            if (now - sess.last_modified) < ACTIVE_SUPPRESS_SEC:
                continue
            # dedupe — don't re-announce the same assistant text
            if self.state.announced.get(sess.session_id) == sess.last_assistant_text:
                continue

            if sess.state == STATE_PENDING:
                question = _condense_question(sess.last_assistant_text)
                self._announce_awaiting(agent, question, sess.last_assistant_text)
                self.state.announced[sess.session_id] = sess.last_assistant_text
            elif sess.state == STATE_COMPLETE:
                summary = _condense_complete(sess.last_assistant_text)
                self._announce_complete(agent, summary)
                self.state.announced[sess.session_id] = sess.last_assistant_text
            else:
                continue

    def _announce_awaiting(self, agent: Agent, question: str, full: str):
        """Open question — short alert. The full question is still in the
        menu/dashboard; voice is intentionally terse."""
        tx_long = (
            f"{agent.callsign} to DISPATCH, awaiting your call on "
            f"\"{agent.title}\". {question} Over."
        )
        self._add(agent.callsign, tx_long)  # detail goes to the channel log
        self.state.awaiting[agent.callsign] = AwaitingAnswer(
            callsign=agent.callsign, question=question, last_assistant_text=full,
        )
        self.on_change()
        CHANNEL.enqueue_alert("awaiting input")
        CHANNEL.enqueue_tx(
            f"{agent.spoken_name} needs your call, over.",
            agent.callsign, voice=agent.voice,
            label=f"{agent.callsign}: needs call",
        )

    def _announce_complete(self, agent: Agent, summary: str):
        """Task-complete — short voice; full summary stays in menu/dashboard."""
        tx_long = f"{agent.callsign} to DISPATCH, task complete. {summary} Over."
        self._add(agent.callsign, tx_long)
        self.state.completed[agent.callsign] = summary
        self.on_change()
        if not self.is_unit_muted(agent.callsign):
            CHANNEL.enqueue_tx(
                f"{agent.spoken_name} complete, over.",
                agent.callsign, voice=agent.voice,
                label=f"{agent.callsign}: complete",
            )

    # ---------- menu hooks ----------
    def grant_pending(self):
        with self._lock:
            pending = self.state.pending
        if pending:
            self._resolve_permission(pending, "GRANTED")

    def deny_pending(self):
        with self._lock:
            pending = self.state.pending
        if pending:
            self._resolve_permission(pending, "DENIED")

    def grant_unit(self, callsign: str):
        """Per-unit grant — used by the unit's submenu when pending matches."""
        with self._lock:
            pending = self.state.pending
        if pending and pending.callsign == callsign:
            self._resolve_permission(pending, "GRANTED")

    def deny_unit(self, callsign: str):
        with self._lock:
            pending = self.state.pending
        if pending and pending.callsign == callsign:
            self._resolve_permission(pending, "DENIED")

    def dismiss_awaiting(self, callsign: str):
        if self.state.awaiting.pop(callsign, None):
            self.on_change()

    def dismiss_complete(self, callsign: str):
        if self.state.completed.pop(callsign, None):
            self.on_change()

    # ---------- voice signal (dashboard -> menu-bar recorder bridge) ----------
    # The dashboard runs in a WKWebView and can't drive the mic directly. It
    # posts /api/voice_to_all or /api/voice_to/UNIT-X, which sets this flag;
    # the menu-bar tick picks it up on the main thread, starts the Recorder,
    # and routes the transcript on stop.
    def signal_voice_target(self, target: str | None):
        self._voice_signal = target

    def consume_voice_signal(self) -> str | None:
        sig = getattr(self, "_voice_signal", None)
        self._voice_signal = None
        return sig

    def broadcast_to_all(self, message: str):
        """Type a transcribed voice message into every live unit's terminal."""
        self._add("DISPATCH (you → ALL)", message)
        threading.Thread(target=self._broadcast_inject, args=(message,),
                         daemon=True).start()

    def _broadcast_inject(self, message: str):
        roster = self.roster(force=True)
        if not roster:
            CHANNEL.enqueue_tx(
                "Dispatch, no units on channel, over.", "DISPATCH",
            )
            return
        for agent in list(roster.values()):
            if self.is_unit_muted(agent.callsign):
                continue
            if not agent.pid:
                continue
            ok, reason = inject_terminal(agent.pid, message)
            if ok:
                self._add(agent.callsign, f"(typed into your terminal): {message[:80]}")
            else:
                self._add("DISPATCH (system)",
                          f"broadcast inject failed for {agent.callsign}: {reason}")

    # ---------- synthetic triggers (Test channel menu) ----------
    def test_trigger_hook(self):
        """Fire a synthetic PreToolUse approval against the oldest open unit
        that ALWAYS makes it to the UI — bypasses the permissions.allow
        short-circuit so the user can exercise the Allow/Deny buttons."""
        agent = next(iter(self.roster(force=True).values()), None)
        if agent is None:
            CHANNEL.enqueue_tx(
                "Dispatch, no units on channel to test, over.", "DISPATCH",
            )
            return
        threading.Thread(
            target=lambda: self.request_hook_approval(
                agent.session_id, "Bash",
                {"command": "synthetic dispatch hook — click Allow or Deny",
                 "description": "dispatch test"},
                skip_allow_check=True,
            ),
            daemon=True,
        ).start()

    def test_trigger_complete(self):
        agent = next(iter(self.roster(force=True).values()), None)
        if agent is None:
            return
        self._announce_complete(agent, "Synthetic test: pretend a task just finished")

    def test_trigger_awaiting(self):
        agent = next(iter(self.roster(force=True).values()), None)
        if agent is None:
            return
        self._announce_awaiting(
            agent,
            "Synthetic test: should I keep going on this branch?",
            "Synthetic last-assistant text for the test.",
        )

    # ---------- hook approval flow ----------

    HOOK_TIMEOUT_SEC = 120

    def request_hook_approval(self, session_id: str, tool_name: str,
                              tool_input: dict, *, skip_allow_check: bool = False) -> str:
        """Called by the HTTP server when a session's PreToolUse hook fires.
        Blocks until the user grants or denies — or until timeout, in which
        case we return 'ask' so Claude's own prompt takes over.

        skip_allow_check=True bypasses the permissions.allow short-circuit,
        used by the Test channel triggers so they always make audible noise."""
        roster = self.roster()
        agent = next(
            (a for a in roster.values() if a.session_id == session_id),
            None,
        )
        callsign = agent.callsign if agent else f"PID-{session_id[:8]}"
        summary = _summarize_tool_call(tool_name, tool_input)
        cwd = agent.cwd if agent else ""
        mode = agent.permission_mode if agent else "default"

        # Elevated unit → auto-allow without announcing or waiting.
        if self.is_unit_elevated(callsign):
            self._add(callsign, f"[hook] auto-allowed (elevated): {summary}")
            return "allow"

        # Session in a permissive mode → mirror Claude's own behavior.
        if mode == "bypassPermissions":
            self._add(callsign, f"[hook] auto-allowed (bypassPermissions): {summary}")
            return "allow"
        if mode == "acceptEdits" and tool_name in (
            "Edit", "MultiEdit", "Write", "NotebookEdit"
        ):
            self._add(callsign, f"[hook] auto-allowed (acceptEdits): {summary}")
            return "allow"

        # Already in Claude's permissions.allow (user-level + project-level)?
        matched_rule = matches_allow_rules(tool_name, tool_input, cwd=cwd)
        if matched_rule:
            self._add(callsign, f"[hook] auto-allowed (rule {matched_rule!r}): {summary}")
            return "allow"

        req_id = uuid.uuid4().hex
        approval = HookApproval(
            request_id=req_id, callsign=callsign, session_id=session_id,
            tool_name=tool_name, tool_summary=summary,
            event=threading.Event(),
        )
        with self._lock:
            self.state.hook_pending[req_id] = approval

        # Don't announce / play audio for muted units, but still wait for the
        # decision via menu (the menu will surface it).
        muted = self.is_unit_muted(callsign)

        self._add(callsign, f"[hook] requesting {summary}")
        if not muted:
            spoken = agent.spoken_name if agent else callsign
            # Use the UNIT's own voice — that's the whole point of per-unit
            # personalities; DISPATCH voice was making every alert sound
            # identical regardless of which unit asked.
            unit_voice = agent.voice if agent else None
            CHANNEL.enqueue_alert("hook approval")
            CHANNEL.enqueue_tx(
                f"{spoken} needs authorization, over.",
                callsign, voice=unit_voice,
                label=f"{callsign}: needs auth",
            )

        # Block waiting for user decision (with timeout)
        approval.event.wait(timeout=self.HOOK_TIMEOUT_SEC)
        decision = approval.decision

        # Cleanup
        with self._lock:
            self.state.hook_pending.pop(req_id, None)
        self._add(callsign, f"[hook] decision: {decision}")

        # User asked: when permission is granted, don't talk back. Just resolve.
        # (The hook caller — Claude Code — already gets the decision via the
        # HTTP response; speaking it adds nothing.)
        return decision

    def grant_hook(self, request_id: str):
        with self._lock:
            a = self.state.hook_pending.get(request_id)
        if a:
            a.decision = "allow"
            a.event.set()
            CHANNEL.interrupt()

    def deny_hook(self, request_id: str):
        with self._lock:
            a = self.state.hook_pending.get(request_id)
        if a:
            a.decision = "deny"
            a.event.set()
            CHANNEL.interrupt()

    def _resolve_oldest_hook(self, decision: str) -> bool:
        """Resolve the oldest pending hook approval. Returns True if one was found."""
        with self._lock:
            if not self.state.hook_pending:
                return False
            oldest = min(self.state.hook_pending.values(),
                         key=lambda a: a.started_at)
        if decision == "GRANTED":
            oldest.decision = "allow"
        else:
            oldest.decision = "deny"
        oldest.event.set()
        return True

    def grant_all_pending(self) -> int:
        """Grant every backed-up hook + text-permission ask. Returns count."""
        n = 0
        with self._lock:
            hooks = list(self.state.hook_pending.values())
            pending = self.state.pending
        for a in hooks:
            a.decision = "allow"
            a.event.set()
            n += 1
        if pending is not None:
            self._resolve_permission(pending, "GRANTED")
            n += 1
        if n > 0:
            self._add("DISPATCH (system)", f"granted {n} backed-up ask(s) in bulk")
            CHANNEL.interrupt()
        return n

    def deny_all_pending(self) -> int:
        n = 0
        with self._lock:
            hooks = list(self.state.hook_pending.values())
            pending = self.state.pending
        for a in hooks:
            a.decision = "deny"
            a.event.set()
            n += 1
        if pending is not None:
            self._resolve_permission(pending, "DENIED")
            n += 1
        if n > 0:
            self._add("DISPATCH (system)", f"denied {n} backed-up ask(s) in bulk")
            CHANNEL.interrupt()
        return n

    def hook_for_callsign(self, callsign: str) -> HookApproval | None:
        """Return the oldest pending hook for this callsign (or None)."""
        with self._lock:
            candidates = [
                a for a in self.state.hook_pending.values()
                if a.callsign == callsign
            ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.started_at)
        return candidates[0]


# ---------- helpers ----------

def _condense_question(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Awaiting your call."
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            keep.append("")
            continue
        if s.count("|") >= 2:
            continue
        if re.match(r"^[-*+]\s", s) or re.match(r"^\d+[.)]\s", s):
            continue
        if re.match(r"^[:|\-\s]+$", s):
            continue
        keep.append(s)
    text = "\n".join(keep)
    text = re.sub(r"[`*_#>|\[\]]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.?!])\s+|(?<=:)\s+(?=[A-Z])", text)
    parts = [p.strip().rstrip(":") for p in parts if len(p.strip()) >= 6]
    if not parts:
        return text[:160]
    q = next((p for p in reversed(parts) if p.endswith("?")), None)
    if q is None:
        for p in reversed(parts):
            if re.search(r"\b(want me to|should i|do you|please|confirm|let me know)\b",
                          p, re.IGNORECASE):
                q = p
                break
    if q is None:
        q = parts[-1]
    return q[:177] + "…" if len(q) > 180 else q


def _summarize_tool_call(tool_name: str, tool_input) -> str:
    """One-liner suitable for radio TX describing a PreToolUse hook payload."""
    if not isinstance(tool_input, dict):
        return f"call {tool_name}"
    if tool_name == "Bash":
        cmd = (tool_input.get("command") or "").strip().splitlines()
        head = cmd[0] if cmd else ""
        head = head if len(head) < 90 else head[:87] + "…"
        return f"run bash: {head}" if head else "run bash"
    if tool_name == "Write":
        return f"write file {tool_input.get('file_path', '?')}"
    if tool_name in ("Edit", "MultiEdit"):
        return f"edit file {tool_input.get('file_path', '?')}"
    if tool_name == "NotebookEdit":
        return f"edit notebook {tool_input.get('notebook_path', '?')}"
    if tool_name == "WebFetch":
        url = tool_input.get("url", "?")
        return f"fetch URL {url}"
    if tool_name == "Task":
        agent_kind = tool_input.get("subagent_type", "agent")
        return f"spawn {agent_kind}"
    return f"call {tool_name}"


def _condense_complete(text: str) -> str:
    """Pull a short completion summary from the assistant text."""
    text = (text or "").strip()
    if not text:
        return "Done."
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.count("|") >= 2 or re.match(r"^[-*+]\s", s) \
                or re.match(r"^\d+[.)]\s", s) or re.match(r"^[:|\-\s]+$", s):
            continue
        keep.append(s)
    text = " ".join(keep)
    text = re.sub(r"[`*_#>|\[\]]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.?!])\s+", text)
    parts = [p.strip() for p in parts if len(p.strip()) >= 6]
    if not parts:
        return text[:160]
    # prefer the sentence that contains a completion word
    from sessions import _COMPLETE_PATTERNS as CP
    for p in reversed(parts):
        if CP.search(p):
            return p[:177] + "…" if len(p) > 180 else p
    return parts[-1][:177] + ("…" if len(parts[-1]) > 180 else "")
