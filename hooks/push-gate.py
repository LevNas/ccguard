#!/usr/bin/env python3
"""PreToolUse(Bash) guard — keep the pre-push gate in force, and gate gh posts.

What a git push updates and sends is checked by the git pre-push gate
(pre_push_gate.py, installed by install_git_hooks.py), which sees the refs
git resolved, however the command was written. This hook only makes sure
that gate runs:

- denies taking hooks out of a push: `--no-verify` (or an abbreviation),
  changing core.hooksPath, overriding HOME / XDG_CONFIG_HOME / GIT_CONFIG_*
  around a push, and `git send-pack`. These are matched on the whole
  command string, so `bash -c`, `eval` and the like do not hide them;
- denies a git push from a repository where core.hooksPath does not point
  at the ccguard hooks directory (not set yet, or the repository sets its
  own).

gh has no such hook, so its posts are checked here: titles, bodies, notes,
comments, descriptions and fields of `gh pr|issue|release|gist|repo|label|
variable|project|api`, and the files and heredocs they read, against the
content patterns of lib/ccguard_content.py. Text that cannot be read before
it is posted — stdin from a pipe, `$(...)` or backticks — is denied, as is a
gh post wrapped in another command. `gh release create` (a tag), `gh repo
sync`, and `gh api` writes to refs, contents, merges and releases are
denied outright: they change a repository the way a push would.

Fails closed: for a command that pushes or posts, anything that cannot be
checked is denied. Other commands are never touched.

Exit 2 denies the tool call (stderr goes back to Claude); exit 0 allows it.
"""

import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from ccguard_content import Deny, content_patterns, first_hit, git_out, set_budget  # noqa: E402

GIT_HOOKS = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"),
    "ccguard", "git-hooks",
)
MAX_FILE = 20 << 20

PUNCT = ";&|<>()\n"
ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
PREFIXES = {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until",
            "time", "command", "builtin", "nohup", "exec"}
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

PUSH = re.compile(r"\bpush\b")
GIT = re.compile(r"\bgit\b")
NO_VERIFY = re.compile(r"--no-v")
HOOKS_PATH = re.compile(r"(\bconfig\b|(^|\s)-c\b|--config-env)[^\n]*hooks-?path", re.I)
CONFIG_ENV = re.compile(r"\b(HOME|XDG_CONFIG_HOME|GIT_CONFIG_GLOBAL|GIT_CONFIG_SYSTEM|"
                        r"GIT_CONFIG_NOSYSTEM|GIT_CONFIG_PARAMETERS|GIT_CONFIG_COUNT|"
                        r"GIT_CONFIG_KEY_\d+|GIT_CONFIG_VALUE_\d+)=|"
                        r"(-u\s+|unset\s+)(HOME|XDG_CONFIG_HOME)\b")
GH_WRAPPED = re.compile(r"(^|[\s;&|(`$'\"])gh\s+(pr|issue|release|gist|api|repo|label|"
                        r"variable|project)\b")


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
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=True, punctuation_chars=PUNCT)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""  # `#` starts a comment only at a word start; never hide text
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
        if tok:
            words.append(tok)
    if words:
        yield words, inputs


def name_of(word):
    """The command a word names: `git` for /usr/bin/git or `git (backtick)."""
    return os.path.basename(word.strip("`"))


def command_words(words):
    """(assignments, words) with assignments, keywords and option-less wrappers dropped."""
    assigns, i = {}, 0
    while i < len(words):
        w = words[i]
        m = ASSIGN.fullmatch(w)
        if m:
            assigns[m.group(1)] = m.group(2)
            i += 1
        elif w in PREFIXES:
            i += 1
        elif w == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or ASSIGN.fullmatch(words[i])):
                m = ASSIGN.fullmatch(words[i])
                if m:
                    assigns[m.group(1)] = m.group(2)
                i += 1
        else:
            break
    return assigns, words[i:]


# ---------------------------------------------------------------------- git

def check_git(raw, parsed, cwd):
    pushes = bool(GIT.search(raw) and PUSH.search(raw))
    if NO_VERIFY.search(raw) and PUSH.search(raw):
        raise Deny("`--no-verify` would skip the pre-push gate. Push without it.")
    if GIT.search(raw) and HOOKS_PATH.search(raw):
        raise Deny("This changes git's hooks path, which keeps the pre-push gate in force. "
                   "That setting is the user's.")
    if pushes and CONFIG_ENV.search(raw):
        raise Deny("This push overrides HOME or git's config environment, which can take the "
                   "pre-push gate out. Push without the override.")
    if re.search(r"\bsend-pack\b", raw):
        raise Deny("`git send-pack` pushes without running hooks. Use git push.")
    if not pushes:
        return
    dirs = [cwd]
    here = cwd
    for words in parsed:
        _assigns, cmd = command_words(words)
        if not cmd:
            continue
        if name_of(cmd[0]) == "cd" and len(cmd) > 1:
            here = os.path.join(here, os.path.expanduser(cmd[1]))
            dirs.append(here)
        for k, w in enumerate(cmd[:-1]):
            if name_of(w) == "git" and cmd[k + 1] == "-C" and k + 2 < len(cmd):
                dirs.append(os.path.join(here, os.path.expanduser(cmd[k + 2])))
    target = os.path.realpath(GIT_HOOKS)
    for d in dict.fromkeys(dirs):
        if not os.path.isdir(d) or git_out(d, "rev-parse", "--is-inside-work-tree") is None:
            continue
        value = git_out(d, "config", "--get", "core.hooksPath")
        if value and os.path.realpath(os.path.join(d, os.path.expanduser(value))) == target:
            continue
        local = git_out(d, "config", "--local", "--get", "core.hooksPath")
        if local:
            raise Deny("This repository sets its own core.hooksPath, so the ccguard pre-push "
                       "gate does not run here. Ask the user to push by hand.")
        raise Deny("The ccguard pre-push gate is not active (core.hooksPath is not set to "
                   f"{GIT_HOOKS}). Ask the user to run, once: "
                   f"git config --global core.hooksPath {GIT_HOOKS}")


# ----------------------------------------------------------------------- gh

GH_TEXT = {"pr": {"create", "edit", "comment", "review", "close", "merge", "reopen"},
           "issue": {"create", "edit", "comment", "close", "reopen"},
           "release": {"create", "edit"},
           "gist": {"create", "edit"},
           "repo": {"create", "edit", "sync"},
           "label": {"create", "edit"},
           "variable": {"set"},
           "project": {"create", "edit", "item-create", "item-edit"},
           "api": None}
LONG_TEXT = {"--title", "--body", "--notes", "--subject", "--comment", "--description",
             "--desc", "--message"}
SHORT_TEXT = {"-t", "-b", "-n", "-c"}
DESC_GROUPS = {"gist", "repo", "label"}  # -d is a description there, --draft/--delete elsewhere
FILE_FLAGS = {"--body-file", "--notes-file", "--input"}
API_TEXT = {"-f", "--raw-field"}
API_FIELD = {"-F", "--field"}
API_WRITES = re.compile(r"(^|/)repos/[^/]+/[^/]+/(git/|contents/|merges|branches/|releases|"
                        r"tags|pulls/\d+/merge|merge-upstream)")


def gh_subcommand(args):
    """(group, action) when args post text or change a repository, else None."""
    if not args or args[0] not in GH_TEXT:
        return None
    actions = GH_TEXT[args[0]]
    if actions is None:
        return args[0], ""
    if len(args) > 1 and args[1] in actions:
        return args[0], args[1]
    return None


def unreadable(value):
    return "$(" in value or "`" in value


def check_gh(cwd, args, inputs, bodies, assigns):
    sub = gh_subcommand(args)
    if not sub:
        return
    group, action = sub
    if (group, action) == ("release", "create"):
        raise Deny("`gh release create` creates a tag and publishes a release. Leave releases "
                   "to the user.")
    if (group, action) == ("repo", "sync"):
        raise Deny("`gh repo sync` updates a branch on the remote. Leave it to the user.")
    texts, files, repo, endpoint, method, fields = [], list(inputs), None, None, None, False
    i = 1 if group == "api" else 2
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            name, value, step = *a.split("=", 1), 1
        elif len(a) > 2 and a[0] == "-" and a[1] != "-":
            name, value, step = a[:2], a[2:], 1  # -bTEXT
        else:
            name, value, step = a, (args[i + 1] if i + 1 < len(args) else ""), 2
        if name in ("-R", "--repo"):
            repo = value
        elif group == "api" and name in ("-X", "--method"):
            method = value.upper()
        elif group == "api" and name in API_TEXT | API_FIELD:
            fields = True
            field = value.partition("=")[2]
            if name in API_FIELD and field.startswith("@"):
                files.append(field[1:])
            else:
                texts.append((name, field))
        elif name in FILE_FLAGS or (name == "-F" and group != "api"):
            files.append(value)
        elif name in LONG_TEXT or name in SHORT_TEXT or (name == "-d" and group in DESC_GROUPS):
            texts.append((name, value))
        elif a.startswith("-"):
            step = 1
        else:
            if group == "api" and endpoint is None:
                endpoint = a
            elif group == "gist":
                files.append(a)
            step = 1
        i += step

    if group == "api" and API_WRITES.search(endpoint or "") and (fields or (method or "GET") != "GET"):
        raise Deny("This `gh api` call changes refs, contents, merges or releases of a "
                   "repository, the way a push would. Leave it to the user.")
    for where, value in texts + [("file", f) for f in files]:
        if unreadable(value):
            raise Deny(f"The {where} value of `gh {group}` is produced by a command "
                       "substitution, which cannot be checked before it is posted. Write the "
                       "text to a file and pass the file.")
    texts += [("heredoc", b) for b in bodies]
    for path in files:
        if path == "-":
            if not bodies:
                raise Deny(f"`gh {group}` would read its text from stdin, which cannot be "
                           "checked before it is posted. Write it to a file and pass the file.")
            continue
        full = os.path.join(cwd, os.path.expanduser(path))
        try:
            if os.path.getsize(full) > MAX_FILE:
                raise Deny(f"`{path}` is too large to check before `gh {group}` posts it.")
            with open(full, encoding="utf-8", errors="replace") as f:
                texts.append((f"file {path}", f.read()))
        except OSError:
            raise Deny(f"Cannot read `{path}`, which `gh {group}` would post.") from None

    repo = repo or assigns.get("GH_REPO")
    m = re.match(r"/?repos/([^/{}]+)/([^/{}]+)", endpoint or "")
    if repo:
        destination = repo if repo.count("/") >= 2 else f"github.com/{repo}"
    elif m:
        destination = f"github.com/{m.group(1)}/{m.group(2)}"
    elif group == "gist":
        destination = "gist.github.com"
    elif group == "api" and not re.match(r"/?repos/\{owner\}/\{repo\}", endpoint or ""):
        destination = ""  # graphql, user, orgs, ...: unknown, so nothing is exempt
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
    parsed = list(segments(stripped))
    check_git(command, [w for w, _ in parsed], cwd)
    here = cwd
    for words, inputs in parsed:
        assigns, cmd = command_words(words)
        if not cmd:
            continue
        name = name_of(cmd[0])
        if name == "cd":
            here = os.path.join(here, os.path.expanduser(cmd[1] if len(cmd) > 1 else "~"))
        elif name == "gh":
            check_gh(here, cmd[1:], inputs, bodies, assigns)
        elif any((name_of(w) == "gh" and gh_subcommand(cmd[k + 1:])) or GH_WRAPPED.search(w)
                 for k, w in enumerate(cmd)):
            raise Deny(f"A `gh` post runs inside `{cmd[0]}`, where the gate cannot check it. "
                       "Run it as a plain command of its own.")


def relevant(command):
    return GIT.search(command) or re.search(r"\bgh\b", command) or "send-pack" in command


def main():
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
    except (ValueError, AttributeError):
        return 0
    if not relevant(command):
        return 0
    set_budget(25)
    try:
        check(command, cwd)
    except Deny as denial:
        print(f"BLOCKED (ccguard push-gate): {denial}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        if (GIT.search(command) and PUSH.search(command)) or GH_WRAPPED.search(" " + command):
            reason = ("cannot be parsed (unbalanced quotes?)" if isinstance(e, ValueError)
                      else f"made the gate fail ({e.__class__.__name__})")
            print(f"BLOCKED (ccguard push-gate): this command {reason}. Run the push or post "
                  "as a separate, plain command.", file=sys.stderr)
            return 2
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
