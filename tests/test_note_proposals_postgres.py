"""Integration tests for the PR-gate's proposal record (`infra/sql/027_note_proposals.sql`).

Runs against a real database (CI provides Postgres; the offline sandbox has none, so these skip).
`tests/test_note_proposals.py` exercises the same contract against `InMemoryProposalStore`, which
is a real backend rather than a double — but it is *not* the backend a `session_store="postgres"`
deployment gets, and the rule under test here lives in an `ON CONFLICT ... DO UPDATE SET` clause
that no amount of Python agreement can prove. This is the compliance record of human decisions on
agent-authored knowledge; it had no coverage of the statement that actually writes it.

What only the database can prove: the upsert's state machine. A `failed` row is superseded by the
retry that succeeded, and a *decision* is not — expressed in SQL as two `CASE` arms reading the
pre-update row, which is precisely the kind of thing that is right in the mirror and wrong here.
"""

import asyncio
from typing import Any

from chemclaw.kg.proposal import NoteProposal, ProposalState
from chemclaw.kg.proposal_store import PostgresProposalStore
from chemclaw.kg.submission import NoteFile
from tests.pg import migrated_db_or_skip


async def _store_or_skip() -> PostgresProposalStore:
    """A migrated store, or skip when no database is reachable."""
    await migrated_db_or_skip()
    return PostgresProposalStore()


def _proposal(note_id: str, **overrides: Any) -> NoteProposal:
    """A submitted proposal for `note_id` with everything the record needs."""
    fields: dict[str, Any] = {
        "note_id": note_id,
        "note_type": "reaction",
        "content": f"rendered note for {note_id}",
        "branch": f"note/{note_id}",
        "actor": "oid-42",
        "session_id": "sess-7",
        "correlation_id": "turn-9",
    }
    fields.update(overrides)
    return NoteProposal(**fields)


def test_a_retry_that_succeeded_supersedes_the_failure_it_replaced() -> None:
    """The bug this table made permanent: a submission that landed still reading `failed`.

    The retries (`durable/memory_jobs`, `report_workflow`, `observation_jobs`, all under
    `note_publish_retry()`) re-render byte-identical content, so the attempt that finally reaches
    git collapses onto the failed row's `(note_id, content_hash)`. Leaving it `failed` made the
    record assert the opposite of what happened — the branch is up awaiting review, while every
    `state='open'` query skips it, the decision route answers 409, and `mark_merged` moves nothing.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        failed = _proposal(
            "pg-reaction-retry", state=ProposalState.FAILED, reason="no route to host"
        )
        failed_id = await store.upsert(failed)

        retried_id = await store.upsert(
            failed.model_copy(
                update={"state": ProposalState.OPEN, "reason": "", "reference": "pr://note/x"}
            )
        )

        assert retried_id == failed_id  # same content, same row — that is why it stayed stuck
        stored = await store.read(failed_id)
        assert stored is not None
        assert stored.state is ProposalState.OPEN
        assert stored.reason == ""  # the stale git error does not outlive the failure it explained
        assert stored.reference == "pr://note/x"
        # ...and it is now reachable through the queue a reviewer actually works.
        listed = await store.listing(ProposalState.OPEN, "", 50, None)
        assert failed_id in [proposal.id for proposal in listed]
        # The loop closes too: the webhook can only move an open row.
        assert await store.mark_merged(["pg-reaction-retry"], "webhook") == 1

    asyncio.run(_run())


def test_a_decision_is_never_superseded_by_a_later_submission() -> None:
    """The rule the fix above must not break, in the backend that enforces it.

    A rejection re-proposed unchanged must not reopen, or the gate is defeatable by re-asking until
    nobody reading the queue can tell it was refused; a merged row is final for the same reason.
    The `CASE` arms read `note_proposals.*` — the row as it was *before* the statement — so this
    holds however the SET list is ordered.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        for note_id, decision, reason in (
            ("pg-reaction-rejected", ProposalState.REJECTED, "not reproducible"),
            ("pg-reaction-merged", ProposalState.MERGED, ""),
        ):
            proposal = _proposal(note_id)
            proposal_id = await store.upsert(proposal)
            assert await store.decide(proposal_id, decision, "reviewer", reason) is not None

            await store.upsert(proposal.model_copy(update={"reference": "pr://again"}))

            stored = await store.read(proposal_id)
            assert stored is not None
            assert stored.state is decision
            assert stored.reason == reason
            assert stored.decided_by == "reviewer"
            # The provenance columns still refresh — only the decision is frozen.
            assert stored.reference == "pr://again"

    asyncio.run(_run())


def test_a_changed_note_is_a_new_version_beside_the_decided_one() -> None:
    """Keying on content, not on the note: a revision must not erase the earlier verdict.

    This is the `note_proposals_version_unique (note_id, content_hash)` constraint doing its job —
    a rejection recorded in July survives the note being re-proposed, changed, in August.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        first = _proposal("pg-reaction-revised", content="yield 82%")
        first_id = await store.upsert(first)
        await store.decide(first_id, ProposalState.REJECTED, "reviewer", "could not reproduce")

        revised = first.model_copy(update={"content": "yield 31% (corrected)"})
        second_id = await store.upsert(revised)

        assert second_id != first_id
        rejected = await store.read(first_id)
        assert rejected is not None and rejected.state is ProposalState.REJECTED
        fresh = await store.read(second_id)
        assert fresh is not None and fresh.state is ProposalState.OPEN

    asyncio.run(_run())


def test_every_file_of_a_multi_file_submission_round_trips_through_the_column() -> None:
    """The `dependencies` JSONB, against the database rather than against the mirror.

    The in-memory backend holds `tuple[NoteFile, ...]` in a dict and cannot be wrong about it. The
    durable one serializes to JSONB, reads back through `dict_row`, and rebuilds the models — three
    steps the Python backend does not have, on the column that decides whether a `FAILED`
    multi-file submission is replayable at all.

    Also pins the refresh rule: an unchanged re-proposal collapses onto the same row (the hash
    still covers the subject note alone) and its `dependencies` are the ones the replay would
    write, not the ones the first attempt happened to carry.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        first = _proposal(
            "pg-reaction-deps",
            state=ProposalState.FAILED,
            reason="no route to host",
            dependencies=(
                NoteFile(path="knowledge/compound/pg-compound.md", content="the compound"),
            ),
        )
        row_id = await store.upsert(first)

        stored = await store.read(row_id)
        assert stored is not None
        assert [file.path for file in stored.dependencies] == ["knowledge/compound/pg-compound.md"]
        assert stored.dependencies[0].content == "the compound"

        # Same subject note, a re-derived dependency set: one row, the newer files.
        again = first.model_copy(
            update={
                "state": ProposalState.OPEN,
                "reason": "",
                "dependencies": (
                    NoteFile(path="knowledge/compound/pg-compound.md", content="the compound v2"),
                ),
            }
        )
        assert await store.upsert(again) == row_id
        refreshed = await store.read(row_id)
        assert refreshed is not None
        assert refreshed.dependencies[0].content == "the compound v2"

    asyncio.run(_run())


def test_a_changed_reproposal_supersedes_the_open_predecessor_in_sql() -> None:
    """The Postgres mirror of the in-memory rule: one open row per note (D-2026-08-27)."""

    async def _run() -> None:
        store = await _store_or_skip()
        first_id = await store.upsert(_proposal("pg-superseded", content="v1"))
        second_id = await store.upsert(_proposal("pg-superseded", content="v2"))
        assert second_id != first_id

        old = await store.read(first_id)
        assert old is not None and old.state is ProposalState.SUPERSEDED
        new = await store.read(second_id)
        assert new is not None and new.state is ProposalState.OPEN

        # The webhook moves only the live version.
        assert await store.mark_merged(["pg-superseded"], "webhook") == 1
        merged = await store.read(second_id)
        assert merged is not None and merged.state is ProposalState.MERGED
        untouched = await store.read(first_id)
        assert untouched is not None and untouched.state is ProposalState.SUPERSEDED

    asyncio.run(_run())


def test_re_proposing_a_superseded_version_reopens_it_in_sql() -> None:
    """The `CASE` arms again, for the other state that is not a decision.

    `superseded` says a newer version took the queue slot, not that anyone judged the bytes — so an
    agent regenerating an earlier form (an ordinary miner path) must reopen that row, exactly as a
    landed retry reopens a `failed` one. With only the `failed` arm, re-proposing v1 refreshed v1's
    row while leaving it superseded *and* `_SUPERSEDE_OTHER_OPEN` closed v2: every row superseded,
    the review queue empty while the branch awaited review, and `mark_merged` moving nothing.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        first_id = await store.upsert(_proposal("pg-reverted", content="v1"))
        second_id = await store.upsert(_proposal("pg-reverted", content="v2"))
        closed = await store.read(first_id)
        assert closed is not None and closed.state is ProposalState.SUPERSEDED

        again_id = await store.upsert(
            _proposal("pg-reverted", content="v1", reference="pr://note/again")
        )

        assert again_id == first_id  # same content, same row
        reopened = await store.read(first_id)
        assert reopened is not None
        assert reopened.state is ProposalState.OPEN
        assert reopened.reason == ""
        assert reopened.reference == "pr://note/again"
        newer = await store.read(second_id)
        assert newer is not None and newer.state is ProposalState.SUPERSEDED
        # v1 is *older* than the row it closed. The statement's reason literal was written for the
        # only case that used to reach it and is false on this path — and it is what a reviewer
        # reads in the compliance table.
        assert "newer" not in newer.reason, newer.reason
        # Exactly one open row, and the webhook can close it.
        assert await store.mark_merged(["pg-reverted"], "webhook") == 1

    asyncio.run(_run())


def test_re_proposing_a_rejected_version_does_not_close_the_live_one_in_sql() -> None:
    """`_SUPERSEDE_OTHER_OPEN` fires on the state the upsert produced, not on the one asked for.

    Measured before the guard moved: v1 rejected, v2 open, re-propose v1 -> v1 still `rejected`
    (correct), v2 `superseded`, and no open row for the note at all. The `CASE` refusing to reopen
    a decision and the sweep closing the live version are the same statement disagreeing with
    itself about what just happened.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        rejected_id = await store.upsert(_proposal("pg-rejected-sweep", content="v1"))
        await store.decide(rejected_id, ProposalState.REJECTED, "reviewer", "not reproducible")
        live_id = await store.upsert(_proposal("pg-rejected-sweep", content="v2"))

        await store.upsert(_proposal("pg-rejected-sweep", content="v1", reference="pr://again"))

        decided = await store.read(rejected_id)
        assert decided is not None and decided.state is ProposalState.REJECTED
        live = await store.read(live_id)
        assert live is not None and live.state is ProposalState.OPEN
        assert await store.mark_merged(["pg-rejected-sweep"], "webhook") == 1

    asyncio.run(_run())
