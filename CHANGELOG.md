# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] - 2026-10-03

This release only tightens checks. Every command 0.2.0 denied is still denied (checked
against about 720 commands run through both versions).

### Fixed
- **push-gate: more ways to turn the pre-push gate off are denied next to git**:
  `CLAUDECODE+=x` (the gate needs exactly `1`), `export -n`, `declare +x`, `read`,
  `mapfile`, `printf -v` and `for CLAUDECODE in ...`, and `unset` or `export` split by a
  line continuation or a quoted `;`. The list is not complete: a name built at run time
  (`N=CLAUDECODE; unset $N`, `"CLAUDE""CODE"`) still goes through, as in 0.2.0.
- **push-gate: more writers count as writes next to ccguard's paths** when the command
  has no `>`: `mkdir`, `link`, `touch`, `patch`, `ed`/`ex`/`vi`, `curl`, `wget`,
  `rsync`, `scp`, `tar`, `unzip`, `cpio`, `sponge`, `awk`, `php`, `lua`, `tclsh`,
  `sort -o`, `git apply`/`am`/`config`, and `sed` with `--in-place` or combined flags
  (`-Ei`).
- **push-gate: a protected path split by a line continuation was not checked**; the
  joined text is now used both to decide whether to check and in the checks.

### Known
- **Reading ccguard's files with any redirect is denied** (#2): `ls <hooks dir> 2>&1`,
  `cat <config> > /tmp/copy`, a commit message written with `cat > "$F" <<'EOF'` that
  names the hooks path. Use the Read and Write tools, or drop the redirect. Exempting
  some `>` targets (fd duplications, `/dev/null`, `/tmp`, heredoc bodies as data) was
  tried in this release and dropped after three reviews: "any `>` is a write" also
  covers writers no list names (`git config --file`, `sort -o`, `sed w`, a link made
  first), and each exemption let one of them through.
- A quoted label such as `echo "CLAUDECODE=$CLAUDECODE"`, `export CLAUDECODE`, or a commit
  message that mentions `CLAUDECODE` after a word such as `read` or `export`, next to git,
  is denied. The check stays on the raw text so that wrappers the parser does not follow
  (`xargs sh -c`, `timeout sh -c`, `find -exec sh -c`) cannot hide a change.
- Writers that need neither `>` nor a listed word, and a protected path split across
  words (`cd ~/.local/share/ccguard; rm git-hooks/pre-push`), go through, as in 0.2.0.
  The guard stops mistakes of a cooperating agent; it is not a sandbox.

## [0.2.0] - 2026-10-03

### Added
- **Push gate**: lets a push to a feature branch run without human confirmation by
  deciding deterministically what must never leave the machine.
  - `hooks/pre_push_gate.py` (git `pre-push`, only when `CLAUDECODE=1`): denies forced
    (non-fast-forward) updates, deletions, tags, refs outside `refs/heads/`, and `main`,
    `master`, the remote's default branch (`refs/remotes/<remote>/HEAD`, else
    `ls-remote --symref`) or `CCGUARD_PROTECTED_BRANCHES`. Scans the commits the remote
    lacks: messages, authors, added lines, file names (renames and empty files included)
    and merge conflict resolutions (`--diff-merges=remerge`). git resolves aliases,
    wrappers, refspecs and push.default before calling the hook, so no shell parsing is
    involved.
  - `hooks/install_git_hooks.py` (SessionStart): writes the hooks to
    `~/.local/share/ccguard/git-hooks`, a path that survives plugin updates; every hook
    first runs the repository's own. Tells the session when `core.hooksPath` is not set
    (setting it is left to the user).
  - `hooks/push-gate.py` (PreToolUse / Bash): keeps the gate in force (denies
    `--no-verify` on a git command, writing `core.hooksPath`, clearing `CLAUDECODE` or the
    environment around git, `HOME`/`XDG_CONFIG_HOME`/`GIT_CONFIG_*` overrides around a
    push, pushes through sudo, `git send-pack`, writes to ccguard's own files, and pushes
    where the gate is not active) and checks the text gh would post (`pr|issue|release|
    gist|repo|label|variable|project`, `gh api` fields, files, redirected files and
    heredocs). Push detection works on parsed words (quoting such as `pu"sh"` and `-c`
    aliases included); `bash -c`, `sh -c` and `eval` strings are checked recursively.
    Command substitution is denied only where the shell would expand it; wrapped gh
    posts are denied; `gh release create`, `gh repo sync`, `gh alias set` and `gh api`
    writes to refs, contents, merges and releases (REST or GraphQL) are denied.
  - `hooks/protect-files.py` (PreToolUse / Edit|Write|MultiEdit|NotebookEdit): denies
    edits to ccguard's hooks directory, config and installed plugin.
  - `hooks/lib/ccguard_content.py`: content patterns shared by both gates. Secret shapes
    block everywhere; private identifiers (`/home/<you>/` plus the regexes in
    `~/.config/ccguard/push-gate.json`) block except for its `skip_remotes`. Denials report
    the location and pattern number, never the matched text.
  - Fails closed for pushes and posts: git failures, timeouts, more than 500 new commits
    (`CCGUARD_PUSH_GATE_MAX_COMMITS`), an invalid config or a crash deny.
- Tests: `tests/test_pre_push_gate.py` (48 checks) runs real `git push` commands against a
  bare remote (wrapped, continued, aliased, globbed and forced pushes; content cases
  including a NUL byte in a diff; hook chaining; the installer); `tests/test_push_gate.py`
  (86) covers the PreToolUse checks; `tests/test_protect_files.py` (5) the file guard.
  Token-shaped samples are assembled at runtime.

## [0.1.0] - 2026-06-23

### Added
- Initial scaffold of ccguard — deterministic PreToolUse guards shipped as a Claude Code plugin.
- **`block-ai-attribution`** (PreToolUse / Bash): deny a `git commit` whose message carries
  AI/assistant attribution or session metadata (Co-Authored-By, Generated with, …-Session,
  claude.ai/code/session, 🤖). Fail-open when `jq` is absent; the brand token is assembled at
  runtime so the guard's own source never trips literal scanners.
- `tests/test_block_ai_attribution.sh` — dependency-light self-tests (allowed vs blocked cases).
