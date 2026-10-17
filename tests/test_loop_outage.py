"""A per-item loop stops when an outage, not an item, is failing it, and leaves the rest to resume.

`DigestWorkflow` and `CommitmentSyncWorkflow` swallow each item's failure, so in a hard outage each
item costs its whole retry window. Driven through the real workflows on the time-skipping server,
whose clock runs through the retry backoff as a deployed broker's would, so the run's own start and
close times say how long the outage held it. Activities are stand-ins under the real names, failing
the way a down database fails an activity: retryably, on every attempt.
"""

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.config.temporal import backoff_seconds
from chemclaw.core.metrics import METRICS
from chemclaw.durable.commitment_sync import (
    CommitmentSyncReport,
    CommitmentSyncResult,
    CommitmentSyncWorkflow,
)
from chemclaw.durable.deliver_message import OutboundMessage
from chemclaw.durable.digest import DigestItem, DigestWorkflow, digest_channel
from chemclaw.durable.loop_outage import resolve_loop_outage_failures
from chemclaw.durable.notify import SessionEventInput
from tests.temporal_env import pydantic_client, start_env_or_skip

_QUEUE = settings.background_task_queue

# Many more items than the bound, so a loop that does not stop is unmistakable.
_POPULATION = 20


def _io_window() -> timedelta:
    """What one swallowed `IO` failure costs: every backoff between its attempts."""
    return timedelta(
        seconds=backoff_seconds(
            settings.io_retry_max_attempts - 1,
            settings.io_retry_initial_seconds,
            settings.io_retry_max_interval_seconds,
        )
    )


def _owner(index: int) -> str:
    return f"oid-outage-{index:02d}"


def _digest_activities(
    failing: Callable[[str], BaseException | None], acknowledged: list[int]
) -> list[Callable[..., Any]]:
    """The digest's activities over `_POPULATION` subscribers; `failing` decides each mailbox."""

    @activity.defn(name="collect_digests")
    async def collect() -> list[DigestItem]:
        return [
            DigestItem(subscription_id=index, owner=_owner(index), query="impurity", note_ids=["n"])
            for index in range(_POPULATION)
        ]

    @activity.defn(name="record_session_event_activity")
    async def record(event: SessionEventInput) -> None:
        failure = failing(event.session_id)
        if failure is not None:
            raise failure

    @activity.defn(name="acknowledge_digest")
    async def acknowledge(subscription_id: int, note_ids: list[str]) -> None:
        acknowledged.append(subscription_id)

    @activity.defn(name="deliver_message_activity")
    async def deliver(message: OutboundMessage) -> list[str]:
        return []

    return [collect, record, acknowledge, deliver, resolve_loop_outage_failures]


async def _run(
    workflow_run: Any, activities: Sequence[Callable[..., Any]], workflow_id: str
) -> tuple[Any, timedelta]:
    """Run one workflow to its end; its result and how long it held the broker's clock."""
    async with await start_env_or_skip() as env:
        client: Client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=_QUEUE,
            workflows=[DigestWorkflow, CommitmentSyncWorkflow],
            activities=list(activities),
        ):
            handle = await client.start_workflow(workflow_run, id=workflow_id, task_queue=_QUEUE)
            result = await handle.result()
            described = await handle.describe()
    assert described.close_time is not None and described.start_time is not None
    return result, described.close_time - described.start_time


async def test_a_digest_in_a_database_outage_stops_after_the_bound_and_ends_within_it() -> None:
    """A digest in a database outage stops after the bound and ends within it.

    Every mailbox write fails retryably, so each subscriber spends the whole `IO` window; the run
    must stop after `loop_outage_consecutive_failures` of them, not walk the population. Nobody is
    acknowledged, so every watermark stays where it was and the next run tells them all.
    """
    attempted: set[str] = set()
    acknowledged: list[int] = []

    def _down(session_id: str) -> BaseException:
        attempted.add(session_id)
        return ConnectionError("the mailbox database is down")

    stops = METRICS.value("chemclaw_loop_outage_stops_total")
    delivered, held = await _run(
        DigestWorkflow.run, _digest_activities(_down, acknowledged), "digest-outage"
    )

    bound = settings.loop_outage_consecutive_failures
    assert bound < _POPULATION
    assert delivered == 0
    assert acknowledged == [], "a watermark advanced past a mailbox that was never written"
    assert attempted == {digest_channel(_owner(index)) for index in range(bound)}, (
        f"{len(attempted)} of {_POPULATION} subscribers were attempted; the loop must stop after "
        f"the first {bound} that each spent their whole retry window"
    )
    assert held >= _io_window() * bound, "the outage was not real: no retry window was spent"
    assert held < _io_window() * (bound + 1), (
        f"the run held the broker for {held}, against {_io_window()} per failed subscriber; an "
        f"unbounded loop costs {_io_window() * _POPULATION}"
    )
    assert METRICS.value("chemclaw_loop_outage_stops_total") == stops + 1


async def test_a_mailbox_refused_for_its_own_content_does_not_stop_the_digest() -> None:
    """A mailbox refused for its own content does not stop the digest, and a success resets it.

    A `ValueError` fails in one attempt and says nothing about the database; counting it would let
    a bad subscriber at the head of the list stop every later run. More refusals in a row than the
    bound come first, then runs of spent failures one short of it, each ended by a success.
    """
    bound = settings.loop_outage_consecutive_failures
    assert bound >= 2, "a bound of one leaves no run of failures short of it to reset"
    outcomes: list[BaseException | None] = [ValueError("payload refused") for _ in range(bound + 2)]
    while len(outcomes) < _POPULATION:
        outcomes += [ConnectionError("a blip outlasting its window") for _ in range(bound - 1)]
        outcomes.append(None)
    outcomes = outcomes[:_POPULATION]
    acknowledged: list[int] = []

    def _mixed(session_id: str) -> BaseException | None:
        return outcomes[int(session_id.rsplit("-", 1)[1])]

    delivered, _ = await _run(
        DigestWorkflow.run, _digest_activities(_mixed, acknowledged), "digest-bad-items"
    )

    told = [index for index, outcome in enumerate(outcomes) if outcome is None]
    assert told, "the fixture tells nobody, so it cannot show the loop reaching its end"
    assert sorted(acknowledged) == told, (
        "the digest stopped on failures that were not an outage: refusals were counted, or a "
        "success did not reset the count"
    )
    assert delivered == len(told)


def _commitment_activities(
    sources: list[str], failing: Callable[[str], BaseException | None], mirrored: list[str]
) -> list[Callable[..., Any]]:
    """The mirror's activities over `sources`, each source's pass decided by `failing`."""

    @activity.defn(name="list_commitment_sources_activity")
    async def listed() -> list[str]:
        return sources

    @activity.defn(name="mirror_commitments_activity")
    async def mirror(source: str) -> CommitmentSyncResult:
        mirrored.append(source)
        failure = failing(source)
        if failure is not None:
            raise failure
        return CommitmentSyncResult(source=source, mirrored=1)

    return [listed, mirror, resolve_loop_outage_failures]


@pytest.mark.parametrize(
    ("failure", "stops"),
    [(ConnectionError("the commitments database is down"), True), (ValueError("bad"), False)],
    ids=["outage", "bad-export"],
)
async def test_a_mirror_stops_on_an_outage_and_names_the_sources_it_left(
    failure: BaseException, stops: bool
) -> None:
    """A mirror stops on an outage and names the sources it left; a bad export does not stop it.

    The sources left keep their cursors, and the report lists them, so the next run resumes them
    and a reader can tell a short run from a quiet one.
    """
    sources = [f"src-{index}" for index in range(6)]
    mirrored: list[str] = []
    bound = settings.loop_outage_consecutive_failures

    report: CommitmentSyncReport
    report, _ = await _run(
        CommitmentSyncWorkflow.run,
        _commitment_activities(sources, lambda _source: failure, mirrored),
        f"commitments-{'outage' if stops else 'bad-export'}",
    )

    attempted = sources[:bound] if stops else sources
    # Distinct, in order: an outage retries each source to its class's bound.
    assert list(dict.fromkeys(mirrored)) == attempted
    assert [result.source for result in report.results] == attempted
    assert report.skipped == ([] if not stops else sources[bound:])
