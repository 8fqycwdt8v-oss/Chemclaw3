"""An effect: what this system changes in a system it does not own, and whether it can be undone.

**The audit that produced this work claimed all three attachment seams refuse a write path. That was
too strong, and the correction is the finding.** `ConnectorManifest` has routed mutation through
`jobs:` since D-029 — "which core authorizes, dry-run-gates and attributes" — so a job could always
write. What nothing said was whether a job writes *this* deployment's database or somebody else's
system of record, and nothing declared reversibility, so every job was gated identically whether it
could be undone or not.

These tests hold the four things the declaration adds: it cannot be declared un-gated, it must say
how it can be undone and say it consistently, an irreversible one waits for a human, and the ledger
records the attempt *before* it is made.
"""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.connectors.manifest import EffectSpec, JobSpec
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable.effect_ledger import (
    EffectRecord,
    begin_effect,
    get_effect,
    settle_effect,
    unsettled,
)
from tests.pg import migrated_db_or_skip

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def _job(**effect: object) -> JobSpec:
    """A job spec declaring an effect, with everything else minimal."""
    return JobSpec(
        name="file_deviation",
        workflow="FileDeviationWorkflow",
        summary="file a deviation",
        expensive=True,
        effect=EffectSpec(**effect),  # type: ignore[arg-type]
    )


def test_an_effect_cannot_be_declared_ungated() -> None:
    """`expensive` is what puts a job in `authorize_trigger`'s set.

    Refused rather than silently corrected: a manifest saying `expensive: false` beside an
    `effect:` block is an author who believed one of the two, and which one they believed matters.
    """
    with pytest.raises(ValidationError, match="must be.*entitled|not `expensive"):
        JobSpec(
            name="file_deviation",
            workflow="W",
            summary="s",
            expensive=False,
            effect=EffectSpec(system="the QMS", reversal="irreversible"),
        )


def test_reversal_has_no_default() -> None:
    """The safe-looking default is the wrong one.

    A job whose author did not think about reversal is far likelier to be irreversible than
    idempotent, so a default would let the un-thought-about case take the cheapest gate.
    """
    with pytest.raises(ValidationError):
        EffectSpec(system="the QMS")  # type: ignore[call-arg]


def test_a_compensating_effect_names_what_undoes_it_and_others_may_not() -> None:
    """Both directions, because both are a claim.

    An unnamed compensation is a reversibility nobody can perform; a compensation on an
    irreversible effect is the opposite claim in the same field.
    """
    with pytest.raises(ValidationError, match="must name the job that undoes it"):
        EffectSpec(system="the LIMS", reversal="compensating")
    with pytest.raises(ValidationError, match="only a compensating effect has one"):
        EffectSpec(system="the LIMS", reversal="irreversible", compensation="retract")

    ok = EffectSpec(system="the LIMS", reversal="compensating", compensation="retract_submission")
    assert ok.compensation == "retract_submission"
    # And the two other kinds are declarable with nothing further.
    assert _job(system="the QMS", reversal="irreversible").effect is not None
    assert _job(system="the LIMS", reversal="idempotent").effect is not None


def test_the_ledger_records_the_attempt_before_it_is_made() -> None:
    """A row in `attempting` after a crash is the honest state, not a bug in the ledger.

    This system may have filed the deviation and lost the acknowledgement. A ledger that recorded
    only successes would answer "nothing happened" for exactly the case an operator most needs to
    investigate — which is why `unsettled` has an index of its own.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM effects WHERE connector = 'effects-test'")
            await conn.commit()

        record = EffectRecord(
            effect_id="eff-crash",
            connector="effects-test",
            job="file_deviation",
            system="the QMS",
            reversal="irreversible",
            requested_by="u-1",
            approved_by="u-qa",
        )
        await begin_effect(record)

        open_now = {row.effect_id for row in await unsettled()}
        assert "eff-crash" in open_now
        stored = await get_effect("eff-crash")
        assert stored is not None and stored.state == "attempting"

        await settle_effect("eff-crash", state="applied", external_ref="DEV-2291")
        settled = await get_effect("eff-crash")
        assert settled is not None
        assert (settled.state, settled.external_ref) == ("applied", "DEV-2291")
        assert "eff-crash" not in {row.effect_id for row in await unsettled()}

    asyncio.run(_run())


def test_an_applied_effect_is_never_walked_back_to_attempting() -> None:
    """A replay must not put the far side's state back in doubt when it is not.

    `begin_effect` is idempotent on the job's deterministic workflow id, so a retried run re-opens
    its own row — but an effect that has already landed has landed.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        record = EffectRecord(
            effect_id="eff-applied",
            connector="effects-test",
            job="file_deviation",
            system="the QMS",
            reversal="idempotent",
        )
        await begin_effect(record)
        await settle_effect("eff-applied", state="applied", external_ref="DEV-1")
        await begin_effect(record)

        stored = await get_effect("eff-applied")
        assert stored is not None
        assert stored.state == "applied"
        assert stored.external_ref == "DEV-1"

    asyncio.run(_run())


def test_the_external_reference_survives_a_failure() -> None:
    """It is the only handle an operator can undo by hand.

    Losing it because the call failed *after* the far side created the record is the worst possible
    time to lose it.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await begin_effect(
            EffectRecord(
                effect_id="eff-partial",
                connector="effects-test",
                job="file_deviation",
                system="the QMS",
                reversal="compensating",
            )
        )
        await settle_effect(
            "eff-partial", state="failed", external_ref="DEV-77", detail="timed out after create"
        )
        stored = await get_effect("eff-partial")
        assert stored is not None
        assert (stored.state, stored.external_ref) == ("failed", "DEV-77")

    asyncio.run(_run())


def test_an_irreversible_effect_waits_for_a_human_and_refuses_on_expiry() -> None:
    """The per-call approval, which is the question D-2026-08-15 left open in as many words.

    Asserted over the workflow source rather than by running it: the property is which branch the
    run takes before it acts, and a live assertion would need a broker, a bundle worker and a
    fake external system. What matters and is checkable here is that the refusal is unconditional —
    anything other than an explicit approval attempts nothing.
    """
    source = (SRC / "durable" / "connector_job.py").read_text(encoding="utf-8")
    assert 'if job.effect_reversal != "irreversible":' in source
    assert 'outcome.state != "answered" or not outcome.payload.get("approved", False)' in source
    assert "nothing was attempted" in source
    # And the approval is awaited *before* the ledger is opened and the child is run, in that order.
    approve = source.index("await self._approve_effect(")
    begin = source.index("await self._begin_effect(")
    child = source.index("await self._run_child(job)")
    assert approve < begin < child


def test_no_job_in_this_repository_declares_an_effect() -> None:
    """The seam ships with no caller, and saying so is the point.

    Every job here writes this system's own stores — a calculation cached, a note proposed, a row
    recorded. Declaring an effect on one of them would be a false claim about what it reaches, and
    a shipped example would be a capability nobody asked for on the surface of every deployment.
    The declaration is for a site that has a system to reach.
    """
    manifests = list(SRC.rglob("connector.yaml"))
    assert manifests, "no connector manifests found — this test would assert nothing"
    declaring = [
        path.relative_to(SRC).as_posix()
        for path in manifests
        if "effect:" in path.read_text(encoding="utf-8")
    ]
    assert declaring == [], (
        f"{declaring} declare an effect. Every job in this repository writes this system's own "
        "stores; an effect names a system this deployment does not own."
    )
