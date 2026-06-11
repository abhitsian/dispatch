# Dispatch

A menu-bar app that turns every live Claude Code session into a unit on a
shared radio channel. Talk to them by voice, gate their tool calls with one
click, and see what's pending across all your terminals from a single icon.

It also keeps you honest about token spend in both directions: a passive meter
for what each session is burning, an advisory recommender for when a task could
run on a cheaper model, a live nudge for the rare prompt worth escalating to a
stronger one, and a measured read on what any of it actually saved.

![Dispatch icon](Dispatch.png)

## Why it exists

As Claude Code and Codex become the main surface for deep knowledge work,
more people run not one agent but several at once, often for hours. The moment
that happens the bottleneck stops being the model and becomes you: you alt-tab
to find which session needs you, and you have no idea what you've spent until
you hit the wall. Native remote control doesn't fix this. It just lets you
carry the context-switching around with you. The problem was never *where* you
reach the sessions from, it's having to hold all of them in your head at once.

Dispatch collapses them into one surface so you can stop.

## Design principles

- **One place for all of them.** Every session on a single surface, not one terminal per agent.
- **Quiet until it matters.** Status lives in your menu bar and moves to the center only when something needs you. You glance, you don't poll.
- **Batch the interruptions.** Permissions queue into one menu with Allow All, instead of each session interrupting you on its own.
- **Labelled, not remembered.** Each session is a numbered unit tagged with what it's working on, so you know what's asking without digging for the right window.
- **Inform, don't alarm.** The meter shows the number and stays quiet. No false urgency, no gating (an earlier tier-alert layer was removed for firing off a guess).
- **Lower the cost of input.** Voice is first-class, so instructing a session doesn't drag you back to the keyboard.

## What it does

- **Discovers your live `claude` sessions** by reading
  `~/.claude/sessions/<pid>.json`. Every running terminal becomes a numbered
  unit (UNIT-1, UNIT-2, ...). Close a terminal → that unit drops off the
  roster within a second.
- **Centralises tool-call approvals.** Installs a PreToolUse hook so every
  `Bash` / `Write` / `Edit` / `WebFetch` from any of your sessions surfaces
  in the dispatch menu and dashboard. One-click Allow / Deny, or voice
  ("permission granted, over"). When 2+ are stacked up, you get a single
  `✅ ALLOW ALL (N)` row at the top of the menu.
- **Voice → typed into the right terminal.** Press `🎙 Talk to ALL`, say
  *"all units, status check, over"*, and the transcribed text is injected
  (typed + Enter) into every live session's iTerm/Terminal tab via
  AppleScript. Per-unit voice for directed messages too.
- **Mirrors Claude's own permission rules** so it doesn't over-prompt.
  Reads `~/.claude/settings(.local).json` and
  `<cwd>/.claude/settings(.local).json`; honours each session's
  `permissionMode` (acceptEdits / bypassPermissions / plan).
- **Web dashboard window** (native `WKWebView`, served by the in-process
  HTTP server on `127.0.0.1:8765`) for visual review — pending asks,
  channel log, per-unit cards with grant / deny / reply / elevate / mute.

## Token-cost awareness

Running several Opus sessions in parallel, you're blind to spend until you
hit a wall. Dispatch surfaces it — without crying wolf:

- **Passive token meter.** Reads usage straight from Claude Code's own
  `~/.claude/projects/*.jsonl` (deduped by message id — those logs repeat each
  message across lines, which inflates naive counts ~2.4x), and shows a
  glanceable 5h + 7d burn gauge. **No alarms, no gating** — an earlier
  tier-alert/enforcement layer was removed because it acted on a guessed
  number. The ceiling is an **estimate** (Anthropic publishes limits in hours,
  not tokens) and is labeled as such; it self-corrects if you actually hit a
  wall.
- **Offload recommender** (`/offload-check`). Scores a task on the three axes
  that decide whether handing it to a cheaper-model sub-agent actually saves —
  reasoning level, volume, and whether it's self-contained (a cheap model on
  cold context can cost *more* than Opus on cached context). Surfaced inline as
  you type, or on demand. **It recommends; you decide** — it never auto-reroutes
  your work.
- **Escalation nudge** (M4). The inverse of offload. Claude Fable 5 sits a tier
  above Opus, so for the rare prompt genuinely worth a higher ceiling —
  irreversible calls, whole-system architecture, taste-critical output — Dispatch
  flags it live and points you at `/model claude-fable-5`. An LLM judge makes the
  call per-prompt, gated to heavyweight prompts and rate-limited so it never taxes
  routine typing. **Suggest-only — it never switches your model; that stays your
  call.**
- **Measured savings** (`/savings`). Reads the actual per-message model from
  the logs and reports real dollars saved by cheaper-model work vs an all-Opus
  baseline — measured, not assumed, so you know whether any of it is working.

> Honest note: I measured both directions before trusting either. For continuous,
> judgment-heavy work — the bulk of real usage — almost everything wants the model
> you're already on: the offload recommender mostly says *keep*, and even an
> intent-aware judge flagged ~0% of real prompts as worth escalating. The payoff
> is at the edges — **bulk, self-contained generation** downward (cheap sub-agents
> win) and the **rare pivotal prompt** upward. Both stay recommendations you act
> on, never automatic reroutes.

## Install

Requires macOS 13+ on Apple Silicon (Intel may work; not tested),
Python 3.13, and these CLI tools on PATH:

```bash
brew install ffmpeg
pipx install openai-whisper       # transcription backend
# claude CLI: https://claude.com/code
```

Then build the .app:

```bash
git clone https://github.com/abhitsian/dispatch.git
cd dispatch
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
./make_app.sh
open /Applications/Dispatch.app
```

`make_app.sh` builds a thin `/Applications/Dispatch.app` that runs *this clone*
directly — so updating is just `git pull`, no rebuild. Re-run it only if the
launcher or icon changes.

The first launch will prompt for **Microphone** and **Automation** permissions
(System Settings → Privacy & Security). Grant both — the mic for voice transmit,
Automation → Terminal/iTerm for typing into your terminals.

To enable cross-session tool-call gating, open the dispatch dashboard window
or menu and click **Settings → 🪝 Hook: not installed**. That writes a
`PreToolUse` entry to `~/.claude/settings.local.json` matching
`Bash|Write|Edit|WebFetch`. Start a new Claude Code session for it to take
effect; flip it off the same way any time.

## Use

Click the menu bar icon (or open `http://127.0.0.1:8765/ui` for the
dashboard window).

**Visual states at a glance:**

| Icon | Meaning |
|---|---|
| 🔘 | idle |
| 🔴 / 🟠 / 🟡 | recording — bigger dot = louder voice |
| 🟡 ↔ 🟤 ↔ 🟠 | transcribing |
| 🚨 ↔ 🟥 (pulsing) | something needs your input |
| 🟢 ↔ 💚 (pulsing) | a unit reported a task complete |
| 🔇 | channel audio muted |

**Voice protocol** (top-level transmit + radio-style):

| Say | Effect |
|---|---|
| `Dispatch to unit one, <message>, over.` | Inject `<message>` into UNIT-1's terminal |
| `Unit-3, <message>, over.` | Same, shorter |
| `All units, <message>, over.` | Broadcast |
| `Status check, over.` | Roll-call — every unit reports |
| `Permission granted, over.` (also: roger, copy, ten-four) | Grants the oldest pending ask |
| `Negative, over.` (also: denied, stand down) | Denies it |
| `Dispatch to unit one, elevate, over.` | Auto-allow all future tool calls from UNIT-1 (revoke with "revoke elevation") |

## How it works

```
        ┌────────────────────┐                   ┌─────────────────────┐
        │   Menu bar icon    │                   │   Dashboard window  │
        │ + macOS notifs     │                   │   (WKWebView)       │
        └────────┬───────────┘                   └──────────┬──────────┘
                 │                                          │
                 ▼                                          ▼
              ┌──────────────────────────────────────────────────┐
              │            Dispatch (Python)                     │
              │  state, router, session poller, hook approval    │
              └─────┬─────────────┬──────────────────────┬───────┘
                    │             │                      │
                    ▼             ▼                      ▼
            claude --resume   ~/.claude/         127.0.0.1:8765  ◀── curl ──┐
            (writes session    sessions/         (in-proc HTTP server)      │
             jsonl, returns    <pid>.json                                   │
             reply)            + projects/                                  │
                                 *.jsonl                              ┌─────┴──────┐
                                                                      │ Claude hook│
                                                                      │ pretool.sh │
                                                                      └────────────┘
                                                                            ▲
                                                                            │
                                                                Claude Code PreToolUse
```

| Layer | Job |
|---|---|
| `sessions.py` | Discovers live sessions via `~/.claude/sessions/<pid>.json`; reads jsonl for title, last assistant message, permission mode |
| `agents.py` | Stable callsign assignment, voice rotation, `claude --resume` bridge |
| `dispatch.py` | Channel state, FIFO radio queue, hook approval coordination, poller |
| `dispatch_server.py` | `127.0.0.1:8765` — `POST /approve` (hook), `GET /state` (dashboard), `POST /api/*` (actions) |
| `audio.py` | Mic record, ffmpeg pre-process, whisper (mlx if available) transcribe, `say`-based radio TTS, single playback queue |
| `terminal_inject.py` | AppleScript injection into iTerm/Terminal — finds the tab matching the session's tty |
| `allowlist.py` | Mirrors Claude's `permissions.allow` resolution so dispatch doesn't over-prompt |
| `hooks/pretooluse.sh` | Tiny curl wrapper. If dispatch is down, exits 0 → Claude shows its own prompt (fail-open) |
| `install_hook.py` | Adds/removes the `PreToolUse` + `UserPromptSubmit` entries in `~/.claude/settings.local.json` |
| `native_window.py` | NSWindow + WKWebView host for the dashboard |
| `app.py` | rumps menu bar, mic recorder, main-thread UI refresh, notifications, Dock-reopen → window |
| `quota.py` | Passive token meter — parses `projects/*.jsonl` usage (deduped by message id), cost-weighted 5h/7d windows vs estimated ceilings |
| `classifier.py` / `routing.py` | Heuristic task classifier + routing policy (downgrade-only, confidence floor, cooldown) for sub-agent model selection; also the two-stage escalation signal (regex → judge) |
| `offload.py` | Offload recommender — scores reasoning / volume / self-contained; advisory only, never executes. Bidirectional: also surfaces escalation candidates |
| `judge.py` | LLM-as-judge for the escalation decision — Sonnet over `claude -p`, fail-closed; only the rare heavyweight prompt reaches it |
| `savings.py` | Measured $ saved by cheaper-model usage vs all-Opus, from the real per-message model in the logs |

## Privacy + safety

- All data stays on your Mac. The HTTP server binds **only** to `127.0.0.1`.
- Voice recordings live at `~/Library/Application Support/Dispatch/voice/` and
  the channel log at `~/Library/Logs/Dispatch/channel.log`. Both are local
  files you can delete any time.
- The hook fails open — if dispatch crashes, Claude Code falls back to its
  own permission prompt. You're never locked out.
- **Known limitation**: the localhost HTTP server is unauthenticated. Any
  process / browser tab on `localhost` can POST to `/api/*`. Single-user
  macOS this is fine; multi-user shared machines, harden it before you
  ship to others.

## License

MIT. See [LICENSE](LICENSE).
