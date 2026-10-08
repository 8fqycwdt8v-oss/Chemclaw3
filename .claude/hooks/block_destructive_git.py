#!/usr/bin/env python3
"""PreToolUse hook: refuse the git verbs that discard uncommitted work (tasks/lessons.md rule 1).

Reads the Claude Code hook payload on stdin; exit 2 blocks the Bash call and shows stderr to the
model. The command is tokenised the way a shell reads it, so quoted text and heredoc bodies are
data, never commands, while `bash -c "..."`, `sh -c` and `eval` strings are scanned too.

Blocked: `git checkout` of paths (`--`, `.`, an existing file, `--ours`/`--theirs`, `-p`) or with
`-f`/`--force`; `git switch` with `-f`/`--force`/`--discard-changes`; `git restore` (except
`--staged` alone); `git stash` that hides or destroys work (bare, `push`, `save`, `drop`,
`clear`); `git reset --hard`; `git clean` with `-f`. Allowed: `stash list/show/pop/apply/branch`,
`checkout -b`, `git -C <dir>` resolved against `<dir>`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"}
_PUNCTUATION = ";&|()\n"
_SAFE_STASH = {"list", "show", "pop", "apply", "branch", "create", "store"}
_PREFIXES = {"sudo", "env", "command", "time", "nohup", "exec", "builtin"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_MAX_DEPTH = 4


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are data fed to a command, not commands."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            word, dash = pending[0]
            if (line.strip() if dash else line) == word:
                pending.pop(0)
            continue
        out.append(line)
        for match in _HEREDOC.finditer(line):
            pending.append((match.group(2), "<<-" in match.group(0)))
    return "\n".join(out)


def _segments(command: str) -> list[list[str]]:
    """Split a command into simple commands, honouring quotes; fall back to a naive split."""
    command = _strip_heredocs(command).replace("`", ";")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = re.split(r"(&&|\|\||[;|&\n()])", command)
        tokens = [piece.strip() for piece in tokens if piece.strip()]
        return [piece.split() for piece in tokens if piece not in _SEPARATORS]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS or (token and set(token) <= set(_PUNCTUATION)):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _git_args(tokens: list[str], cwd: str) -> tuple[list[str], str] | None:
    """Return the git subcommand with its args and the directory it runs in, or None."""
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        if rest[0] == "-C" and len(rest) > 1:
            cwd = os.path.join(cwd, rest[1])
        rest = rest[2:] if rest[0] in _GLOBAL_WITH_VALUE else rest[1:]
    return rest, cwd


def _flags(opts: list[str]) -> set[str]:
    """Every single-letter flag present, from clusters like `-fq`, up to a `--`."""
    letters: set[str] = set()
    for opt in opts:
        if opt == "--":
            break
        if opt.startswith("-") and not opt.startswith("--"):
            letters.update(opt[1:])
    return letters


def _reason(args: list[str], cwd: str) -> str | None:
    """Name why this git invocation can discard uncommitted work, or None if it cannot."""
    if not args:
        return None
    verb, opts = args[0], args[1:]
    letters = _flags(opts)
    if verb == "checkout":
        if any(o in {"-b", "-B", "--orphan"} for o in opts):
            return None
        if "f" in letters or "--force" in opts:
            return "`git checkout -f` throws away local changes"
        if {"--ours", "--theirs"} & set(opts) or "p" in letters or "--patch" in opts:
            return "`git checkout --ours/--theirs/-p` overwrites working-tree content"
        if "--" in opts or "." in opts:
            return "`git checkout` of paths discards their uncommitted changes"
        for o in opts:
            if not o.startswith("-") and os.path.exists(os.path.join(cwd, o)):
                return f"`git checkout {o}` discards that path's uncommitted changes"
        return None
    if verb == "switch":
        if "f" in letters or {"--force", "--discard-changes"} & set(opts):
            return "`git switch --discard-changes` throws away local changes"
        return None
    if verb == "restore":
        staged_only = bool({"--staged", "-S"} & set(opts)) and not {"--worktree", "-W"} & set(opts)
        return None if staged_only else "`git restore` discards working-tree changes"
    if verb == "stash":
        return None if opts and opts[0] in _SAFE_STASH else "`git stash` hides or destroys work"
    if verb == "reset" and "--hard" in opts:
        return "`git reset --hard` discards every uncommitted change"
    if verb == "clean":
        dry = "n" in letters or "--dry-run" in opts
        if not dry and ("f" in letters or "--force" in opts):
            return "`git clean -f` deletes untracked files"
    return None


def _scan(command: str, cwd: str, depth: int = 0) -> str | None:
    """Return the first reason any simple command in `command` discards work."""
    for tokens in _segments(command):
        while tokens and (tokens[0] in _PREFIXES or _ASSIGNMENT.match(tokens[0])):
            tokens = tokens[1:]
        if not tokens:
            continue
        head = os.path.basename(tokens[0])
        if depth < _MAX_DEPTH:
            if head in _SHELLS:
                for index, token in enumerate(tokens[1:], start=1):
                    is_c = token.startswith("-") and not token.startswith("--") and "c" in token
                    if is_c and index + 1 < len(tokens):
                        found = _scan(tokens[index + 1], cwd, depth + 1)
                        if found:
                            return found
                        break
            elif head == "eval":
                found = _scan(" ".join(tokens[1:]), cwd, depth + 1)
                if found:
                    return found
        parsed = _git_args(tokens, cwd)
        reason = _reason(*parsed) if parsed is not None else None
        if reason:
            return reason
    return None


def main() -> int:
    """Block the Bash call (exit 2) when any simple command in it is a destructive git verb."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    cwd = str(payload.get("cwd") or os.getcwd())
    reason = _scan(command, cwd)
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
