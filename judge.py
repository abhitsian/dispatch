"""LLM-as-judge for the Fable escalation decision — stage 2 behind the regex.

Why this exists: the regex (`_FABLE_PATTERN` in classifier.py) is high-precision
but blind to intent. It fired 0% on real prompts because escalation-worthy work
rarely announces itself in keywords — you write "help with the canvas 2-pager",
not "architect the entire high-stakes system". A judge reads intent.

It runs the SAME way every other Dispatch model call does: a `claude -p`
subprocess on your Claude Code OAuth login — no API key, no SDK (see
sessions.send_to_session and the classifier's deferred-fallback note).

Fail-closed: any timeout / parse failure / empty output → no escalation. A false
upgrade costs ~2x, so silence always beats a guess.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

from classifier import ClassificationResult, FABLE, SONNET, HAIKU  # noqa: F401

# The judge should be smarter than the thing it replaces — a Haiku judge is
# barely better than regex at reading stakes/taste, which is the whole problem.
# Sonnet is the sweet spot: still ~5x cheaper than Opus, far cheaper than a wrong
# Fable call. Flip to HAIKU if judge cost ever matters more than judge quality.
JUDGE_MODEL = SONNET

_RUBRIC = """You are a router deciding whether ONE Claude Code prompt is worth \
escalating from Claude Opus to Claude Fable 5 — a model about 2x the cost with a \
higher intelligence ceiling.

Escalate to Fable ONLY when more intelligence materially changes the outcome:
- irreversible or expensive-to-get-wrong decisions
- whole-system architecture or product strategy (not a single component)
- novel, genuinely hard reasoning or proofs
- taste-critical output that ships to customers, execs, or a board

Do NOT escalate routine building, exploration, mechanical edits, research, \
debugging, or anything Opus already handles well. Default to NO — escalation is \
the rare exception, not the norm.

Respond with ONLY a JSON object, no prose, no code fence:
{"escalate": true|false, "confidence": 0.0-1.0, "reason": "<= 12 words"}

PROMPT TO JUDGE:
"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first {...} object out of the model's reply (tolerates a stray
    code fence or preamble)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def judge_escalation(prompt: str, *, model: str = JUDGE_MODEL,
                     timeout: int = 45) -> Optional[ClassificationResult]:
    """Ask an LLM whether `prompt` is worth Fable.

    Returns a ClassificationResult (recommended_model=FABLE) on a confident YES;
    None otherwise — including every error path (fail closed)."""
    if not prompt or not prompt.strip():
        return None
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, _RUBRIC + prompt.strip()],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    data = _extract_json(result.stdout or "")
    if not data or not data.get("escalate"):
        return None
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(data.get("reason", "")).strip()[:120] or "escalate"
    return ClassificationResult(
        recommended_model=FABLE,
        confidence=conf,
        reason=f"judge: {reason}",
        stage="judge",
    )
