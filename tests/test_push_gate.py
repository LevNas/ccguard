#!/usr/bin/env python3
"""Self-tests for hooks/push-gate.py (PreToolUse).

Run: python3 tests/test_push_gate.py   (exit 0 = all pass)

What a push updates and sends is tested in test_pre_push_gate.py. Here: the
pre-push gate cannot be taken out of a push, a push is denied where the
gate is not active, and gh posts are checked. Each test builds a throwaway
repository and runs the hook as a subprocess, the way the harness does.
Token-shaped samples are assembled at runtime.
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "push-gate.py")
TOKEN = "gh" + "p_" + "A" * 36
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)
        if detail != "":
            print(f"      {detail!r}"[:300])


class Repo:
    """A work repository whose origin is a bare remote; core.hooksPath set by default."""

    def __init__(self, base, active=True):
        self.base = base
        self.data = os.path.join(base, "data")
        self.config_home = os.path.join(base, "config")
        self.gitconfig = os.path.join(base, "gitconfig")
        self.hooks_dir = os.path.join(self.data, "ccguard", "git-hooks")
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = t\n\temail = t@example.com\n")
            if active:
                f.write(f"[core]\n\thooksPath = {self.hooks_dir}\n")
        self.remote = os.path.join(base, "remote.git")
        self.work = os.path.join(base, "work")
        self.git(base, "init", "-q", "--bare", self.remote)
        self.git(base, "init", "-q", self.work)
        self.git(self.work, "remote", "add", "origin", self.remote)

    def env(self):
        env = dict(os.environ, XDG_DATA_HOME=self.data, XDG_CONFIG_HOME=self.config_home,
                   GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_NOSYSTEM="1", USER="alice")
        env.pop("GH_REPO", None)
        return env

    def git(self, cwd, *args):
        subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                       env=self.env() if hasattr(self, "work") else None)

    def config(self, private=(), skip=()):
        os.makedirs(os.path.join(self.config_home, "ccguard"), exist_ok=True)
        with open(os.path.join(self.config_home, "ccguard", "push-gate.json"), "w") as f:
            json.dump({"private_patterns": list(private), "skip_remotes": list(skip)}, f)

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w", encoding="utf-8") as f:
            f.write(text)

    def run(self, command):
        payload = {"tool_input": {"command": command}, "cwd": self.work}
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(), timeout=60)
        return proc.returncode, proc.stderr


def case(name, command, expected, setup=None, active=True):
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(base, active=active)
        if setup:
            setup(repo)
        rc, err = repo.run(command.replace("{work}", repo.work))
        check(name, rc == expected, (rc, err))
        if os.environ.get("VERBOSE") and err:
            print("      " + err.strip().removeprefix("BLOCKED (ccguard push-gate): ")[:150])
        return err


# ---------------------------------------------------------------- the gate

def test_push_needs_the_gate():
    case("allowed: push where the gate is active", "git push origin main", 0)
    case("denied: push where core.hooksPath is not set", "git push origin feat", 2, active=False)
    case("denied: repository with its own core.hooksPath", "git push origin feat", 2,
         lambda r: r.git(r.work, "config", "core.hooksPath", ".husky"))
    case("denied: cd into a repository without the gate", "cd {work} && git push", 2,
         active=False)
    case("allowed: no push, no gate needed", "git status && git log -1", 0, active=False)
    case("allowed: commit message mentioning push, gate inactive",
         "git commit -m 'feat: push-gate guard' --allow-empty", 0, active=False)
    case("allowed: git log --grep=push, gate inactive", "git log --grep=push", 0, active=False)
    case("denied: push spelled with quotes, gate inactive", "git pu\"sh\" origin feat", 2,
         active=False)
    case("denied: push alias from -c, gate inactive", "git -c alias.p=push p origin feat", 2,
         active=False)
    case("denied: push inside bash -c, gate inactive", "bash -c 'git push origin feat'", 2,
         active=False)
    case("denied: push through xargs, gate inactive", "echo feat | xargs git push origin", 2,
         active=False)


def test_gate_cannot_be_taken_out():
    for cmd in ("git push --no-verify origin feat", "git push --no-veri origin feat",
                "bash -c 'git push --no-verify origin feat'",
                "git -c core.hooksPath=/dev/null push origin feat",
                "git config --global core.hooksPath /tmp/x",
                "git config --unset core.hooksPath",
                "HOME=/tmp git push origin feat", "env -u HOME git push origin feat",
                "GIT_CONFIG_GLOBAL=/dev/null git push origin feat",
                "git send-pack origin feat",
                "git pu\"sh\" --no-verify origin feat",
                "git commit --no-verify -m x",
                "bash -c 'git config --global core.hooksPath /x'",
                "git config set core.hooksPath /x",
                "export HOME=/tmp; git push origin feat",
                "env -u CLAUDECODE git push origin feat",
                "CLAUDECODE= git push origin feat",
                "unset CLAUDECODE; git push origin feat",
                "env -i PATH=/usr/bin git push origin feat",
                "sudo git push origin feat",
                "rm -f ~/.local/share/ccguard/git-hooks/pre-push",
                "printf 'x' > ~/.config/ccguard/push-gate.json",
                "sed -i s/a/b/ ~/.config/ccguard/push-gate.json"):
        case(f"denied: {cmd}", cmd, 2)
    case("allowed: reading about hooksPath", "grep -rn hooksPath README.md", 0)
    case("allowed: reading core.hooksPath", "git config --get core.hooksPath", 0)
    case("allowed: reading core.hooksPath (subcommand)", "git config get core.hooksPath", 0)
    case("allowed: message that mentions --no-verify", "git commit -m '--no-verify is denied'",
         0)
    case("allowed: reading the ccguard config", "cat ~/.config/ccguard/push-gate.json", 0)
    case("allowed: env with a rebase", "env X=1 git rebase -i HEAD~1", 0)


HOOKS = "~/.local/share/ccguard/git-hooks"
CONFIG = "~/.config/ccguard/push-gate.json"


def test_own_files_raw_check():
    # Writes to ccguard's own files stay denied however they are wrapped: the check
    # reads the raw text, only with data heredocs and fd duplications set aside.
    for cmd in (f"echo {HOOKS}/pre-push | xargs rm",
                f"echo x > $(echo {CONFIG})",
                f"f={CONFIG}; echo x > \"$f\"",
                f"echo x >> {HOOKS}/pre-push",
                f"echo x >| {CONFIG}",
                f"echo x &> {CONFIG}",
                f"ls {HOOKS} 2>&1 > {HOOKS}/pre-push",
                f"echo x | tee {CONFIG}",
                f"find {HOOKS} -delete -exec rm {{}} +",
                f"python3 -c \"open('{CONFIG}', 'w')\"",
                f"bash <<'EOF'\nrm -f {HOOKS}/pre-push\nEOF",
                f"cat <<'EOF' | sh\necho x > {CONFIG}\nEOF",
                f"cat <<'EOF' > /tmp/x.sh\nrm -f {HOOKS}/pre-push\nEOF\nbash /tmp/x.sh",
                f"xargs rm <<'EOF'\n{HOOKS}/pre-push\nEOF",
                f"rm -f {HOOKS}/pre-push '"):
        case(f"denied: {cmd!r}", cmd, 2)
    # Reading them, or mentioning the path in text written elsewhere, is not a write.
    for cmd in (f"ls -la {HOOKS}/ 2>&1",
                f"ls {HOOKS} >/dev/null 2>&1 && echo present",
                f"cat {CONFIG} > /tmp/copy.json",
                f"cat {HOOKS}/pre-push 2>/dev/null | head -5",
                f"cat > \"$TMPDIR/msg.txt\" <<'EOF'\ndotfiles: set core.hooksPath to {HOOKS}\nEOF\n"
                "git commit -q -F \"$TMPDIR/msg.txt\""):
        case(f"allowed: {cmd!r}", cmd, 0)


def test_claudecode_stays_set():
    # The pre-push gate acts only when CLAUDECODE=1, however the change is wrapped.
    for cmd in ("xargs sh -c 'CLAUDECODE= git push origin feat'",
                "timeout 5 sh -c 'CLAUDECODE= git push origin feat'",
                "find . -maxdepth 0 -exec sh -c 'CLAUDECODE= git push origin feat' \\;",
                "echo `CLAUDECODE= git push origin feat`",
                "CLAUDECODE+=x git push origin feat",
                "export -n CLAUDECODE; git push origin feat",
                "declare +x CLAUDECODE; git push origin feat"):
        case(f"denied: {cmd}", cmd, 2)


# ----------------------------------------------------------------------- gh

def gh_setup(body="", skip=()):
    def setup(repo):
        repo.config(private=[r"secret-handle"], skip=skip)
        repo.write("body.md", body)
    return setup


SKIP_ORIGIN = [r"remote\.git$", r"someone/private"]


def test_gh_text():
    case("denied: body file", "gh pr create --title t --body-file body.md", 2,
         gh_setup("hi secret-handle"))
    case("allowed: clean body file under the home path", "gh pr create -t t -F {work}/body.md",
         0, gh_setup("hi"))
    case("denied: inline body", "gh issue create -t x -b 'by secret-handle'", 2, gh_setup())
    case("denied: attached short body", "gh pr create -t x -b'by secret-handle'", 2, gh_setup())
    case("denied: value that starts with a dash", f"gh issue comment 1 --body '-{TOKEN}'", 2,
         gh_setup())
    case("denied: title after the value-less -d", "gh pr create -d --title 'secret-handle'", 2,
         gh_setup())
    case("denied: heredoc on stdin",
         "gh pr comment 3 --body-file - <<'EOF'\nby secret-handle\nEOF", 2, gh_setup())
    case("denied: line continuation", "gh pr comment 1 \\\n  --body 'secret-handle'", 2,
         gh_setup())
    case("denied: '#' inside a word", "echo a#; gh pr comment 1 -b 'secret-handle'", 2,
         gh_setup())
    for group in ("gh release edit v1 --notes 'secret-handle'",
                  "gh pr merge 3 --squash --body 'secret-handle'",
                  "gh issue close 3 -c 'secret-handle'",
                  "gh repo edit --description 'secret-handle'",
                  "gh label create x -d 'secret-handle'",
                  "gh api repos/o/r/issues -f body='secret-handle'"):
        case(f"denied: {group.split(' -')[0]}", group, 2, gh_setup())
    case("denied: gist file", "gh gist create body.md", 2, gh_setup("secret-handle"))
    case("allowed: gh pr view", "gh pr view 3", 0, gh_setup())


def test_gh_quoting():
    case("allowed: markdown backticks in single quotes",
         "gh pr create --title t --body 'Use `foo` here'", 0, gh_setup())
    case("allowed: body from a heredoc in a command substitution",
         "gh pr create --title t --body \"$(cat <<'EOF'\nclean text\nEOF\n)\"", 0, gh_setup())
    case("denied: heredoc substitution with a private value",
         "gh pr create --title t --body \"$(cat <<'EOF'\nby secret-handle\nEOF\n)\"", 2,
         gh_setup())
    case("allowed: stdin redirected from a clean file",
         "gh pr comment 1 --body-file - < body.md", 0, gh_setup("hi"))
    case("denied: stdin redirected from a file with a private value",
         "gh pr comment 1 --body-file - < body.md", 2, gh_setup("secret-handle"))
    case("allowed: substitution elsewhere in the command",
         "cd \"$(pwd)\" && gh pr create -t t -b 'clean'", 0, gh_setup())
    case("denied: body after a boolean -m on pr merge",
         "gh pr merge 3 -m -b 'secret-handle'", 2, gh_setup())


def test_gh_unreadable_text():
    case("denied: stdin from a pipe", "cat body.md | gh pr comment 1 --body-file -", 2,
         gh_setup("hi"))
    case("denied: command substitution", "gh pr comment 1 --body \"$(cat body.md)\"", 2,
         gh_setup("hi"))
    case("denied: backticks", "gh pr comment 1 --body \"`cat body.md`\"", 2, gh_setup("hi"))
    case("denied: wrapped gh", "echo 3 | xargs gh pr comment -b hi", 2, gh_setup())
    case("denied: gh inside bash -c is checked like a plain one",
         "bash -c \"gh pr comment 1 -b 'secret-handle'\"", 2, gh_setup())
    case("allowed: clean gh inside bash -c", "bash -c 'gh pr comment 1 -b hi'", 0, gh_setup())


def test_gh_destination():
    case("allowed: skipped origin", "gh pr create -t t -b 'secret-handle'", 0,
         gh_setup(skip=SKIP_ORIGIN))
    case("allowed: -R to a skipped repo", "gh issue create -R someone/private -b 'secret-handle'",
         0, gh_setup(skip=SKIP_ORIGIN))
    case("allowed: GH_REPO to a skipped repo", "GH_REPO=someone/private gh issue create -b "
         "'secret-handle'", 0, gh_setup(skip=[r"someone/private"]))
    case("denied: gh api to another repo from a skipped origin",
         "gh api repos/other/public/issues -f body='secret-handle'", 2, gh_setup(skip=SKIP_ORIGIN))
    case("denied: gist from a skipped origin", "gh gist create body.md", 2,
         gh_setup("secret-handle", skip=SKIP_ORIGIN))


def test_gh_repository_changes():
    for cmd in ("gh release create v1 --notes x", "gh repo sync",
                "gh api -X PUT repos/o/r/contents/f -f content=x",
                "gh api repos/o/r/git/refs/heads/main -X PATCH -f sha=abc",
                "gh api repos/o/r/merges -f base=main -f head=x",
                "gh api -H 'Accept: application/vnd.github+json' -X PUT repos/o/r/contents/f -f content=x",
                "gh api graphql -f query='mutation { createRef(input: {}) { clientMutationId } }'",
                "gh alias set p 'pr comment'"):
        case(f"denied: {cmd}", cmd, 2, gh_setup())
    case("allowed: gh api read of contents", "gh api repos/o/r/contents/f", 0, gh_setup())
    case("allowed: gh api graphql query", "gh api graphql -f query='query { viewer { login } }'",
         0, gh_setup())
    case("allowed: gist with a file name option", "gh gist create -f name.md body.md", 0,
         gh_setup("hi"))


def main():
    for t in sorted(k for k in globals() if k.startswith("test_")):
        globals()[t]()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
