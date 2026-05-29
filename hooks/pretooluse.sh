#!/usr/bin/env bash
# PreToolUse hook for Claude Code -> Dispatch.
#
# Reads JSON on stdin (the tool-call payload), POSTs it to the dispatch HTTP
# server, prints the JSON decision on stdout.
#
# If dispatch is not running, exits 0 with no decision -> Claude falls back to
# its own permission prompt (fail-open is safer than fail-closed here).

INPUT=$(cat)

# Quick health check — short timeout so we don't block when dispatch is down.
if ! curl -fsS --max-time 1 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  exit 0
fi

RESPONSE=$(curl -sS --max-time 130 \
  -H 'Content-Type: application/json' \
  --data "$INPUT" \
  http://127.0.0.1:8765/approve 2>/dev/null)

if [ -z "$RESPONSE" ]; then
  # network error / timeout — fall through
  exit 0
fi

echo "$RESPONSE"
exit 0
