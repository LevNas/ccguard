#!/usr/bin/env python3
"""PreToolUse(Bash) guard — gate what leaves the machine through git push and gh.

A push to a feature branch needs no human confirmation; what must never
happen is decided here, deterministically, before the command runs.

Destinations are resolved by git itself: the push is replayed as
`git push --dry-run --porcelain`, which reports every ref it would update,
whether the update is forced or a deletion, and the URL it goes to. That
covers push.default, @{push}, globs and odd refspecs without guessing.
Denied are forced updates, deletions, tags, anything outside refs/heads/,
and updates of `main`, `master`, the remote's default branch or
CCGUARD_PROTECTED_BRANCHES (comma-separated). Force, delete, bulk and tag
options are also denied from the command line before git is asked.

Content is scanned in the commits the push would send that the remote does
not have yet (messages, authors, added lines, file names, and the conflict
resolutions of merges), and in the text that `gh` would post (titles,
bodies, notes, comments, fields, the files and heredocs they read).
Secret shapes (private keys, GitHub, Anthropic, AWS and Slack tokens, raw
1Password item ids) block everywhere. Private identifiers — `/home/<you>/`
and the regexes under "private_patterns" in
${XDG_CONFIG_HOME:-~/.config}/ccguard/push-gate.json — block except for
remotes matching one of its "skip_remotes" regexes. The config file lives
outside every repository, so the values it names are never committed. A
hit is reported by location and pattern number only; the matched text is
never printed, so the denial cannot spread the value it caught.

The shell command is split on newlines, `;`, `&&`, `||`, `|`, `&` and
parentheses, with redirections and heredoc bodies taken apart. A git push
or gh post that is not the command of its own segment (wrapped in sudo,
timeout, xargs, ...) is denied: run it as a plain command.

Fails closed: for a command that pushes or posts, anything that cannot be
checked (unparseable command, git failure, timeout, too many commits,
invalid config) is denied. Other commands are never touched.

Exit 2 denies the tool call (stderr goes back to Claude); exit 0 allows it.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time

CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "ccguard", "push-gate.json",
)
MAX_COMMITS = int(os.environ.get("CCGUARD_PUSH_GATE_MAX_COMMITS") or 500)
BUDGET_S = 50  # hooks.json allows 60
DEADLINE = time.monotonic() + BUDGET_S

PUNCT = ";&|<>()\n"
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
PREFIXES = {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until",
            "time", "command", "builtin", "nohup", "exec"}
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

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


# ------------------------------------------------------------------ helpers

def remaining():
    left = DEADLINE - time.monotonic()
    if left < 1:
        raise Deny("Ran out of time checking this push. Push fewer refs at once.")
    return left


def run_git(cwd, args, limit=20):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              errors="replace", timeout=min(limit, remaining()), env=env)
    except subprocess.TimeoutExpired:
        raise Deny(f"`git {args[0]}` timed out while checking this push.") from None
    except OSError as e:
        raise Deny(f"Could not run git to check this push ({e.__class__.__name__}).") from None


def git_out(cwd, *args):
    """stdout of an optional lookup, or None when git says no."""
    proc = run_git(cwd, list(args), limit=10)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_must(cwd, args, what, limit=20):
    proc = run_git(cwd, args, limit)
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["no output"])[-1][:200]
        raise Deny(f"Could not {what} ({detail}).")
    return proc.stdout


# ------------------------------------------------------------------- config

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
    """(label, compiled regex) pairs that apply to a destination URL or repo."""
    patterns = [(label, re.compile(p)) for label, p in SECRET_PATTERNS]
    private, skip = load_config()
    if destination and any(s.search(destination) for s in skip):
        return patterns
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    if user and user != "root":
        patterns.append(("home path", re.compile(r"/home/" + re.escape(user) + r"(?![A-Za-z0-9_.-])")))
    for i, regex in enumerate(private, 1):
        patterns.append((f"private pattern #{i} in {CONFIG}", regex))
    return patterns


def first_hit(text, patterns):
    for label, regex in patterns:
        if regex.search(text):
            return label
    return None


# ------------------------------------------------------------------ parsing

def split_heredocs(command):
    """The command without heredoc bodies, and the bodies."""
    lines = command.split("\n")
    kept, bodies = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            dash, delim = m.group(1), m.group(3)
            j, body = i, []
            while j < len(lines) and (lines[j].lstrip("\t") if dash else lines[j]) != delim:
                body.append(lines[j])
                j += 1
            if j >= len(lines):  # no terminator: keep the rest as commands
                break
            bodies.append("\n".join(body))
            i = j + 1
    return "\n".join(kept), bodies


def segments(command):
    """Yield (words, input files) per simple command; redirections removed."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCT)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    words, inputs, target = [], [], None
    for tok in lexer:
        if target is not None:
            if target == "<":
                inputs.append(tok)
            target = None
            continue
        if tok and all(c in PUNCT for c in tok):
            if "<" in tok or ">" in tok:
                if words and words[-1].isdigit():
                    words.pop()  # the fd of 2>&1
                if not tok.endswith("("):
                    target = "<" if tok in ("<", "<>") else ">"
                continue
            if words:
                yield words, inputs
            words, inputs = [], []
            continue
        tok = tok.strip("`")
        if tok:
            words.append(tok)
    if words:
        yield words, inputs


def command_words(words):
    """Drop assignments, shell keywords and option-less wrappers in front."""
    i = 0
    while i < len(words):
        w = words[i]
        if ASSIGN.fullmatch(w) or w in PREFIXES:
            i += 1
        elif w == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or ASSIGN.fullmatch(words[i])):
                i += 1
        else:
            break
    return words[i:]


def split_git(words, cwd):
    """(working dir, global options, subcommand, args) of a git invocation."""
    opts, i = [], 1
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            cwd = os.path.join(cwd, os.path.expanduser(words[i + 1]))
            i += 2
        elif w in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
            opts += words[i:i + 2]
            i += 2
        elif w.startswith("-"):
            opts.append(w)
            i += 1
        else:
            return cwd, opts, w, words[i + 1:]
    return cwd, opts, None, []


# --------------------------------------------------------------------- push

VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
BROAD = {"--all", "--branches", "--mirror", "--tags", "--follow-tags", "--delete", "--prune"}
PROBE_DROP = {"--set-upstream", "--verify", "--no-verify", "--porcelain", "--progress"}


def check_push(cwd, gopts, args):
    filtered, positional, denial, dry_run = [], [], None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a in VALUE_OPTIONS:
            filtered += args[i:i + 2]
            i += 2
            continue
        if a.startswith("--"):
            name, _, value = a.partition("=")
            if name.startswith("--force") or name in BROAD:
                denial = denial or (f"`{name}` rewrites, removes or bulk-publishes refs. Push "
                                    "one feature branch without it; publish through a reviewed merge.")
            elif name == "--recurse-submodules" and value not in ("check", "no"):
                denial = denial or "`--recurse-submodules` pushes other repositories unchecked."
            elif name == "--dry-run":
                dry_run = True
            if name not in PROBE_DROP:
                filtered.append(a)
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if "f" in letters:
                denial = denial or "`-f` (force) rewrites published history. Push without it."
            if "d" in letters:
                denial = denial or "`-d` deletes a remote branch. Leave branch deletion to the user."
            if "n" in letters:
                dry_run = True
            rest = letters.replace("u", "")
            if rest:
                filtered.append("-" + rest)
        else:
            positional.append(a)
            filtered.append(a)
        i += 1
    if dry_run:  # sends nothing, so nothing to gate
        return
    if denial:
        raise Deny(denial)
    for spec in positional[1:]:
        if spec.startswith("+"):
            raise Deny(f"`{spec}` is a forced refspec. Push without the `+`.")
        if spec.startswith(":"):
            raise Deny(f"`{spec}` deletes a remote ref. Leave deletion to the user.")

    probe = run_git(cwd, [*gopts, "push", "--dry-run", "--porcelain", "--no-verify", *filtered])
    url, refs = None, []
    for line in probe.stdout.splitlines():
        if line.startswith("To "):
            url = line[3:].strip()
        elif "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 2 and ":" in parts[1]:
                src, dst = parts[1].split(":", 1)
                refs.append((parts[0].strip() or " ", src, dst))
    if not refs:
        detail = (probe.stderr.strip().splitlines() or ["no refs to push"])[-1][:200]
        raise Deny(f"`git push --dry-run` could not tell what this push would update ({detail}).")

    remote = resolve_remote(cwd, positional[0] if positional else None)
    protected = {"main", "master"}
    protected.update(b.strip() for b in os.environ.get("CCGUARD_PROTECTED_BRANCHES", "").split(",")
                     if b.strip())
    if remote:
        default = git_out(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if default and "/" in default:
            protected.add(default.split("/", 1)[1])

    patterns = None
    for flag, src, dst in refs:
        # Destinations are checked on every line, up to date or not: aiming
        # a push at main is denied even when it would change nothing.
        if flag == "-":
            raise Deny(f"This push deletes `{dst}`. Leave deletion to the user.")
        if flag == "+":
            raise Deny(f"This push force-updates `{dst}`. Push without rewriting history.")
        if dst.startswith("refs/tags/"):
            raise Deny(f"This push creates or moves the tag `{dst[10:]}`, which publishes a "
                       "release. Leave tags to the release step.")
        if not dst.startswith("refs/heads/"):
            raise Deny(f"This push updates `{dst}`, outside refs/heads/. Push a branch.")
        branch = dst[len("refs/heads/"):]
        if branch in protected:
            raise Deny(f"This push updates the protected branch `{branch}`. Push a feature "
                       "branch and open a pull request; merging is the user's call.")
        if flag in ("=", "!"):  # nothing sent: up to date, or rejected by the remote
            continue
        if patterns is None:
            patterns = content_patterns(url or "")
        scan_commits(cwd, remote, src, branch, patterns)


def resolve_remote(cwd, arg):
    """The configured remote this push goes to, or None for a URL."""
    if arg is not None:
        return arg if git_out(cwd, "config", "--get", f"remote.{arg}.url") else None
    branch = git_out(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    for key in ([f"branch.{branch}.pushRemote"] if branch else []) + ["remote.pushDefault"] + \
               ([f"branch.{branch}.remote"] if branch else []):
        value = git_out(cwd, "config", "--get", key)
        if value:
            return value
    return "origin" if git_out(cwd, "config", "--get", "remote.origin.url") else None


def scan_commits(cwd, remote, src, branch, patterns):
    """Deny when a commit the push would send carries a blocked pattern."""
    sha = git_must(cwd, ["rev-parse", "--verify", "--end-of-options", f"{src}^{{commit}}"],
                   f"resolve `{src}`").strip()
    revs = [sha, "--not", f"--remotes={remote}"] if remote else [sha]
    count = int(git_must(cwd, ["rev-list", "--count", *revs, "--"], "count the commits to push"))
    if count == 0:
        return
    if count > MAX_COMMITS:
        raise Deny(f"This push sends {count} commits, more than the {MAX_COMMITS} the gate scans. "
                   "Check them yourself and push by hand.")
    log = git_must(cwd, ["log", "-p", "--no-color", "--no-ext-diff", "--diff-merges=remerge",
                         "--format=%x00%h%n%an <%ae>%n%cn <%ce>%n%B%x00", *revs, "--"],
                   "read the commits to push")
    parts = log.split("\x00")  # ["", header, diff, header, diff, ...]
    for k in range(1, len(parts) - 1, 2):
        lines = parts[k].split("\n")
        commit = lines[0]
        where = {"author or committer": "\n".join(lines[1:3]), "message": "\n".join(lines[3:])}
        for part, text in where.items():
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {commit} ({part}) matches {label}. "
                           f"Rewrite the commit before pushing `{branch}`.")
        for path, text in added_lines(parts[k + 1]):
            if first_hit(path, patterns):
                raise Deny(f"Commit {commit} has a file whose path matches "
                           f"{first_hit(path, patterns)}. Rename it before pushing `{branch}`.")
            label = first_hit(text, patterns)
            if label:
                raise Deny(f"Commit {commit} adds a line in {path} that matches {label}. "
                           f"Remove it before pushing `{branch}`.")


def added_lines(diff):
    """(path, text) for every added line; the path alone as ("path", "")."""
    path, width = "?", 0
    for line in diff.split("\n"):
        if line.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
            path, width = "?", 0
            continue
        if line.startswith("@@"):
            width = len(re.match(r"@+", line).group()) - 1
            continue
        if width == 0:
            if line.startswith("+++ ") and line != "+++ /dev/null":
                path = line[6:] if line.startswith("+++ b/") else line[4:]
                yield path, ""
            continue
        prefix = line[:width]
        if "+" in prefix and "-" not in prefix:
            yield path, line[width:]


# ----------------------------------------------------------------------- gh

GH_TEXT = {"pr": {"create", "edit", "comment", "review", "close", "merge", "reopen"},
           "issue": {"create", "edit", "comment", "close", "reopen"},
           "release": {"create", "edit"},
           "gist": {"create", "edit"},
           "api": None}
TEXT_FLAGS = {"-t", "--title", "-b", "--body", "-n", "--notes", "--subject", "-c",
              "--comment", "-d", "--desc", "--description", "-m", "--message"}
FILE_FLAGS = {"-F", "--body-file", "--notes-file", "--input"}
API_FIELDS = {"-f", "--raw-field", "-F", "--field"}


def gh_subcommand(args):
    """(group, action) when args post text, else None."""
    if not args or args[0] not in GH_TEXT:
        return None
    actions = GH_TEXT[args[0]]
    if actions is None:
        return args[0], ""
    if len(args) > 1 and args[1] in actions:
        return args[0], args[1]
    return None


def check_gh(cwd, args, inputs, bodies):
    sub = gh_subcommand(args)
    if not sub:
        return
    group, _action = sub
    texts, files, repo = [], list(inputs), None
    i = 1 if group == "api" else 2
    while i < len(args):
        a = args[i]
        name, eq, inline = a.partition("=") if a.startswith("--") else (a, "", "")
        if not eq and len(a) > 2 and a[0] == "-" and a[1] != "-":
            name, eq, inline = a[:2], "=", a[2:]  # -bTEXT
        value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
        step = 1 if eq else 2
        if not eq and re.fullmatch(r"-\S+", value):  # `-d --title x`: -d takes no value here
            value, step = "", 1
        if name in ("-R", "--repo"):
            repo = value
        elif group == "api" and name in API_FIELDS:
            field = value.partition("=")[2]
            if field.startswith("@"):
                files.append(field[1:])
            else:
                texts.append((name, field))
        elif name in FILE_FLAGS:
            files.append(value)
        elif name in TEXT_FLAGS:
            texts.append((name, value))
        elif a.startswith("-"):
            step = 1
        else:
            if group == "gist":
                files.append(a)
            step = 1
        i += step
    texts += [("heredoc", b) for b in bodies]
    for path in files:
        if not path or path == "-":
            continue
        full = os.path.join(cwd, os.path.expanduser(path))
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                texts.append((f"file {path}", f.read(1 << 20)))
        except OSError:
            raise Deny(f"Cannot read `{path}`, which `gh {group}` would post.") from None
    if repo:
        destination = repo if repo.count("/") >= 2 else f"github.com/{repo}"
    else:
        destination = git_out(cwd, "remote", "get-url", "origin") or ""
    patterns = content_patterns(destination)
    for where, text in texts:
        label = first_hit(text, patterns)
        if label:
            raise Deny(f"The {where} text that `gh {group}` would post matches {label}. "
                       "Rewrite it before posting.")


# --------------------------------------------------------------------- main

def check(command, cwd):
    stripped, bodies = split_heredocs(command)
    for words, inputs in segments(stripped):
        cmd = command_words(words)
        if not cmd:
            continue
        name = os.path.basename(cmd[0])
        if name == "cd":
            target = cmd[1] if len(cmd) > 1 else os.path.expanduser("~")
            cwd = os.path.join(cwd, os.path.expanduser(target))
        elif name == "git":
            where, gopts, sub, args = split_git(cmd, cwd)
            if sub == "push":
                check_push(where, gopts, args)
        elif name == "gh":
            check_gh(cwd, cmd[1:], inputs, bodies)
        else:
            names = [os.path.basename(w) for w in cmd]
            for k, n in enumerate(names):
                if (n == "git" and "push" in cmd[k + 1:]) or (n == "gh" and gh_subcommand(cmd[k + 1:])):
                    raise Deny(f"`{n}` runs inside `{cmd[0]}`, where the gate cannot check it. "
                               "Run it as a plain command of its own.")


def relevant(command):
    return re.search(r"\bgit\b[\s\S]*\bpush\b", command) or re.search(r"\bgh\b", command)


def main():
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
    except (ValueError, AttributeError):
        return 0
    if not relevant(command):
        return 0
    try:
        check(command, cwd)
    except Deny as denial:
        print(f"BLOCKED (ccguard push-gate): {denial}", file=sys.stderr)
        return 2
    except ValueError:
        if re.search(r"\bgit\b[\s\S]*\bpush\b", command) or re.search(r"\bgh\s+(pr|issue|release|gist|api)\b", command):
            print("BLOCKED (ccguard push-gate): this command cannot be parsed (unbalanced "
                  "quotes?). Run the push or post as a separate, plain command.", file=sys.stderr)
            return 2
        return 0
    except Exception as e:  # noqa: BLE001 — a gate that crashed must not wave the push through
        print(f"BLOCKED (ccguard push-gate): the gate failed ({e.__class__.__name__}). Run the "
              "push as a plain command, or report the failure.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
