# ccguard

Deterministic PreToolUse guards for Claude Code — the things that must *never* happen,
enforced in a hook instead of in prose.

## Why

Per Anthropic's "Steering Claude Code" guidance, policies that must hold **every time** belong in
a deterministic hook: prose in `CLAUDE.md` / rules is followed only probabilistically. ccguard ships
those guards as a plugin so they apply in every environment **without touching your (public)
dotfiles** — a better home than a machine-local `settings.local.json` entry.

## Guards

### `block-ai-attribution` (PreToolUse / Bash)

Denies a `git commit` whose message carries AI/assistant attribution or session metadata —
`Co-Authored-By: <assistant>`, `Generated with … <assistant>`, `<assistant>-Session:`,
`claude.ai/code/session…`, or the 🤖 marker. Exit 2 blocks the tool call and tells Claude to remove
the trailer and commit again.

- **Fail-open**: if `jq` is absent the guard does nothing (any policy you document still stands).
- **Self-safe**: the brand token is assembled at runtime, so the guard's own source never trips
  literal scanners — including a future run of the guard over ccguard's own commits.
- **Scope**: it inspects the *command string* of the Bash tool call, not file contents, so it only
  fires on actual `git commit` invocations.

### `push-gate` (PreToolUse / Bash)

Lets Claude push a feature branch without asking you, by deciding deterministically what
must never leave the machine. Human confirmation moves to where it matters: opening and
merging the pull request.

**Where a push goes is asked of git, not guessed.** The push is replayed as
`git push --dry-run --porcelain`, which reports every ref it would update, whether the update
is forced or a deletion, and the URL it goes to — through push.default, `@{push}`, globs and
any refspec. Denied:

| What | Why |
|---|---|
| a forced update: `-f`, `--force*`, `+refspec`, or any update git reports as forced | rewrites published history |
| a deletion: `:branch`, `--delete`, `-d`, `--prune` | removes remote refs |
| `--all`, `--branches`, `--mirror`, `--tags`, `--follow-tags`, a tag | publishes in bulk or releases |
| any ref outside `refs/heads/` | not a branch |
| `main`, `master`, the remote's default branch, `CCGUARD_PROTECTED_BRANCHES` (comma-separated) — even when the push would change nothing | changes go in through a reviewed merge |
| `--recurse-submodules` other than `check`/`no` | pushes other repositories unchecked |

`--dry-run` sends nothing and is never gated. The probe costs one round trip to the remote
(about two seconds).

**Content.** Scanned are the commits the remote does not have yet — messages, authors, added
lines, file names, and the conflict resolutions of merge commits — and the text `gh` would post:
titles, bodies, notes and comments of `gh pr|issue|release|gist`, `gh api` fields, and the files
and heredocs they read. Secret shapes (private keys; GitHub, Anthropic, AWS and Slack tokens;
raw 1Password item ids in `op://` references) block everywhere. Private identifiers block
except for remotes you mark as private:

```jsonc
// ~/.config/ccguard/push-gate.json  (outside every repository; never commit it)
{
  "private_patterns": ["<a work handle>", "<a private repository name>"],
  "skip_remotes": ["github\\.com[:/]<you>/<private-repo>"]
}
```

`/home/<you>/` is always a private identifier. Patterns are case-insensitive regexes. A denial
names the commit, the part (message, author, file name or line) and the pattern number —
never the matched text, so the message cannot spread the value it caught.

**Shell commands.** The command is split on newlines, `;`, `&&`, `||`, `|`, `&` and
parentheses; redirections and heredoc bodies are taken apart, and `git -C <dir>` and a
preceding `cd <dir>` are followed. A push or post wrapped in another command (`sudo`,
`timeout`, `xargs`, …) is denied: run it as a plain command of its own.

- **Fails closed** for a command that pushes or posts: an unparseable command, a git
  failure or timeout, more than 500 new commits (`CCGUARD_PUSH_GATE_MAX_COMMITS`), or an
  invalid config file is a denial. Other commands are never touched.
- **Limits**: it reads the Bash *command string*. A push inside `bash -c "…"`, a script file
  or an alias is not seen; keep the permission deny rules as a second layer.

## Install

Via the marketplace:

```
/plugin marketplace add LevNas/claudecode-plugins
/plugin install ccguard
```

## Testing

```
bash tests/test_block_ai_attribution.sh
python3 tests/test_push_gate.py
```

Dependency-light: `jq` for `block-ai-attribution`; `python3` and `git` for `push-gate`.

## License

[MIT](LICENSE).
