"""Mine real user prompts from Claude Code session transcripts.

Produces a candidate eval set grounded in YOUR actual usage instead of
hand-written cases. Extracts genuine user turns from ~/.claude/projects/*.jsonl,
filters out noise (tool results, slash commands, hook-injected context, dispatch
radio chatter, trivial/huge turns), dedupes, and emits the most recent N with
the current deterministic verdict pre-filled — ready for labeling.

Run: python3 evals/mine_prompts.py [N]   (default 80)
Writes: evals/mined_prompts.jsonl
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import offload

PROJECTS = Path.home() / ".claude" / "projects"
OUT = Path(__file__).resolve().parent / "mined_prompts.jsonl"

# Lines that aren't genuine task prompts.
_NOISE = re.compile(
    r"^/|^\s*$|^\[Image:|\[Dispatch|<command-|<system-reminder|<task-notification|"
    r"<task-id|<tool-use-id|<local-command|DISPATCH|UNIT-\d|"
    r"\bover\.\s*$|TRANSMISSION FROM|caveat:|Channel transcript|Reply now in one",
    re.I)
# Cap per session so one long marathon session doesn't dominate the sample.
PER_SESSION_CAP = 6


def _text(content) -> str:
    """User message content → plain text, or '' if it's a tool result / non-text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    return ""   # tool-result turn, not a prompt
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
        return " ".join(parts)
    return ""


def _keep(text: str) -> bool:
    t = text.strip()
    if _NOISE.search(t):
        return False
    words = t.split()
    if len(words) < 3 or len(words) > 120:   # trivial or a giant paste
        return False
    return True


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    seen = set()
    rows = []
    for fp in PROJECTS.glob("*/*.jsonl"):
        per = 0
        try:
            for raw in fp.open():
                if per >= PER_SESSION_CAP:
                    break
                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                if o.get("type") != "user":
                    continue
                msg = o.get("message") or {}
                if msg.get("role") != "user":
                    continue
                text = _text(msg.get("content")).strip()
                if not _keep(text):
                    continue
                key = re.sub(r"\s+", " ", text.lower())[:160]
                if key in seen:
                    continue
                seen.add(key)
                ts = o.get("timestamp", "")
                rows.append({"prompt": text, "ts": ts, "session": fp.stem[:12]})
                per += 1
        except OSError:
            continue

    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows = rows[:n]
    with OUT.open("w") as f:
        for r in rows:
            rec = offload.recommend(r["prompt"])
            f.write(json.dumps({
                "prompt": r["prompt"],
                "session": r["session"],
                "ts": r["ts"],
                "scorer_verdict": rec["verdict"],
                "scorer_model": rec["model"],
                "expected": "",   # <- to be labeled (offload | marginal | keep)
            }) + "\n")

    print(f"mined {len(rows)} distinct real prompts -> {OUT}")
    # quick distribution of what the scorer says on real data
    from collections import Counter
    c = Counter(offload.recommend(r["prompt"])["verdict"] for r in rows)
    print("scorer verdict distribution on YOUR prompts:")
    for k, v in c.most_common():
        print(f"  {k:<18} {v:>3}  ({v/len(rows)*100:.0f}%)")


if __name__ == "__main__":
    main()
