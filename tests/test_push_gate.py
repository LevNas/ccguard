#!/usr/bin/env python3
"""Self-tests for hooks/push-gate.py.

Run: python3 tests/test_push_gate.py   (exit 0 = all pass)

Each test builds a throwaway repository with a bare remote and runs the hook
as a subprocess, the way the harness does. Token-shaped samples are assembled
at runtime so this file carries no literal secret shapes (secret scanners
would otherwise flag the repository).
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "push-gate.py")
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)
        if detail != "":
            print(f"      {detail!r}"[:300])


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "init.defaultBranch=main", "-C", cwd, *args],
                   check=True, capture_output=True)


class Repo:
    """A work repository on `feat/x` with a bare `origin` whose main is pushed."""

    def __init__(self, base):
        self.base = base
        self.remote = os.path.join(base, "remote.git")
        self.work = os.path.join(base, "work")
        self.config_home = os.path.join(base, "config")
        git(base, "init", "-q", "--bare", self.remote)
        git(base, "init", "-q", self.work)
        git(self.work, "remote", "add", "origin", self.remote)
        self.commit("README.md", "hello\n", "init")
        git(self.work, "push", "-q", "-u", "origin", "main")
        git(self.work, "switch", "-q", "-c", "feat/x")

    def commit(self, path, text, message):
        with open(os.path.join(self.work, path), "w", encoding="utf-8") as f:
            f.write(text)
        git(self.work, "add", path)
        git(self.work, "commit", "-q", "-m", message)

    def config(self, private=(), skip=()):
        os.makedirs(os.path.join(self.config_home, "ccguard"), exist_ok=True)
        with open(os.path.join(self.config_home, "ccguard", "push-gate.json"), "w") as f:
            json.dump({"private_patterns": list(private), "skip_remotes": list(skip)}, f)

    def run(self, command, cwd=None, user="alice"):
        env = dict(os.environ, XDG_CONFIG_HOME=self.config_home, USER=user)
        env.pop("CCGUARD_PROTECTED_BRANCHES", None)
        payload = {"tool_input": {"command": command}, "cwd": cwd or self.work}
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=60)
        return proc.returncode, proc.stderr


def case(name, command, expected, setup=None, cwd=None):
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(base)
        if setup:
            setup(repo)
        rc, err = repo.run(command.replace("{work}", repo.work), cwd=cwd and cwd(repo))
        check(name, rc == expected, (rc, err))
        return err


def add_commit(path, text, message="change"):
    return lambda repo: repo.commit(path, text, message)


def test_feature_branch_allowed():
    case("push feature branch", "git push -u origin feat/x", 0, add_commit("a.txt", "ok\n"))
    case("bare push of feature branch", "git push", 0,
         lambda r: (r.commit("a.txt", "ok\n", "c"), git(r.work, "push", "-q", "-u", "origin", "feat/x"),
                    r.commit("b.txt", "ok\n", "c2")))
    case("HEAD refspec on feature branch", "git push origin HEAD", 0, add_commit("a.txt", "ok\n"))
    case("dry run of a force push", "git push --dry-run -f origin feat/x", 0)


def test_rewrites_and_bulk_denied():
    for cmd in ("git push -f origin feat/x", "git push --force origin feat/x",
                "git push --force-with-lease origin feat/x", "git push -uf origin feat/x",
                "git push origin +feat/x", "git push origin :feat/x",
                "git push --delete origin feat/x", "git push --all origin",
                "git push --mirror origin", "git push --tags origin"):
        case(f"denied: {cmd}", cmd, 2)


def test_protected_destinations_denied():
    case("denied: origin main", "git push origin main", 2)
    case("denied: HEAD:main", "git push origin HEAD:main", 2)
    case("denied: refs/heads/main", "git push origin feat/x:refs/heads/main", 2)
    case("denied: bare push on main", "git push", 2,
         lambda r: git(r.work, "switch", "-q", "main"))
    case("denied: git -C dir push origin main", "git -C {work} push origin main", 2,
         cwd=lambda r: r.base)
    case("denied: cd then push main", "cd {work} && git push origin main", 2,
         cwd=lambda r: r.base)
    case("denied: tag refspec", "git push origin v1.0", 2,
         lambda r: git(r.work, "tag", "v1.0"))


def test_not_a_push():
    case("allowed: echo mentioning git push", "echo 'git push origin main'", 0)
    case("allowed: git status", "git status && git log -1", 0)
    case("denied: unparseable push", "git push origin main 'unclosed", 2)


def test_secret_in_diff_denied():
    token = "gh" + "p_" + "A" * 36
    err = case("denied: token in added line", "git push origin feat/x", 2,
               add_commit("conf.txt", f"token={token}\n"))
    check("token value not echoed", token not in err, err)


def test_private_pattern_and_skip():
    def setup(repo, skip=()):
        repo.config(private=[r"secret-handle"], skip=skip)
        repo.commit("note.md", "written by secret-handle\n", "note")
    case("denied: private pattern in diff", "git push origin feat/x", 2, setup)
    case("allowed: private pattern, remote skipped", "git push origin feat/x", 0,
         lambda r: setup(r, skip=[r"remote\.git$"]))

    def message(repo):
        repo.config(private=[r"secret-handle"])
        repo.commit("a.txt", "x\n", "fix for SECRET-HANDLE")
    case("denied: private pattern in message (case-insensitive)",
         "git push origin feat/x", 2, message)


def test_home_path():
    case("denied: own home path", "git push origin feat/x", 2,
         add_commit("a.md", "see /home/alice/notes\n"))
    case("allowed: someone else's home path", "git push origin feat/x", 0,
         add_commit("a.md", "see /home/bob/notes\n"))
    case("allowed: only commits not on the remote are scanned", "git push origin feat/x", 0,
         lambda r: (git(r.work, "switch", "-q", "main"),
                    r.commit("a.md", "/home/alice/x\n", "old"),
                    git(r.work, "push", "-q", "origin", "main"),
                    git(r.work, "switch", "-q", "feat/x")))


def test_gh_text():
    def body(text, skip=()):
        def setup(repo):
            repo.config(private=[r"secret-handle"], skip=skip)
            with open(os.path.join(repo.work, "body.md"), "w") as f:
                f.write(text)
        return setup
    case("denied: gh pr create body file", "gh pr create --title t --body-file body.md", 2,
         body("hi secret-handle"))
    case("allowed: clean gh pr body", "gh pr create --title t --body-file body.md", 0,
         body("hi"))
    case("denied: gh issue create inline body", "gh issue create -t x -b 'by secret-handle'", 2,
         body(""))
    case("allowed: gh issue to a skipped repo",
         "gh issue create -R someone/private -t x -b 'by secret-handle'", 0,
         body("", skip=[r"someone/private"]))
    case("allowed: gh pr view", "gh pr view 3", 0, body(""))


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
