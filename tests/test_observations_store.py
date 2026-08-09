"""The observations tier against a real database — the half no pure test can reach (D-161).

`tests/test_observations.py` covers the miners and the model, which is where the domain rules
live. It cannot cover the SQL, and the SQL here is not boilerplate: the upsert makes a run
authoritative for the rows it names *when it saw the whole corpus* — `evidence_note_ids` and
`projects_seen` are then replaced rather than merged — and the anti-feedback rule is a CHECK
constraint. Both are the kind of thing that is valid Python and wrong SQL, and both are
load-bearing: replacement is what makes support track the corpus in *both* directions (the
`array_agg(DISTINCT …)` union it replaces could only grow, so a reaction re-assayed SUCCESS backed
a promotion forever), the union survives for the partial pass that has not earned replacement, and
the constraint is the guarantee behind "an observation can never corroborate itself".

Skipped where no Postgres is reachable, so this is the offline sandbox's blind spot and CI's job.
"""

import asyncio

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import Component, OrdReaction, OutcomeClass, Role
from chemclaw.memory import observations as store
from chemclaw.memory.observation_mining import mine_corpus
from chemclaw.memory.observations import Observation
from tests.pg import migrated_db_or_skip

_ESTER = ("CCO", "CC(=O)O", "CCOC(C)=O")


def _esterification(reaction_id: str, project: str, outcome: OutcomeClass) -> OrdReaction:
    """One esterification, so every fixture reaction lands in a single similarity cluster.

    The same fixture `tests/test_observations.py` mines with — kept identical on purpose, so the
    pure miner test and this end-to-end one are talking about the same cluster.
    """
    return OrdReaction(
        reaction_id=reaction_id,
        inputs=[
            Component(smiles=_ESTER[0], role=Role.REACTANT),
            Component(smiles=_ESTER[1], role=Role.REACTANT),
        ],
        outcomes=[Component(smiles=_ESTER[2], role=Role.PRODUCT)],
        provenance=f"test:{reaction_id}",
        project=project,
        outcome_class=outcome,
        failure_reason="decomposed on workup" if outcome is OutcomeClass.FAILURE else None,
    )


async def _clean_db_or_skip() -> None:
    """A migrated database with an empty `observations` table, or skip."""
    await migrated_db_or_skip()
    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM observations")
        await conn.commit()


def _finding(statement: str = "s", **overrides: object) -> Observation:
    """One observation, defaulting to a single-project single-note finding."""
    fields: dict[str, object] = {
        "statement": statement,
        "scope": "transformation:r1",
        "evidence_note_ids": ["reaction-r1"],
        "projects_seen": ["alpha"],
    }
    return Observation(**{**fields, **overrides})  # type: ignore[arg-type]


def test_a_growing_finding_accumulates_the_support_its_run_observed() -> None:
    """The whole reason support means anything across runs.

    Support must follow the corpus: a finding seen again with another reaction behind it ends up
    backed by both notes and both projects.

    **This used to assert something subtly different** — that two runs reporting *disjoint*
    evidence are unioned by the SQL. That union is what let evidence outlive the corpus, so it is
    gone: a run is authoritative for the rows it names, and both miners emit an observation's
    complete membership because they mine the whole corpus every pass (`all_reactions()` reads
    from `datetime.min`). Accumulation now comes from the miner seeing more, which is the only
    place it can come from and still mean "what the record currently shows".
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=True)
        await store.record(
            [
                _finding(
                    evidence_note_ids=["reaction-r1", "reaction-r2"],
                    projects_seen=["alpha", "beta"],
                )
            ],
            complete=True,
        )

        found = await store.open_observations()
        assert len(found) == 1  # one row, not two — the id is the scope
        assert found[0].evidence_note_ids == ["reaction-r1", "reaction-r2"]
        assert found[0].projects_seen == ["alpha", "beta"]
        assert found[0].support == 2

    asyncio.run(_run())


def test_a_run_drops_the_evidence_the_corpus_has_since_retracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Support must not count a reaction that has left the cluster.

    Run end to end through the real `mine_corpus` → `record` → `promotable` chain, because that is
    what the claim is about and hand-written payloads cannot show it: three cross-project failures
    make a 3-note observation over the promotion threshold; `ddd3` is then re-assayed a SUCCESS, so
    `mine_corpus` drops it *before* fingerprinting and the next pass's cluster holds two. Under the
    old union the row kept all three — a promotion crossed on a documented success, and a PR body
    that says "failed in 2 runs across 2 projects … no successful run is in this cluster" one
    paragraph before "supported by 3 merged notes across 3 projects".

    **This test used to hand-write two `_finding(...)` payloads** while its docstring claimed the
    real chain, which proved only that the SQL replaces an array — not that the miner ever emits
    the shrunken cluster that makes replacement mean anything.
    """
    monkeypatch.setattr(settings, "observation_promote_min_evidence", 3)
    monkeypatch.setattr(settings, "observation_promote_min_projects", 2)

    async def _run() -> None:
        await _clean_db_or_skip()
        corpus = [
            _esterification("ddd1", "alpha", OutcomeClass.FAILURE),
            _esterification("ddd2", "beta", OutcomeClass.FAILURE),
            _esterification("ddd3", "gamma", OutcomeClass.FAILURE),
        ]
        await store.record(mine_corpus(corpus), complete=True)
        promoted = await store.promotable()
        assert len(promoted) == 1 and promoted[0].support == 3

        # The re-assay: ddd3 succeeded after all, so the next full pass never fingerprints it.
        corpus[2] = _esterification("ddd3", "gamma", OutcomeClass.SUCCESS)
        await store.record(mine_corpus(corpus), complete=True)

        found = await store.open_observations()
        assert len(found) == 1
        assert found[0].evidence_note_ids == ["reaction-ddd1", "reaction-ddd2"]
        assert found[0].projects_seen == ["alpha", "beta"]
        assert found[0].support == 2
        assert await store.promotable() == []  # and it drops back below the threshold

    asyncio.run(_run())


def test_a_partial_pass_may_not_rewrite_an_observation_down() -> None:
    """Replacement is what an *authoritative* pass earns, and a degraded pass has not earned it.

    The retraction fix made every pass replace the stored arrays. But a pass is only authoritative
    if it saw the whole corpus, and `read_corpus()` cannot promise that: an entry `map_to_ord`
    rejects is skipped and the read continues. So a run that saw one project's reactions rewrote a
    three-project observation down to one — measured on live Postgres, support 3 → 1 — and could
    knock a row out of `promotable()`. That is a degraded input rendering as an authoritative
    complete result, which is the very defect this lane is named for.

    Both halves are pinned here: a partial pass may only add (the old union, now scoped to the case
    that needs it), and a complete pass still drops what the corpus retracted.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record(
            [
                _finding(
                    "three projects",
                    evidence_note_ids=["reaction-r1", "reaction-r2", "reaction-r3"],
                    projects_seen=["alpha", "beta", "gamma"],
                )
            ],
            complete=True,
        )

        # A degraded pass: one source answered, the rest of the corpus was never read.
        await store.record(
            [_finding("one project", evidence_note_ids=["reaction-r1"], projects_seen=["alpha"])],
            complete=False,
        )
        found = (await store.open_observations())[0]
        assert found.evidence_note_ids == ["reaction-r1", "reaction-r2", "reaction-r3"]
        assert found.projects_seen == ["alpha", "beta", "gamma"]
        # The statement is not refreshed either: rewriting it to "one project" beside three-project
        # evidence is the self-contradiction the replacement was introduced to remove.
        assert found.statement == "three projects"

        # A partial pass still *adds* what it did see — accumulation is unaffected.
        await store.record(
            [_finding("new note", evidence_note_ids=["reaction-r4"], projects_seen=["delta"])],
            complete=False,
        )
        found = (await store.open_observations())[0]
        assert found.evidence_note_ids == [
            "reaction-r1",
            "reaction-r2",
            "reaction-r3",
            "reaction-r4",
        ]
        assert found.projects_seen == ["alpha", "beta", "delta", "gamma"]

        # And a complete pass is still authoritative: the retraction fix is untouched.
        await store.record(
            [_finding("two", evidence_note_ids=["reaction-r1"], projects_seen=["alpha"])],
            complete=True,
        )
        found = (await store.open_observations())[0]
        assert found.evidence_note_ids == ["reaction-r1"] and found.statement == "two"

    asyncio.run(_run())


def test_the_statement_follows_the_evidence_it_accumulated() -> None:
    """A row backed by two projects must not still read as though it were backed by one.

    The statement is the mutable part now that identity is the scope, so the upsert refreshes it.
    Keeping the first run's wording would leave the tier saying one thing and its own evidence
    column saying another — and the statement is what a reviewer reads on a promotion PR.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding("seen in 1 project")], complete=True)
        await store.record(
            [
                _finding(
                    "seen in 2 projects", evidence_note_ids=["reaction-r2"], projects_seen=["beta"]
                )
            ],
            complete=True,
        )

        found = await store.open_observations()
        assert len(found) == 1
        assert found[0].statement == "seen in 2 projects"

    asyncio.run(_run())


def test_re_recording_an_identical_finding_changes_nothing_but_last_seen() -> None:
    """A nightly no-op must stay a no-op, or every run would look like new support."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=True)
        await store.record([_finding()], complete=True)

        found = await store.open_observations()
        assert len(found) == 1
        assert found[0].evidence_note_ids == ["reaction-r1"]
        assert found[0].last_seen is not None and found[0].first_seen is not None

    asyncio.run(_run())


def test_the_database_refuses_an_observation_citing_an_observation() -> None:
    """The guarantee, not the courtesy.

    `Observation` refuses this at construction so a miner fails where it is written. That protects
    the path that goes through the model; this protects the *table*, including from a future
    writer that does not. The insert is made deliberately around the validator to prove it.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO observations (id, statement, scope, evidence_note_ids, origin) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("observation-x", "s", "t", ["observation-y"], "corpus-mining"),
                )

    asyncio.run(_run())


def test_only_a_finding_over_both_thresholds_is_promotable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two thresholds because they answer different questions, and neither alone is enough.

    Ten notes from one project is a well-evidenced *episodic* fact, which the campaign layer
    already covers; two projects with one note each is a coincidence.
    """
    monkeypatch.setattr(settings, "observation_promote_min_evidence", 3)
    monkeypatch.setattr(settings, "observation_promote_min_projects", 2)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record(
            [
                # Enough notes, one project.
                _finding(
                    "deep but local",
                    scope="a",
                    evidence_note_ids=["reaction-1", "reaction-2", "reaction-3"],
                    projects_seen=["alpha"],
                ),
                # Enough projects, too few notes.
                _finding(
                    "broad but thin",
                    scope="b",
                    evidence_note_ids=["reaction-4"],
                    projects_seen=["alpha", "beta"],
                ),
                # Both.
                _finding(
                    "real",
                    scope="c",
                    evidence_note_ids=["reaction-5", "reaction-6", "reaction-7"],
                    projects_seen=["alpha", "beta"],
                ),
            ],
            complete=True,
        )
        assert [o.statement for o in await store.promotable()] == ["real"]

    asyncio.run(_run())


def test_a_promoted_observation_leaves_the_open_set() -> None:
    """Otherwise it would be re-promoted every night, opening the same PR forever."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=True)
        observation = (await store.open_observations())[0]

        await store.set_status(observation.id, "promoted")
        assert await store.open_observations() == []
        assert await store.promotable() == []

    asyncio.run(_run())


@pytest.mark.parametrize("complete", [True, False], ids=["replace", "accumulate"])
def test_a_retired_observation_comes_back_when_the_corpus_does(
    complete: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retirement has to be reversible, or the tier empties permanently instead of breathing.

    Neither `_REPLACE` nor `_ACCUMULATE` touched `status`, and every read is `status = 'open'`, so
    a re-observed finding had its evidence replaced and its `last_seen` bumped while staying
    invisible to `open_observations`, `promotable` and `recall_observations` forever. Measured
    before the fix, on both statements: `retire_stale() == 1`, then re-recording left
    `status='retired'` with a fresh `last_seen`, and the row appeared in neither read.

    Two ordinary paths reach it — a finding that lapses for `observation_retire_after_days` and
    returns, and an ingest source quiet for that long — so this is a permanently dead row rather
    than a temporarily hidden one. It also stops being counted by `retire_stale`, which is the
    tier's own stated instrumentation for whether the miners are producing noise.
    """
    monkeypatch.setattr(settings, "observation_retire_after_days", 30)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=complete)
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("UPDATE observations SET last_seen = now() - interval '90 days'")
            await conn.commit()

        assert await store.retire_stale() == 1
        assert await store.open_observations() == []

        # The corpus produces the finding again: it must return to the open set.
        await store.record([_finding()], complete=complete)
        revived = await store.open_observations()
        assert len(revived) == 1, "a re-observed finding must leave the retired state"
        assert revived[0].status == "open"

    asyncio.run(_run())


@pytest.mark.parametrize("complete", [True, False], ids=["replace", "accumulate"])
def test_re_observing_a_promoted_observation_does_not_reopen_it(complete: bool) -> None:
    """Revival must reach `retired` only — `promoted` is the state that stops the nightly PR.

    `test_a_promoted_observation_leaves_the_open_set` pins why: a promoted finding that returned to
    the open set would be re-promoted on the next pass and open the same PR forever. The miners
    keep re-observing a promoted finding by construction, so this is the routine case, not an edge.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=complete)
        promoted = (await store.open_observations())[0]
        await store.set_status(promoted.id, "promoted")

        await store.record([_finding()], complete=complete)
        assert await store.open_observations() == []
        assert await store.promotable() == []

    asyncio.run(_run())


def test_retirement_spares_what_was_just_re_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`last_seen` is refreshed by every run that still finds the finding.

    That is what makes retirement mean "the corpus stopped supporting this" rather than "this is
    old" — a finding the miners keep confirming must never age out from under them.
    """
    monkeypatch.setattr(settings, "observation_retire_after_days", 30)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=True)
        assert await store.retire_stale() == 0  # just recorded, so nothing is stale
        assert len(await store.open_observations()) == 1

    asyncio.run(_run())


def test_retirement_is_off_when_the_window_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that wants observations to persist indefinitely must be able to say so."""
    monkeypatch.setattr(settings, "observation_retire_after_days", 0)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()], complete=True)
        assert await store.retire_stale() == 0
        assert len(await store.open_observations()) == 1

    asyncio.run(_run())


def test_the_best_supported_observation_is_read_first() -> None:
    """The page is small, so the ordering decides what is seen at all."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record(
            [
                _finding("thin", scope="a", evidence_note_ids=["reaction-1"]),
                _finding("solid", scope="b", evidence_note_ids=[f"reaction-{i}" for i in range(4)]),
            ],
            complete=True,
        )
        assert [o.statement for o in await store.open_observations()] == ["solid", "thin"]

    asyncio.run(_run())
