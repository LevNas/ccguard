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


def test_gate_cannot_be_taken_out():
    for cmd in ("git push --no-verify origin feat", "git push --no-veri origin feat",
                "bash -c 'git push --no-verify origin feat'",
                "git -c core.hooksPath=/dev/null push origin feat",
                "git config --global core.hooksPath /tmp/x",
                "git config --unset core.hooksPath",
                "HOME=/tmp git push origin feat", "env -u HOME git push origin feat",
                "GIT_CONFIG_GLOBAL=/dev/null git push origin feat",
                "git send-pack origin feat"):
        case(f"denied: {cmd}", cmd, 2)
    case("allowed: reading about hooksPath", "grep -rn hooksPath README.md", 0)


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


def test_gh_unreadable_text():
    case("denied: stdin from a pipe", "cat body.md | gh pr comment 1 --body-file -", 2,
         gh_setup("hi"))
    case("denied: command substitution", "gh pr comment 1 --body \"$(cat body.md)\"", 2,
         gh_setup("hi"))
    case("denied: backticks", "gh pr comment 1 --body \"`cat body.md`\"", 2, gh_setup("hi"))
    case("denied: wrapped gh", "echo 3 | xargs gh pr comment -b hi", 2, gh_setup())
    case("denied: gh inside bash -c", "bash -c 'gh pr comment 1 -b hi'", 2, gh_setup())


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
                "gh api repos/o/r/merges -f base=main -f head=x"):
        case(f"denied: {cmd}", cmd, 2, gh_setup())
    case("allowed: gh api read of contents", "gh api repos/o/r/contents/f", 0, gh_setup())


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
