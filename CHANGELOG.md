# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-10-03

### Added
- **`push-gate`** (PreToolUse / Bash, `hooks/push-gate.py`): lets a push to a feature branch
  run without human confirmation by deciding deterministically what must never leave the
  machine. Denies force pushes (`-f`, `--force*`, `+refspec`), deletions (`:branch`,
  `--delete`), `--all`, `--mirror`, `--tags`, `--follow-tags`, tag refspecs, and pushes to
  `main`, `master`, the remote's default branch or `CCGUARD_PROTECTED_BRANCHES`; a bare
  `git push` is resolved through `@{push}`. Scans the commits the push would send (messages,
  authors, added lines) and the title/body of `gh pr|issue` create, edit and comment for
  secret shapes (everywhere) and private identifiers (`/home/<you>/` plus the regexes in
  `~/.config/ccguard/push-gate.json`, except for its `skip_remotes`). Reports the location
  and pattern number only, never the matched text. A `git push` that cannot be parsed is
  denied; other failures fail open. `--dry-run` is never gated.
- `tests/test_push_gate.py`: 37 cases against throwaway repositories with a bare remote;
  token-shaped samples are assembled at runtime.

## [0.1.0] - 2026-06-23

### Added
- Initial scaffold of ccguard — deterministic PreToolUse guards shipped as a Claude Code plugin.
- **`block-ai-attribution`** (PreToolUse / Bash): deny a `git commit` whose message carries
  AI/assistant attribution or session metadata (Co-Authored-By, Generated with, …-Session,
  claude.ai/code/session, 🤖). Fail-open when `jq` is absent; the brand token is assembled at
  runtime so the guard's own source never trips literal scanners.
- `tests/test_block_ai_attribution.sh` — dependency-light self-tests (allowed vs blocked cases).
