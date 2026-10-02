#!/usr/bin/env python3
"""Self-tests for hooks/protect-files.py.

Run: python3 tests/test_protect_files.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "protect-files.py")
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)
        print(f"      {detail!r}"[:300])


def run(base, tool, path):
    env = dict(os.environ, XDG_DATA_HOME=os.path.join(base, "data"),
               XDG_CONFIG_HOME=os.path.join(base, "config"))
    payload = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": base}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                          text=True, env=env).returncode


def main():
    with tempfile.TemporaryDirectory() as base:
        for label, path, expected in (
                ("denied: generated pre-push hook", "data/ccguard/git-hooks/pre-push", 2),
                ("denied: push-gate.json", "config/ccguard/push-gate.json", 2),
                ("denied: path that climbs back in", "elsewhere/../config/ccguard/x", 2),
                ("allowed: another config dir", "config/other/x.json", 0),
                ("allowed: a project file", "src/main.py", 0)):
            rc = run(base, "Write", path)
            check(label, rc == expected, rc)
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
