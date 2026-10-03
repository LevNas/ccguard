#!/usr/bin/env python3
"""SessionStart: keep the ccguard git hooks directory up to date.

Writes the hooks described in lib/git_hooks.py into
${XDG_DATA_HOME:-~/.local/share}/ccguard/git-hooks. push-gate.py compares
the same directory with the same content before every Bash command and
restores it when it differs.

Setting core.hooksPath is left to the user: it changes git for every
repository. When it does not point here, the session is told how.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from git_hooks import TARGET, install  # noqa: E402


def configured():
    try:
        value = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(value) and os.path.realpath(os.path.expanduser(value)) == os.path.realpath(TARGET)


def main():
    try:
        install()
    except OSError as e:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"ccguard could not write its git hooks to {TARGET} "
                                 f"({e.__class__.__name__}); pushes are blocked by push-gate.",
        }}))
        return 0
    if not configured():
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "ccguard: the pre-push gate is not active, so push-gate will "
                                 "deny git push. Ask the user to run (once): "
                                 f"git config --global core.hooksPath {TARGET}",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
