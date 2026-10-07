"""Every in-process store in `src/` is either a deployment backend or a declared test oracle.

The `InMemory*` classes stay in `src/` beside the Protocol they define, which is safe only while
nothing in the shipped tree constructs an oracle. The enumeration is derived by walking `src/`,
so a new class must be declared in one of the two tables below.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: The in-memory implementations a configuration selects, with the setting that selects each.
#:
#: `InMemoryVectorStore` belongs here: `vector_store_provider` accepts any `module:callable`, so
#: naming it is a supported configuration even though `SHIPPED` lists only two providers.
SELECTABLE = {
    "InMemoryCampaignStore": "session_store",
    "InMemoryHistoryProvider": "session_store",
    "InMemoryPlanApprovalStore": "session_store",
    # Same switch and the same argument, one layer up: a proposal authorizes a change to what the
    # agent does for one person, and under `session_store="memory"` that person's whole context is
    # a process — so a durable queue would outlive the thing it changes.
    "InMemoryProposalStore": "session_store",
    # Same switch, same argument: whether this deployment keeps conversation state durably
    # is one decision, and a composed workflow that vanishes with a CLI process is the
    # honest behaviour there rather than a failure to configure something.
    "InMemoryComposedStore": "session_store",
    "InMemoryDesignStore": "session_store",
    # Artefacts belong to a session, so they are durable exactly when the session is.
    "InMemoryExhibitStore": "session_store",
    "InMemoryArmResultStore": "session_store",
    # Uploads too (`D-2026-10-04-an-upload-is-session-state-not-pod-state`): an attachment is part
    # of the conversation, so it is durable exactly when the conversation is.
    "InMemoryAttachmentStore": "session_store",
    # Same switch, for `InMemoryPlanApprovalStore`'s reason: a membership admits somebody to a
    # session, and under `session_store="memory"` the session is a process.
    "InMemorySessionMemberStore": "session_store",
    # And a session's wait line (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`):
    # two replicas share one order only where they share one session store.
    "InMemoryTurnQueue": "session_store",
    "InMemoryVectorStore": "vector_store_provider",
}

#: The differential oracles: no configuration selects them, and that is deliberate.
#:
#: Each computes in Python what its Postgres sibling expresses as SQL, and tests run both and
#: compare.
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
    """The partition is total, so a new in-memory class cannot arrive undeclared."""
    declared = set(SELECTABLE) | set(ORACLES)
    actual = set(_in_memory_classes())
    assert actual == declared, (
        f"undeclared in-memory classes: {sorted(actual - declared)}; "
        f"declared but gone: {sorted(declared - actual)}. Add each to SELECTABLE (naming the "
        "setting that reaches it) or to ORACLES (and give it a differential test)."
    )


def test_no_shipped_module_constructs_a_reference_oracle() -> None:
    """No shipped module names a reference oracle outside a docstring.

    A shipped caller would be a deployment backend with no configuration selecting it. Names
    rather than calls: any non-docstring mention is already a coupling.
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

    Prose is asserted because the claim itself is the defect.
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

    Driven through `registry.default_vector_store` and settings, since the claim is about the
    configuration path.
    """
    from chemclaw.core.config import settings
    from chemclaw.retrieval.vectors import registry
    from chemclaw.retrieval.vectors.memory import InMemoryVectorStore

    reference = "chemclaw.retrieval.vectors.memory:InMemoryVectorStore"
    monkeypatch.setattr(settings, "vector_store_provider", reference)
    monkeypatch.setattr(registry, "_STORE", None)
    assert isinstance(registry.default_vector_store(), InMemoryVectorStore)
