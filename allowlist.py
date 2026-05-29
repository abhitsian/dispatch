"""Mirror Claude Code's `permissions.allow` rules so dispatch only intervenes
when Claude itself would have asked the user.

Claude Code merges ~/.claude/settings.json and ~/.claude/settings.local.json
(local wins). Each rule is a string like:

  "Bash(npm install:*)"     — Bash, command starts with "npm install" (+args)
  "Bash(xcodebuild -version)" — Bash, exact command
  "WebFetch(domain:github.com)" — WebFetch, URL host == github.com
  "WebSearch"                — tool name only, any input matches

We deliberately do NOT honor deny rules — those are Claude's responsibility.
We only short-circuit the *allow* side so we stop double-gating.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

CLAUDE_DIR = Path.home() / ".claude"


def matches_allow_rules(tool_name: str, tool_input, *, cwd: str = "") -> str | None:
    """Return the matching rule string if this tool call is already
    auto-allowed by Claude's own permission config; None otherwise.

    Also scans the session's project-level .claude/settings*.json (passed via
    cwd), since Claude applies BOTH user- and project-level allows.
    """
    rules = _load_allow_rules(cwd=cwd)
    if not rules or not isinstance(tool_input, dict):
        return None
    for rule in rules:
        if _rule_matches(rule, tool_name, tool_input):
            return rule
    return None


def _load_allow_rules(*, cwd: str = "") -> list[str]:
    """Load Claude's allow rules from every place Claude looks at runtime."""
    rules: list[str] = []
    # 1) user-level
    for fname in ("settings.json", "settings.local.json"):
        rules += _read_allow_from(CLAUDE_DIR / fname)
    # 2) project-level (Claude reads <cwd>/.claude/settings.json too)
    if cwd:
        proj = Path(cwd) / ".claude"
        for fname in ("settings.json", "settings.local.json"):
            rules += _read_allow_from(proj / fname)
    return rules


def _read_allow_from(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    out: list[str] = []
    for r in (data.get("permissions", {}) or {}).get("allow", []) or []:
        if isinstance(r, str):
            out.append(r)
    return out


def _rule_matches(rule: str, tool_name: str, tool_input: dict) -> bool:
    rule = rule.strip()
    if "(" not in rule:
        # Bare tool name (e.g., "WebSearch") — any input matches.
        return rule == tool_name

    open_idx = rule.index("(")
    rule_tool = rule[:open_idx].strip()
    if rule_tool != tool_name:
        return False
    if not rule.endswith(")"):
        return False
    pattern = rule[open_idx + 1 : -1]

    if tool_name == "Bash":
        return _bash_pattern_matches(pattern, (tool_input.get("command") or ""))
    if tool_name == "WebFetch":
        return _webfetch_pattern_matches(pattern, (tool_input.get("url") or ""))
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"):
        # Path patterns (uncommon in allow list, but support them).
        return _path_pattern_matches(pattern, (tool_input.get("file_path") or ""))
    # Other tools: only allow bare name match (handled above).
    return False


def _bash_pattern_matches(pattern: str, cmd: str) -> bool:
    """Bash(cmd:*) → cmd starts with "<pattern>"; Bash(cmd) → exact match."""
    cmd = cmd.strip()
    pattern = pattern.strip()
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        if not prefix:
            return True  # "Bash(:*)" — allow everything
        if cmd == prefix:
            return True
        # require the next char to be a word boundary so "git" doesn't match "github"
        return cmd.startswith(prefix) and (len(cmd) == len(prefix) or cmd[len(prefix)] in " \t\n;|&")
    return cmd == pattern


def _webfetch_pattern_matches(pattern: str, url: str) -> bool:
    if pattern.startswith("domain:"):
        want = pattern[len("domain:") :].strip().lower()
        try:
            host = (urlparse(url).netloc or "").lower().split(":")[0]
        except Exception:
            return False
        return host == want or host.endswith("." + want)
    return False


def _path_pattern_matches(pattern: str, path: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern)
