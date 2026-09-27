"""Whether a path is the git checkout this process is running from.

Two callers ask this and they must agree. `kg/git_writer.py` refuses to commit notes into the
running application's own tree — a write there would publish agent-authored notes into the source
repository — and `core/netguard.py` skips deriving that tree's git remote into the egress
allowlist, on the ground that the writer refuses anyway so there is no destination to permit.

**They disagreed, which is how the second one became a widening.** The netguard side was a bare
`repo_dir == "."`, so `./`, `$PWD`, an absolute path to the same directory, `src/..` and a symlink
all derived the *source* repository's host while the writer went on refusing them — the exact
outcome its own comment said the check prevented. One predicate, asked the same way by both.

`core/` rather than beside the writer because `core/` may not import `kg/`
(`tests/test_layering.py::test_the_kernel_imports_no_sibling`), and the netguard side is in the
kernel.
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

    Resolved rather than compared as a string, so every spelling of one directory answers alike —
    a trailing slash, `src/..`, an absolute path, a symlink. An unresolvable path answers `False`:
    it is not this checkout, and the caller that cares raises its own error for it.
    """
    if not repo_dir:
        return False
    try:
        resolved = Path(repo_dir).resolve()
    except OSError:
        return False
    return resolved == Path.cwd().resolve() or resolved == process_checkout_root()
