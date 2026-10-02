#!/usr/bin/env python3
"""PreToolUse(Edit|Write|MultiEdit|NotebookEdit) guard — ccguard's own files are the user's.

Denies file edits under the ccguard git hooks directory
(${XDG_DATA_HOME:-~/.local/share}/ccguard), the ccguard config directory
(${XDG_CONFIG_HOME:-~/.config}/ccguard) and the installed plugin itself:
changing them would change what the push gate lets through. Bash writes to
the same places are denied by push-gate.py. Exit 2 denies; 0 allows.
"""

import json
import os
import sys

HOME = os.path.expanduser("~")
PROTECTED = [
    os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "ccguard"),
    os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "ccguard"),
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # the installed plugin
]


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def main():
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    except (ValueError, AttributeError):
        return 0
    if not path:
        return 0
    path = os.path.join(data.get("cwd") or os.getcwd(), os.path.expanduser(path))
    if "/plugins/cache/" not in PROTECTED[2] and inside(path, PROTECTED[2]):
        return 0  # running from a development checkout: the checkout is fair game
    if any(inside(path, root) for root in PROTECTED):
        print("BLOCKED (ccguard): this file belongs to ccguard's push gate (hooks, gate or "
              "config). Changing it changes what the gate lets through; that is the user's "
              "call.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
