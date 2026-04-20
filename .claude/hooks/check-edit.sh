#!/usr/bin/env bash
# PostToolUse hook: cheap syntax check on the file Claude just edited.
# - .py  → py_compile (uses .venv if available)
# - .ts/.tsx → no-op (frontend type-check is too slow per-file; use /lint-code)
# Reads tool-call JSON from stdin; exits 0 always (non-blocking).
set -u

INPUT="$(cat || true)"
FILE="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin)
  p=d.get("tool_input",{}).get("file_path") or d.get("tool_response",{}).get("file_path","")
  print(p)
except Exception:
  pass' 2>/dev/null)"

[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0

case "$FILE" in
  *.py)
    PY=".venv/bin/python"
    [ -x "$PY" ] || PY="python3"
    "$PY" -m py_compile "$FILE" 2>&1 || echo "py_compile failed: $FILE"
    ;;
esac

exit 0
