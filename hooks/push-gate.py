#!/usr/bin/env python3
"""PreToolUse(Bash) guard — keep the pre-push gate in force, and gate gh posts.

What a git push updates and sends is checked by the git pre-push gate
(pre_push_gate.py, installed by install_git_hooks.py), which sees the refs
git resolved, however the command was written. This hook makes sure that
gate runs:

- denies skipping hooks (`--no-verify` on a git command), writing
  core.hooksPath (reading it is fine), clearing CLAUDECODE (the gate acts
  only when it is 1) or the environment around git, overriding HOME /
  XDG_CONFIG_HOME / GIT_CONFIG_* around a push, `git send-pack`, a push
  through sudo, and writes to the ccguard hooks directory or config;
- denies a git push from a repository where core.hooksPath does not point
  at the ccguard hooks directory (not set yet, or the repository sets its
  own).

gh has no such hook, so its posts are checked here: titles, bodies, notes,
comments, descriptions and fields of `gh pr|issue|release|gist|repo|label|
variable|project|api`, and the files, redirected files and heredocs they
read, against the content patterns of lib/ccguard_content.py. Text the
shell would produce by command substitution (`$(...)` or backticks outside
single quotes) and stdin from a pipe cannot be read first and are denied;
`--body "$(cat <<'EOF' ... EOF)"` is read from the heredoc. A gh post
wrapped in another command is denied. `gh release create` (a tag),
`gh repo sync`, `gh alias set`, and `gh api` writes to refs, contents,
merges and releases (REST, or GraphQL ref and merge mutations) are denied:
they change a repository the way a push would. `gh pr merge` is left to the
permission rules (ask).

`bash -c`, `sh -c` and `eval` strings are checked like the command itself.
The guard is for a cooperating agent: it stops mistakes and shortcuts, not
a determined attempt to defeat it. Server-side branch protection and push
protection remain the real guarantee for a public repository.

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
MAX_DEPTH = 3

PUNCT = ";&|<>()\n"
ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
PREFIXES = {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until",
            "time", "command", "builtin", "nohup", "exec"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SWITCH_USER = {"sudo", "doas", "su", "runuser", "pkexec"}
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
HEREDOC_CAT = re.compile(r"\$\(\s*cat\s+<<-?\s*['\"]?\w+['\"]?\s*\)\s*", re.S)

GIT = re.compile(r"\bgit\b")
CLEARS_CLAUDECODE = re.compile(r"\bCLAUDECODE\s*\+?=|-u\s*['\"]?CLAUDECODE\b|"
                               r"\b(unset|export|declare|typeset|readonly|local)\b[^\n;&|]*"
                               r"\bCLAUDECODE\b|"
                               r"\benv\s+((-\S+|\w+=\S*)\s+)*(-i|--ignore-environment|-)(\s|$)")
CONFIG_ENV = re.compile(r"\b(HOME|XDG_CONFIG_HOME|GIT_CONFIG_GLOBAL|GIT_CONFIG_SYSTEM|"
                        r"GIT_CONFIG_NOSYSTEM|GIT_CONFIG_PARAMETERS|GIT_CONFIG_COUNT|"
                        r"GIT_CONFIG_KEY_\d+|GIT_CONFIG_VALUE_\d+)=|"
                        r"(-u\s+|unset\s+)(HOME|XDG_CONFIG_HOME)\b")
PROTECTED_PATHS = re.compile(r"ccguard/(git-hooks|push-gate\.json)|plugins/cache/[^/\s]+/ccguard/")
WRITE_WORDS = re.compile(r"\b(tee|rm|rmdir|chmod|chown|mv|cp|ln|truncate|install|dd|unlink|"
                         r"shred)\b|\b(sed|perl)\b[^\n]*\s-i|\b(python3?|perl|ruby|node)\b|"
                         r"\bfind\b[^\n]*\s-(delete|exec|execdir|ok|okdir|fprint\w*|fls)\b")
# Words that make heredoc text run as code or arguments (`bash <<EOF`, `cat <<EOF | sh`,
# `xargs rm <<EOF`, a script written now and run later in the same command).
RUNNERS = re.compile(r"\b(bash|sh|zsh|dash|ksh|python3?|perl|ruby|node|xargs|source|eval|exec)\b|"
                     r"(^|[\s;&|(])\.\s")
FD_DUP = re.compile(r"\d*[<>]&(\d+|-)(?![\w/.~$])")
REDIRECT = re.compile(r">>?\|?\s*(\S*)")
PLAIN_TARGET = re.compile(r"['\"]?[\w./~+@%:,=-]+['\"]?")


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


def substitutions(text):
    """Bodies of the command substitutions the shell would expand (not in single quotes)."""
    out, i, quote, n = [], 0, None, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 2
            continue
        elif quote == '"' and c == '"':
            quote = None
        elif quote is None and c in "'\"":
            quote = c
        elif c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            out.append(text[i + 1:j])
            i = j + 1
            continue
        elif c == "$" and text.startswith("(", i + 1):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append(text[i + 2:j - 1])
            i = j
            continue
        i += 1
    return out


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


def split_git(words, cwd):
    """(working dir, global options, subcommand, args) of a git invocation."""
    opts, i = [], 1
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            cwd = os.path.join(cwd, os.path.expanduser(words[i + 1]))
            i += 2
        elif w in ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env") and i + 1 < len(words):
            opts += words[i:i + 2]
            i += 2
        elif w.startswith("-"):
            opts.append(w)
            i += 1
        else:
            return cwd, opts, w, words[i + 1:]
    return cwd, opts, None, []


# ---------------------------------------------------------------------- git

MESSAGE_VALUES = {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message",
                  "--reedit-message", "-t", "--template", "--fixup", "--squash"}
CONFIG_WRITE_FLAGS = {"--unset", "--unset-all", "--add", "--replace-all", "--edit", "-e",
                      "--rename-section", "--remove-section"}
CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
CONFIG_VALUE_OPTS = {"--file", "-f", "--blob", "--type", "--default", "--comment"}
CONFIG_SUBCOMMANDS = {"get": False, "list": False, "set": True, "unset": True, "edit": True,
                      "rename-section": True, "remove-section": True}


def check_git(where, gopts, sub, args, pushes):
    if any(re.search(r"hooks-?path", o, re.I) for o in gopts):
        raise Deny("This sets git's hooks path for one command, which takes the pre-push gate "
                   "out. That setting is the user's.")
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            break
        if a in MESSAGE_VALUES:
            skip = True
            continue
        if a.startswith("--no-v") and not a.startswith("--no-verbose"):
            raise Deny("`--no-verify` skips git hooks, including the ccguard pre-push gate. "
                       "Run the command without it.")
    if sub == "config":
        check_git_config(args)
    elif sub == "send-pack":
        raise Deny("`git send-pack` pushes without running hooks. Use git push.")
    elif sub == "push" or is_push_alias(where, gopts, sub):
        pushes.append(where)


def is_push_alias(where, gopts, sub):
    if not sub or not re.fullmatch(r"[A-Za-z0-9_.-]+", sub):
        return False
    for k in range(len(gopts) - 1):
        if gopts[k] == "-c" and gopts[k + 1].lower().startswith(f"alias.{sub.lower()}="):
            return "push" in gopts[k + 1]
    alias = git_out(where, "config", "--get", f"alias.{sub}") if os.path.isdir(where) else None
    return bool(alias and re.search(r"\bpush\b", alias))


def check_git_config(args):
    positional, flags, skip = [], set(), False
    for a in args:
        if skip:
            skip = False
            continue
        name = a.split("=", 1)[0]
        if name in CONFIG_VALUE_OPTS and "=" not in a:
            skip = True
        elif a.startswith("-"):
            flags.add(name)
        else:
            positional.append(a)
    keys = [k for k, p in enumerate(positional) if re.search(r"hooks-?path", p, re.I)]
    if not keys:
        return
    if positional and positional[0] in CONFIG_SUBCOMMANDS:
        write = CONFIG_SUBCOMMANDS[positional[0]]
    else:
        write = bool(flags & CONFIG_WRITE_FLAGS) or (
            not flags & CONFIG_READ_FLAGS and len(positional) > keys[0] + 1)
    if write:
        raise Deny("This changes core.hooksPath, which keeps the pre-push gate in force. "
                   "That setting is the user's.")


def check_gate_active(dirs):
    target = os.path.realpath(GIT_HOOKS)
    for d in dict.fromkeys(dirs):
        if not os.path.isdir(d) or git_out(d, "rev-parse", "--is-inside-work-tree") is None:
            continue
        value = git_out(d, "config", "--get", "core.hooksPath")
        if value and os.path.realpath(os.path.join(d, os.path.expanduser(value))) == target:
            continue
        if git_out(d, "config", "--local", "--get", "core.hooksPath"):
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
           "alias": {"set", "import"},
           "api": None}
TEXT_FLAGS = {"pr": {"-t", "--title", "-b", "--body", "-c", "--comment", "--subject"},
              "issue": {"-t", "--title", "-b", "--body", "-c", "--comment"},
              "release": {"-t", "--title", "-n", "--notes"},
              "gist": {"-d", "--desc"},
              "repo": {"-d", "--description", "-h", "--homepage"},
              "label": {"-d", "--description"},
              "variable": {"-b", "--body"},
              "project": {"--title", "--body", "-d", "--description", "--readme"},
              "alias": set(),
              "api": set()}
FILE_FLAGS = {"--body-file", "--notes-file", "--input"}
VALUE_FLAGS = {"-R", "--repo", "-B", "--base", "-H", "--head", "--header", "-l", "--label",
               "-a", "--assignee", "-r", "--reviewer", "-m", "--milestone", "-p", "--project",
               "-T", "--template", "-X", "--method", "-q", "--jq", "-t", "--hostname", "--cache",
               "--preview", "-f", "--filename", "-c", "--color", "--target", "--owner",
               "--format", "-A", "--author-email", "--match-head-commit", "--source", "--env"}
API_TEXT = {"-f", "--raw-field"}
API_FIELD = {"-F", "--field"}
API_WRITES = re.compile(r"(^|/)repos/[^/]+/[^/]+/(git/|contents/|merges|branches/|releases|"
                        r"tags|pulls/\d+/merge|merge-upstream)")
GRAPHQL_WRITES = re.compile(r"\b(createRef|updateRefs?|deleteRef|mergePullRequest|mergeBranch|"
                            r"createCommitOnBranch|enablePullRequestAutoMerge|"
                            r"updatePullRequestBranch)\b")
GH_WRAPPED = re.compile(r"(^|[\s;&|(`$'\"])gh\s+(pr|issue|release|gist|api|repo|label|"
                        r"variable|project|alias)\b")


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


def check_gh(cwd, args, inputs, bodies, assigns, substituted):
    sub = gh_subcommand(args)
    if not sub:
        return
    group, action = sub
    if (group, action) == ("release", "create"):
        raise Deny("`gh release create` creates a tag and publishes a release. Leave releases "
                   "to the user.")
    if (group, action) == ("repo", "sync"):
        raise Deny("`gh repo sync` updates a branch on the remote. Leave it to the user.")
    if group == "alias":
        raise Deny("`gh alias` defines commands this gate cannot see. Leave aliases to the user.")
    text_flags = TEXT_FLAGS[group]
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
        if group == "api" and name in API_TEXT | API_FIELD:
            fields = True
            field = value.partition("=")[2]
            if name in API_FIELD and field.startswith("@"):
                files.append(field[1:])
            else:
                texts.append((name, field))
        elif name in text_flags:
            texts.append((name, value))
        elif name in FILE_FLAGS or (name == "-F" and group != "api"):
            files.append(value)
        elif name in VALUE_FLAGS:
            if name in ("-R", "--repo"):
                repo = value
            elif name in ("-X", "--method"):
                method = value.upper()
            if step == 2 and value.startswith("-"):
                step = 1  # a boolean spelled like a value flag (`gh pr merge -m -b ...`)
        elif a.startswith("-"):
            step = 1
        else:
            if group == "api" and endpoint is None and " " not in a:
                endpoint = a
            elif group == "gist":
                files.append(a)
            step = 1
        i += step

    if group == "api":
        if API_WRITES.search(endpoint or "") and (fields or (method or "GET") != "GET"):
            raise Deny("This `gh api` call changes refs, contents, merges or releases of a "
                       "repository, the way a push would. Leave it to the user.")
    for where, value in texts + [("file", f) for f in files]:
        if ("$(" in value or "`" in value) and not HEREDOC_CAT.fullmatch(value):
            if any(s.strip() and s.strip() in value for s in substituted):
                raise Deny(f"The {where} value of `gh {group}` comes from a command "
                           "substitution, which cannot be checked before it is posted. Write "
                           "the text to a file and pass the file.")
    texts = [(w, v) for w, v in texts if not HEREDOC_CAT.fullmatch(v)]
    texts += [("heredoc", b) for b in bodies]
    for path in files:
        if path == "-":
            if not bodies and not inputs:
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
    if group == "api" and (endpoint or "").strip("/") == "graphql":
        if any(re.search(r"\bmutation\b", t) and GRAPHQL_WRITES.search(t) for _, t in texts):
            raise Deny("This GraphQL mutation changes refs or merges pull requests, the way a "
                       "push would. Leave it to the user.")

    repo = repo or assigns.get("GH_REPO") or os.environ.get("GH_REPO")
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

def without_data_heredocs(command):
    """The command with heredoc bodies left out, unless something in it may run them.

    A body written by `cat > file <<EOF` is data; the same body is code when a shell,
    an interpreter, xargs, source or eval appears anywhere else in the command."""
    stripped, bodies = split_heredocs(command)
    return command if bodies and RUNNERS.search(stripped) else stripped


def writes_somewhere(text):
    """Whether text writes a file: a write command, or a `>` whose target is not a plain path."""
    if WRITE_WORDS.search(text):
        return True
    for m in REDIRECT.finditer(text):
        target = m.group(1)
        if PROTECTED_PATHS.search(target) or not PLAIN_TARGET.fullmatch(target):
            return True
    return False


def check_raw(command):
    """Checks on the raw text, so that no spelling or wrapper hides them."""
    if GIT.search(command) and CLEARS_CLAUDECODE.search(command):
        raise Deny("This clears CLAUDECODE or the environment around git, which turns the "
                   "pre-push gate off. Run git with the environment as it is.")
    text = FD_DUP.sub(" ", without_data_heredocs(command))
    if PROTECTED_PATHS.search(text) and writes_somewhere(text):
        raise Deny("This would change ccguard's hooks, gate or config. Those are the user's "
                   "to change.")


def check(command, cwd, depth=0):
    """Return the directories a push in `command` runs from; raise Deny."""
    check_raw(command)
    stripped, bodies = split_heredocs(command)
    substituted = substitutions(stripped)
    pushes, here = [], cwd
    for words, inputs in segments(stripped):
        assigns, cmd = command_words(words)
        if not cmd:
            continue
        name = name_of(cmd[0])
        if name == "cd":
            target = os.path.join(here, os.path.expanduser(cmd[1] if len(cmd) > 1 else "~"))
            if os.path.isdir(target):  # `cd "$(...)"` and the like cannot be followed
                here = target
        elif name == "git":
            where, gopts, sub, args = split_git(cmd, here)
            before = len(pushes)
            check_git(where, gopts, sub, args, pushes)
            if len(pushes) > before and (CONFIG_ENV.search(" ".join(words)) or
                                         any(k in assigns for k in ("HOME", "XDG_CONFIG_HOME"))):
                raise Deny("This push overrides HOME or git's config environment, which can "
                           "take the pre-push gate out. Push without the override.")
        elif name == "gh":
            check_gh(here, cmd[1:], inputs, bodies, assigns, substituted)
        elif name in SHELLS and "-c" in cmd[1:] and depth < MAX_DEPTH:
            k = cmd.index("-c", 1)
            if k + 1 < len(cmd):
                pushes += check(cmd[k + 1], here, depth + 1)
        elif name == "eval" and depth < MAX_DEPTH:
            pushes += check(" ".join(cmd[1:]), here, depth + 1)
        else:
            names = [name_of(w) for w in cmd]
            for k, n in enumerate(names):
                if n == "git" and "push" in cmd[k + 1:]:
                    if name in SWITCH_USER:
                        raise Deny(f"A push through `{name}` runs without your git config, so "
                                   "the pre-push gate does not run. Push as yourself.")
                    pushes.append(here)
                if (n == "gh" and gh_subcommand(cmd[k + 1:])) or GH_WRAPPED.search(cmd[k]):
                    raise Deny(f"A `gh` post runs inside `{cmd[0]}`, where the gate cannot check "
                               "it. Run it as a plain command of its own.")
    if pushes and CONFIG_ENV.search(command):  # e.g. `export HOME=/tmp; git push`
        raise Deny("This push overrides HOME or git's config environment, which can take the "
                   "pre-push gate out. Push without the override.")
    return pushes


def relevant(command):
    return (GIT.search(command) or re.search(r"\bgh\b", command)
            or PROTECTED_PATHS.search(command))


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
        check_gate_active(check(command, cwd))
    except Deny as denial:
        print(f"BLOCKED (ccguard push-gate): {denial}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        if (GIT.search(command) and re.search(r"\bpush\b", command)) or GH_WRAPPED.search(" " + command):
            reason = ("cannot be parsed (unbalanced quotes?)" if isinstance(e, ValueError)
                      else f"made the gate fail ({e.__class__.__name__})")
            print(f"BLOCKED (ccguard push-gate): this command {reason}. Run the push or post "
                  "as a separate, plain command.", file=sys.stderr)
            return 2
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
