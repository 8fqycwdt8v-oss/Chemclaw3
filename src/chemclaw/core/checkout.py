"""Whether a path is the git checkout this process is running from.

`kg/git_writer.py` refuses to commit notes into the running application's own tree, and
`core/netguard.py` skips deriving that tree's git remote into the egress allowlist; they must
agree on every spelling of the same directory, so both ask this one predicate. In `core` because
the netguard side is in the kernel, which may not import `kg`.
"""

from __future__ import annotations

from pathlib import Path


def process_checkout_root() -> Path | None:
    """The root of the git checkout this process runs from, or `None` outside any checkout.

    The nearest ancestor of the CWD containing `.git`.
    """
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def is_the_processes_own_checkout(repo_dir: str) -> bool:
    """Whether `repo_dir` resolves to the CWD or to the root of this process's own checkout.

    Resolved, so every spelling of one directory (trailing slash, `src/..`, absolute, symlink)
    answers
    alike. An unresolvable path answers `False`.
    """
    if not repo_dir:
        return False
    try:
        resolved = Path(repo_dir).resolve()
    except OSError:
        return False
    return resolved == Path.cwd().resolve() or resolved == process_checkout_root()
