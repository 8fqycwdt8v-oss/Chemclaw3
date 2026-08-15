"""Every module path and qualified name a docstring or comment points at must still exist.

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

**Two forms are checked, and the second was added because the first was not enough.** A path
(`agent/scratchpad.py`, optionally `::symbol`) and a fully-qualified name
(`chemclaw.agent.scratchpad.memory_prefix`). The gap the second closes is the one D-2026-08-15 hit
twice in a single session: `build_agent` had **zero definitions** and 32 references in source
docstrings, and nothing could see it, because a symbol is not a file. The measurement behind the
scope is beside `_QUALIFIED` — two broader rules were tried and rejected on their false-positive
rate, which is a property of the tree rather than a preference.

Proven non-vacuous by mutation rather than assumed: a deleted module, a missing function, a missing
class and a dangling `::symbol` each turn this red on their own, and the tree is green without them.

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

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEARCH_ROOTS = (_REPO_ROOT / "src", _REPO_ROOT / "tests")

# A backticked path with at least one directory segment, ending in `.py`. Requiring a separator is
# what keeps this off bare module names (`config.py` alone is ambiguous between four packages and
# is not a pointer anyone can follow anyway).
# The optional `::symbol` suffix is why this is a two-group pattern. It was a hole: a pointer
# written `agent/team.py::_AttributedSpecialist.invoke` did not match at all, because the original
# required the closing backtick immediately after `.py` — so two references to a deleted module
# survived the deletion that removed every other one, in the same commit whose whole subject was
# removing them (D-2026-08-15). A form that escapes the guard is worse than one the guard rejects.
_POINTER = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_.]*(?:/[A-Za-z0-9_.-]+)+\.py)(?:::([A-Za-z_][A-Za-z0-9_.]*))?`"
)

# A path in a *sibling repository*, written `Chemclaw3-mcp:servers/chem/tools.py`. Skipped, because
# this gate can only resolve what is in this checkout and a cross-repo citation is not a broken one.
#
# **The prefix is required rather than optional, and that is the point.** The alternative — letting
# any unresolvable path off when it "looks external" — would reopen exactly the hole this file
# closes. A reader following `Chemclaw3-mcp:...` knows which checkout to open; a reader following a
# bare path that silently resolves to nothing does not. The migration of scientific capability out
# of this tree (`CLAUDE.md`, "Where a capability belongs") makes these common enough to need a form,
# and one that is greppable per repository.
# Any other `prefix:path.py`. Rewritten to the bare path so the local check below sees it.
_PREFIXED = re.compile(r"`[A-Za-z0-9_-]+:([A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_.]*)?)`")

_CROSS_REPO = re.compile(
    r"`(Chemclaw3[A-Za-z0-9_-]*):([A-Za-z0-9_./-]+\.py)(?:::[A-Za-z_][A-Za-z0-9_.]*)?`"
)

# A fully-qualified first-party name: `chemclaw.agent.scratchpad.memory_prefix`, or the module
# alone. **Scoped to the qualified form deliberately, and the scope was measured rather than
# chosen.** Two wider rules were tried against the tree first:
#
# - *Every backticked bare identifier* (`build_agent`): 3,351 distinct, of which 1,077 resolve to
#   no first-party definition — `None`, `ValueError`, `await`, `finally`, SQL table names
#   (`session_messages`), SQL functions (`ts_rank`) and upstream types (`ToolMessage`). A gate
#   with a 32% false-positive rate is one people learn to suppress.
# - *`module.symbol`* (`chemclaw_agent.build_agent`): 528 occurrences, 426 unresolved — because the
#   same shape spells attribute access on an object (`app.state`, `store.prefix`), a filename
#   (`retention.py`) and a subpackage (`ingest.eln`), and nothing in the syntax separates them.
#
# The qualified form has none of that ambiguity: 764 occurrences, 327 distinct, and only 30
# unresolved — a set small enough to read one by one, which is what a gate's findings have to be.
_QUALIFIED = re.compile(r"`(chemclaw(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")

# Dotted `chemclaw.*` names that are not Python and never were. Two kinds, both real: Helm values
# paths (`chemclaw.image`, `chemclaw.knowledgeMounts` — the chart's own value tree) and
# OpenTelemetry span names (`chemclaw.turn`, `chemclaw.tool`, `chemclaw.db`). They share a prefix
# with the package and nothing else. An explicit list rather than a camelCase heuristic, for
# `_REMOVED`'s reason: adding an entry should cost a review conversation.
_NOT_PYTHON = frozenset(
    {
        "chemclaw.config",
        "chemclaw.connectorUrls",
        "chemclaw.db",
        "chemclaw.env",
        "chemclaw.image",
        "chemclaw.knowledgeMounts",
        "chemclaw.knowledgePublishPath",
        "chemclaw.mcp",
        "chemclaw.migrationEnv",
        "chemclaw.pooledProcesses",
        # An Entra app-role name, which shares the prefix because the tenant names
        # roles after the application ("Alice holding `chemclaw.sharedrive.reader`").
        "chemclaw.sharedrive.reader",
        "chemclaw.tool",
        "chemclaw.turn",
    }
)

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
        # Named by this file's own `_POINTER` comment: the module whose two surviving
        # pointers are the reason the `::symbol` form is matched at all.
        "agent/team.py",
    }
)


def _source_files() -> list[Path]:
    """Every first-party Python file whose prose this rule covers."""
    return sorted(path for root in _SEARCH_ROOTS for path in root.rglob("*.py"))


def _resolve(pointer: str) -> Path | None:
    """The file a path pointer names, or `None` if no base resolves it."""
    for base in _RESOLUTION_BASES:
        candidate = base / pointer
        if candidate.is_file():
            return candidate
    return None


def _defines(path: Path, symbol: str) -> bool:
    """Is `symbol` defined at the top level of `path`, or a member of something that is?

    Walks one level into a class so `ScreenResult.verdict` resolves — a pointer at a field is as
    followable as one at the class, and rejecting it would push prose towards the vaguer form.
    """
    head, _, member = symbol.partition(".")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        name = getattr(node, "name", None)
        if name != head:
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == head for t in node.targets):
                    return True
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == head:
                    return True
            continue
        if not member or not isinstance(node, ast.ClassDef):
            return True
        # A class member: a method, or an annotated/assigned attribute.
        return any(
            getattr(child, "name", None) == member
            or (isinstance(child, ast.AnnAssign) and getattr(child.target, "id", None) == member)
            or (
                isinstance(child, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == member for t in child.targets)
            )
            for child in node.body
        )
    return False


def _module_file(dotted: str) -> Path | None:
    """The file a dotted first-party module name resolves to, module or package."""
    base = _REPO_ROOT / "src" / Path(*dotted.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _dangling(path: Path) -> list[str]:
    """The pointers in `path` that resolve to nothing, in order of appearance.

    Two kinds, checked together because a reader follows them the same way: a file path, optionally
    naming a symbol inside it, and a fully-qualified dotted name.
    """
    text = path.read_text()
    # Blank the cross-repo citations before the local patterns run, so a `<repo>:path.py` is never
    # also read as a local `path.py` by the pattern below.
    #
    # **Then re-admit every *other* colon-prefixed path, because `_POINTER` never matched a colon
    # and so has always let them through.** That hole predates the cross-repo form and was found by
    # probing this function with a made-up repo prefix on a deleted path, which it skipped. (The
    # example is spelled out in prose rather than backticked, because backticking it here would
    # make this comment a broken pointer — which this gate then caught, on itself.) Introducing a
    # meaningful colon prefix is exactly when it stops being theoretical: an unrecognised prefix now
    # gets checked as the local path it is, so a typo'd repo name fails loudly instead of buying
    # silence.
    text = _CROSS_REPO.sub("`<cross-repo>`", text)
    text = _PREFIXED.sub(r"`\1`", text)
    bad: list[str] = []
    for pointer, symbol in _POINTER.findall(text):
        if pointer in _REMOVED:
            continue
        target = _resolve(pointer)
        if target is None:
            bad.append(pointer)
        elif symbol and not _defines(target, symbol):
            bad.append(f"{pointer}::{symbol}")
    for dotted in _QUALIFIED.findall(text):
        if dotted in _NOT_PYTHON or _module_file(dotted):
            continue
        head, _, tail = dotted.rpartition(".")
        module = _module_file(head)
        if module is None or not _defines(module, tail):
            # A two-deep tail (`module.Class.attr`) resolves against the class one level up.
            grandparent, _, cls = head.rpartition(".")
            module = _module_file(grandparent)
            if module is None or not _defines(module, f"{cls}.{tail}"):
                bad.append(dotted)
    return bad


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
    texts = [path.read_text() for path in _source_files()]
    paths = sum(len(_POINTER.findall(text)) for text in texts)
    qualified = sum(len(_QUALIFIED.findall(text)) for text in texts)
    assert paths > 100, f"only {paths} backticked module pointers found — has the pattern rotted?"
    assert qualified > 400, (
        f"only {qualified} qualified `chemclaw.*` names found — has the pattern rotted? "
        "It matched 764 when it was written."
    )
