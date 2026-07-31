"""The QM result reaches the calculation store, and comes back out of it (D-157).

Two halves, and the second is the one that matters: persisting an expensive result makes it
*durable*, but only the lookup makes it *reused*. Before this, the sole thing preventing a repeat
DFT run was the deterministic workflow id — and Temporal frees that id once the execution ages out
of retention, so the same molecule re-ran hours of cluster time and overwrote an identical row.

The end-to-end workflow assertions live in `test_qm_workflow.py` beside the other workflow tests;
what is here is everything provable without the Temporal test server, which a network-restricted
sandbox cannot download: key derivation, both activities under `ActivityEnvironment`, and the
round trip out through the note's `calc_refs` into `chemclaw.kg.crosslink` — the read side that
until now had no writer anywhere in `src/`.
"""

import asyncio
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from chemclaw.connectors.qm import activities as qm_activities
from chemclaw.connectors.qm.cache import UNVERSIONED, calculation_key, version_slug
from chemclaw.connectors.qm.knowledge import note_from_qm_result
from chemclaw.connectors.qm.specs import QmCacheLookup, QMJobInput, QMJobResult, QmJobSpec
from chemclaw.core.config import settings
from chemclaw.kg.crosslink import cited_calculations
from chemclaw.kg.note import Note, parse_note
from chemclaw.kg.render import render_note
from chemclaw.science.calc.store import InMemoryStore

_SPEC = QmJobSpec(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP")


def _result(smiles: str = "CCO", energy: float = -154.5) -> QMJobResult:
    """A parsed result for the spec above."""
    return QMJobResult(
        molecule_smiles=smiles,
        method="B3LYP",
        basis_set="def2-SVP",
        total_energy_hartree=energy,
        converged=True,
        requested_by="oid-1",
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryStore:
    """Swap the activities' Postgres store for an in-memory one.

    Patched on the *importing* module, because `activities` binds `default_store` by name at import
    — patching `postgres_store.default_store` would leave the already-bound reference in place.
    """
    fake = InMemoryStore()
    monkeypatch.setattr(qm_activities, "default_store", lambda: fake)
    return fake


def test_the_key_is_a_valid_calc_ref() -> None:
    """The derived key survives the note's `calc_refs` shape check.

    `qm_job_key` — the obvious thing to reach for — is a bare 16-character digest and fails this
    outright, which is why the store key is derived separately rather than reused.
    """
    note = Note(id="job-x", type="job-result", calc_refs=[calculation_key(_SPEC).as_str()])
    assert note.calc_refs[0].startswith("dft@")


def test_the_same_molecule_spelled_differently_shares_one_key() -> None:
    """Canonicalization happens inside the key, so "OCC" and "CCO" are one calculation."""
    other = _SPEC.model_copy(update={"molecule_smiles": "OCC"})
    assert calculation_key(other) == calculation_key(_SPEC)


def test_method_and_basis_separate_calculations() -> None:
    """Method and basis are parameters of the key, so changing either is a miss, not a wrong hit."""
    method = _SPEC.model_copy(update={"method": "PBE0"})
    basis = _SPEC.model_copy(update={"basis_set": "def2-TZVP"})
    assert calculation_key(method) != calculation_key(_SPEC)
    assert calculation_key(basis) != calculation_key(_SPEC)


def test_a_pipeline_bump_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new pipeline version must recompute rather than return a stale number (D-011/D-033)."""
    monkeypatch.setattr(settings, "hpc_pipeline_version", "1.0")
    first = calculation_key(_SPEC)
    monkeypatch.setattr(settings, "hpc_pipeline_version", "2.0")
    assert calculation_key(_SPEC) != first


def test_versions_that_slug_alike_still_key_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanitizing the version for display must never merge two pipelines into one cache entry.

    `"pipe 1"` and `"pipe:1"` both render as `pipe-1`, so the readable half collides by design.
    The raw value rides in the parameter hash precisely so the *key* does not — otherwise a
    cosmetic slug rule would silently serve one pipeline's number for another's.
    """
    monkeypatch.setattr(settings, "hpc_pipeline_version", "pipe 1")
    spaced = calculation_key(_SPEC)
    monkeypatch.setattr(settings, "hpc_pipeline_version", "pipe:1")
    coloned = calculation_key(_SPEC)
    assert spaced.calc_version == coloned.calc_version == "pipe-1"
    assert spaced != coloned


def test_version_slug_falls_back_when_unset() -> None:
    """An unset pipeline version still yields a non-empty, reference-safe version segment."""
    assert version_slug("") == UNVERSIONED
    assert version_slug("   ") == UNVERSIONED


def _persist(result: QMJobResult) -> str:
    """Run the persist activity the way Temporal would."""
    return str(asyncio.run(ActivityEnvironment().run(qm_activities.persist_qm_result, result)))


def _lookup(spec: QmJobSpec) -> QmCacheLookup:
    """Run the lookup activity the way Temporal would."""
    job = QMJobInput(**spec.model_dump())
    found = asyncio.run(ActivityEnvironment().run(qm_activities.lookup_qm_result, job))
    assert isinstance(found, QmCacheLookup)
    return found


def test_persist_then_lookup_returns_the_result(store: InMemoryStore) -> None:
    """The round trip: what the persist activity writes, the lookup activity finds."""
    key = _persist(_result())
    found = _lookup(_SPEC)

    assert key.startswith("dft@")
    assert found.calc_key == key
    assert found.result is not None
    assert found.result.total_energy_hartree == pytest.approx(-154.5)


def test_a_hit_is_attributed_to_whoever_asked_this_time(store: InMemoryStore) -> None:
    """A cached result must not credit the chemist who happened to run it first.

    `requested_by` is deliberately outside the key — the energy of a molecule does not depend on
    who wanted it, so identical science shares one entry across users. That makes returning the
    stored value an attribution bug rather than a caching detail: the string becomes the note's
    `source`, so the audit trail would name someone who never asked for the run.

    Found by CI, not locally: the workflow tests skip without the Temporal test server, and against
    a real Postgres one test's persisted result was served to the next test's differently-attributed
    request.
    """
    first = _result()
    first = first.model_copy(update={"requested_by": "oid-first"})
    asyncio.run(ActivityEnvironment().run(qm_activities.persist_qm_result, first))

    job = QMJobInput(**_SPEC.model_dump(), requested_by="oid-second")
    found = asyncio.run(ActivityEnvironment().run(qm_activities.lookup_qm_result, job))

    assert found.result is not None
    # The science is the first run's...
    assert found.result.total_energy_hartree == pytest.approx(-154.5)
    # ...the attribution is this one's.
    assert found.result.requested_by == "oid-second"


def test_lookup_misses_on_an_uncomputed_molecule(store: InMemoryStore) -> None:
    """A miss still carries the key, so the caller has one shape to handle."""
    found = _lookup(_SPEC)
    assert found.result is None
    assert found.calc_key.startswith("dft@")


def test_persistence_can_be_switched_off(
    store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag off nothing is written, nothing is read, and no key is minted.

    The escape hatch for a deployment whose qm worker has no Postgres: the job still completes and
    still publishes, it just loses the cache entry and the note's reference.
    """
    monkeypatch.setattr(settings, "qm_persist_to_calc_store", False)

    assert _persist(_result()) == ""
    found = _lookup(_SPEC)
    assert found.calc_key == ""
    assert found.result is None
    assert asyncio.run(store.get(calculation_key(_SPEC))) is None


def test_the_note_cites_the_calculation_and_crosslink_reads_it_back(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """The whole point, end to end: a QM note finally has a producer for `calc_refs`.

    `chemclaw.kg.crosslink` has been able to answer "which notes rest on this calculation" since
    D-133 and had **no writer anywhere in `src/`** — only tests. This closes that loop, and does it
    through a real render/parse round trip so the reference survives the Markdown the PR-gate
    actually commits.
    """
    key = _persist(_result())
    note = note_from_qm_result(_result(), key)
    assert cited_calculations(note) == [key]

    written = tmp_path / "job-result.md"
    written.write_text(render_note(note), encoding="utf-8")
    assert cited_calculations(parse_note(written)) == [key]


def test_a_note_without_a_key_cites_nothing() -> None:
    """No key means no reference.

    A `calc_refs` pointing at a row that was never written would fail `kg-validate` on the very PR
    the note opens, so the disabled and failed paths both have to leave it empty.
    """
    assert note_from_qm_result(_result(), "").calc_refs == []
    assert note_from_qm_result(_result()).calc_refs == []
