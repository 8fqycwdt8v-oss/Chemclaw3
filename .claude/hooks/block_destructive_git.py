#!/usr/bin/env python3
"""PreToolUse hook: refuse the git verbs that discard uncommitted work (tasks/lessons.md rule 1).

Reads the Claude Code hook payload on stdin; exit 2 blocks the Bash call and shows stderr to the
model. Blocked: `git checkout` of paths (`--`, `.`, or an existing file), `git restore` (except
`--staged` alone), `git stash` (except list/show), `git reset --hard`, `git clean` with -f.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

_SEPARATORS = re.compile(r"&&|\|\||[;|&\n()]")
_SAFE_STASH = {"list", "show"}
_PREFIXES = {"sudo", "env", "command", "time", "nohup"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _git_args(tokens: list[str]) -> list[str] | None:
    """Return the git subcommand and its args, skipping `git -C dir`-style global options."""
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in {"-C", "-c", "--git-dir", "--work-tree"} else rest[1:]
    return rest


def _reason(args: list[str], cwd: str) -> str | None:
    """Name why this git invocation can discard uncommitted work, or None if it cannot."""
    if not args:
        return None
    verb, opts = args[0], args[1:]
    if verb == "checkout":
        if any(o in {"-b", "-B", "--orphan"} for o in opts):
            return None
        if "--" in opts or "." in opts:
            return "`git checkout` of paths discards their uncommitted changes"
        for o in opts:
            if not o.startswith("-") and os.path.exists(os.path.join(cwd, o)):
                return f"`git checkout {o}` discards that path's uncommitted changes"
        return None
    if verb == "restore":
        staged_only = bool({"--staged", "-S"} & set(opts)) and not {"--worktree", "-W"} & set(opts)
        return None if staged_only else "`git restore` discards working-tree changes"
    if verb == "stash":
        return None if opts and opts[0] in _SAFE_STASH else "`git stash` hides uncommitted work"
    if verb == "reset" and "--hard" in opts:
        return "`git reset --hard` discards every uncommitted change"
    if verb == "clean":
        short = "".join(o[1:] for o in opts if o.startswith("-") and not o.startswith("--"))
        dry = "n" in short or "--dry-run" in opts
        if not dry and ("f" in short or "--force" in opts):
            return "`git clean -f` deletes untracked files"
    return None


def main() -> int:
    """Block the Bash call (exit 2) when any segment of the command is a destructive git verb."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    cwd = str(payload.get("cwd") or os.getcwd())
    for segment in _SEPARATORS.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        while tokens and (tokens[0] in _PREFIXES or _ASSIGNMENT.match(tokens[0])):
            tokens = tokens[1:]
        args = _git_args(tokens)
        reason = _reason(args, cwd) if args is not None else None
        if reason:
            print(
                f"Blocked: {reason} (tasks/lessons.md rule 1). Commit first, or copy the file "
                "aside with `cp` and back. If you really mean it, ask the user to run it.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
