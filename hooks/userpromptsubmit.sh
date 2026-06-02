#!/usr/bin/env bash
# UserPromptSubmit hook for Claude Code -> Dispatch (M3b).
#
# Reads the user's prompt on stdin, POSTs to dispatch's /prompt endpoint.
# Dispatch may respond with hookSpecificOutput.additionalContext that hints
# Opus to delegate the prompt to a cheaper Task sub-agent.
#
# Fail-open: if dispatch is down, exit 0 with no output so Claude proceeds
# normally. Quota routing is an optimization, not a correctness gate.

INPUT=$(cat)

if ! curl -fsS --max-time 1 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  exit 0
fi

RESPONSE=$(curl -sS --max-time 5 \
  -H 'Content-Type: application/json' \
  --data "$INPUT" \
  http://127.0.0.1:8765/prompt 2>/dev/null)

if [ -z "$RESPONSE" ]; then
  exit 0
fi

echo "$RESPONSE"
exit 0
