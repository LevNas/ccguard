#!/usr/bin/env python3
"""git pre-push gate — what a push from Claude Code may update and send.

Runs from the ccguard git hooks directory (see install_git_hooks.py) and
acts only when CLAUDECODE=1, i.e. for pushes started by Claude Code; a push
you run yourself is not touched. git hands the hook the refs it is about to
update after every alias, wrapper, refspec and push.default has been
resolved, so no shell parsing is involved:

    argv:  <remote name> <remote url>
    stdin: <local ref> <local oid> <remote ref> <remote oid>   (one per ref)

Denied: deletions, tags, refs outside refs/heads/, forced (non-fast-forward)
updates, and updates of `main`, `master`, the remote's default branch
(refs/remotes/<remote>/HEAD, else `git ls-remote --symref`) or
CCGUARD_PROTECTED_BRANCHES (comma-separated). The commits the remote does
not have yet are scanned for blocked content (lib/ccguard_content.py).

Fails closed: any error while checking denies the push (exit 1).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from ccguard_content import (  # noqa: E402
    Deny, content_patterns, git_out, run_git, scan_commits, set_budget,
)

ZERO = set("0")


def protected_branches(cwd, remote, url, configured):
    names = {"main", "master"}
    names.update(b.strip() for b in os.environ.get("CCGUARD_PROTECTED_BRANCHES", "").split(",")
                 if b.strip())
    default = None
    if configured:
        head = git_out(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if head and "/" in head:
            default = head.split("/", 1)[1]
    if default is None and url:
        listing = git_out(cwd, "ls-remote", "--symref", url, "HEAD")
        if listing is None:
            raise Deny("Could not find the remote's default branch (`git ls-remote` failed), so "
                       "the push cannot be checked. Run `git remote set-head <remote> --auto` "
                       "or push by hand.")
        for line in listing.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                default = line[len("ref: refs/heads/"):-len("\tHEAD")]
    if default:
        names.add(default)
    return names


def check(remote, url, lines):
    cwd = os.getcwd()
    configured = remote != url and git_out(cwd, "config", "--get", f"remote.{remote}.url") is not None
    protected = None
    patterns = None
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4:
            raise Deny(f"Unexpected pre-push input from git: {line[:80]!r}.")
        _local_ref, local_oid, remote_ref, remote_oid = fields
        if set(local_oid) <= ZERO:
            raise Deny(f"This push deletes `{remote_ref}`. Leave deletion to the user.")
        if remote_ref.startswith("refs/tags/"):
            raise Deny(f"This push creates or moves the tag `{remote_ref[10:]}`, which publishes "
                       "a release. Leave tags to the release step (with push.followTags, push "
                       "with --no-follow-tags).")
        if not remote_ref.startswith("refs/heads/"):
            raise Deny(f"This push updates `{remote_ref}`, outside refs/heads/. Push a branch.")
        branch = remote_ref[len("refs/heads/"):]
        if protected is None:
            protected = protected_branches(cwd, remote, url, configured)
        if branch in protected:
            raise Deny(f"This push updates the protected branch `{branch}`. Push a feature "
                       "branch and open a pull request; merging is the user's call.")
        new_ref = set(remote_oid) <= ZERO
        if not new_ref:
            known = git_out(cwd, "cat-file", "-e", f"{remote_oid}^{{commit}}") is not None
            ancestor = known and run_git(cwd, ["merge-base", "--is-ancestor",
                                               remote_oid, local_oid]).returncode == 0
            if not ancestor:
                raise Deny(f"This push rewrites `{branch}` (not a fast-forward). Push without "
                           "rewriting published history.")
        excludes = ([f"--remotes={remote}"] if configured else []) + ([] if new_ref else [remote_oid])
        revs = [local_oid, "--not", *excludes] if excludes else [local_oid]
        if patterns is None:
            patterns = content_patterns(url)
        scan_commits(cwd, revs, f"`{branch}`", patterns)


def main(argv):
    if os.environ.get("CLAUDECODE") != "1":
        return 0
    set_budget(50)
    remote = argv[1] if len(argv) > 1 else ""
    url = argv[2] if len(argv) > 2 else remote
    try:
        check(remote, url, sys.stdin.read().splitlines())
    except Deny as denial:
        print(f"BLOCKED (ccguard pre-push): {denial}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — a gate that crashed must not wave the push through
        print(f"BLOCKED (ccguard pre-push): the gate failed ({e.__class__.__name__}). "
              "Report the failure; do not bypass the hook.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
