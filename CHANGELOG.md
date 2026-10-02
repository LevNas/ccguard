# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-10-03

### Added
- **`push-gate`** (PreToolUse / Bash, `hooks/push-gate.py`): lets a push to a feature branch
  run without human confirmation by deciding deterministically what must never leave the
  machine.
  - Destinations are resolved by replaying the push as `git push --dry-run --porcelain`.
    Denied: forced updates, deletions, tags, refs outside `refs/heads/`, and `main`,
    `master`, the remote's default branch or `CCGUARD_PROTECTED_BRANCHES` (even a no-op
    push to them); also `--all`, `--branches`, `--mirror`, `--tags`, `--follow-tags`,
    `--force*`, `-f`, `-d`, `+refspec`, `:ref` and `--recurse-submodules=on-demand` on
    the command line.
  - Content: the commits the remote does not have yet (messages, authors, added lines,
    file names, merge conflict resolutions via `--diff-merges=remerge`) and the text `gh`
    would post (`gh pr|issue|release|gist` titles, bodies, notes, comments; `gh api`
    fields; the files and heredocs they read). Secret shapes block everywhere; private
    identifiers (`/home/<you>/` plus the regexes in `~/.config/ccguard/push-gate.json`)
    block except for its `skip_remotes`. Denials report the location and pattern number,
    never the matched text.
  - Shell parsing: newlines, `;`, `&&`, `||`, `|`, `&` and parentheses split commands;
    redirections and heredoc bodies are taken apart; a push or post wrapped in another
    command (`sudo`, `timeout`, `xargs`, ...) is denied.
  - Fails closed for commands that push or post: an unparseable command, a git failure or
    timeout, more than 500 new commits (`CCGUARD_PUSH_GATE_MAX_COMMITS`), or an invalid
    config is a denial. `--dry-run` is never gated.
- `tests/test_push_gate.py`: 80 checks against throwaway repositories with a bare remote,
  including the multi-line, heredoc, redirection, wrapper, glob-refspec, push.default,
  merge-resolution and commit-cap cases; token-shaped samples are assembled at runtime.

## [0.1.0] - 2026-06-23

### Added
- Initial scaffold of ccguard — deterministic PreToolUse guards shipped as a Claude Code plugin.
- **`block-ai-attribution`** (PreToolUse / Bash): deny a `git commit` whose message carries
  AI/assistant attribution or session metadata (Co-Authored-By, Generated with, …-Session,
  claude.ai/code/session, 🤖). Fail-open when `jq` is absent; the brand token is assembled at
  runtime so the guard's own source never trips literal scanners.
- `tests/test_block_ai_attribution.sh` — dependency-light self-tests (allowed vs blocked cases).
