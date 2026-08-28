"""A refused record is a question somebody will ask, and this is what makes it answerable.

The seeded corpus has exactly one entry that can never arrive: a well logged at 119.43% yield,
refused because `OrdReaction` bounds a yield at 100. Before the ledger, a chemist asking about it
could only be told "I have no such record" — true of the corpus, false of what the system knows.

Five claims, each of which the change would be worthless without:

1. The refusal reaches a durable row carrying the reason, not just a WARNING.
2. Re-offering the record moves `last_seen` and adds no row — a ledger, not a second log.
3. The question a chemist actually asks reaches that row through `gather_evidence`, and what comes
   back is unmistakably a *rejection* rather than a reaction record.
4. An entry that ingests cleanly leaves nothing behind.
5. Growth is bounded: a source that refuses everything cannot write an unbounded table.

Postgres-backed, because a ledger nothing durably wrote is the thing this replaces.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from chemclaw.agent import research_tools
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.ingest import rejections
from chemclaw.ingest.eln.ord_adapter import LEDGER_SOURCE, OrdJsonAdapter
from chemclaw.ingest.rejections import IngestRejection, record_refusals, refusals_matching
from chemclaw.retrieval.evidence import EvidenceChunk
from tests.pg import migrated_db_or_skip

_ROOT = Path(__file__).resolve().parents[1]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The real entry, spelled as ORD would export it: the 119.43% well from the seeded HTE corpus.
_WELL_ID = "santanilla-orgsyn-boronate-well-Y36"

# The chemist's question, taken verbatim from `data/evals/probes/grounded.yaml`'s `gr-08`. It names
# no tool, no source and no entry id — which is the whole test: the ledger has to be reachable from
# how a question is asked, not from how the row was written.
_GR_08 = (
    "One of the wells on that 3-bromopyridine plate is logged at 119% yield. Is our data any good?"
)


def _ord_payload(reaction_id: str, yield_percent: float) -> dict[str, object]:
    """A minimal, well-formed ORD `Reaction` message reporting `yield_percent`."""
    return {
        "reaction_id": reaction_id,
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "Brc1cccnc1"}],
                        "reaction_role": "REACTANT",
                    }
                ]
            }
        },
        "outcomes": [
            {
                "products": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "c1ccc(-c2cccnc2)cc1"}],
                        "measurements": [{"type": "YIELD", "percentage": {"value": yield_percent}}],
                    }
                ]
            }
        ],
        "provenance": {"record_created": {"time": {"value": "2026-03-01T00:00:00Z"}}},
    }


def _write(directory: Path, reaction_id: str, yield_percent: float) -> None:
    """Drop one ORD export into `directory`, as an ELN's exporter would."""
    (directory / f"{reaction_id}.json").write_text(
        json.dumps(_ord_payload(reaction_id, yield_percent)), encoding="utf-8"
    )


async def _rows(source: str) -> list[tuple[str, str, datetime, datetime, int]]:
    """Every ledger row for `source`, read back through SQL rather than through the reader."""
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT entry_id, reason, first_seen, last_seen, occurrences "
            "FROM ingest_rejections WHERE source = %s ORDER BY entry_id",
            (source,),
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in await cursor.fetchall()]


async def _clear(source: str) -> None:
    """Forget everything this source has had refused (test isolation, not a product path)."""
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM ingest_rejections WHERE source = %s", (source,))
        await conn.commit()


def test_the_119_percent_well_is_refused_and_lands_in_the_ledger(tmp_path: Path) -> None:
    """The defect itself: the one entry that can never arrive, now with its reason on file."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear(LEDGER_SOURCE)
        _write(tmp_path, _WELL_ID, 119.43)

        entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)

        # The refusal is unchanged: the entry is still fetched and still refused by the mapper, so
        # the sync's own summary reports it exactly as before.
        assert [entry.entry_id for entry in entries] == [_WELL_ID]
        with pytest.raises(ValueError, match="119.43"):
            OrdJsonAdapter(str(tmp_path)).map_to_ord(entries[0])

        rows = await _rows(LEDGER_SOURCE)
        assert len(rows) == 1, "the refused well must leave exactly one ledger row"
        entry_id, reason, first_seen, last_seen, occurrences = rows[0]
        assert entry_id == _WELL_ID
        # The reason is what turns "I have no such record" into an answer: it has to carry both the
        # value that was refused and the rule that refused it.
        assert "119.43" in reason and "100" in reason
        assert occurrences == 1 and first_seen == last_seen

    asyncio.run(_run())


def test_re_offering_the_same_record_moves_last_seen_and_adds_no_row(tmp_path: Path) -> None:
    """A ledger, not a second log: the row is the record, and the run is a timestamp on it."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear(LEDGER_SOURCE)
        _write(tmp_path, _WELL_ID, 119.43)
        adapter = OrdJsonAdapter(str(tmp_path))

        await adapter.fetch_new_entries(_EPOCH)
        first = await _rows(LEDGER_SOURCE)
        await adapter.fetch_new_entries(_EPOCH)
        second = await _rows(LEDGER_SOURCE)

        assert len(second) == 1, "a record refused twice is one row, or this is a log again"
        assert second[0][4] == 2, "occurrences must count the refusals"
        assert second[0][3] > first[0][3], "last_seen must move when the record is re-offered"
        assert second[0][2] == first[0][2], "first_seen must not move: it is when this started"

    asyncio.run(_run())


def test_a_record_that_ingests_cleanly_leaves_no_ledger_row(tmp_path: Path) -> None:
    """The control. The ledger is about refusals, so a good corpus writes nothing at all."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear(LEDGER_SOURCE)
        _write(tmp_path, "well-ok", 84.0)

        entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)

        assert [entry.entry_id for entry in entries] == ["well-ok"]
        assert OrdJsonAdapter(str(tmp_path)).map_to_ord(entries[0]).yield_percent == 84.0
        assert await _rows(LEDGER_SOURCE) == []

    asyncio.run(_run())


def test_the_gr_08_question_reaches_the_refusal_through_gather_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: the chemist's own words, and the answer that was unreachable.

    The evidence sources are stubbed to a healthy, empty corpus — which is the true state for this
    well, since it never arrived — so what the tool returns about it comes from the ledger and
    from nowhere else.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear(LEDGER_SOURCE)
        _write(tmp_path, _WELL_ID, 119.43)
        await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)

        monkeypatch.setattr(research_tools, "_sources", lambda _anchor: [("graph", _Empty())])
        sweep = await research_tools.gather_evidence(query=_GR_08)

        assert sweep.chunks == [], "the well is genuinely absent; nothing may be cited for it"
        assert sweep.refusals_unavailable == ""
        assert [r.entry_id for r in sweep.refused_on_ingest] == [_WELL_ID]
        rejection = sweep.refused_on_ingest[0]
        assert "119.43" in rejection.reason
        # Unmistakably a rejection: the discriminator is on the object the model reads, and the
        # object carries nothing a reaction record carries — no yield, no structure, no body.
        assert rejection.kind == "ingest-rejection"
        assert not {"yield_percent", "body", "smiles", "conditions"} & set(
            IngestRejection.model_fields
        ), "a rejection that can carry a result can be read as one"
        rendered = repr(sweep)
        assert "refused_on_ingest" in rendered and "ingest-rejection" in rendered, (
            "a pydantic tool return reaches the model as its repr, so the discriminator has to "
            "survive into it (tests/test_upstream_surface.py)"
        )

    asyncio.run(_run())


def test_an_unreadable_ledger_is_reported_rather_than_rendered_as_nothing_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage and a clean corpus must not render alike — the `sources_failed` rule again."""

    async def _blows_up(_question: str) -> list[IngestRejection]:
        raise ConnectionError("Postgres unreachable")

    async def _run() -> None:
        monkeypatch.setattr(research_tools, "_sources", lambda _anchor: [("graph", _Empty())])
        monkeypatch.setattr(research_tools, "refusals_matching", _blows_up)

        sweep = await research_tools.gather_evidence(query=_GR_08)

        assert sweep.refused_on_ingest == []
        assert "ConnectionError" in sweep.refusals_unavailable

    asyncio.run(_run())


def test_a_systematically_broken_source_cannot_grow_the_table_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The growth bound, exercised: the newest `cap` refusals survive and the rest are evicted."""

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-broken-source"
        await _clear(source)
        monkeypatch.setattr(rejections, "_MAX_ROWS_PER_SOURCE", 3)

        await record_refusals(source, {f"entry-{index}": "always broken" for index in range(10)})

        rows = await _rows(source)
        assert len(rows) == 3, "the per-source cap is what keeps this a ledger and not a log"
        # Which three is decided by the tie-break, not by luck: every row of one batch shares the
        # transaction timestamp, so `last_seen DESC, entry_id` settles it the same way every run.
        # Across runs the timestamps differ and recency leads, which is the case the cap is for.
        assert [row[0] for row in rows] == ["entry-0", "entry-1", "entry-2"]
        await _clear(source)

    asyncio.run(_run())


def test_a_long_refusal_message_is_cut_and_says_so() -> None:
    """A message cut without saying so reads as the whole of what the refusal said."""

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-verbose-source"
        await _clear(source)

        await record_refusals(source, {"entry": "x" * 5_000})

        rows = await _rows(source)
        assert len(rows[0][1]) < 1_000 and "truncated" in rows[0][1]
        await _clear(source)

    asyncio.run(_run())


def test_the_reader_matches_the_words_that_carry_the_question() -> None:
    """Matching is on distinctive words, and a question about something else finds nothing."""

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-matching-source"
        await _clear(source)
        # And the ORD source's own rows, because matching deliberately spans sources: a question
        # about data quality is about the corpus, and each row names the source it came from.
        await _clear(LEDGER_SOURCE)
        await record_refusals(source, {_WELL_ID: "yield_percent 119.43 exceeds 100"})

        assert [(r.source, r.entry_id) for r in await refusals_matching(_GR_08)] == [
            (source, _WELL_ID)
        ]
        assert await refusals_matching("what solvent did we use for the Boc removal") == []
        # A short all-letter word matches nothing on its own, or every question would drag the
        # whole ledger into the answer.
        assert await refusals_matching("is our data any good") == []
        await _clear(source)

    asyncio.run(_run())


def test_the_adapter_files_its_refusals_under_the_registry_source_name() -> None:
    """`LEDGER_SOURCE` is a constant beside a manifest, so the manifest is what checks it.

    The ingest half is constructed from `manifest.config` alone and is never told its own name, so
    nothing but this test can notice the two drifting apart — and a drifted name files a refusal
    under a source no operator can join to anything.
    """
    declaring = [
        manifest["name"]
        for path in (_ROOT / "src" / "chemclaw" / "ingest" / "sources").glob("*/datasource.yaml")
        if isinstance(manifest := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
        and "OrdJsonAdapter" in str(manifest.get("ingest", ""))
    ]
    assert declaring == [LEDGER_SOURCE], (
        f"the ORD adapter files rejections under {LEDGER_SOURCE!r}, but the manifests naming it "
        f"are {declaring}"
    )


class _Empty:
    """A healthy evidence source with nothing to say — which is the truth about this well."""

    name = "graph"

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        """Answer, and find nothing."""
        return []
