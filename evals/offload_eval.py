"""Eval harness for the offload recommender (offload.py).

Labeled cases across categories. `expect` is the verdict family a reasonable PM
would want from STRING ALONE: 'offload' | 'marginal' | 'keep'. Cases flagged
needs_context=True are ones a string-only scorer cannot get right (they depend
on live conversation) — the /offload-check skill fixes those via in-session
judgment, so we score them separately rather than pretending the heuristic
should nail them.

Run: python3 evals/offload_eval.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import offload

# (task, expected_family, needs_context, note)
CASES = [
    # --- A. mechanical search → offload (Haiku) ---
    ("find all TODO comments across the repo", "offload", False, "search/bulk"),
    ("grep for deprecated API calls in src/", "offload", False, "search"),
    ("list all functions defined across the codebase", "offload", False, "search/bulk"),
    ("count how many test files are in the project", "offload", False, "count"),
    ("search every file for hardcoded secrets", "offload", False, "search/bulk"),

    # --- B. transforms / extraction → offload (Sonnet) ---
    ("summarize these 15 meeting-note files", "offload", False, "summarize/bulk"),
    ("extract all email addresses from contacts.csv", "offload", False, "extract"),
    ("translate every UI string in en.json to Spanish", "offload", False, "translate/bulk"),
    ("reformat all markdown files to consistent headings", "offload", False, "format/bulk"),

    # --- C. design / reasoning → keep ---
    ("design the database schema for a multi-tenant app", "keep", False, "design"),
    ("architect the SSO auth flow", "keep", False, "architect"),
    ("decide whether to use REST or GraphQL here", "keep", False, "decide"),
    ("debug why the race condition only happens under load", "keep", False, "subtle debug"),
    ("what's the best approach for caching this", "keep", False, "judgment"),

    # --- D. conversational / context-dependent → keep ---
    ("what should we do next", "keep", False, "no signal"),
    ("fix the bug we just discussed", "keep", True, "needs convo context"),
    ("continue from where we left off", "keep", True, "needs convo context"),
    ("refactor that module we were looking at", "keep", True, "needs convo context"),

    # --- E. low-volume mechanical → marginal ---
    ("rename this one variable", "marginal", False, "low volume"),
    ("read the single config file and tell me the port", "marginal", False, "low volume, self-contained"),

    # --- F. known-hard (keyword looks mechanical, actually judgment) ---
    ("refactor the auth module for clarity", "keep", True, "‘refactor’ keyword but it's judgment"),
    ("clean up this code", "keep", True, "vague + judgment"),
]


def family(verdict: str) -> str:
    return {"offload": "offload", "offload-marginal": "marginal", "keep": "keep"}[verdict]


def main():
    rows = []
    overall_hit = ctx_total = ctx_hit = str_total = str_hit = 0
    for task, expect, needs_ctx, note in CASES:
        rec = offload.recommend(task)
        got = family(rec["verdict"])
        ok = (got == expect)
        rows.append((task, expect, got, ok, needs_ctx, note,
                     rec["model"].split("-")[1] if got != "keep" else "—"))
        if ok:
            overall_hit += 1
        if needs_ctx:
            ctx_total += 1; ctx_hit += ok
        else:
            str_total += 1; str_hit += ok

    print(f"{'TASK':<52} {'EXP':<9} {'GOT':<9} {'OK':<3} {'NEEDS-CTX':<9} MODEL")
    print("-" * 100)
    for task, expect, got, ok, needs_ctx, note, model in rows:
        mark = "✓" if ok else "✗"
        print(f"{task[:50]:<52} {expect:<9} {got:<9} {mark:<3} {('yes' if needs_ctx else ''):<9} {model}")

    n = len(CASES)
    print("\n" + "=" * 50)
    print(f"Overall:                 {overall_hit}/{n}  ({overall_hit/n*100:.0f}%)")
    print(f"String-solvable cases:   {str_hit}/{str_total}  ({str_hit/str_total*100:.0f}%)  ← what the heuristic SHOULD nail")
    print(f"Needs-context cases:     {ctx_hit}/{ctx_total}  ({ctx_hit/ctx_total*100:.0f}%)  ← expected to need /offload-check's in-session judgment")
    print("\nMisses on string-solvable cases (real bugs to fix):")
    any_bug = False
    for task, expect, got, ok, needs_ctx, note, model in rows:
        if not ok and not needs_ctx:
            any_bug = True
            print(f"  ✗ {task!r}: expected {expect}, got {got}  ({note})")
    if not any_bug:
        print("  (none)")


if __name__ == "__main__":
    main()
