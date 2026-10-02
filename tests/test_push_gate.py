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
TOKEN = "gh" + "p_" + "A" * 36
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)
        if detail != "":
            print(f"      {detail!r}"[:300])


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "init.defaultBranch=main", "-C", cwd, *args],
                          check=check, capture_output=True, text=True)


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

    def commit(self, path, text, message="change"):
        full = os.path.join(self.work, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        git(self.work, "add", path)
        git(self.work, "commit", "-q", "-m", message)

    def config(self, private=(), skip=(), raw=None):
        os.makedirs(os.path.join(self.config_home, "ccguard"), exist_ok=True)
        with open(os.path.join(self.config_home, "ccguard", "push-gate.json"), "w") as f:
            f.write(raw if raw is not None else
                    json.dumps({"private_patterns": list(private), "skip_remotes": list(skip)}))

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w", encoding="utf-8") as f:
            f.write(text)

    def run(self, command, cwd=None, env=None):
        full_env = dict(os.environ, XDG_CONFIG_HOME=self.config_home, USER="alice")
        full_env.pop("CCGUARD_PROTECTED_BRANCHES", None)
        full_env.pop("CCGUARD_PUSH_GATE_MAX_COMMITS", None)
        full_env.update(env or {})
        payload = {"tool_input": {"command": command}, "cwd": cwd or self.work}
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=full_env, timeout=120)
        return proc.returncode, proc.stderr


def case(name, command, expected, setup=None, cwd=None, env=None):
    with tempfile.TemporaryDirectory() as base:
        repo = Repo(base)
        if setup:
            setup(repo)
        rc, err = repo.run(command.replace("{work}", repo.work).replace("{base}", base),
                           cwd=cwd and cwd(repo), env=env)
        check(name, rc == expected, (rc, err))
        if os.environ.get("VERBOSE") and err:
            print("      " + err.strip().removeprefix("BLOCKED (ccguard push-gate): ")[:150])
        return err


def add(path, text, message="change"):
    return lambda repo: repo.commit(path, text, message)


def steps(*fns):
    def run(repo):
        for fn in fns:
            fn(repo)
    return run


# ------------------------------------------------------------- destinations

def test_feature_branch_allowed():
    case("push feature branch", "git push -u origin feat/x", 0, add("a.txt", "ok\n"))
    case("bare push with upstream", "git push", 0,
         steps(add("a.txt", "ok\n"), lambda r: git(r.work, "push", "-q", "-u", "origin", "feat/x"),
               add("b.txt", "ok\n")))
    case("HEAD refspec", "git push origin HEAD", 0, add("a.txt", "ok\n"))
    case("to a new branch name", "git push origin feat/x:other", 0, add("a.txt", "ok\n"))
    case("dry run of a force push", "git push --dry-run -f origin feat/x", 0)
    case("redirections and a pipe", "git push origin feat/x 2>&1 | tail -1", 0, add("a.txt", "ok\n"))


def test_rewrites_and_bulk_denied():
    for cmd in ("git push -f origin feat/x", "git push --force origin feat/x",
                "git push --force-with-lease origin feat/x", "git push -uf origin feat/x",
                "git push origin +feat/x", "git push origin :feat/x",
                "git push --delete origin feat/x", "git push --all origin",
                "git push --mirror origin", "git push --tags origin",
                "git push --recurse-submodules=on-demand origin feat/x"):
        case(f"denied: {cmd}", cmd, 2)


def test_protected_destinations_denied():
    case("denied: origin main", "git push origin main", 2)
    case("denied: HEAD:main", "git push origin HEAD:main", 2)
    case("denied: refs/heads/main", "git push origin feat/x:refs/heads/main", 2)
    case("denied: bare push on main", "git push", 2,
         steps(lambda r: git(r.work, "switch", "-q", "main"), add("a.txt", "ok\n")))
    case("denied: bare push whose upstream is main (push.default=upstream)", "git push", 2,
         steps(add("a.txt", "ok\n"),
               lambda r: git(r.work, "branch", "-q", "--set-upstream-to=origin/main"),
               lambda r: git(r.work, "config", "push.default", "upstream")))
    case("denied: glob refspec", "git push origin 'refs/heads/*:refs/heads/*'", 2,
         add("a.txt", "ok\n"))
    case("denied: outside refs/heads", "git push origin HEAD:refs/remotes/origin/main", 2,
         add("a.txt", "ok\n"))
    case("denied: git -C dir push origin main", "git -C {work} push origin main", 2,
         cwd=lambda r: r.base)
    case("denied: cd then push main", "cd {work} && git push origin main", 2,
         cwd=lambda r: r.base)
    case("denied: tag refspec", "git push origin v1.0", 2, lambda r: git(r.work, "tag", "v1.0"))
    case("denied: CCGUARD_PROTECTED_BRANCHES", "git push origin feat/x", 2,
         add("a.txt", "ok\n"), env={"CCGUARD_PROTECTED_BRANCHES": "dev, feat/x"})
    case("denied: no upstream, cannot resolve", "git push", 2, add("a.txt", "ok\n"))


def test_shell_shapes():
    case("denied: second line pushes main", "git status\ngit push origin main", 2)
    case("denied: second line force-pushes", "git status\ngit push -f origin feat/x", 2)
    case("denied: push after a heredoc with an apostrophe",
         "cat <<'EOF' > note.txt\ndon't\nEOF\ngit push origin main", 2)
    case("allowed: feature push after a heredoc",
         "cat <<'EOF' > note.txt\ndon't\nEOF\ngit push origin feat/x", 0, add("a.txt", "ok\n"))
    case("denied: redirected push to main", "git push origin main >/dev/null 2>&1", 2)
    for cmd in ("sudo git push origin feat/x", "timeout 60 git push -f origin main",
                "echo feat/x | xargs git push origin"):
        case(f"denied: wrapped `{cmd}`", cmd, 2)
    for cmd in ("(git push origin main)", "if git push origin main; then echo ok; fi",
                "env -i X=1 git push origin main", "x=$(git push origin main)",
                "! git push origin main", "{ git push origin main; }"):
        case(f"denied: `{cmd}`", cmd, 2)
    case("allowed: echo mentioning git push", "echo 'git push origin main'", 0)
    case("allowed: git status", "git status && git log -1", 0)
    case("denied: unparseable push", "git push origin main 'unclosed", 2)


# ------------------------------------------------------------------ content

def test_secret_in_commits():
    err = case("denied: token in an added line", "git push origin feat/x", 2,
               add("conf.txt", f"token={TOKEN}\n"))
    check("token value not echoed", TOKEN not in err, err)
    case("denied: added line starting with '++ '", "git push origin feat/x", 2,
         add("notes.txt", f"++ {TOKEN}\n"))
    case("denied: branch named like a directory",
         "git push origin docs", 2,
         steps(lambda r: git(r.work, "switch", "-q", "-c", "docs"), add("docs/a.md", f"{TOKEN}\n")))
    case("allowed: clean branch named like a directory", "git push origin docs", 0,
         steps(lambda r: git(r.work, "switch", "-q", "-c", "docs"), add("docs/a.md", "ok\n")))


def conflict_resolved_with(text):
    def setup(repo):
        repo.commit("a.txt", "feat\n")
        git(repo.work, "switch", "-q", "-c", "other", "main")
        repo.commit("a.txt", "other\n")
        git(repo.work, "switch", "-q", "feat/x")
        git(repo.work, "merge", "-q", "other", check=False)
        repo.write("a.txt", text)
        git(repo.work, "add", "a.txt")
        git(repo.work, "commit", "-q", "--no-edit")
    return setup


def test_merge_resolution():
    case("denied: secret only in a conflict resolution", "git push origin feat/x", 2,
         conflict_resolved_with(f"{TOKEN}\n"))
    case("allowed: clean conflict resolution", "git push origin feat/x", 0,
         conflict_resolved_with("both\n"))


def test_commit_cap():
    case("denied: more commits than the cap", "git push origin feat/x", 2,
         steps(add("a", "1\n"), add("b", "2\n"), add("c", "3\n")),
         env={"CCGUARD_PUSH_GATE_MAX_COMMITS": "2"})
    case("allowed: within the cap", "git push origin feat/x", 0,
         steps(add("a", "1\n"), add("b", "2\n")), env={"CCGUARD_PUSH_GATE_MAX_COMMITS": "2"})


def test_private_pattern_and_skip():
    def setup(repo, skip=()):
        repo.config(private=[r"secret-handle"], skip=skip)
        repo.commit("note.md", "written by secret-handle\n")
    case("denied: private pattern in a diff", "git push origin feat/x", 2, setup)
    case("allowed: private pattern, remote skipped", "git push origin feat/x", 0,
         lambda r: setup(r, skip=[r"remote\.git$"]))
    case("denied: private pattern in a message (case-insensitive)", "git push origin feat/x", 2,
         steps(lambda r: r.config(private=[r"secret-handle"]), add("a.txt", "x\n", "for SECRET-HANDLE")))
    case("denied: private pattern in a file name", "git push origin feat/x", 2,
         steps(lambda r: r.config(private=[r"secret-handle"]), add("by-secret-handle.md", "x\n")))


def test_home_path():
    case("denied: own home path", "git push origin feat/x", 2, add("a.md", "see /home/alice/notes\n"))
    case("allowed: someone else's home path", "git push origin feat/x", 0,
         add("a.md", "see /home/bob/notes\n"))
    case("allowed: commits already on the remote are not scanned", "git push origin feat/x", 0,
         steps(add("a.md", "/home/alice/x\n", "old"),
               lambda r: git(r.work, "push", "-q", "origin", "feat/x"),
               add("b.md", "clean\n", "new")))


def test_bad_config_denies():
    case("denied: invalid private pattern", "git push origin feat/x", 2,
         steps(lambda r: r.config(private=["("]), add("a.txt", "ok\n")))
    case("denied: invalid skip_remotes", "git push origin feat/x", 2,
         steps(lambda r: r.config(skip=["("]), add("a.txt", "ok\n")))
    case("denied: config is not JSON", "git push origin feat/x", 2,
         steps(lambda r: r.config(raw="{not json"), add("a.txt", "ok\n")))


# ----------------------------------------------------------------------- gh

def gh_setup(body="", skip=()):
    def setup(repo):
        repo.config(private=[r"secret-handle"], skip=skip)
        repo.write("body.md", body)
    return setup


def test_gh_text():
    case("denied: gh pr create body file", "gh pr create --title t --body-file body.md", 2,
         gh_setup("hi secret-handle"))
    case("allowed: clean gh pr body", "gh pr create --title t --body-file body.md", 0, gh_setup("hi"))
    case("allowed: body file under the home path, clean content",
         "gh pr create -t t -F {work}/body.md", 0, gh_setup("hi"))
    case("denied: inline body", "gh issue create -t x -b 'by secret-handle'", 2, gh_setup())
    case("denied: attached short body", "gh pr create -t x -b'by secret-handle'", 2, gh_setup())
    case("denied: title after a value-less flag", "gh pr create -d --title 'secret-handle'", 2,
         gh_setup())
    case("denied: heredoc on stdin",
         "gh pr comment 3 --body-file - <<'EOF'\nby secret-handle\nEOF", 2, gh_setup())
    case("denied: gh api field", "gh api repos/o/r/issues -f body='secret-handle'", 2, gh_setup())
    case("denied: gh release notes", "gh release create v1 --notes 'secret-handle'", 2, gh_setup())
    case("denied: gh pr merge body", "gh pr merge 3 --squash --body 'secret-handle'", 2, gh_setup())
    case("denied: gh issue close comment", "gh issue close 3 -c 'secret-handle'", 2, gh_setup())
    case("denied: gh gist file", "gh gist create body.md", 2, gh_setup("secret-handle"))
    case("denied: wrapped gh", "echo 3 | xargs gh pr comment -b hi", 2, gh_setup())
    case("allowed: gh issue to a skipped repo",
         "gh issue create -R someone/private -t x -b 'by secret-handle'", 0,
         gh_setup(skip=[r"someone/private"]))
    case("allowed: gh pr view", "gh pr view 3", 0, gh_setup())


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
