"""Routing decision engine — wraps the classifier with safety policy.

Applies:
  - mode gating (off / shadow / suggest / auto_notify / auto_silent)
  - confidence floor (don't route on weak signal)
  - downgrade-only safety (never auto-upgrade)
  - cooldown (don't ping-pong models within window)
  - audit log of every decision (shadow OR real)

Returns RoutingDecision objects that the hook layer translates into
hookSpecificOutput.updatedInput (for sub-agents) or additionalContext
(for prompts).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from paths import USER_DATA_DIR
from classifier import classify, OPUS, SONNET, HAIKU, RELATIVE_COST

AUDIT_FILE = USER_DATA_DIR / "routing-audit.jsonl"

# (session_id, target_model) -> last applied timestamp. Process-local; resets
# on Dispatch restart, which is fine — cooldown is a flow-protection feature,
# not a persistent quota.
_COOLDOWNS: dict[tuple[str, str], float] = {}


@dataclass
class RoutingDecision:
    source: str               # "subagent" | "prompt"
    apply: bool               # caller should mutate the input
    notify_user: bool         # surface in radio / notification
    recommended_model: str
    original_model: str
    confidence: float
    reason: str
    classifier_stage: str
    mode: str = "off"
    cooldown_blocked: bool = False

    @property
    def is_downgrade(self) -> bool:
        return (RELATIVE_COST.get(self.recommended_model, 1.0)
                < RELATIVE_COST.get(self.original_model, 1.0))

    @property
    def cost_ratio(self) -> float:
        """Recommended cost as a fraction of original (lower = cheaper)."""
        orig = RELATIVE_COST.get(self.original_model, 1.0)
        rec = RELATIVE_COST.get(self.recommended_model, 1.0)
        return rec / orig if orig else 1.0


def route_subagent(
    session_id: str,
    tool_input: dict,
    config: dict,
) -> RoutingDecision:
    """Classify a Task sub-agent invocation and decide whether to rewrite
    its model.

    Returns a RoutingDecision describing what the hook should do:
      apply=True   → caller sets hookSpecificOutput.updatedInput.model
      apply=False  → caller passes through unchanged (still audited)
    """
    rcfg = (config or {}).get("routing", {})
    mode = rcfg.get("subagent_mode", "off")
    original_model = tool_input.get("model") or OPUS

    if mode == "off":
        return _off_decision("subagent", original_model, mode)

    description = (tool_input.get("description") or "")
    prompt = (tool_input.get("prompt") or "")
    text = f"{description}\n\n{prompt}"

    res = classify(text, current_model=original_model, config=rcfg)
    decision = RoutingDecision(
        source="subagent", apply=False, notify_user=False,
        recommended_model=res.recommended_model,
        original_model=original_model,
        confidence=res.confidence,
        reason=res.reason,
        classifier_stage=res.stage,
        mode=mode,
    )
    _apply_safety_and_mode(decision, rcfg, session_id)
    _audit(decision, session_id, {
        "tool": "Task",
        "description": description[:200],
    })
    return decision


def route_prompt(
    session_id: str,
    prompt_text: str,
    current_model: str,
    config: dict,
) -> RoutingDecision:
    """Classify a user prompt and decide whether to hint delegation."""
    rcfg = (config or {}).get("routing", {})
    mode = rcfg.get("prompt_mode", "off")

    if mode == "off":
        return _off_decision("prompt", current_model, mode)

    res = classify(prompt_text, current_model=current_model, config=rcfg)
    decision = RoutingDecision(
        source="prompt", apply=False, notify_user=False,
        recommended_model=res.recommended_model,
        original_model=current_model,
        confidence=res.confidence,
        reason=res.reason,
        classifier_stage=res.stage,
        mode=mode,
    )
    _apply_safety_and_mode(decision, rcfg, session_id)
    _audit(decision, session_id, {
        "source": "prompt",
        "prompt": prompt_text[:200],
    })
    return decision


# ---------- internals ----------

def _off_decision(source: str, original_model: str, mode: str) -> RoutingDecision:
    return RoutingDecision(
        source=source, apply=False, notify_user=False,
        recommended_model=original_model, original_model=original_model,
        confidence=0.0, reason="routing off",
        classifier_stage="off", mode=mode,
    )


def _apply_safety_and_mode(d: RoutingDecision, rcfg: dict, session_id: str):
    """Mutate decision in place: apply safety gates, then set apply/notify
    based on mode. Returns nothing — modifies d directly."""
    confidence_floor = float(rcfg.get("confidence_floor", 0.85))
    downgrade_only = bool(rcfg.get("downgrade_only", True))
    cooldown_sec = int(rcfg.get("cooldown_sec", 600))

    # Confidence floor — keep but tag the reason.
    if d.confidence < confidence_floor:
        d.reason += f" (below floor {confidence_floor})"
        return

    if downgrade_only and not d.is_downgrade:
        d.reason += " (not a downgrade — skipped)"
        return

    if d.recommended_model == d.original_model:
        d.reason = "no change recommended"
        return

    # Cooldown check — same session+target within window blocks.
    #
    # Cooldown exists to stop the MAIN SESSION model from ping-ponging
    # (Opus↔Sonnet within one train of thought). It does NOT apply to
    # per-call delegations: each subagent (M3a) and each prompt hint (M3b)
    # is an independent, cheap routing decision that never switches the
    # session's own model — so a cooldown there just silently forces repeat
    # mechanical work back onto Opus for no benefit. Skip it for those.
    now = time.time()
    key = (session_id, d.recommended_model)
    if d.source not in ("subagent", "prompt"):
        last = _COOLDOWNS.get(key, 0)
        if cooldown_sec > 0 and (now - last) < cooldown_sec:
            d.cooldown_blocked = True
            d.reason += f" (cooldown {int(cooldown_sec - (now - last))}s)"
            return

    # Survived all gates. Mode determines apply/notify.
    if d.mode == "shadow":
        d.apply, d.notify_user = False, False
    elif d.mode == "suggest":
        d.apply, d.notify_user = False, True
    elif d.mode == "auto_notify":
        d.apply, d.notify_user = True, True
        _COOLDOWNS[key] = now
    elif d.mode == "auto_silent":
        d.apply, d.notify_user = True, False
        _COOLDOWNS[key] = now


def _audit(decision: RoutingDecision, session_id: str, payload: dict):
    """Append-only JSONL log. Includes shadow decisions so the user can
    review what WOULD have happened before enabling auto."""
    entry = {
        "ts": time.time(),
        "session": session_id[:12] if session_id else "",
        "decision": asdict(decision),
        "payload": {k: str(v)[:300] for k, v in (payload or {}).items()},
    }
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def recent_audits(n: int = 20) -> list[dict]:
    """Tail the audit log — used by the dashboard / detail window."""
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text().splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []


def audit_summary(window_sec: int = 86400) -> dict:
    """Quick stats over a window: total decisions, applied, by source, savings."""
    now = time.time()
    cutoff = now - window_sec
    entries = recent_audits(n=2000)
    total = applied = shadowed = subagent = prompt = 0
    estimated_savings_ratio_sum = 0.0
    by_target: dict[str, int] = {}
    for e in entries:
        if e.get("ts", 0) < cutoff:
            continue
        d = e.get("decision", {})
        total += 1
        if d.get("source") == "subagent": subagent += 1
        elif d.get("source") == "prompt": prompt += 1
        if d.get("apply"):
            applied += 1
            orig_cost = RELATIVE_COST.get(d.get("original_model"), 1.0)
            rec_cost = RELATIVE_COST.get(d.get("recommended_model"), 1.0)
            if orig_cost > 0:
                estimated_savings_ratio_sum += 1 - (rec_cost / orig_cost)
            by_target[d.get("recommended_model", "?")] = (
                by_target.get(d.get("recommended_model", "?"), 0) + 1
            )
        else:
            shadowed += 1
    avg_savings = (estimated_savings_ratio_sum / applied) if applied else 0
    return {
        "window_sec": window_sec,
        "total": total,
        "applied": applied,
        "shadowed": shadowed,
        "subagent": subagent,
        "prompt": prompt,
        "by_target": by_target,
        "avg_savings_ratio": round(avg_savings, 3),
    }
