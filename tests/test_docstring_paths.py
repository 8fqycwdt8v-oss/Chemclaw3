"""Every module path a docstring or comment points at must be a file that exists.

Prose is how this codebase navigates: a module opens by naming the two or three modules a reader
has to hold alongside it (`durable/artifact_eviction.py` cites the pruner it must not duplicate,
`core/turn_signals.py` cites the event types it feeds). That only works while the names resolve.

D-148 renamed five packages and moved modules between them, and the ~1200 imports were carried
along by tools that understand imports. The pointers inside docstrings are not imports, so nothing
carried them: 78 of them across 51 files survived the restructure, naming `workflows/`, `workers/`,
`service/` and `agents/` — directories that no longer exist. Two had been *rewritten* by a blind
substitution into `…/chemclaw.durable.py`, a filename that never existed anywhere. `mypy --strict`
cannot see prose and neither can `ruff`, so the tree was fully green while its own map pointed at
nothing, which is the worst state for a document whose only job is to orient someone.

Six other declarations in this repository are guarded against the live surface — `kg-validate`,
`skill-validate`, `connector-validate`, `template-validate`, `prose-validate`, `eln-validate`. This
class had no guard, which is exactly why it drifted through a rename in silence. This is that guard
(D-149).

**Scope, and why it is drawn here.** Only backticked paths ending in `.py` are checked, and only
under `src/` and `tests/`. A backtick is the repository's own marker for "this is a name, not
English", so the rule stays precise instead of heuristic. `docs/decisions/` is deliberately outside
the scope: the ADR record is append-only and its stale paths are *accurate about the past*, which
`docs/README.md` states as policy. A path containing a placeholder segment (`connectors/<name>/…`)
does not match either, and should not: it names a shape, not a file.

**A pointer names the file as it is now, including in past-tense sentences.** "They used to live in
`connectors/calc/specs.py`" reads oddly for a second, and it is still the right trade: the sentence
exists to tell a reader where the code is, and the old name is preserved in the ADR that renamed it.
The alternative — an exemption for prose that *sounds* historical — is a hole big enough to drive
the next restructure through.

A file that was **deleted** rather than moved is the one case that cannot be rewritten, because
there is no current name to rewrite to, and naming it is often the whole point of the sentence
(`agents/job_status.py` is what `agent/durable_tools.py` exists to explain the absence of). Those
go in `_REMOVED` below, one line each — the same explicit-allowlist shape
`cli/validate_prose_contract.py` uses, and for the same reason: adding an entry should cost a
review conversation, which is the friction this check exists to create.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEARCH_ROOTS = (_REPO_ROOT / "src", _REPO_ROOT / "tests")

# A backticked path with at least one directory segment, ending in `.py`. Requiring a separator is
# what keeps this off bare module names (`config.py` alone is ambiguous between four packages and
# is not a pointer anyone can follow anyway).
_POINTER = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*(?:/[A-Za-z0-9_.-]+)+\.py)`")

# Where a pointer is allowed to resolve, in the order a reader would try them: inside the package,
# then spelled from `src/`, then from the repository root (`tests/…`, `deploy/…`, `examples/…`).
_RESOLUTION_BASES = (_REPO_ROOT / "src" / "chemclaw", _REPO_ROOT / "src", _REPO_ROOT)

# Files this system deliberately no longer has, which prose still names because their *absence* is
# the thing being explained. Each was deleted, not moved, so there is no current path to point at.
_REMOVED = frozenset(
    {
        # The four bespoke durable adapters the connector seam replaced (D-118). Three modules
        # explain their own shape by contrast with these, which is worth more than the names cost.
        "agents/job_status.py",
        "agents/qm_tools.py",
        "agents/safety_tools.py",
        # The ELN-specific adapter registry, folded into the generic `DataSource` seam
        # (DUP-1/D-120).
        "eln/registry.py",
    }
)


def _source_files() -> list[Path]:
    """Every first-party Python file whose prose this rule covers."""
    return sorted(path for root in _SEARCH_ROOTS for path in root.rglob("*.py"))


def _dangling(path: Path) -> list[str]:
    """The pointers in `path` that resolve to no file, in order of appearance."""
    return [
        pointer
        for pointer in _POINTER.findall(path.read_text())
        if pointer not in _REMOVED
        and not any((base / pointer).is_file() for base in _RESOLUTION_BASES)
    ]


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_every_backticked_module_pointer_resolves(path: Path) -> None:
    """A file cited in prose must exist, or the prose is a map to a deleted room."""
    dangling = _dangling(path)
    assert not dangling, (
        f"{path.relative_to(_REPO_ROOT)} points at {len(dangling)} file(s) that do not exist: "
        f"{', '.join(dangling)}. Name the current location; the old name is in the ADR that "
        "moved it."
    )


def test_the_rule_has_something_to_check() -> None:
    """Guard against the failure mode of the check itself: a pattern that matches nothing.

    D-148's post-mortem named this exactly — after a rename, a test that iterates a now-empty set
    reports green while asserting nothing, and neither type checking nor linting can see it. So the
    corpus is asserted non-trivial rather than assumed to be.
    """
    found = sum(len(_POINTER.findall(path.read_text())) for path in _source_files())
    assert found > 100, f"only {found} backticked module pointers found — has the pattern rotted?"
