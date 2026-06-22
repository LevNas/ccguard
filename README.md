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

## Install

Via the marketplace:

```
/plugin marketplace add LevNas/claudecode-plugins
/plugin install ccguard
```

## Testing

```
bash tests/test_block_ai_attribution.sh
```

Dependency-light (needs `jq`, which the guard also requires).

## License

[MIT](LICENSE).
