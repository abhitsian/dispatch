"""Measured token-cost savings (M3 verification).

Honest accounting, in two clearly-separated parts:

  1. MEASURED savings — real dollars saved by work that ran on a cheaper model
     instead of Opus, computed from the actual per-message `model` + `usage`
     in ~/.claude/projects/*.jsonl. This is true regardless of *why* it ran
     cheaper (Dispatch routing, a Sonnet sub-agent, a manual /model switch).

  2. ATTRIBUTABLE to Dispatch — how many routes the M3a/M3b router actually
     applied, from routing-audit.jsonl. We do NOT claim part (1) as ours;
     conflating "cheaper-model usage exists" with "Dispatch caused it" is the
     exact dishonesty that sank M2.

Prices are list API rates per 1M tokens (editable). Cache write = 1.25x input,
cache read = 0.1x input — Anthropic's prompt-cache multipliers.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

from paths import USER_DATA_DIR

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# $/1M tokens (input, output). Cache derived from input. Editable if rates move.
PRICING = {
    "opus":   {"in": 15.0, "out": 75.0},
    "sonnet": {"in": 3.0,  "out": 15.0},
    "haiku":  {"in": 1.0,  "out": 5.0},
}
BASELINE = "opus"   # counterfactual: what it would have cost if all on Opus


def _family(model: str) -> str | None:
    if not model:
        return None
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return None   # <synthetic> / unknown — no cost


def _cost(fam: str, u: dict) -> float:
    """Dollar cost of one usage block on model family `fam`."""
    p = PRICING[fam]
    inp = p["in"] / 1e6
    out = p["out"] / 1e6
    return (
        (u.get("input_tokens", 0) or 0) * inp
        + (u.get("output_tokens", 0) or 0) * out
        + (u.get("cache_creation_input_tokens", 0) or 0) * inp * 1.25
        + (u.get("cache_read_input_tokens", 0) or 0) * inp * 0.1
    )


def measure(window_sec: float, now: float) -> dict:
    """Scan all project JSONLs in the window, dedupe by message id, group by
    model family, and compute actual vs all-Opus-counterfactual cost."""
    cutoff = now - window_sec
    seen: set[str] = set()
    by_fam: dict[str, dict] = {}

    for fp in glob.glob(str(CLAUDE_PROJECTS / "*" / "*.jsonl")):
        try:
            with open(fp) as f:
                for raw in f:
                    try:
                        o = json.loads(raw)
                    except Exception:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    msg = o.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = _parse_ts(o.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    mid = msg.get("id")
                    if mid:
                        if mid in seen:
                            continue
                        seen.add(mid)
                    fam = _family(msg.get("model", ""))
                    if fam is None:
                        continue
                    actual = _cost(fam, u)
                    opus = _cost(BASELINE, u)
                    rec = by_fam.setdefault(
                        fam, {"messages": 0, "actual_cost": 0.0, "opus_cost": 0.0}
                    )
                    rec["messages"] += 1
                    rec["actual_cost"] += actual
                    rec["opus_cost"] += opus
        except OSError:
            continue

    total_actual = sum(r["actual_cost"] for r in by_fam.values())
    total_if_opus = sum(r["opus_cost"] for r in by_fam.values())
    saved = total_if_opus - total_actual
    return {
        "window_sec": window_sec,
        "by_model": {
            f: {
                "messages": r["messages"],
                "actual_cost": round(r["actual_cost"], 4),
                "if_opus_cost": round(r["opus_cost"], 4),
                "saved": round(r["opus_cost"] - r["actual_cost"], 4),
            }
            for f, r in sorted(by_fam.items())
        },
        "total_actual_cost": round(total_actual, 4),
        "total_if_all_opus": round(total_if_opus, 4),
        "saved": round(saved, 4),
        "saved_pct": round(saved / total_if_opus, 4) if total_if_opus else 0.0,
    }


def _parse_ts(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None
