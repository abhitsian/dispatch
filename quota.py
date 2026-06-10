"""Token quota tracker for Claude Code Pro/Max sessions.

Anthropic doesn't expose a quota API, so we observe usage by scanning the
session JSONL files in ~/.claude/projects/<key>/<session>.jsonl. Every
assistant message line contains a `message.usage` object with
input_tokens / output_tokens / cache_creation_input_tokens /
cache_read_input_tokens, plus an ISO timestamp.

We sum these across all sessions, windowed by 5h (Claude's rolling session
limit) and 7d (weekly cap). State is persisted to USER_DATA_DIR so the meter
survives Dispatch restarts.

Tuning lives in USER_DATA_DIR/quota-config.json — ceiling per window and the
tier thresholds (60/80/90% by default). Hit-the-wall events get logged to
quota-history.json and are used to recalibrate ceilings over time.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from paths import USER_DATA_DIR

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

STATE_FILE = USER_DATA_DIR / "quota-offsets.json"
CONFIG_FILE = USER_DATA_DIR / "quota-config.json"
HISTORY_FILE = USER_DATA_DIR / "quota-history.json"

WINDOW_5H_SEC = 5 * 60 * 60
WINDOW_7D_SEC = 7 * 24 * 60 * 60

# Tier presets. Anthropic's exact quota formula isn't public, so these are
# calibrated against observed usage on each plan. They are starting points —
# the wall-hit feedback loop (record_wall_hit) tightens the ceiling toward
# the value at which the user actually hit a limit.
TIER_PRESETS = {
    # Cost-equiv ceilings per rolling window. These are ESTIMATES — Anthropic
    # doesn't publish exact subscription token limits — and self-correct via
    # record_wall_hit() when the user actually hits a wall. Calibrated up from
    # the original guesses after a real heavy session (deduped ~5.8M weighted
    # over 5h with no rate-limit hit) showed the old 5M/5h was far too low.
    "pro": {"ceiling_5h": 3_000_000, "ceiling_7d": 40_000_000},
    "max_5x": {"ceiling_5h": 7_000_000, "ceiling_7d": 150_000_000},
    "max_20x": {"ceiling_5h": 12_000_000, "ceiling_7d": 500_000_000},
}

DEFAULT_TIER = "max_20x"

DEFAULT_CONFIG = {
    "tier": DEFAULT_TIER,
    **TIER_PRESETS[DEFAULT_TIER],
    # Color bands for the passive gauge ONLY (green/yellow/orange/red). These
    # are NOT alert thresholds — M2 (tier alerts + enforcement) was removed
    # because it alarmed and gated on a guessed ceiling. The meter is now
    # purely informational; optimization lives in M3a/M3b routing.
    "tier_nudge": 0.60,
    "tier_enforce": 0.80,
    "tier_emergency": 0.90,
    # Cost-equivalent weights (in input-token units). Sonnet rates: output is
    # 5x input, cache_creation is 1.25x, cache_read is 0.1x. The meter sums
    # tokens × these weights so the ceiling can be expressed as a single
    # number that approximates Anthropic's plan-quota accounting.
    "weight_input": 1.0,
    "weight_output": 5.0,
    "weight_cache_create": 1.25,
    "weight_cache_read": 0.1,
    # Feature flags — top-level switches to disable any milestone wholesale.
    # M1 = passive meter (observation only). M3a/M3b = the optimization layer
    # (model routing). M2 (tier alerts/enforcement) was removed entirely.
    "features": {
        "m1_quota_meter": True,        # Scanning, pill, gauge, /state quota field
        "m3a_subagent_router": False,  # Rewrite Task model via PreToolUse updatedInput
        "m3b_prompt_router": False,    # UserPromptSubmit hint to delegate to cheaper model
        "m4_escalation": True,         # UserPromptSubmit nudge to ESCALATE up to Fable
    },
    # Routing controls — apply when m3a / m3b features are on.
    "routing": {
        "subagent_mode": "shadow",     # off | shadow | suggest | auto_notify | auto_silent
        "prompt_mode": "shadow",       # same enum
        "confidence_floor": 0.85,      # below this, never route
        "cooldown_sec": 600,           # don't ping-pong models within window
        "downgrade_only": True,        # safety: never auto-upgrade
        "safe_task_types": [           # patterns we trust to downgrade
            "research", "search", "find", "grep", "list",
            "extract", "summarize", "classify", "lookup",
        ],
        "haiku_classifier_enabled": False,  # use heuristics only until you trust
    },
    # Escalation (UPWARD) — the inverse of routing/offload. Fable is a tier above
    # Opus (~2x), so this is a quality lever, not a savings one. It can only
    # SUGGEST: Dispatch never switches your live session model (Claude Code hooks
    # can't, and by design that choice is yours — set Fable with `/model`). The
    # LLM judge (judge.py) decides per-prompt; the regex is just a free fast-path.
    "escalation": {
        "mode": "suggest",          # off | shadow (log only) | suggest (surface a tip)
        "use_judge": True,          # consult the LLM judge when the regex is silent
        "judge_model": "",          # blank = judge.JUDGE_MODEL (Sonnet)
        "gate": "complex_only",     # complex_only (heavyweight prompts) | always
        "cooldown_sec": 900,        # judge + nudge at most once per window per session
    },
}

DEFAULT_CEILING_5H = TIER_PRESETS[DEFAULT_TIER]["ceiling_5h"]
DEFAULT_CEILING_7D = TIER_PRESETS[DEFAULT_TIER]["ceiling_7d"]


@dataclass
class UsageSample:
    ts: float
    session_id: str
    project_key: str
    input_tokens: int
    output_tokens: int
    cache_create_tokens: int
    cache_read_tokens: int
    message_id: str = ""   # Anthropic message id — used to dedupe repeat JSONL lines

    def cost_equiv(self, weights: dict) -> int:
        return int(
            self.input_tokens * weights.get("weight_input", 1.0)
            + self.output_tokens * weights.get("weight_output", 5.0)
            + self.cache_create_tokens * weights.get("weight_cache_create", 1.25)
            + self.cache_read_tokens * weights.get("weight_cache_read", 0.1)
        )


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay into base recursively — keeps nested defaults intact
    when the user's config file only sets some sub-keys."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            merged = _deep_merge(DEFAULT_CONFIG, data)
            # Auto-upgrade: if the file is missing top-level keys we added
            # later (e.g. `features`, `routing`), write the merged version
            # back so the user can see and edit them.
            missing = set(DEFAULT_CONFIG.keys()) - set(data.keys())
            if missing:
                try:
                    CONFIG_FILE.write_text(json.dumps(merged, indent=2))
                except OSError:
                    pass
            return merged
        except Exception:
            pass
    # Write defaults on first run so the user has a file to edit.
    try:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    except OSError:
        pass
    return dict(DEFAULT_CONFIG)


def feature_enabled(name: str) -> bool:
    """Read-through check: is this feature flag on in the user's config?

    Falls back to DEFAULT_CONFIG['features'] if config is missing or stale.
    Used as a top-level gate before any feature-specific work runs.
    """
    cfg = tracker().config if _TRACKER else _load_config()
    return bool(cfg.get("features", {}).get(name,
                DEFAULT_CONFIG["features"].get(name, False)))


# ---------- runtime config mutation (UI toggles) ----------

VALID_ROUTING_MODES = ("off", "shadow", "suggest", "auto_notify", "auto_silent")
KNOWN_FEATURES = (
    "m1_quota_meter",
    "m3a_subagent_router",
    "m3b_prompt_router",
)


def set_feature(name: str, value: bool) -> dict:
    """Flip a feature flag. Updates in-memory config AND the on-disk file
    so the change persists and takes effect immediately — no restart needed.
    """
    if name not in KNOWN_FEATURES:
        raise ValueError(f"unknown feature: {name}")
    t = tracker()
    features = t.config.setdefault("features", {})
    features[name] = bool(value)
    _persist_config(t.config)
    return {"feature": name, "enabled": bool(value)}


def set_routing_mode(source: str, mode: str) -> dict:
    """Change subagent_mode or prompt_mode. Same in-memory + disk update."""
    if source not in ("subagent", "prompt"):
        raise ValueError(f"unknown routing source: {source}")
    if mode not in VALID_ROUTING_MODES:
        raise ValueError(f"unknown mode: {mode}")
    key = f"{source}_mode"
    t = tracker()
    routing_cfg = t.config.setdefault("routing", {})
    routing_cfg[key] = mode
    _persist_config(t.config)
    return {"source": source, "mode": mode}


def _persist_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


def all_feature_states() -> dict:
    """Snapshot of all feature flags + routing modes — for /state response."""
    cfg = tracker().config
    return {
        "features": {f: bool(cfg.get("features", {}).get(f, False))
                     for f in KNOWN_FEATURES},
        "routing": {
            "subagent_mode": cfg.get("routing", {}).get("subagent_mode", "off"),
            "prompt_mode": cfg.get("routing", {}).get("prompt_mode", "off"),
            "confidence_floor": cfg.get("routing", {}).get("confidence_floor", 0.85),
        },
        "valid_routing_modes": list(VALID_ROUTING_MODES),
    }


def _load_offsets() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_offsets(offsets: dict):
    try:
        STATE_FILE.write_text(json.dumps(offsets))
    except OSError:
        pass


def _parse_iso(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _safe_int(v) -> int:
    """Defensive int conversion — usage objects sometimes carry nested dicts."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _extract_usage(line: dict, default_session_id: str) -> Optional[UsageSample]:
    if line.get("type") != "assistant":
        return None
    m = line.get("message") or {}
    u = m.get("usage")
    if not isinstance(u, dict):
        return None
    ts = _parse_iso(line.get("timestamp", "")) or time.time()
    return UsageSample(
        ts=ts,
        session_id=line.get("sessionId") or default_session_id,
        project_key="",
        input_tokens=_safe_int(u.get("input_tokens")),
        output_tokens=_safe_int(u.get("output_tokens")),
        cache_create_tokens=_safe_int(u.get("cache_creation_input_tokens")),
        cache_read_tokens=_safe_int(u.get("cache_read_input_tokens")),
        message_id=str(m.get("id") or ""),
    )


@dataclass
class QuotaTracker:
    samples: list = field(default_factory=list)       # rolling 7d of UsageSample
    file_offsets: dict = field(default_factory=dict)  # str(path) -> bytes
    config: dict = field(default_factory=_load_config)
    last_scan: float = 0.0
    _bootstrapped: bool = False

    def scan(self) -> dict:
        """Incremental scan of JSONL files. Cheap to call every second.

        First call after init does a full bootstrap (reads all 7d of history).
        Subsequent calls read only new bytes since last scan.
        """
        now = time.time()
        if not self._bootstrapped:
            # Reset offsets on bootstrap so we re-read everything fresh.
            self.file_offsets = {}
            self._bootstrapped = True

        if PROJECTS_DIR.exists():
            for project_dir in PROJECTS_DIR.iterdir():
                if not project_dir.is_dir():
                    continue
                for jsonl in project_dir.glob("*.jsonl"):
                    self._scan_file(jsonl, project_dir.name)

        # Evict samples older than 7d.
        cutoff = now - WINDOW_7D_SEC
        if self.samples:
            self.samples = [s for s in self.samples if s.ts >= cutoff]

        self.last_scan = now
        _save_offsets(self.file_offsets)
        return self.snapshot(now)

    def _scan_file(self, jsonl: Path, project_key: str):
        key = str(jsonl)
        try:
            size = jsonl.stat().st_size
        except OSError:
            return
        offset = self.file_offsets.get(key, 0)
        if size <= offset:
            return
        session_id = jsonl.stem
        try:
            with jsonl.open("rb") as f:
                f.seek(offset)
                for raw in f:
                    try:
                        line = json.loads(raw)
                    except Exception:
                        continue
                    sample = _extract_usage(line, session_id)
                    if sample:
                        sample.project_key = project_key
                        self.samples.append(sample)
                self.file_offsets[key] = f.tell()
        except OSError:
            return

    def snapshot(self, now: float = 0.0) -> dict:
        if now == 0.0:
            now = time.time()
        cut5 = now - WINDOW_5H_SEC
        cut7 = now - WINDOW_7D_SEC

        tokens_5h = 0
        tokens_7d = 0
        per_session: dict[str, int] = {}
        oldest_5h = now

        # Dedupe by Anthropic message id: Claude Code writes the same assistant
        # message to the JSONL on several lines (partial/final/tool-attached),
        # each carrying the SAME usage block. Counting every line double-counts
        # badly (~2.4x in practice) and pushes the meter into false EMERGENCY.
        # First occurrence of each id wins; id-less samples always count.
        seen_ids: set[str] = set()

        for s in self.samples:
            if s.ts < cut7:
                continue
            if s.message_id:
                if s.message_id in seen_ids:
                    continue
                seen_ids.add(s.message_id)
            cost = s.cost_equiv(self.config)
            tokens_7d += cost
            if s.ts >= cut5:
                tokens_5h += cost
                per_session[s.session_id] = per_session.get(s.session_id, 0) + cost
                if s.ts < oldest_5h:
                    oldest_5h = s.ts

        ceil5 = self.config.get("ceiling_5h", DEFAULT_CEILING_5H)
        ceil7 = self.config.get("ceiling_7d", DEFAULT_CEILING_7D)
        pct5 = (tokens_5h / ceil5) if ceil5 else 0.0
        pct7 = (tokens_7d / ceil7) if ceil7 else 0.0
        pct = max(pct5, pct7)

        reset_5h_at = oldest_5h + WINDOW_5H_SEC if tokens_5h else now + WINDOW_5H_SEC

        return {
            "tokens_5h": tokens_5h,
            "ceiling_5h": ceil5,
            "pct_5h": round(pct5, 4),
            "tokens_7d": tokens_7d,
            "ceiling_7d": ceil7,
            "pct_7d": round(pct7, 4),
            "pct": round(pct, 4),
            "tier": self._tier(pct),
            "reset_5h_at": reset_5h_at,
            "reset_5h_in_sec": max(0, int(reset_5h_at - now)),
            "per_session": per_session,
            "config": dict(self.config),
            "sample_count": len(self.samples),
        }

    def _tier(self, pct: float) -> str:
        c = self.config
        if pct >= c.get("tier_emergency", 0.90):
            return "emergency"
        if pct >= c.get("tier_enforce", 0.80):
            return "enforce"
        if pct >= c.get("tier_nudge", 0.60):
            return "nudge"
        return "normal"

    def record_wall_hit(self, kind: str, observed_tokens: int):
        """Log when the user actually hit a limit — used to recalibrate.

        kind: '5h' or '7d'. observed_tokens: what we measured at the time.
        """
        entry = {
            "ts": time.time(),
            "kind": kind,
            "observed": observed_tokens,
            "ceiling_5h_then": self.config.get("ceiling_5h"),
            "ceiling_7d_then": self.config.get("ceiling_7d"),
        }
        try:
            history = []
            if HISTORY_FILE.exists():
                history = json.loads(HISTORY_FILE.read_text())
            history.append(entry)
            HISTORY_FILE.write_text(json.dumps(history, indent=2))
        except Exception:
            pass
        # Tighten the ceiling toward the observed value (90% of it — gives a
        # safety margin for next time).
        if kind == "5h" and observed_tokens > 0:
            self.config["ceiling_5h"] = int(observed_tokens * 0.90)
        elif kind == "7d" and observed_tokens > 0:
            self.config["ceiling_7d"] = int(observed_tokens * 0.90)
        try:
            CONFIG_FILE.write_text(json.dumps(self.config, indent=2))
        except OSError:
            pass


_TRACKER: Optional[QuotaTracker] = None


def tracker() -> QuotaTracker:
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = QuotaTracker()
    return _TRACKER


def snapshot() -> dict:
    """Scan + return current usage snapshot. Safe to call every second.

    Passive (M1): reports tokens / ceilings / percentages and a color band.
    No alerting, no enforcement — the band exists only to color the gauge.
    """
    return tracker().scan()


# ---------- pretty helpers for the menu bar ----------

def pct_glyph(pct: float, config: dict | None = None) -> str:
    """Return a single-char color glyph for the menu bar pill."""
    c = config or _load_config()
    if pct >= c.get("tier_emergency", 0.90):
        return "🔴"
    if pct >= c.get("tier_enforce", 0.80):
        return "🟠"
    if pct >= c.get("tier_nudge", 0.60):
        return "🟡"
    return "🟢"


def format_pill(snap: dict) -> str:
    """Compact pill string for the menu bar: '🟢 12% · 1h32m'."""
    pct = snap.get("pct", 0.0)
    glyph = pct_glyph(pct, snap.get("config"))
    reset_sec = snap.get("reset_5h_in_sec", 0)
    h = reset_sec // 3600
    m = (reset_sec % 3600) // 60
    if h > 0:
        reset_str = f"{h}h{m:02d}m"
    else:
        reset_str = f"{m}m"
    return f"{glyph} {int(pct * 100)}% · resets {reset_str}"


def format_detail(snap: dict) -> str:
    """Two-line detail row for the menu."""
    p5 = int(snap.get("pct_5h", 0) * 100)
    p7 = int(snap.get("pct_7d", 0) * 100)
    t5 = snap.get("tokens_5h", 0)
    t7 = snap.get("tokens_7d", 0)
    return f"5h: {p5}% ({t5:,} tok)  ·  Week: {p7}% ({t7:,} tok)"
