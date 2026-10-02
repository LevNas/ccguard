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

### Push gate (git pre-push + PreToolUse / Bash)

Lets Claude push a feature branch without asking you, by deciding deterministically what
must never leave the machine. Human confirmation moves to where it matters: opening and
merging the pull request.

**Setup (once).** At session start ccguard writes its git hooks to
`~/.local/share/ccguard/git-hooks` (`$XDG_DATA_HOME`), a path that survives plugin updates.
Point git at it:

```
git config --global core.hooksPath ~/.local/share/ccguard/git-hooks
```

Every hook there first runs the repository's own hook (`.git/hooks/<name>`), so existing
hooks keep working. Until this is set, ccguard denies Claude's pushes and says so at session
start. A repository that sets its own `core.hooksPath` (husky, for example) does not get the
gate, so Claude's pushes there are denied too.

**What a push may update — the pre-push gate.** git calls `pre-push` with the refs it is about
to update, after every alias, wrapper (`bash -c`, `eval`), refspec, glob and push.default has
been resolved, so no shell parsing is involved. For pushes started by Claude Code
(`CLAUDECODE=1`; your own pushes are not touched) it denies:

| What | Why |
|---|---|
| a forced (non-fast-forward) update, however requested | rewrites published history |
| a deletion | removes remote refs |
| a tag | publishes a release |
| any ref outside `refs/heads/` | not a branch |
| `main`, `master`, the remote's default branch, `CCGUARD_PROTECTED_BRANCHES` (comma-separated) | changes go in through a reviewed merge |

**What a push may send.** The commits the remote does not have yet are scanned: messages,
authors, added lines, file names (including renames and empty files) and the conflict
resolutions of merge commits.

**Keeping the gate in force — PreToolUse.** ccguard denies `--no-verify` on a git command,
writing `core.hooksPath` (reading it is fine), clearing `CLAUDECODE` or the environment
around git, overriding `HOME`, `XDG_CONFIG_HOME` or `GIT_CONFIG_*` around a push, a push
through `sudo`, `git send-pack`, and writes to ccguard's hooks directory, config or plugin
files (from Bash, and from the Edit/Write tools via `protect-files.py`). `bash -c`, `sh -c`
and `eval` strings are checked like the command itself.

**gh posts — PreToolUse.** gh has no hook, so the text it would post is checked before the
command runs: titles, bodies, notes, comments and descriptions of
`gh pr|issue|release|gist|repo|label|variable|project`, `gh api` fields, and the files and
heredocs they read. Text that cannot be read first — stdin from a pipe, `$(...)`, backticks —
is denied, as is a gh post wrapped in another command. The destination comes from `-R`,
`GH_REPO`, the `gh api` endpoint or gist, else `origin`. Command substitution is denied only
where the shell would expand it (not inside single quotes), and
`--body "$(cat <<'EOF' … EOF)"` is read from its heredoc. `gh release create`,
`gh repo sync`, `gh alias set` and `gh api` writes to refs, contents, merges and releases
(REST, or GraphQL ref and merge mutations) are denied: they change a repository the way a
push would. `gh pr merge` is left to your permission rules — put it under `ask`.

**Content patterns.** Secret shapes (private keys; GitHub, Anthropic, AWS and Slack tokens;
raw 1Password item ids in `op://` references) block everywhere. Private identifiers block
except for destinations you mark as private:

```jsonc
// ~/.config/ccguard/push-gate.json  (outside every repository; never commit it)
{
  "private_patterns": ["<a work handle>", "<a private repository name>"],
  "skip_remotes": ["github\\.com[:/]<you>/<private-repo>"]
}
```

`/home/<you>/` is always a private identifier. Patterns are case-insensitive regexes. A denial
names the commit or option, the part and the pattern number — never the matched text, so the
message cannot spread the value it caught.

- **Fails closed** for a push or a post: a git failure or timeout, more than 500 new commits
  (`CCGUARD_PUSH_GATE_MAX_COMMITS`), an invalid config or a crash of the gate is a denial.
  Other commands are never touched.
- **Threat model**: ccguard stops a cooperating agent's mistakes and shortcuts. It is not a
  sandbox: an agent with a shell that is set on defeating it can (a script file that runs
  `git push --no-verify`, for one). For a public repository, server-side branch protection
  on the default branch and GitHub push protection are the real guarantee; keep them on,
  and keep the permission deny rules as another layer.
- **Side effects of a global `core.hooksPath`**: every git hook call starts a small shell
  that looks for the repository's own hook, a few milliseconds each. The pre-commit
  framework's `pre-commit install` refuses to run while `core.hooksPath` is set; a global
  hooks directory you had before is replaced, not chained.

## Install

Via the marketplace:

```
/plugin marketplace add LevNas/claudecode-plugins
/plugin install ccguard
```

## Testing

```
bash tests/test_block_ai_attribution.sh
python3 tests/test_pre_push_gate.py
python3 tests/test_push_gate.py
python3 tests/test_protect_files.py
```

Dependency-light: `jq` for `block-ai-attribution`; `python3` and `git` for the push gate.

## License

[MIT](LICENSE).
