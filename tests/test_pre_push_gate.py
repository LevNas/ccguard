#!/usr/bin/env python3
"""Self-tests for hooks/pre_push_gate.py and hooks/install_git_hooks.py.

Run: python3 tests/test_pre_push_gate.py   (exit 0 = all pass)

Each test installs the ccguard git hooks into a throwaway data directory,
points a throwaway global git config at them, and runs real `git push`
commands against a bare remote, with CLAUDECODE=1 as Claude Code sets it.
A denied push must leave the remote unchanged. Token-shaped samples are
assembled at runtime so this file carries no literal secret shapes.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, "..", "hooks", "install_git_hooks.py")
TOKEN = "gh" + "p_" + "A" * 36
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)
        if detail != "":
            print(f"      {detail!r}"[:400])


class Env:
    """A throwaway HOME-like world: data dir with the hooks, global config, ccguard config."""

    def __init__(self, base, hooks_path=True):
        self.base = base
        self.data = os.path.join(base, "data")
        self.config_home = os.path.join(base, "config")
        self.gitconfig = os.path.join(base, "gitconfig")
        with open(self.gitconfig, "w") as f:
            f.write("[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
            if hooks_path:
                f.write(f"[core]\n\thooksPath = {self.hooks_dir}\n")

    @property
    def hooks_dir(self):
        return os.path.join(self.data, "ccguard", "git-hooks")

    def env(self, claude=True, extra=None):
        env = dict(os.environ, XDG_DATA_HOME=self.data, XDG_CONFIG_HOME=self.config_home,
                   GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_NOSYSTEM="1", USER="alice")
        for key in ("CLAUDECODE", "CCGUARD_PROTECTED_BRANCHES", "CCGUARD_PUSH_GATE_MAX_COMMITS"):
            env.pop(key, None)
        if claude:
            env["CLAUDECODE"] = "1"
        env.update(extra or {})
        return env

    def install(self):
        return subprocess.run([sys.executable, INSTALLER], capture_output=True, text=True,
                              env=self.env(claude=False), timeout=30)


class Repo:
    """A work repository on `feat/x` with a bare `origin` whose main is pushed."""

    def __init__(self, world):
        self.world = world
        self.remote = os.path.join(world.base, "remote.git")
        self.work = os.path.join(world.base, "work")
        world.install()
        self.git(world.base, "init", "-q", "--bare", self.remote)
        self.git(world.base, "init", "-q", self.work)
        self.git(self.work, "remote", "add", "origin", self.remote)
        self.commit("README.md", "hello\n", "init")
        self.git(self.work, "push", "-q", "-u", "origin", "main")
        self.git(self.work, "switch", "-q", "-c", "feat/x")

    def git(self, cwd, *args, check=True):
        return subprocess.run(["git", "-C", cwd, *args], check=check, capture_output=True,
                              text=True, env=self.world.env(claude=False))

    def commit(self, path, text, message="change"):
        full = os.path.join(self.work, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)

    def config(self, private=(), skip=()):
        os.makedirs(os.path.join(self.world.config_home, "ccguard"), exist_ok=True)
        with open(os.path.join(self.world.config_home, "ccguard", "push-gate.json"), "w") as f:
            json.dump({"private_patterns": list(private), "skip_remotes": list(skip)}, f)

    def remote_refs(self):
        return self.git(self.remote, "for-each-ref", "--format=%(refname) %(objectname)").stdout

    def push(self, command, claude=True, extra=None):
        proc = subprocess.run(["bash", "-c", command.replace("{remote}", self.remote)],
                              cwd=self.work, capture_output=True, text=True,
                              env=self.world.env(claude=claude, extra=extra), timeout=120)
        return proc.returncode, proc.stderr


def case(name, command, allowed, setup=None, claude=True, extra=None):
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(Env(base))
        if setup:
            setup(repo)
        before = repo.remote_refs()
        rc, err = repo.push(command, claude=claude, extra=extra)
        after = repo.remote_refs()
        if allowed:
            check(name, rc == 0 and before != after, (rc, err))
        else:
            check(name, rc != 0 and before == after and "BLOCKED (ccguard pre-push)" in err,
                  (rc, err))
        if os.environ.get("VERBOSE") and "BLOCKED" in err:
            print("      " + err.split("BLOCKED (ccguard pre-push): ")[1].strip()[:150])
        return err


def add(path, text, message="change"):
    return lambda repo: repo.commit(path, text, message)


def steps(*fns):
    def run(repo):
        for fn in fns:
            fn(repo)
    return run


def published(repo):
    repo.git(repo.work, "push", "-q", "-u", "origin", "feat/x")


# ------------------------------------------------------------ destinations

def test_feature_pushes_allowed():
    case("push a feature branch", "git push -u origin feat/x", True, add("a.txt", "ok\n"))
    case("bare push with upstream", "git push", True,
         steps(add("a.txt", "ok\n"), published, add("b.txt", "ok\n")))
    case("to a new branch name", "git push origin feat/x:other", True, add("a.txt", "ok\n"))
    case("fast-forward of a published branch", "git push origin feat/x", True,
         steps(add("a.txt", "ok\n"), published, add("b.txt", "more\n")))
    case("wrapped in bash -c (nothing to hide)", "bash -c 'git push origin feat/x'", True,
         add("a.txt", "ok\n"))


def test_protected_denied_however_written():
    for cmd in ("git push origin HEAD:main",
                "bash -c 'git push origin HEAD:main'",
                "eval \"git push origin HEAD:main\"",
                "G=git; $G push origin HEAD:main",
                "git push \\\n  origin HEAD:main",
                "git -c alias.p=push p origin HEAD:main",
                "git push -on origin HEAD:main"):
        case(f"denied: {cmd!r}", cmd, False, add("a.txt", "ok\n"))
    # A glob only updates main when local main moved; git hands up-to-date refs to nobody.
    case("denied: glob refspec that moves main", "git push origin 'refs/heads/*:refs/heads/*'",
         False, steps(lambda r: r.git(r.work, "switch", "-q", "main"), add("m.txt", "ok\n"),
                      lambda r: r.git(r.work, "switch", "-q", "feat/x")))
    case("allowed: glob refspec when main has not moved",
         "git push origin 'refs/heads/*:refs/heads/*'", True, add("a.txt", "ok\n"))
    case("denied: bare push whose upstream is main (push.default=upstream)", "git push", False,
         steps(add("a.txt", "ok\n"),
               lambda r: r.git(r.work, "branch", "-q", "--set-upstream-to=origin/main"),
               lambda r: r.git(r.work, "config", "push.default", "upstream")))
    case("denied: outside refs/heads", "git push origin HEAD:refs/remotes/origin/main", False,
         add("a.txt", "ok\n"))
    case("denied: CCGUARD_PROTECTED_BRANCHES", "git push origin feat/x", False,
         add("a.txt", "ok\n"), extra={"CCGUARD_PROTECTED_BRANCHES": "dev, feat/x"})

    def develop_default(repo):
        repo.git(repo.work, "push", "-q", "origin", "main:develop")
        repo.git(repo.remote, "symbolic-ref", "HEAD", "refs/heads/develop")
        repo.commit("a.txt", "ok\n")
    case("denied: remote default branch found by ls-remote", "git push origin HEAD:develop",
         False, develop_default)


def test_rewrites_tags_deletions_denied():
    amended = steps(add("a.txt", "ok\n"), published,
                    lambda r: r.git(r.work, "commit", "-q", "--amend", "-m", "amended"))
    for cmd in ("git push -f origin feat/x", "git push --force-with-lease origin feat/x",
                "git push origin +feat/x"):
        case(f"denied: {cmd}", cmd, False, amended)
    for cmd in ("git push origin :feat/x", "git push --delete origin feat/x"):
        case(f"denied: {cmd}", cmd, False, steps(add("a.txt", "ok\n"), published))
    case("denied: a tag", "git push origin v1", False,
         lambda r: r.git(r.work, "tag", "v1"))
    case("denied: --tags", "git push --tags origin", False,
         lambda r: r.git(r.work, "tag", "v1"))


def test_not_claude():
    case("allowed: a push to main by the user (no CLAUDECODE)", "git push origin HEAD:main",
         True, add("a.txt", "ok\n"), claude=False)


# ----------------------------------------------------------------- content

def conflict_resolved_with(text):
    def setup(repo):
        repo.commit("a.txt", "feat\n")
        repo.git(repo.work, "switch", "-q", "-c", "other", "main")
        repo.commit("a.txt", "other\n")
        repo.git(repo.work, "switch", "-q", "feat/x")
        repo.git(repo.work, "merge", "-q", "other", check=False)
        with open(os.path.join(repo.work, "a.txt"), "w") as f:
            f.write(text)
        repo.git(repo.work, "add", "a.txt")
        repo.git(repo.work, "commit", "-q", "--no-edit")
    return setup


def private(repo, skip=()):
    repo.config(private=[r"secret-handle"], skip=skip)


def test_content():
    err = case("denied: token in an added line", "git push origin feat/x", False,
               add("conf.txt", f"token={TOKEN}\n"))
    check("token value not echoed", TOKEN not in err, err)
    case("denied: line starting with '++ '", "git push origin feat/x", False,
         add("n.txt", f"++ {TOKEN}\n"))
    case("denied: secret only in a conflict resolution", "git push origin feat/x", False,
         conflict_resolved_with(f"{TOKEN}\n"))
    case("allowed: clean conflict resolution", "git push origin feat/x", True,
         conflict_resolved_with("both\n"))
    case("denied: own home path", "git push origin feat/x", False, add("a.md", "/home/alice/x\n"))
    case("allowed: someone else's home path", "git push origin feat/x", True,
         add("a.md", "/home/bob/x\n"))
    case("denied: private pattern in a message", "git push origin feat/x", False,
         steps(private, add("a.txt", "x\n", "for SECRET-HANDLE")))
    case("allowed: private pattern, remote skipped", "git push origin feat/x", True,
         steps(lambda r: private(r, skip=[r"remote\.git$"]), add("a.txt", "secret-handle\n")))
    case("denied: renamed to a private name", "git push origin feat/x", False,
         steps(add("plain.md", "x\n"), published, private,
               lambda r: r.git(r.work, "mv", "plain.md", "by-secret-handle.md"),
               lambda r: r.git(r.work, "commit", "-q", "-m", "rename")))
    case("denied: empty new file with a private name", "git push origin feat/x", False,
         steps(private, add("secret-handle notes.md", "")))
    case("allowed: commits already on the remote are not scanned", "git push origin feat/x",
         True, steps(add("a.md", "/home/alice/x\n"), published, add("b.md", "clean\n")))
    case("allowed: push by URL scans only what the remote lacks", "git push {remote} feat/x",
         True, steps(add("a.md", "/home/alice/x\n"), published, add("b.md", "clean\n")))
    case("denied: more commits than the cap", "git push origin feat/x", False,
         steps(add("a", "1\n"), add("b", "2\n"), add("c", "3\n")),
         extra={"CCGUARD_PUSH_GATE_MAX_COMMITS": "2"})


# ---------------------------------------------------------- hook chaining

def test_repository_hooks_still_run():
    def local_hook(body):
        def setup(repo):
            path = os.path.join(repo.work, ".git", "hooks", "pre-push")
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
            repo.commit("a.txt", "ok\n")
        return setup
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(Env(base))
        marker = os.path.join(base, "seen")
        local_hook(f"cat > '{marker}'\n")(repo)
        rc, err = repo.push("git push origin feat/x")
        seen = open(marker).read() if os.path.exists(marker) else ""
        check("repository pre-push runs with the same input",
              rc == 0 and "refs/heads/feat/x" in seen, (rc, err, seen))
    case_rc = []
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(Env(base))
        local_hook("exit 1\n")(repo)
        before = repo.remote_refs()
        rc, _ = repo.push("git push origin feat/x", claude=False)
        case_rc.append(rc)
        check("a failing repository pre-push still stops the push",
              rc != 0 and repo.remote_refs() == before, rc)
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(Env(base))
        path = os.path.join(repo.work, ".git", "hooks", "pre-commit")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(path, 0o755)
        with open(os.path.join(repo.work, "b.txt"), "w") as f:
            f.write("x\n")
        repo.git(repo.work, "add", "b.txt")
        rc = repo.git(repo.work, "commit", "-q", "-m", "x", check=False).returncode
        check("other repository hooks run through core.hooksPath", rc != 0, rc)


def test_installer():
    with tempfile.TemporaryDirectory() as base:
        world = Env(base, hooks_path=False)
        out = world.install()
        names = os.listdir(world.hooks_dir)
        check("installer writes executable hooks",
              "pre-push" in names and "pre-commit" in names and
              all(os.access(os.path.join(world.hooks_dir, n), os.X_OK) for n in names), names)
        check("installer tells the session when core.hooksPath is not set",
              "core.hooksPath" in out.stdout, out.stdout)
    with tempfile.TemporaryDirectory() as base:
        out = Env(base).install()
        check("installer is silent once core.hooksPath points at it", out.stdout.strip() == "",
              out.stdout)


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
