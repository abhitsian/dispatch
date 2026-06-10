"""Shadow eval for the Fable escalation signal — regex (stage 1) and the
optional LLM judge (stage 2, classifier.escalation_signal(use_judge=True)).

Mirrors mine_prompts.py: runs over YOUR real mined prompts and reports how often
the upward signal fires — the same discipline that proved the DOWNWARD offload
lever was empty (~96% keep, $0 saved). The question here is the inverse:

  Of the prompts that stay on Opus, how many are worth escalating to Fable?

Stage 1 (regex) already answered ~0% — it's blind to intent. Stage 2 asks an LLM
to read intent. If the judge ALSO lands near 0% with clean precision, the upgrade
lever is genuinely empty for your usage and you can stop. If it finds a real,
believable slice, that's a surface worth a deliberate `/model fable` habit (and
only then, maybe, routing).

Run:
  python3 evals/escalation_eval.py                 # regex only (free, instant)
  python3 evals/escalation_eval.py --judge          # + LLM judge (slow, costs)
  python3 evals/escalation_eval.py --judge --limit 20   # cheap first pass
  python3 evals/escalation_eval.py --judge --model claude-haiku-4-5-20251001

Judge calls are cached in evals/.judge_cache.json (gitignored) keyed by
(model, prompt), so re-runs are free. Reads evals/mined_prompts.jsonl (gitignored).
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import offload
from classifier import escalation_signal

HERE = Path(__file__).resolve().parent
DATA = HERE / "mined_prompts.jsonl"
CACHE = HERE / ".judge_cache.json"
_FAMILY = {"offload": "offload", "offload-marginal": "marginal", "keep": "keep"}


def _load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(c: dict) -> None:
    try:
        CACHE.write_text(json.dumps(c))
    except OSError:
        pass


def _key(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}\x00{prompt}".encode()).hexdigest()


def _flag_for(prompt: str, use_judge: bool, model: str, cache: dict):
    """Return a ClassificationResult-like dict {confidence, reason, stage} or None.
    Caches judge calls; regex is free so it's never cached."""
    # Stage 1 regex is cheap — run it directly to know the stage, even with judge.
    res = escalation_signal(prompt)
    if res is not None:
        return {"confidence": res.confidence, "reason": res.reason, "stage": res.stage}
    if not use_judge:
        return None
    k = _key(model, prompt)
    if k in cache:
        return cache[k]
    from judge import judge_escalation
    r = judge_escalation(prompt, model=model)
    val = None if r is None else {"confidence": r.confidence, "reason": r.reason, "stage": r.stage}
    cache[k] = val
    _save_cache(cache)
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="consult the LLM judge (stage 2)")
    ap.add_argument("--model", default=None, help="judge model (default: judge.JUDGE_MODEL)")
    ap.add_argument("--limit", type=int, default=0, help="only score the N most recent prompts")
    args = ap.parse_args()

    if not DATA.exists():
        print(f"no mined prompts at {DATA} — run: python3 evals/mine_prompts.py")
        return
    rows = [json.loads(l) for l in DATA.open() if l.strip()]
    if not rows:
        print("mined_prompts.jsonl is empty")
        return
    if args.limit:
        rows = rows[:args.limit]

    if args.model is None:
        from judge import JUDGE_MODEL
        model = JUDGE_MODEL
    else:
        model = args.model

    cache = _load_cache() if args.judge else {}

    down = Counter()
    by_stage = Counter()
    flagged = []            # (prompt, downgrade_family, reason, stage)
    flagged_in_keep = 0
    keep_pool = 0

    for i, r in enumerate(rows, 1):
        prompt = r["prompt"]
        fam = _FAMILY[offload.recommend(prompt)["verdict"]]
        down[fam] += 1
        if fam == "keep":
            keep_pool += 1
        if args.judge:
            print(f"\r  judging {i}/{len(rows)} …", end="", file=sys.stderr, flush=True)
        flag = _flag_for(prompt, args.judge, model, cache)
        if flag is not None:
            by_stage[flag["stage"]] += 1
            flagged.append((prompt, fam, flag["reason"], flag["stage"]))
            if fam == "keep":
                flagged_in_keep += 1
    if args.judge:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    n = len(rows)
    mode = f"regex + judge ({model})" if args.judge else "regex only"
    print(f"mined prompts: {n}   |   mode: {mode}\n")
    print("DOWNGRADE axis (existing offload scorer):")
    for k in ("keep", "marginal", "offload"):
        v = down.get(k, 0)
        print(f"  {k:<10} {v:>3}  ({v / n * 100:.0f}%)")

    f = len(flagged)
    print("\nUPGRADE axis (escalation signal):")
    print(f"  flagged for Fable        {f:>3}  ({f / n * 100:.0f}% of all prompts)")
    if by_stage:
        parts = ", ".join(f"{s}={c}" for s, c in by_stage.items())
        print(f"    by stage: {parts}")
    if keep_pool:
        print(f"  flagged within keep-pool {flagged_in_keep:>3}  "
              f"({flagged_in_keep / keep_pool * 100:.0f}% of the {keep_pool} Opus-bound prompts)")

    print("\nFlagged prompts (eyeball precision — are these REALLY worth 2x?):")
    if not flagged:
        print("  (none — upgrade lever looks empty for your usage, same as downgrade)")
    for prompt, fam, reason, stage in sorted(flagged, key=lambda x: x[3]):
        print(f"  • [{fam}|{stage}] {reason}")
        print(f"      {prompt[:110]}")

    print("\nRead this like the offload eval: a low flag-rate (with clean precision)")
    print("means Fable doesn't belong in Dispatch's auto path — at most a manual")
    print("/model fable habit. The signal stays advisory-only until this says otherwise.")


if __name__ == "__main__":
    main()
