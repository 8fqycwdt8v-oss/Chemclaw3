"""Every in-process store in `src/` is either a deployment backend or a declared test oracle.

`D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend` decided that the eight
`InMemory*` classes no configuration can select stay in `src/`, beside the Protocol and the
Postgres implementation they define the contract for, rather than moving to `tests/`. That decision
rests on one property — **nothing in the shipped tree constructs them** — and on one correction:
one class the backlog row called dead is in fact reachable through the vector-store seam.

Both are asserted here rather than believed, because the sweep that opened the row will happen
again. A file of prose saying "these nine are oracles" is a claim about the afternoon it was
written; this is the thing that fails when it stops being true.

**The enumeration is derived, not listed.** `_in_memory_classes` walks `src/` and finds every
`InMemory*` class there is, so a tenth added next year lands in one of the two tables below and its
author has to say which. A hand-written list is a list of what the tree looked like the week
somebody wrote it, which is the failure `framing._INVISIBLE` was rewritten to avoid one layer down.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: The in-memory implementations a *configuration* selects, with the setting that selects each.
#:
#: These are deployment backends by this repository's own predicate
#: (`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold`: a thing no configuration can reach is
#: dead, and its converse). Four of them answer `session_store="memory"` — the backlog row that
#: opened this file named only the first as the thing not to confuse with an oracle, which was a
#: quarter of the set.
#:
#: `InMemoryVectorStore` is the fifth and it is the correction that matters. The row listed it
#: among the unreachable nine on the strength of `retrieval/vectors/registry.SHIPPED` holding only
#: `qdrant` and `databricks` — but `vector_store_provider` takes a `module:callable`
#: (`D-2026-08-26-the-driver-s-signature-is-the-schema`), and this class takes no constructor
#: arguments and implements `VectorStore`, so naming it is a supported configuration.
#: `test_the_vector_store_seam_really_does_reach_the_in_memory_reference` drives it.
SELECTABLE = {
    "InMemoryCampaignStore": "session_store",
    "InMemoryHistoryProvider": "session_store",
    "InMemoryPlanApprovalStore": "session_store",
    "InMemoryDesignStore": "session_store",
    "InMemoryVectorStore": "vector_store_provider",
}

#: The differential oracles: no configuration selects them, and that is deliberate.
#:
#: Each computes in Python what its Postgres sibling expresses as SQL, and the tests that matter
#: run both and compare — `test_store.py::test_find_matches_the_in_memory_backend` and its
#: siblings. They are not mocks; `retrieval/vectors/memory.py`'s docstring calls the shape "the
#: definition of what the adapters are expected to agree with", and the Postgres modules' own
#: correctness comments cite them by name as the answer they are written to reproduce.
ORACLES = {
    "InMemoryDocumentIndex": "chemclaw/ingest/documents/index.py",
    "InMemoryReactionRecordStore": "chemclaw/ingest/eln/records.py",
    "InMemoryNoteIndex": "chemclaw/retrieval/vector_index.py",
    "InMemoryArtifactStore": "chemclaw/science/calc/artifacts.py",
    "InMemoryStore": "chemclaw/science/calc/store.py",
    "InMemoryStructureStore": "chemclaw/science/calc/structures.py",
    "InMemoryFingerprintStore": "chemclaw/science/fingerprints/store.py",
    "InMemoryLabelIndex": "chemclaw/science/labels/store.py",
}


def _in_memory_classes() -> dict[str, pathlib.Path]:
    """Every `InMemory*` class defined under `src/chemclaw`, by name, with the file defining it."""
    found: dict[str, pathlib.Path] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name.startswith("InMemory"):
                assert node.name not in found, f"two definitions of {node.name}"
                found[node.name] = path
    return found


def test_every_in_memory_class_is_declared_as_a_backend_or_as_an_oracle() -> None:
    """The partition is total, so a new one cannot arrive undeclared.

    This is the assertion that makes the decision durable rather than dated. The backlog row asked
    that "a third sweep re-finding nine undecided classes" must not happen; what stops it is not
    the ADR, which a sweep may not read, but a red test naming the class and the two tables.
    """
    declared = set(SELECTABLE) | set(ORACLES)
    actual = set(_in_memory_classes())
    assert actual == declared, (
        f"undeclared in-memory classes: {sorted(actual - declared)}; "
        f"declared but gone: {sorted(declared - actual)}. Add each to SELECTABLE (naming the "
        "setting that reaches it) or to ORACLES (and give it a differential test)."
    )


def test_no_shipped_module_constructs_a_reference_oracle() -> None:
    """The property the whole decision rests on: an oracle has no caller in the shipped tree.

    `tests/` is not in the image (`deploy/Containerfile` copies `src`, `data`, `skills`,
    `knowledge`, `infra`, `schema` and no tests), so keeping these here costs 40,025 bytes of
    unreachable Python — a price worth paying only while it stays unreachable. The moment a
    first-party module constructs one, that is a deployment backend arriving without a
    configuration to select it, which is the shape this repository keeps deleting.

    Names rather than calls: an oracle *mentioned* by a shipped module in anything but a docstring
    is already a coupling, and a docstring mention is an `ast.Constant` this does not see.
    """
    definitions = _in_memory_classes()
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if name in ORACLES and path != definitions[name]:
                # `getattr`, because `lineno` lives on statement and expression nodes
                # rather than on `ast.AST` itself, and the walk yields both.
                line = getattr(node, "lineno", 0)
                offenders.append(f"{path.relative_to(SRC.parent)}:{line} names {name}")
    assert offenders == [], (
        "a shipped module reaches a reference oracle, so it is no longer test-only:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", sorted(ORACLES))
def test_no_oracle_claims_a_deployment_mode_it_does_not_have(name: str) -> None:
    """An oracle's docstring may not offer a Postgres-free deployment, because there isn't one.

    Seven of these said "for tests and single-run use" and one said "for tests and for a deployment
    with no database" — a mode `default_store()`, `default_note_index()` and every other selector
    refuse to return. That is the same shape as a comment asserting a control exists: a reader
    checking whether this system can run without Postgres would have found eight classes agreeing
    that it can.

    Prose is asserted here, which this repository is otherwise sparing about, for the reason
    `tests/test_tool_authz.py` asserts the absence of a withdrawn promise: the claim is the defect,
    so the absence of the claim is the fix, and nothing else can hold it.
    """
    definitions = _in_memory_classes()
    tree = ast.parse(definitions[name].read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name)
    doc = ast.get_docstring(node) or ""
    assert doc, f"{name} has no docstring"
    for claim in ("single-run use", "deployment with no database", "a deployment without"):
        assert claim not in doc, (
            f"{name} offers a deployment mode no configuration selects ({claim!r}); "
            "it is a differential oracle, and saying otherwise is a claim about a capability"
        )


def test_the_vector_store_seam_really_does_reach_the_in_memory_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`InMemoryVectorStore` is selectable, which is why it is not in `ORACLES`.

    Measured rather than reasoned, because reasoning is how it got misfiled: reading `SHIPPED` says
    two providers, and the seam accepts any `module:callable` beside them. A future sweep counting
    `SHIPPED` will reach the same wrong conclusion, and this is the test that argues back.

    Driven through `registry.default_vector_store` and `settings`, not by importing the class — the
    claim is about the configuration path, and importing the class would prove only that the file
    exists.
    """
    from chemclaw.core.config import settings
    from chemclaw.retrieval.vectors import registry
    from chemclaw.retrieval.vectors.memory import InMemoryVectorStore

    reference = "chemclaw.retrieval.vectors.memory:InMemoryVectorStore"
    monkeypatch.setattr(settings, "vector_store_provider", reference)
    monkeypatch.setattr(registry, "_STORE", None)
    assert isinstance(registry.default_vector_store(), InMemoryVectorStore)
