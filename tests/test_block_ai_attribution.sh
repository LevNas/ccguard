#!/usr/bin/env bash
# Dependency-light self-tests for hooks/block-ai-attribution.sh.
# Run: bash tests/test_block_ai_attribution.sh   (exit 0 = all pass)
#
# Requires jq (the guard itself requires jq; without it the guard fails open and these
# block-expecting cases would not hold). The brand token is assembled at runtime so this
# test file carries no literal AI-brand attribution of its own.

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/block-ai-attribution.sh"
brand="Cla""ude"  # runtime assembly — keep this file free of a literal brand attribution
pass=0
fail=0

if ! command -v jq >/dev/null 2>&1; then
  echo "SKIP: jq not available; the guard fails open without it." >&2
  exit 0
fi

# check <desc> <expected-exit> <command-string>
check() {
  local desc="$1" expected="$2" cmd="$3" json rc
  json="$(jq -nc --arg c "$cmd" '{tool_input: {command: $c}}')"
  printf '%s' "$json" | bash "$HOOK" >/dev/null 2>&1
  rc=$?
  if [ "$rc" = "$expected" ]; then
    echo "  ok  $desc"
    pass=$((pass + 1))
  else
    echo "FAIL  $desc (expected exit $expected, got $rc)"
    fail=$((fail + 1))
  fi
}

# Allowed (exit 0)
check "plain commit allowed"          0 "git commit -m 'fix: a real bug'"
check "non-commit git allowed"        0 "git status"
check "unrelated command allowed"     0 "ls -la && echo done"

# Blocked (exit 2)
check "co-authored-by blocked"        2 "git commit -m 'x

Co-Authored-By: ${brand} <noreply@anthropic.com>'"
check "generated-with blocked"        2 "git commit -m 'x

Generated with ${brand} Code'"
check "session trailer blocked"       2 "git commit -m 'x

${brand}-Session: https://claude.ai/code/session_abc123'"
check "robot-emoji blocked"           2 "git commit -m 'shipped 🤖'"
check "commit later in pipeline"      2 "echo hi && git commit -m '${brand}-Session: foo'"

echo ""
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
