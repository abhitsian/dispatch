"""Tool / prompt classifier — recommends cheapest sufficient model.

Two-stage by design:
  1. HEURISTIC (free, ~0ms): keyword patterns covering the obvious cases.
     About 60-70% of real workload should be confidently classified here.
  2. HAIKU CALL (~500ms, ~$0.0001): only when heuristics are ambiguous AND
     routing.haiku_classifier_enabled = true in config. Default OFF until
     the user has watched heuristic-mode shadow logs and wants more coverage.

Returns ClassificationResult with model + confidence + human-readable reason.
The caller (routing.py) decides what to do with it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Models we route between. Names match what Claude Code accepts in /model.
OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"
FABLE = "claude-fable-5"   # tier ABOVE Opus — the only "upgrade" target

# Relative cost vs Opus (1.0). Used to detect downgrades and estimate savings.
# Fable is the first model worth MORE than Opus (~2x list price), so it's the
# only entry > 1.0 — an escalation target, never a savings one.
RELATIVE_COST = {
    FABLE: 2.0,      # ~2x Opus
    OPUS: 1.0,
    SONNET: 0.20,    # ~5x cheaper
    HAIKU: 0.08,     # ~12x cheaper
}


@dataclass
class ClassificationResult:
    recommended_model: str
    confidence: float       # 0.0 - 1.0
    reason: str             # human-readable, shows in audit log
    stage: str              # "heuristic" | "haiku" | "default"


# Patterns that strongly indicate the cheapest tier (Haiku) is sufficient:
# pure lookup / search / list / count tasks where Opus is overkill.
_HAIKU_PATTERN = re.compile(
    r"\b("
    r"find (all|the|every|any|where)|"
    r"list (all|the|every|files?)|"
    r"grep( for)?|count (the|how many|occurrences)|"
    r"search (for|in|across|every|all|the|through)|locate|where (is|are|does|do)|"
    r"which files?|what files? (contain|reference|import|use)|"
    r"show (me )?(all|the|every)|"
    r"how many|read (the |this )?(\w+\s+){0,3}file|cat |tail |head |"
    r"look up|check (if|whether) .* (exists?|is present)|"
    r"what (is|are) the (value|contents?|names?) of"
    r")\b",
    re.IGNORECASE,
)

# Patterns indicating Sonnet is sufficient: mechanical transformations,
# explanations, extractions — work that benefits from real language ability
# but doesn't need Opus's reasoning depth.
_SONNET_PATTERN = re.compile(
    r"\b("
    r"summari[zs]e|extract|classify|categori[zs]e|tag|label|"
    r"rename( the| all| every)?|refactor( this| the| these| to| into)?|"
    r"convert|translate|format|"
    r"explain (what|how|why) (this|the|that)|describe (the|what|how)|"
    r"document(ation)? for|write (docs|a docstring|a comment|tests for)|"
    r"identify (the |all )?(bugs?|issues?|problems?)|"
    r"list (the )?(steps|tradeoffs|options|approaches|files?)|"
    r"add (a )?(comment|docstring|type ?hints?|logging|log line)|"
    r"write (a |an )?(unit |integration )?tests?|fix the typo|reformat|"
    r"add (doc)?strings?|update the (docstring|comment|readme)"
    r")\b",
    re.IGNORECASE,
)

# Patterns indicating Opus is needed: strategic thinking, design, complex
# debugging, judgment calls. STRONGEST match — overrides cheaper patterns.
_OPUS_PATTERN = re.compile(
    r"\b("
    r"design (the|a|an)|architect|"
    r"strategy|strategic|approach to|"
    r"decide (whether|if|between|on)|recommend|advise|"
    r"think (about|through|deeply|hard) about|"
    r"reason about|analyz?e (the|whether|why|how)|"
    r"debug (the|this|a) (subtle|tricky|hard|complex|weird)|"
    r"trade-?offs?|"
    r"think (about|through) (the )?(tradeoffs?|trade-?offs?|approaches?|options?)|"
    r"plan (the|a|how)"
    r")\b",
    re.IGNORECASE,
)

# Patterns indicating the work is worth ESCALATING above Opus to Fable — the
# ceiling cases where a higher intelligence ceiling can justify ~2x cost:
# irreversible/high-stakes decisions, whole-system architecture, one-shot
# correctness, taste-critical generation, first-principles reasoning. Deliberately
# conservative (phrase-anchored, not single keywords like "best") — escalation is
# fuzzy and a false upgrade costs more than a false downgrade, so we'd rather
# under-flag. Advisory only; never feeds the downgrade router.
_FABLE_PATTERN = re.compile(
    r"\b("
    r"irreversible|one[- ]?shot|can'?t (undo|reverse|be undone)|no second chance|"
    r"architect(ure|ing)? (of |for )?(the )?(whole|entire|overall|complete|end[- ]to[- ]end)|"
    r"system[- ]?(wide |level )?(design|architecture|redesign)|"
    r"from first principles|"
    r"(highest|publication|production|world)[- ]?(quality|grade|class)|"
    r"best[- ]possible|most (robust|rigorous|elegant)|"
    r"comprehensive\b.{0,40}\b(strategy|architecture|analysis|plan|migration|redesign)|"
    r"(formally |formal )?(prove|verify) (that |the |correctness)|"
    r"high[- ]stakes|board[- ](ready|level)|exec(utive)?[- ]ready"
    r")\b",
    re.IGNORECASE,
)


def classify_heuristic(text: str, current_model: str = OPUS) -> Optional[ClassificationResult]:
    """Stage 1: pattern-match. Returns None when no strong signal."""
    if not text:
        return None
    head = text[:800]

    # Opus-needed patterns win even when cheaper patterns also match.
    if _OPUS_PATTERN.search(head):
        return ClassificationResult(
            recommended_model=OPUS,
            confidence=0.90,
            reason="design/strategy/complex-debug pattern",
            stage="heuristic",
        )
    if _HAIKU_PATTERN.search(head):
        return ClassificationResult(
            recommended_model=HAIKU,
            confidence=0.88,
            reason="search/find/list pattern",
            stage="heuristic",
        )
    if _SONNET_PATTERN.search(head):
        return ClassificationResult(
            recommended_model=SONNET,
            confidence=0.86,
            reason="summarize/extract/refactor pattern",
            stage="heuristic",
        )
    return None


def classify(text: str, current_model: str = OPUS,
             config: Optional[dict] = None) -> ClassificationResult:
    """Top-level classify. Heuristic first, optional Haiku fallback."""
    res = classify_heuristic(text, current_model)
    if res is not None:
        return res

    # Haiku-call fallback would go here. Disabled by default; relies on
    # `claude -p --model haiku ...` subprocess (uses user OAuth, no API key).
    # Wiring deferred until heuristic shadow logs prove we need it.

    return ClassificationResult(
        recommended_model=current_model,
        confidence=0.40,
        reason="no strong signal",
        stage="default",
    )


def escalation_signal(text: str, *, use_judge: bool = False,
                      judge_model: Optional[str] = None) -> Optional[ClassificationResult]:
    """Upward counterpart to classify(): returns a Fable recommendation when the
    text is worth escalating above Opus, else None.

    Two-stage, mirroring classify():
      1. REGEX (free, ~0ms): high-precision phrase match (`_FABLE_PATTERN`).
      2. LLM JUDGE (opt-in, ~2-5s, ~Sonnet cost): reads intent the regex can't —
         only consulted when stage 1 is silent AND use_judge=True.

    Intentionally SEPARATE from classify()/classify_heuristic() — the downgrade
    router must never accidentally route work UP. Escalation is advisory
    (surfaced by /offload-check, measured in shadow), not an auto-apply path.
    """
    if not text:
        return None

    # Stage 1 — regex. Fast positive; confidence kept modest (it's still fuzzy).
    m = _FABLE_PATTERN.search(text[:800])
    if m:
        return ClassificationResult(
            recommended_model=FABLE,
            confidence=0.70,
            reason=f"high-ceiling pattern ({m.group(0).strip().lower()!r})",
            stage="escalation",
        )

    # Stage 2 — LLM judge. Lazy import (judge.py imports from this module).
    if use_judge:
        from judge import judge_escalation
        kwargs = {"model": judge_model} if judge_model else {}
        return judge_escalation(text, **kwargs)

    return None
