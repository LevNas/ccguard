#!/usr/bin/env bash
# PreToolUse(Bash) guard — block git commits carrying AI/assistant attribution or session metadata.
#
# "Things that must never happen" belong in a deterministic hook, not in prose. Per Anthropic's
# steering guidance, CLAUDE.md / rules are followed only probabilistically; a hook enforces the
# policy every time. This guard is that deterministic layer for a "no AI attribution in commits"
# policy.
#
# Shipped as a ccguard plugin hook, so it applies in every environment without touching the user's
# (public) dotfiles. Exit 2 => deny the tool call; stderr is shown back to Claude.

set -uo pipefail

input="$(cat)"

# Need jq to parse the tool input safely. If absent, fail-open (do not block) — the policy still
# exists wherever the user documents it.
command -v jq >/dev/null 2>&1 || exit 0

cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -n "$cmd" ] || exit 0

# Only police git commit invocations.
printf '%s' "$cmd" | grep -qE '\bgit\b[^|]*\bcommit\b' || exit 0

# Build the brand token at runtime so this guard's own source never trips literal scanners
# (including a future run of this very guard over ccguard's own commits).
brand="Cla""ude"
patt="(co-authored-by:[[:space:]]*${brand}|generated with[[:space:]].*${brand}|${brand}-session:|claude\.ai/code/session|🤖)"

if printf '%s' "$cmd" | grep -qiE "$patt"; then
  echo "BLOCKED: the commit message carries AI attribution / session metadata, which your policy forbids. Remove the trailer(s) and commit again." >&2
  exit 2
fi
exit 0
