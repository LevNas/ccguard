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

Denied:

| What | Why |
|---|---|
| `-f`, `--force`, `--force-with-lease`, `--force-if-includes`, `+refspec` | rewrites published history |
| `:branch`, `--delete`, `-d`, `--prune` | removes remote refs |
| `--all`, `--mirror`, `--tags`, `--follow-tags`, a tag refspec | publishes in bulk or releases |
| a push to `main`, `master`, the remote's default branch, or `CCGUARD_PROTECTED_BRANCHES` (comma-separated) | changes go in through a reviewed merge |
| a commit to be pushed, or `gh pr\|issue create\|edit\|comment` text, matching a content pattern | content cannot be unpublished |

A bare `git push` is resolved through `@{push}` (or the current branch), and `git -C <dir>`
and a preceding `cd <dir>` are followed. `--dry-run` sends nothing and is never gated.

**Content patterns.** Secret shapes (private keys; GitHub, Anthropic, AWS and Slack tokens;
raw 1Password item ids in `op://` references) block everywhere. Private identifiers block
except for remotes you mark as private:

```jsonc
// ~/.config/ccguard/push-gate.json  (outside every repository; never commit it)
{
  "private_patterns": ["<a work handle>", "<a private repository name>"],
  "skip_remotes": ["github\\.com[:/]<you>/<private-repo>"]
}
```

`/home/<you>/` is always a private identifier. Patterns are case-insensitive regexes. Only the
commits the remote does not have yet are scanned (up to 500). A denial names the commit, the
part (message, author, or file) and the pattern number — never the matched text, so the
message cannot spread the value it caught.

- **Fail-open**, except for a `git push` that cannot be parsed (denied: run it as a plain,
  separate command).
- **Scope and limits**: it reads the Bash *command string*. A push hidden inside
  `bash -c "…"`, a script file or an alias is not seen; keep the permission deny rules as a
  second layer.

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
