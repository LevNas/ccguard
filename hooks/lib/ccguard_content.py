"""Content checks shared by the ccguard gates (push-gate, pre-push gate).

Secret shapes (private keys, GitHub, Anthropic, AWS and Slack tokens, raw
1Password item ids) block everywhere. Private identifiers — `/home/<you>/`
and the regexes under "private_patterns" in
${XDG_CONFIG_HOME:-~/.config}/ccguard/push-gate.json — block except for
destinations matching one of its "skip_remotes" regexes. The config lives
outside every repository, so the values it names are never committed. A hit
is reported by location and pattern number only; the matched text is never
printed, so a denial cannot spread the value it caught.
"""

import json
import os
import re
import secrets
import subprocess
import time

CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "ccguard", "push-gate.json",
)
MAX_COMMITS = int(os.environ.get("CCGUARD_PUSH_GATE_MAX_COMMITS") or 500)

SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    ("Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("1Password raw item id", r"op://[^/\s]+/[a-z0-9]{26}(?:/|\b)"),
]

_deadline = [time.monotonic() + 50]


class Deny(Exception):
    """Raised with the message to send back to Claude."""


def set_budget(seconds):
    _deadline[0] = time.monotonic() + seconds


def _remaining():
    left = _deadline[0] - time.monotonic()
    if left < 1:
        raise Deny("Ran out of time checking this push.")
    return left


# --------------------------------------------------------------------- git

def run_git(cwd, args, limit=20):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd,
                              capture_output=True, text=True, errors="replace",
                              timeout=min(limit, _remaining()), env=env)
    except subprocess.TimeoutExpired:
        raise Deny(f"`git {args[0]}` timed out while checking.") from None
    except OSError as e:
        raise Deny(f"Could not run git to check ({e.__class__.__name__}).") from None


def git_out(cwd, *args):
    """stdout of an optional lookup, or None when git says no."""
    proc = run_git(cwd, list(args), limit=15)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_must(cwd, args, what, limit=20):
    proc = run_git(cwd, args, limit)
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["no output"])[-1][:200]
        raise Deny(f"Could not {what} ({detail}).")
    return proc.stdout


# ------------------------------------------------------------------ config

def load_config():
    if not os.path.exists(CONFIG):
        return [], []
    try:
        with open(CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Deny(f"{CONFIG} cannot be read ({e.__class__.__name__}). Fix or remove it.") from None
    if not isinstance(data, dict):
        raise Deny(f"{CONFIG} must hold a JSON object.")
    lists = []
    for key in ("private_patterns", "skip_remotes"):
        compiled = []
        for i, p in enumerate(data.get(key) or [], 1):
            if not isinstance(p, str) or not p:
                raise Deny(f"{key} #{i} in {CONFIG} is not a non-empty string.")
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error:
                raise Deny(f"{key} #{i} in {CONFIG} is not a valid regex.") from None
        lists.append(compiled)
    return lists[0], lists[1]


def content_patterns(destination):
    """(label, compiled regex) pairs for a destination URL or repo ("" = unknown)."""
    patterns = [(label, re.compile(p)) for label, p in SECRET_PATTERNS]
    private, skip = load_config()
    if destination and any(s.search(destination) for s in skip):
        return patterns
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    if user and user != "root":
        patterns.append(("home path",
                         re.compile(r"/home/" + re.escape(user) + r"(?![A-Za-z0-9_.-])")))
    for i, regex in enumerate(private, 1):
        patterns.append((f"private pattern #{i} in {CONFIG}", regex))
    return patterns


def first_hit(text, patterns):
    for label, regex in patterns:
        if regex.search(text):
            return label
    return None


# ----------------------------------------------------------------- commits

def scan_commits(cwd, revs, target, patterns):
    """Deny when a commit in `revs` (rev-list arguments) carries a blocked pattern."""
    count = int(git_must(cwd, ["rev-list", "--count", *revs, "--"], "count the commits to push"))
    if count == 0:
        return
    if count > MAX_COMMITS:
        raise Deny(f"This push sends {count} commits, more than the {MAX_COMMITS} the gate "
                   "scans. Check them yourself and push by hand.")
    # A random marker separates commits: a fixed byte such as NUL can occur in a diff.
    mark = secrets.token_hex(16)
    log = git_must(cwd, ["log", "-p", "--no-color", "--no-ext-diff", "--diff-merges=remerge",
                         f"--format={mark}%n%h%n%an <%ae>%n%cn <%ce>%n%B{mark}", *revs, "--"],
                   "read the commits to push")
    parts = log.split(mark)  # ["", header, diff, header, diff, ...]
    for k in range(1, len(parts) - 1, 2):
        lines = parts[k].strip("\n").split("\n")
        commit = lines[0]
        where = {"author or committer": "\n".join(lines[1:3]), "message": "\n".join(lines[3:])}
        for part, text in where.items():
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {commit} ({part}) matches {label}. "
                           f"Rewrite the commit before pushing {target}.")
        for path, text in diff_items(parts[k + 1]):
            if text is None:
                label = first_hit(path, patterns)
                if label:
                    raise Deny(f"Commit {commit} has a file whose path matches {label}. "
                               f"Rename it before pushing {target}.")
                continue
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {commit} adds a line in {path} that matches {label}. "
                           f"Remove it before pushing {target}.")


def _diff_git_path(line):
    """The path of `diff --git a/P b/P` when both sides are the same path."""
    s = line[len("diff --git "):]
    if len(s) % 2 == 1 and s.startswith("a/"):
        half = (len(s) - 5) // 2
        if s[2:2 + half] == s[half + 5:] and s[half + 2:half + 5] == " b/":
            return s[2:2 + half]
    return None


def diff_items(diff):
    """Yield (path, None) for every file name a diff introduces, and
    (path, text) for every added line, tracking hunks rather than prefixes."""
    path, width = "?", 0
    for line in diff.split("\n"):
        if line.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
            width = 0
            path = _diff_git_path(line) or "?"
            if path != "?":
                yield path, None
            continue
        if line.startswith("@@"):
            width = len(re.match(r"@+", line).group()) - 1
            continue
        if width == 0:
            for prefix in ("rename to ", "copy to "):
                if line.startswith(prefix):
                    path = line[len(prefix):]
                    yield path, None
            if line.startswith("+++ b/"):
                path = line[6:]
                yield path, None
            continue
        prefix = line[:width]
        if "+" in prefix and "-" not in prefix:
            yield path, line[width:]
