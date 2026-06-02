"""Offload recommender — advisor only, never executes.

Scores a task on the three axes that decide whether handing it to a cheaper
sub-agent actually saves money (see the analysis: cheap model × cold context
can cost MORE than Opus × cached context):

  1. reasoning      — mechanical work is safe to downgrade; design/judgment isn't
  2. volume         — high volume means the sub-agent dominates cost (worth it);
                      low volume means Opus orchestration overhead eats the gain
  3. self-contained — if it needs the live conversation, shipping context cold to
                      a fresh agent loses Opus's cache (reads at 0.1x) and often
                      costs more than it saves

Verdict: OFFLOAD (all green) / OFFLOAD-MARGINAL (reasoning ok but volume or
context works against it) / KEEP. Recommends a model + honest est. savings.
Apply is the user's job — this prints advice and stops.
"""
from __future__ import annotations

import re
import sys

from classifier import classify, OPUS, SONNET, HAIKU

# Savings vs Opus for a fully-offloaded task (rough list-price ratios).
_FULL_SAVE = {HAIKU: 92, SONNET: 80, OPUS: 0}

_VOLUME_HIGH = re.compile(
    r"\b(all|every|each|across|entire|whole|bulk|these|those|everything|"
    r"repo-?wide|\d{2,}\s+(files?|items?|rows?|records?|entries|functions?))\b", re.I)
_VOLUME_LOW = re.compile(
    r"\b(this (one|file|function|line|method|variable|change|bug)|"
    r"a single|\bsingle\b|just (this|the|one)|one\s+\w+|the \w+ file\b)\b", re.I)

# References to the live conversation → NOT self-contained.
_CONTEXT_DEP = re.compile(
    r"\b(the above|what we|we (just|were|had|discussed|did|built|talked|wrote)|earlier|"
    r"our\s|continue|keep going|the code we|as discussed|same as|like before|"
    r"looking at|working on|previous(ly)?|"
    r"that (file|function|change|one|module|code|thing|part|section)|it\b)\b", re.I)

# 'refactor/clean up X for clarity' reads as mechanical by keyword but is
# judgment work — keep on Opus regardless of the classifier.
_JUDGMENT_REFACTOR = re.compile(
    r"\b(refactor|clean ?up|rewrite|reorganit?ze|restructure)\b.*\b"
    r"(clarity|readab\w*|maintainab\w*|cleaner|better|simpler|simplif\w*|"
    r"elegan\w*|structure|design|idiomatic)\b", re.I)
# Explicit standalone scope → self-contained.
_SELF_CONTAINED = re.compile(
    r"\b(in (the |this )?(repo|directory|folder|codebase)|at /|under /|"
    r"the file [\w./-]+\.\w+|[\w./-]+\.\w{1,5}\b)\b", re.I)


def _reasoning_axis(task: str):
    if _JUDGMENT_REFACTOR.search(task):
        return "complex", "judgment work (refactor/clean-up for quality)", OPUS, False
    r = classify(task)
    if r.stage == "heuristic" and r.recommended_model == HAIKU:
        return "mechanical", r.reason, HAIKU, True
    if r.stage == "heuristic" and r.recommended_model == SONNET:
        return "moderate", r.reason, SONNET, True
    if r.stage == "heuristic" and r.recommended_model == OPUS:
        return "complex", r.reason, OPUS, False
    return "uncertain", "no strong mechanical signal", OPUS, False


def _volume_axis(task: str):
    if _VOLUME_HIGH.search(task):
        return "high", True
    if _VOLUME_LOW.search(task):
        return "low", False
    return "medium", None


def _self_contained_axis(task: str):
    dep = _CONTEXT_DEP.search(task)
    explicit = _SELF_CONTAINED.search(task)
    if explicit and not dep:
        return "yes (explicit scope)", True
    if dep and not explicit:
        return "no (refers to current context)", False
    return "unclear", None


def recommend(task: str) -> dict:
    rlevel, rreason, model, offloadable = _reasoning_axis(task)
    vlevel, v_ok = _volume_axis(task)
    slevel, s_ok = _self_contained_axis(task)

    caveats: list[str] = []
    if not offloadable:
        verdict = "keep"
        model = OPUS
        why = ("needs Opus-level reasoning — don't offload"
               if rlevel == "complex"
               else "no clear mechanical signal — default to Opus")
    else:
        marginal = False
        if v_ok is False:
            marginal = True
            caveats.append("low volume — Opus orchestration overhead may erase the saving")
        if s_ok is False:
            marginal = True
            caveats.append("not self-contained — shipping context cold to a fresh agent "
                           "loses Opus's cache (0.1x reads); can cost MORE than it saves")
        verdict = "offload-marginal" if marginal else "offload"
        why = (f"{rlevel} work, safe to run on {model.split('-')[1].title()}"
               if not marginal else
               f"{rlevel} but conditions work against it — offload only if you're sure")

    full = _FULL_SAVE[model]
    est = full if verdict == "offload" else (full // 2 if verdict == "offload-marginal" else 0)
    return {
        "task": task,
        "verdict": verdict,
        "model": model,
        "est_savings_pct": est,
        "axes": {
            "reasoning": {"level": rlevel, "ok": offloadable, "note": rreason},
            "volume": {"level": vlevel, "ok": v_ok},
            "self_contained": {"level": slevel, "ok": s_ok},
        },
        "why": why,
        "caveats": caveats,
    }


def _mark(ok):
    return "✓" if ok is True else ("✗" if ok is False else "·")


def format_human(rec: dict) -> str:
    v = rec["verdict"]
    head = {
        "offload": f"OFFLOAD → {rec['model'].split('-')[1].title()} (~{rec['est_savings_pct']}% cheaper)",
        "offload-marginal": f"OFFLOAD (marginal) → {rec['model'].split('-')[1].title()} (~{rec['est_savings_pct']}%, see caveats)",
        "keep": "KEEP on Opus",
    }[v]
    a = rec["axes"]
    lines = [
        f"Dispatch ▸ {head}",
        f"  reasoning:      {a['reasoning']['level']:<10} {_mark(a['reasoning']['ok'])}  ({a['reasoning']['note']})",
        f"  volume:         {a['volume']['level']:<10} {_mark(a['volume']['ok'])}",
        f"  self-contained: {a['self_contained']['level']:<10} {_mark(a['self_contained']['ok'])}",
        f"  verdict: {rec['why']}",
    ]
    for c in rec["caveats"]:
        lines.append(f"  ⚠ {c}")
    return "\n".join(lines)


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]).strip()
    if not task:
        print("usage: offload.py <task description>")
        sys.exit(1)
    if "--json" in sys.argv:
        import json
        task = " ".join(a for a in sys.argv[1:] if a != "--json").strip()
        print(json.dumps(recommend(task), indent=2))
    else:
        print(format_human(recommend(task)))
