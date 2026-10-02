#!/usr/bin/env python3
"""PreToolUse(Bash) guard — gate what leaves the machine through git push and gh.

A push to a feature branch needs no human confirmation; what must never
happen is decided here, deterministically, before the command runs:

- pushes that rewrite or remove published history, or publish a release:
  force (`-f`, `--force*`, `+refspec`), deletion (`:branch`, `--delete`),
  `--all`, `--mirror`, `--tags`, `--follow-tags`, and tag refspecs;
- pushes to a protected branch: `main`, `master`, the remote's default
  branch, and any listed in CCGUARD_PROTECTED_BRANCHES (comma-separated).
  A bare `git push` is resolved through `@{push}` (or the current branch);
- content that must not be published: in the commits the push would send
  (messages, authors, added lines) and in the title/body of `gh pr|issue`
  create, edit and comment.

Content patterns come in two kinds. Secret shapes (private keys, GitHub,
Anthropic, AWS and Slack tokens, raw 1Password item ids) block everywhere.
Private identifiers — `/home/<you>/` and the regexes listed under
"private_patterns" in ${XDG_CONFIG_HOME:-~/.config}/ccguard/push-gate.json —
block except for remotes matching one of its "skip_remotes" regexes (private
repositories, where such names are expected). The config file lives outside
every repository, so the values it names are never committed anywhere. A hit
is reported by location and pattern number only; the matched text is never
printed, so the denial message cannot spread the value it caught.

Exit 2 denies the tool call (stderr goes back to Claude); exit 0 allows it.
A `git push` command that cannot be parsed is denied; any other failure
fails open, like the rest of ccguard.
"""

import json
import os
import re
import shlex
import subprocess
import sys

CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "ccguard", "push-gate.json",
)
MAX_COMMITS = 500
OPERATORS = {"&&", "||", ";", "|", "&", ";;", "|&"}
WRAPPERS = {"command", "env", "nohup", "time", "exec"}

SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    ("Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("1Password raw item id", r"op://[^/\s]+/[a-z0-9]{26}(?:/|\b)"),
]


class Deny(Exception):
    """Raised with the message to send back to Claude."""


def git(cwd, *args):
    """stdout of a git command in cwd, or None when it fails."""
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    private = [p for p in data.get("private_patterns") or [] if isinstance(p, str) and p]
    skip = [p for p in data.get("skip_remotes") or [] if isinstance(p, str) and p]
    return private, skip


def content_patterns(remote_url):
    """(label, compiled regex) pairs that apply to a destination."""
    patterns = [(label, re.compile(p)) for label, p in SECRET_PATTERNS]
    private, skip = load_config()
    if remote_url and any(re.search(s, remote_url) for s in skip):
        return patterns
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    if user and user != "root":
        patterns.append(("home path", re.compile(r"/home/" + re.escape(user) + r"(?:/|\b)")))
    for i, p in enumerate(private, 1):
        try:
            patterns.append((f"private pattern #{i} in {CONFIG}", re.compile(p, re.IGNORECASE)))
        except re.error:
            continue
    return patterns


def first_hit(text, patterns):
    for label, regex in patterns:
        if regex.search(text):
            return label
    return None


# ---------------------------------------------------------------- parsing

def segments(command):
    """The command split into simple commands (lists of words)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    current = []
    for token in lexer:
        if token in OPERATORS:
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def command_words(words):
    """Drop leading VAR=value assignments and wrapper commands."""
    i = 0
    while i < len(words) and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i])
                              or words[i] in WRAPPERS):
        i += 1
    return words[i:]


def split_git(words, cwd):
    """(working dir, subcommand, args) of a git invocation."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            cwd = os.path.join(cwd, os.path.expanduser(words[i + 1]))
            i += 2
        elif w in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return cwd, w, words[i + 1:]
    return cwd, None, []


# ------------------------------------------------------------------- push

VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
BROAD = {"--all", "--mirror", "--tags", "--follow-tags", "--delete", "--prune"}


def current_branch(cwd):
    return git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")


def protected_branches(cwd, remote):
    names = {"main", "master"}
    names.update(b.strip() for b in os.environ.get("CCGUARD_PROTECTED_BRANCHES", "").split(",")
                 if b.strip())
    default = git(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if default and "/" in default:
        names.add(default.split("/", 1)[1])
    return names


def check_push(cwd, args):
    remote, refspecs, dry_run, denial = None, [], False, None
    i = 0
    while i < len(args):
        a = args[i]
        if a in VALUE_OPTIONS:
            i += 2
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name.startswith("--force") or name in BROAD:
                denial = denial or (f"`{name}` rewrites, removes or bulk-publishes refs. Push "
                                    "one feature branch without it; publish through a reviewed merge.")
            if name == "--dry-run":
                dry_run = True
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if "f" in letters:
                denial = denial or "`-f` (force) rewrites published history. Push without it."
            if "d" in letters:
                denial = denial or "`-d` deletes a remote branch. Leave branch deletion to the user."
            if "n" in letters:
                dry_run = True
        elif remote is None:
            remote = a
        else:
            refspecs.append(a)
        i += 1
    if dry_run:  # sends nothing, so nothing to gate
        return
    if denial:
        raise Deny(denial)

    branch = current_branch(cwd)
    if remote is None:
        target = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        if target and "/" in target:
            remote, dst = target.split("/", 1)
        else:
            remote, dst = "origin", branch
        if not dst:
            raise Deny("Cannot tell which branch a bare `git push` would update "
                       "(detached HEAD). Name the remote and branch.")
        pairs = [("HEAD", dst)]
    else:
        pairs = []
        if not refspecs:
            target = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
            dst = target.split("/", 1)[1] if target and "/" in target else branch
            if not dst:
                raise Deny("Cannot tell which branch this push would update. Name the branch.")
            pairs.append(("HEAD", dst))
        for spec in refspecs:
            if spec.startswith("+"):
                raise Deny(f"`{spec}` is a forced refspec. Push without the `+`.")
            src, sep, dst = spec.partition(":")
            if sep and not src:
                raise Deny(f"`{spec}` deletes a remote ref. Leave deletion to the user.")
            if not sep:
                dst = src
            if dst.startswith("refs/tags/") or git(cwd, "show-ref", "--verify", "--quiet",
                                                   f"refs/tags/{src}") is not None:
                raise Deny(f"`{spec}` pushes a tag, which publishes a release. "
                           "Leave tags to the release step.")
            dst = dst.removeprefix("refs/heads/")
            if dst == "HEAD":
                dst = branch or "HEAD"
            pairs.append((src or "HEAD", dst))

    protected = protected_branches(cwd, remote)
    for _src, dst in pairs:
        if dst == "HEAD":
            raise Deny("`HEAD` is detached, so the branch this push would update is "
                       "unknown. Name the destination branch.")
        if dst in protected:
            raise Deny(f"Pushing to `{remote}/{dst}` updates a protected branch. Push a "
                       "feature branch and open a pull request; merging is the user's call.")

    url = git(cwd, "remote", "get-url", remote) or (remote if "/" in remote else "")
    patterns = content_patterns(url)
    for src, dst in pairs:
        scan_commits(cwd, remote, src, dst, patterns)


def scan_commits(cwd, remote, src, dst, patterns):
    """Deny when a commit the push would send carries a blocked pattern."""
    log = git(cwd, "log", "-p", "--no-color", "--no-ext-diff", f"-n{MAX_COMMITS}",
              "--format=%x00%h%n%an <%ae>%n%cn <%ce>%n%B%x00", src, "--not",
              f"--remotes={remote}")
    if not log:
        return
    parts = log.split("\x00")  # ["", header, diff, header, diff, ...]
    for k in range(1, len(parts) - 1, 2):
        header, diff = parts[k], parts[k + 1]
        lines = header.split("\n")
        sha = lines[0]
        where = {
            "author or committer": "\n".join(lines[1:3]),
            "message": "\n".join(lines[3:]),
        }
        path = "?"
        added = []
        for line in diff.split("\n"):
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("+") and not line.startswith("+++"):
                added.append((path, line[1:]))
        for part, text in where.items():
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {sha} ({part}) matches {label}. "
                           f"Rewrite the commit before pushing to {remote}/{dst}.")
        for p, text in added:
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {sha} adds a line in {p} that matches {label}. "
                           f"Remove it before pushing to {remote}/{dst}.")


# --------------------------------------------------------------------- gh

GH_TEXT = {("pr", "create"), ("pr", "edit"), ("pr", "comment"), ("pr", "review"),
           ("issue", "create"), ("issue", "edit"), ("issue", "comment")}


def check_gh(cwd, args):
    if len(args) < 2 or (args[0], args[1]) not in GH_TEXT:
        return
    texts, repo = [], None
    i = 2
    while i < len(args):
        a, value = args[i], args[i + 1] if i + 1 < len(args) else ""
        name, eq, inline = a.partition("=")
        if eq:
            value = inline
        step = 1 if eq else 2
        if name in ("-t", "--title", "-b", "--body"):
            texts.append((name, value))
            i += step
        elif name in ("-F", "--body-file"):
            if value and value != "-":
                path = os.path.join(cwd, os.path.expanduser(value))
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        texts.append((name, f.read()))
                except OSError:
                    pass
            i += step
        elif name in ("-R", "--repo"):
            repo = value
            i += step
        else:
            i += 1
    url = f"github.com/{repo}" if repo else (git(cwd, "remote", "get-url", "origin") or "")
    patterns = content_patterns(url)
    for name, text in texts:
        label = first_hit(text, patterns)
        if label:
            raise Deny(f"The `{name}` text of `gh {args[0]} {args[1]}` matches {label}. "
                       "Rewrite it before posting.")


# ------------------------------------------------------------------- main

def check(command, cwd):
    try:
        parsed = list(segments(command))
    except ValueError:
        if re.search(r"\bgit\b.*\bpush\b", command):
            raise Deny("This command runs `git push` but cannot be parsed (unbalanced "
                       "quotes?). Run the push as a separate, plain command.")
        return
    for words in parsed:
        words = command_words(words)
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "cd":
            target = words[1] if len(words) > 1 else os.path.expanduser("~")
            cwd = os.path.join(cwd, os.path.expanduser(target))
        elif name == "git":
            where, sub, args = split_git(words, cwd)
            if sub == "push":
                check_push(where, args)
        elif name == "gh":
            check_gh(cwd, words[1:])


def main():
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
    except (ValueError, AttributeError):
        return 0
    if not re.search(r"\b(git|gh)\b", command):
        return 0
    try:
        check(command, cwd)
    except Deny as denial:
        print(f"BLOCKED (ccguard push-gate): {denial}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 — fail open, like the rest of ccguard
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
