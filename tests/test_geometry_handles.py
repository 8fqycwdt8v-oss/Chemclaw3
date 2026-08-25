"""A computed geometry is an address the next calculation takes, not coordinates in a transcript.

`D-2026-08-21-a-geometry-is-an-address-not-a-payload`. Three properties, and the first is the one
everything else rests on: **every `structure_id` the agent is shown resolves.** It holds because the
write and the projection are two halves of one act — the walker that strips a geometry out of a
model-facing payload is the walker that found it to be kept — so these drive the real composites
against the fake server rather than asserting about the walker alone.

The measurements quoted here were taken on `main` before the change, on celecoxib (40 atoms) at the
shipped `crest_max_members`: a conformer search's envelope was 29,086 characters carrying 2,400
distinct numeric values, and one stored `xtb.conformers` row was 66,520 characters against a
`calc_find_max_results` of 50.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from chemclaw.connectors.calc import compose
from chemclaw.connectors.calc.server import tools
from chemclaw.core.quantities import returned_values
from chemclaw.science.calc.geometry import (
    check_server_address,
    structures_in,
    without_geometry,
)
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structures import (
    InMemoryStructureStore,
    UnknownStructureError,
    require_structure,
)
from tests.calc_server_fake import FakeCalcServer, install


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion, the shape every test here uses."""
    return asyncio.run(coroutine)


def _structure(smiles: str = "CCO") -> Structure:
    """A small real geometry, built the way the fake server builds one."""
    from tests.calc_server_fake import embed as fake_embed

    return Structure.model_validate(fake_embed(smiles))


# --- the projection --------------------------------------------------------------------------


def test_a_projected_geometry_keeps_its_address_and_loses_its_coordinates() -> None:
    """What replaces a geometry says which molecule, which state, and where to find it."""
    structure = _structure()
    projected = without_geometry({"structure": structure.model_dump(mode="json")})["structure"]

    assert projected["structure_id"] == structure.structure_id
    assert projected["smiles"] == "CCO"
    assert projected["atom_count"] == len(structure.elements)
    # A neutral closed-shell singlet is what every reader assumes, so it is not restated — twenty
    # times per ensemble is what makes that worth a rule rather than a preference.
    assert "charge" not in projected and "multiplicity" not in projected
    # Stated rather than implied: a reader must be able to tell a geometry that was projected away
    # from one the calculation never produced.
    assert projected["geometry_omitted"] is True
    assert "positions" not in projected and "elements" not in projected


def test_an_unusual_electronic_state_is_reported() -> None:
    """The other half of omitting the default: a radical or an ion must still say so."""
    radical = Structure(
        elements=[6, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        multiplicity=2,
        smiles="[CH3]",
    )
    projected = without_geometry(radical.model_dump(mode="json"))
    # Both, or neither: half a statement about an electronic state reads as a complete one.
    assert projected["multiplicity"] == 2
    assert projected["charge"] == 0


def test_the_projection_reaches_a_geometry_nested_anywhere() -> None:
    """An ensemble holds one per member, several levels down — a top-level walk would miss them."""
    payload = {
        "ensemble": {
            "conformers": [
                {"population": 0.6, "structure": _structure("CCO").model_dump(mode="json")},
                {"population": 0.4, "structure": _structure("CCC").model_dump(mode="json")},
            ]
        }
    }
    projected = without_geometry(payload)
    members = projected["ensemble"]["conformers"]
    assert [member["structure"]["smiles"] for member in members] == ["CCO", "CCC"]
    assert all(member["structure"]["geometry_omitted"] for member in members)
    # The populations are the answer and are untouched.
    assert [member["population"] for member in members] == [0.6, 0.4]


def test_the_projection_is_idempotent() -> None:
    """It runs twice on the durable path — at the envelope and again at the collector."""
    payload = {"structure": _structure().model_dump(mode="json")}
    once = without_geometry(payload)
    assert without_geometry(once) == once


def test_a_lookalike_that_is_not_a_geometry_is_left_alone() -> None:
    """Shape *and* validation, so a field sharing two names is not replaced by a derived address."""
    payload = {"elements": ["carbon", "hydrogen"], "positions": "on the bench"}
    assert without_geometry(payload) == payload
    assert list(structures_in(payload)) == []


def test_the_projection_removes_the_bulk_and_the_numeric_flood() -> None:
    """Both halves of the measured harm: the characters, and what they do to the grounding pool.

    `api/runner_trace._capped_numbers` collects every distinct value a result returned so a figure
    in an answer can be recognised as quoted, and caps the list at `stream_max_result_numbers`
    (512) on the stated grounds that the cap is "unreachable in normal traffic". A twenty-member
    ensemble of a drug-sized molecule returns 2,400 — 4.7x over — which both drops real values and
    fills the pool with coordinates that will round-match almost any claim.
    """
    # Celecoxib, the molecule the measurement was taken on: 40 atoms with hydrogens, which is an
    # ordinary drug-sized case rather than a contrived one.
    drug = _structure("Cc1ccc(cc1)-c1cc(nn1-c1ccc(cc1)S(N)(=O)=O)C(F)(F)F")
    members = [{"population": 0.05, "structure": drug.model_dump(mode="json")} for _ in range(20)]
    payload = {"ensemble": {"conformers": members}}
    before = json.dumps(payload)
    after = json.dumps(without_geometry(payload))

    assert len(after) < len(before) / 5
    assert len(returned_values(after)) < len(returned_values(before)) / 5


# --- the round trip through the real composites ------------------------------------------------


def test_a_relaxed_geometry_is_addressable_afterwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant: `optimize_geometry` reports an id, and that id resolves to a geometry.

    Through the real tool, the real composite and the real store seam — an assertion about the
    walker alone would pass on a build where nothing ever called it.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(tools, "default_store", InMemoryStore)

    summary = _run(tools.optimize_geometry("CCO"))
    resolved = _run(require_structure(server.structures, summary.structure_id))

    assert resolved.structure_id == summary.structure_id
    assert resolved.smiles == "CCO"


def test_a_named_geometry_is_what_the_next_calculation_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain the whole change exists for: relax, then compute at *that* geometry.

    The second call must send the named structure rather than embedding again, which is the
    difference between describing the conformer a chemist chose and describing an arbitrary one.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    monkeypatch.setattr(tools, "default_store", lambda: store)

    summary = _run(tools.optimize_geometry("CCO"))
    _run(tools.compute_electronic_properties("CCO", structure_id=summary.structure_id))

    sent = server.arguments("compute_properties_at")
    assert len(sent) == 1
    assert Structure.model_validate(sent[0]["structure"]).structure_id == summary.structure_id
    # And the SMILES route is untouched, so no stored `xtb.properties` row is orphaned.
    assert server.count("compute_electronic_properties") == 0


def test_the_smiles_route_still_asks_the_smiles_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-identical behaviour without a handle — the cache-compatibility half of the branch."""
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(tools, "default_store", InMemoryStore)

    _run(tools.compute_electronic_properties("CCO"))

    assert server.count("compute_electronic_properties") == 1
    assert server.count("compute_properties_at") == 0


def test_a_fukui_ranking_runs_at_the_named_conformer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regiochemistry question a chemist asks *after* a conformer search.

    `predict_site_reactivity` was the one geometry-describing calculator that could not take a
    handle, because the server had `compute_properties_at` and no `compute_fukui_at`. That gap is
    what `DEFERRED.md` carried, and the honest consequence while it stood was that "which site is
    reactive in this conformer" was answered on a fresh force-field embedding.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    monkeypatch.setattr(tools, "default_store", lambda: store)

    summary = _run(tools.optimize_geometry("CCO"))
    _run(tools.predict_site_reactivity("CCO", structure_id=summary.structure_id))

    sent = server.arguments("compute_fukui_at")
    assert len(sent) == 1
    assert Structure.model_validate(sent[0]["structure"]).structure_id == summary.structure_id
    # The SMILES route is untouched, so no stored `xtb.fukui` row is orphaned.
    assert server.count("predict_site_reactivity") == 0


def test_a_second_fukui_mode_at_one_geometry_costs_no_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row serves every mode, at a named geometry exactly as at an embedded one.

    The three single points behind a Fukui ranking do not depend on the mode — the server computes
    all three indices and `ranked_for` sorts locally — so `compute_fukui_at` is keyed with an empty
    params tuple. Keying on `mode` would write three rows for one calculation and, worse, make a
    cache hit authoritative about an ordering it never chose.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    monkeypatch.setattr(tools, "default_store", lambda: store)

    summary = _run(tools.optimize_geometry("CCO"))
    at = summary.structure_id
    electrophilic = _run(tools.predict_site_reactivity("CCO", "electrophilic", structure_id=at))
    nucleophilic = _run(tools.predict_site_reactivity("CCO", "nucleophilic", structure_id=at))

    assert server.count("compute_fukui_at") == 1, "the second mode ran a second calculation"
    assert electrophilic.sites[0].index != nucleophilic.sites[0].index, "the ranking did not move"


def test_a_conformer_search_hands_back_addresses_that_all_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty geometries out, twenty handles in — the user-facing shape of the chain."""
    server = install(monkeypatch, FakeCalcServer())
    ensemble, _ = _run(compose.conformer_ensemble(InMemoryStore(), "CCO"))

    ids = [conformer.structure.structure_id for conformer in ensemble.conformers]
    assert ids
    for structure_id in ids:
        assert _run(server.structures.get(structure_id)) is not None


def test_an_unresolvable_handle_names_what_to_run_instead() -> None:
    """A stale id is not a retry — the message has to say so, because a retry never works."""
    with pytest.raises(UnknownStructureError) as caught:
        _run(require_structure(InMemoryStructureStore(), "st_gone"))
    message = str(caught.value)
    assert "st_gone" in message
    assert "sample_conformers" in message


def test_a_handle_for_a_different_molecule_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structure id addresses a geometry, not a compound — nothing about it says which molecule.

    Silently honouring it would answer a question about propane under the name of ethanol.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(tools, "default_store", InMemoryStore)
    propane = _structure("CCC")
    _run(server.structures.put([propane]))

    with pytest.raises(ValueError, match="is a geometry of"):
        _run(tools.optimize_geometry("CCO", structure_id=propane.structure_id))


# --- the server's authoritative address ---------------------------------------------------------


def test_a_divergent_server_address_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """The one failure that costs no calculation and breaks every lookup from then on.

    The server's `structure_id` is a `computed_field` and arrives on every payload; ours is a
    property, so pydantic drops it. The two derivations agree only while the rounding does, and the
    server's is an ENV-overridable setting where this side holds a constant — so a disagreement is
    possible, silent, and permanent.
    """
    payload = _structure().model_dump(mode="json") | {"structure_id": "st_something_else"}
    with caplog.at_level("ERROR"):
        check_server_address({"structure": payload})
    assert "degraded[structure_id]" in caplog.text
    assert "st_something_else" in caplog.text


def test_an_agreeing_server_address_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The normal case must be silent, or the signal is noise."""
    structure = _structure()
    payload = structure.model_dump(mode="json") | {"structure_id": structure.structure_id}
    with caplog.at_level("ERROR"):
        check_server_address({"structure": payload})
    assert "degraded[structure_id]" not in caplog.text


# --- the listing that was the largest exposure --------------------------------------------------


def test_a_listed_calculation_is_bounded_and_says_when_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`find_calculations` was the biggest unbounded model-facing payload and nothing said so.

    A stored `xtb.conformers` row holds every member the search found — 66,520 characters on one
    40-atom molecule — and `calc_find_max_results` is 50, so one read-only call on two agent
    profiles could render ~830,000 tokens. Past a provider's context limit the failure is hard
    rather than graceful (`agent/compaction.py`), which is why this is a bound and not a hope.

    Both halves are asserted: geometries become addresses, and what is *still* over the ceiling is
    withheld whole with `result_omitted` set — never trimmed, because a truncated payload that
    still parses reads as a complete one.
    """
    from datetime import UTC, datetime

    from chemclaw.core.config import settings
    from chemclaw.science.calc.store import CalculationKey, CalculationQuery, StoredResult

    drug = _structure("Cc1ccc(cc1)-c1cc(nn1-c1ccc(cc1)S(N)(=O)=O)C(F)(F)F")
    store = InMemoryStore()

    async def _seed() -> None:
        await store.put(
            StoredResult(
                key=CalculationKey(
                    calc_type="xtb.conformers",
                    calc_version="v1",
                    input_hash="a" * 16,
                    params_hash="b" * 16,
                ),
                result={
                    "members": [
                        {"energy_hartree": -1.0, "structure": drug.model_dump(mode="json")}
                        for _ in range(47)
                    ]
                },
                created_at=datetime.now(UTC),
            )
        )

    _run(_seed())
    monkeypatch.setattr(tools, "default_store", lambda: store)

    monkeypatch.setattr(settings, "calc_find_max_result_chars", 100_000)
    generous = _run(tools.find_calculations(calc_type="xtb.conformers"))[0]
    assert generous.result_omitted is False
    # The projection alone is most of the reduction: 47 geometries became 47 addresses. Measured
    # against the row rather than against a literal, so the claim is about the change and not
    # about one molecule's atom count.
    stored = _run(store.find(CalculationQuery(calc_type="xtb.conformers")))[0]
    assert len(json.dumps(generous.result)) < len(json.dumps(stored.result)) / 5
    assert all(member["structure"]["geometry_omitted"] for member in generous.result["members"])

    monkeypatch.setattr(settings, "calc_find_max_result_chars", 500)
    bounded = _run(tools.find_calculations(calc_type="xtb.conformers"))[0]
    assert bounded.result_omitted is True
    assert bounded.result == {}
    # The identity survives the bound — a listing whose rows could not be named would be useless.
    assert bounded.calc_ref == generous.calc_ref


def test_a_geometry_keyed_calculation_is_findable_by_its_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question D-011's cache exists to answer and `find_calculations` could not ask.

    A molecule filter is refused on these families and rightly — `input_hash` is over a geometry —
    but nothing replaced it, so "have we already relaxed this conformer?" had no query at all.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    monkeypatch.setattr(tools, "default_store", lambda: store)

    # The geometry a chemist holds after a conformer search, and the one they would relax next.
    chosen = _run(compose.embed("CCO"))
    _run(tools.optimize_geometry("CCO", structure_id=chosen.structure_id))
    _run(tools.optimize_geometry("CCC"))

    # The recorded id is the geometry each calculation *ran on*, which is what makes this the
    # chemist's question: "here is the conformer I picked — what has already been computed on it?"
    found = _run(tools.find_calculations(structure_id=chosen.structure_id))
    assert [record.calc_type for record in found] == ["xtb.opt"]

    # And the refusal that remains points somewhere workable rather than at a different question.
    with pytest.raises(ValueError, match="structure_id"):
        _run(tools.find_calculations(smiles="CCO", calc_type="xtb.opt"))


def test_the_geometry_store_round_trips_through_postgres() -> None:
    """The backend the deployment actually uses, not just the in-memory twin.

    The cross-process reach is the whole reason a durable backend exists: the conformer search runs
    on the `calc` bundle's queue and the follow-up optimization is launched from the chat service,
    so an in-process map would resolve nothing.
    """
    from chemclaw.science.calc.postgres_structures import PostgresStructureStore
    from tests.pg import migrated_db_or_skip

    async def _drive() -> None:
        await migrated_db_or_skip()
        store = PostgresStructureStore()
        first, second = _structure("CCO"), _structure("CCC")
        await store.put([first, second])
        # Idempotent by content address: a second write is a no-op, not a conflict.
        await store.put([first])

        assert (await store.get(first.structure_id)) == first
        assert (await store.get(second.structure_id)) == second
        assert await store.get("st_never_written") is None

    asyncio.run(_drive())


def test_writing_no_geometries_touches_nothing() -> None:
    """Most calculations return none, and the common case must not open a connection.

    Asserted against a store whose DSN cannot connect: if an empty write reached the database this
    would raise rather than pass.
    """
    from chemclaw.science.calc.postgres_structures import PostgresStructureStore

    unreachable = PostgresStructureStore(dsn="postgresql://nobody@127.0.0.1:1/none")
    _run(unreachable.put([]))
