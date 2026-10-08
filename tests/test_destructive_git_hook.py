"""The destructive-git hook blocks every verb that discards uncommitted work and nothing else.

Each case drives the real script through a subprocess with a Claude Code hook payload, so the
tokenising, heredoc handling and exit codes are exercised as the harness uses them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "block_destructive_git.py"


def _run(command: str, cwd: Path, tool: str = "Bash") -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A directory holding an existing file, so `git checkout notes.txt` is a path checkout."""
    (tmp_path / "notes.txt").write_text("x")
    return tmp_path


BLOCKED = [
    "git checkout -- notes.txt",
    "git checkout .",
    "git checkout notes.txt",
    "git checkout -f main",
    "git checkout --force main",
    "git checkout -fq main",
    "git checkout --theirs notes.txt",
    "git checkout -p",
    "git switch -f main",
    "git switch --force main",
    "git switch --discard-changes main",
    "git restore notes.txt",
    "git restore --worktree notes.txt",
    "git restore --staged --worktree notes.txt",
    "git stash",
    "git stash push",
    "git stash save wip",
    "git stash drop",
    "git stash clear",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git clean -f",
    "git clean -fd",
    "git clean --force",
    "git add -A && git checkout .",
    "make test; git reset --hard",
    "true || git clean -fdx",
    "echo hi | git checkout .",
    "FOO=1 git checkout .",
    "sudo git reset --hard",
    "env GIT_TRACE=1 git restore notes.txt",
    "/usr/bin/git checkout .",
    "git -c core.editor=true checkout .",
    'bash -c "git reset --hard"',
    "sh -c 'git checkout .'",
    'bash -lc "make lint && git clean -fd"',
    'eval "git restore notes.txt"',
    "bash -c \"bash -c 'git reset --hard'\"",
    "(cd sub && git checkout .)",
    "echo `git reset --hard`",
    "echo $(git checkout .)",
    "git status\ngit checkout .",
]

ALLOWED = [
    "git status",
    "git diff",
    "git log --oneline -5",
    "git add -A && git commit -m wip",
    "git checkout -b feature/x",
    "git checkout -B feature/x origin/main",
    "git checkout main",
    "git switch main",
    "git switch -c feature/x",
    "git restore --staged notes.txt",
    "git restore -S notes.txt",
    "git stash list",
    "git stash show -p",
    "git stash pop",
    "git stash apply",
    "git stash branch recovered",
    "git reset --soft HEAD~1",
    "git reset HEAD notes.txt",
    "git clean -n",
    "git clean -fn",
    "git clean --dry-run -fd",
    "git push -u origin HEAD",
    "git fetch origin main",
    'git commit -m "docs: explain why git checkout . is blocked"',
    "git commit -m 'git reset --hard; git clean -f'",
    'echo "git checkout ."',
    "git commit -F- <<'EOF'\nfix: never run git checkout .\ngit reset --hard is blocked\nEOF",
    "cat <<EOF > notes.md\ngit stash\nEOF\ngit add notes.md",
    'bash -c "echo hello"',
    'sh -c "git status"',
    "ls && make test",
    "",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_a_command_that_discards_uncommitted_work_is_blocked(command: str, work: Path) -> None:
    result = _run(command, work)
    assert result.returncode == 2, f"{command!r} should be blocked: {result.stderr!r}"
    assert "Blocked" in result.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_a_command_that_cannot_discard_work_is_allowed(command: str, work: Path) -> None:
    result = _run(command, work)
    assert result.returncode == 0, f"{command!r} should pass: {result.stderr!r}"


def test_git_dash_c_resolves_a_path_against_the_directory_it_names(work: Path) -> None:
    """`git -C <dir> checkout notes.txt` is a path checkout only if the file is in <dir>."""
    other = work / "elsewhere"
    other.mkdir()
    assert _run(f"git -C {other} checkout notes.txt", work).returncode == 0
    assert _run(f"git -C {work} checkout notes.txt", other).returncode == 2


def test_other_tools_and_malformed_payloads_are_ignored(work: Path) -> None:
    assert _run("git reset --hard", work, tool="Read").returncode == 0
    bad = subprocess.run(
        [sys.executable, str(HOOK)], input="{not json", capture_output=True, text=True
    )
    assert bad.returncode == 0


def test_an_unbalanced_quote_is_still_scanned(work: Path) -> None:
    """A command the shell would reject is read leniently rather than waved through."""
    assert _run("git checkout . && echo 'unterminated", work).returncode == 2
