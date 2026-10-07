"""The observations tier against a real database (D-161).

Covers the SQL the pure tests cannot: a complete pass replaces `evidence_note_ids` and
`projects_seen` so support tracks the corpus both ways, a partial pass only unions, and a CHECK
constraint forbids an observation citing an observation. Skipped without Postgres.
"""

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


async def test_a_growing_finding_accumulates_the_support_its_run_observed() -> None:
    """A finding seen again with another reaction is backed by both notes and both projects.

    Accumulation comes from the miner seeing more: both miners emit an observation's complete
    membership every pass, so the SQL can replace evidence rather than union it.
    """
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


async def test_a_run_drops_the_evidence_the_corpus_has_since_retracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run drops evidence the corpus has since retracted.

    Driven through `mine_corpus` → `record` → `promotable`: `ddd3` is re-assayed a SUCCESS, the
    miner drops it, and the stored row must shrink from three notes to two. Hand-written payloads
    could only show that the SQL replaces an array, not that the miner emits the shrunken cluster.
    """
    monkeypatch.setattr(settings, "observation_promote_min_evidence", 3)
    monkeypatch.setattr(settings, "observation_promote_min_projects", 2)

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


async def test_a_partial_pass_may_not_rewrite_an_observation_down() -> None:
    """A partial pass may only add; a complete pass still drops what the corpus retracted.

    `read_corpus()` skips entries `map_to_ord` rejects, so a pass is authoritative only when it saw
    the whole corpus. A degraded pass must not rewrite a row down or knock it out of `promotable()`.
    """
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


async def test_the_statement_follows_the_evidence_it_accumulated() -> None:
    """The upsert refreshes the statement so it matches the evidence it accumulated."""
    await _clean_db_or_skip()
    await store.record([_finding("seen in 1 project")], complete=True)
    await store.record(
        [_finding("seen in 2 projects", evidence_note_ids=["reaction-r2"], projects_seen=["beta"])],
        complete=True,
    )

    found = await store.open_observations()
    assert len(found) == 1
    assert found[0].statement == "seen in 2 projects"


async def test_re_recording_an_identical_finding_changes_nothing_but_last_seen() -> None:
    """A nightly no-op must stay a no-op, or every run would look like new support."""
    await _clean_db_or_skip()
    await store.record([_finding()], complete=True)
    await store.record([_finding()], complete=True)

    found = await store.open_observations()
    assert len(found) == 1
    assert found[0].evidence_note_ids == ["reaction-r1"]
    assert found[0].last_seen is not None and found[0].first_seen is not None


async def test_the_database_refuses_an_observation_citing_an_observation() -> None:
    """The database itself refuses an observation citing an observation.

    `Observation` refuses it at construction; the CHECK protects the table from writers that bypass
    the model. The insert here deliberately goes around the validator.
    """
    await _clean_db_or_skip()
    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO observations (id, statement, scope, evidence_note_ids, origin) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("observation-x", "s", "t", ["observation-y"], "corpus-mining"),
            )


async def test_only_a_finding_over_both_thresholds_is_promotable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two thresholds because they answer different questions, and neither alone is enough.

    Ten notes from one project is a well-evidenced *episodic* fact, which the campaign layer
    already covers; two projects with one note each is a coincidence.
    """
    monkeypatch.setattr(settings, "observation_promote_min_evidence", 3)
    monkeypatch.setattr(settings, "observation_promote_min_projects", 2)

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


async def test_a_promoted_observation_leaves_the_open_set() -> None:
    """Otherwise it would be re-promoted every night, opening the same PR forever."""
    await _clean_db_or_skip()
    await store.record([_finding()], complete=True)
    observation = (await store.open_observations())[0]

    await store.set_status(observation.id, "promoted")
    assert await store.open_observations() == []
    assert await store.promotable() == []


@pytest.mark.parametrize("complete", [True, False], ids=["replace", "accumulate"])
async def test_a_retired_observation_comes_back_when_the_corpus_does(
    complete: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retired observation reopens when the corpus shows it again.

    Every read is `status = 'open'`, so without revival a returning finding would stay invisible
    forever and drop out of `retire_stale`'s count.
    """
    monkeypatch.setattr(settings, "observation_retire_after_days", 30)

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


@pytest.mark.parametrize("complete", [True, False], ids=["replace", "accumulate"])
async def test_re_observing_a_promoted_observation_does_not_reopen_it(complete: bool) -> None:
    """Revival reaches `retired` only; a promoted finding stays promoted.

    Miners re-observe promoted findings by construction, and reopening one would re-promote it every
    pass.
    """
    await _clean_db_or_skip()
    await store.record([_finding()], complete=complete)
    promoted = (await store.open_observations())[0]
    await store.set_status(promoted.id, "promoted")

    await store.record([_finding()], complete=complete)
    assert await store.open_observations() == []
    assert await store.promotable() == []


async def test_retirement_spares_what_was_just_re_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`last_seen` is refreshed by every run that still finds the finding.

    That is what makes retirement mean "the corpus stopped supporting this" rather than "this is
    old" — a finding the miners keep confirming must never age out from under them.
    """
    monkeypatch.setattr(settings, "observation_retire_after_days", 30)

    await _clean_db_or_skip()
    await store.record([_finding()], complete=True)
    assert await store.retire_stale() == 0  # just recorded, so nothing is stale
    assert len(await store.open_observations()) == 1


async def test_retirement_is_off_when_the_window_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that wants observations to persist indefinitely must be able to say so."""
    monkeypatch.setattr(settings, "observation_retire_after_days", 0)

    await _clean_db_or_skip()
    await store.record([_finding()], complete=True)
    assert await store.retire_stale() == 0
    assert len(await store.open_observations()) == 1


async def test_the_best_supported_observation_is_read_first() -> None:
    """The page is small, so the ordering decides what is seen at all."""
    await _clean_db_or_skip()
    await store.record(
        [
            _finding("thin", scope="a", evidence_note_ids=["reaction-1"]),
            _finding("solid", scope="b", evidence_note_ids=[f"reaction-{i}" for i in range(4)]),
        ],
        complete=True,
    )
    assert [o.statement for o in await store.open_observations()] == ["solid", "thin"]


async def test_the_recall_page_says_how_much_of_the_tier_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recall page says how much of the tier it shows.

    `open_observations` clamps to `observation_max_results`; `count_open_observations` puts the
    total in the payload the model reads when it answers.
    """
    monkeypatch.setattr(settings, "observations_enabled", True)

    await _clean_db_or_skip()
    page = settings.observation_max_results
    await store.record(
        [
            _finding(statement=f"finding {index}", scope=f"transformation:{index}")
            for index in range(page + 5)
        ],
        complete=True,
    )

    assert await store.count_open_observations() == page + 5
    assert len(await store.open_observations()) == page

    from chemclaw.agent import memory_tools

    recall = await memory_tools.recall_observations()
    assert recall.enabled is True
    assert len(recall.observations) == page
    assert recall.total_open == page + 5
    payload = recall.model_dump()
    assert "PARTIAL" in payload["verdict"]
    assert str(page + 5) in payload["verdict"]


async def test_an_enabled_tier_that_has_noticed_nothing_says_so_as_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled-and-empty and disabled must not render alike, nor either look like the other."""
    monkeypatch.setattr(settings, "observations_enabled", True)

    await _clean_db_or_skip()
    from chemclaw.agent import memory_tools

    recall = await memory_tools.recall_observations()
    assert recall.enabled is True
    assert recall.observations == []
    assert recall.total_open == 0
    assert "NOTHING NOTICED" in recall.model_dump()["verdict"]
