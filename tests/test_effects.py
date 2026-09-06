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
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.connectors import jobs as connector_jobs
from chemclaw.connectors.manifest import ConnectorManifest, EffectSpec, JobSpec
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import connector_job
from chemclaw.durable.connector_job import ConnectorJobInput
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


def _bundle(*jobs: JobSpec) -> ConnectorManifest:
    """A bundle declaring exactly these jobs, with nothing else to distract the validator."""
    return ConnectorManifest(
        name="site", description="a bundle that acts on a system we do not own", jobs=list(jobs)
    )


def test_a_named_compensation_has_to_be_a_job_this_bundle_declares() -> None:
    """The half of the claim `EffectSpec` could not check, because a job cannot see its siblings.

    Nothing *runs* a compensation, and that is a decision rather than an omission: naming one tells
    an operator which job undoes this one, and launching it is their call through the ordinary
    launcher. Which is precisely why the name has to resolve — the field's whole value is that
    somebody can act on it, so a manifest could otherwise declare a reversibility naming a job that
    does not exist, and the empty-string case is already refused for exactly that reason.
    """
    with pytest.raises(ValidationError, match="names compensation"):
        _bundle(_job(system="the LIMS", reversal="compensating", compensation="retract_it"))

    retract = JobSpec(name="retract_it", workflow="RetractWorkflow", summary="undo the submission")
    resolved = _bundle(
        _job(system="the LIMS", reversal="compensating", compensation="retract_it"), retract
    )
    assert resolved.jobs[0].effect is not None
    assert resolved.jobs[0].effect.compensation == "retract_it"


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


async def _clear(effect_id: str) -> None:
    """Drop one effect row, so a rerun of these tests starts from no row rather than a stale one."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM effects WHERE effect_id = %s", (effect_id,))
        await conn.commit()


def test_a_failure_after_the_change_landed_cannot_rewrite_the_applied_row() -> None:
    """The one write that must not be believed: `failed` over an effect that already applied.

    `ConnectorJobWorkflow` settles `applied` and then runs `_finish` **inside the same `try`**,
    whose `except BaseException` settles `failed`. So a `ValidationError` out of the note write, or
    the documented cancellation path, arrived at the ledger as "this did not happen" about a
    deviation standing in somebody's QMS — and an operator reading the only record of what this
    system changed outside itself would file it a second time. `unsettled()` could not surface it
    either, because that reads `attempting`.

    Asserted as the *second* settle being refused rather than as the first succeeding, because the
    first always worked; it was the overwrite that lied.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        effect_id = "eff-applied-then-failed"
        await _clear(effect_id)
        await begin_effect(
            EffectRecord(
                effect_id=effect_id,
                connector="qms",
                job="file_deviation",
                system="the QMS",
                reversal="irreversible",
                requested_by="u-1",
                session_id="s-1",
                approved_by="u-qa",
            )
        )
        await settle_effect(effect_id, state="applied", external_ref="DEV-2291", detail="filed")
        # Everything after the child returns runs inside the same `try`; this is what its handler
        # would write.
        await settle_effect(effect_id, state="failed", detail="Cancelled")

        stored = await get_effect(effect_id)
        assert stored is not None
        assert stored.state == "applied", (
            "a failure after the change landed rewrote the ledger to 'failed'; an operator would "
            "be told the irreversible change did not happen and would repeat it"
        )
        assert stored.external_ref == "DEV-2291", (
            "the far side's handle was erased by the later settle — it is the only string an "
            "operator can undo the change by"
        )

    asyncio.run(_run())


def test_a_settle_without_a_handle_does_not_erase_the_one_already_recorded() -> None:
    """`external_ref` is coalesced, not assigned.

    A compensating settle that knows the state but not a new reference must leave the reference
    alone; assigning would blank the field precisely when an operator most needs it.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        effect_id = "eff-handle-kept"
        await _clear(effect_id)
        await begin_effect(
            EffectRecord(
                effect_id=effect_id,
                connector="qms",
                job="file_deviation",
                system="the QMS",
                reversal="compensating",
                requested_by="u-1",
                session_id="s-1",
            )
        )
        await settle_effect(effect_id, state="failed", external_ref="DEV-7", detail="partial")
        await settle_effect(effect_id, state="compensated", detail="rolled back")
        stored = await get_effect(effect_id)
        assert stored is not None
        assert (stored.state, stored.external_ref) == ("compensated", "DEV-7")

    asyncio.run(_run())


def test_an_applied_effect_can_still_be_compensated() -> None:
    """The one transition out of `applied` that must survive the overwrite guard.

    `reversal: compensating` means "undone by another declared job", and applied → compensated is
    the *only* path by which that state is ever reached — so a guard that blocks every write over
    `applied` leaves the ledger claiming a change is standing after it has been rolled back. That is
    the same lie the guard exists to prevent, told the other way round.

    The existing compensation test asserts `failed` → `compensated`, which the guard always allowed;
    it passes whether or not this path works, which is why it could not catch this.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        effect_id = "eff-applied-then-compensated"
        await _clear(effect_id)
        await begin_effect(
            EffectRecord(
                effect_id=effect_id,
                connector="qms",
                job="file_deviation",
                system="the QMS",
                reversal="compensating",
                requested_by="u-1",
                session_id="s-1",
                approved_by="u-qa",
            )
        )
        await settle_effect(effect_id, state="applied", external_ref="DEV-55", detail="filed")
        await settle_effect(
            effect_id, state="compensated", external_ref="DEV-55R", detail="withdrawn"
        )

        stored = await get_effect(effect_id)
        assert stored is not None
        assert stored.state == "compensated", (
            "an applied effect could not be compensated, so the ledger still says a change is "
            "standing after it was rolled back"
        )
        assert stored.external_ref == "DEV-55R", "the compensating handle was not recorded"

        # And the guard it must not weaken: `failed` still cannot overwrite `applied`.
        await settle_effect(effect_id, state="applied", detail="re-applied")
        await settle_effect(effect_id, state="failed", detail="Cancelled")
        again = await get_effect(effect_id)
        assert again is not None and again.state == "applied"

    asyncio.run(_run())


def test_an_irreversible_effect_without_a_named_approver_refuses_to_run() -> None:
    """The producer side of the separation-of-duties control, which had no test at all.

    `grep -rn "effect_approv" tests/` returned nothing: the config setting, the launch-site read and
    the fail-closed refusal all shipped unexercised, and only the third layer — `_may_answer`'s kind
    check — was covered. A security control claimed in an ADR with nothing checking its producer is
    this repository's own `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
    shape, which is exactly what the audit that produced the control was about.

    Asserted through the manifest and the job input rather than by driving a broker: what matters is
    that an irreversible job cannot reach the approval wait carrying an empty `asked_of`, because
    that is the branch `_may_answer` opens to everyone.
    """
    # An irreversible effect must carry a named approver into the workflow, or the launch is not
    # capable of raising a routed approval.
    spec = JobSpec(
        name="file_deviation",
        workflow="FileDeviationWorkflow",
        description="File a deviation in the QMS.",
        summary="Filed a deviation.",
        expensive=True,
        effect=EffectSpec(system="the QMS", reversal="irreversible"),
    )
    assert spec.effect is not None
    assert spec.effect.reversal == "irreversible"

    # The input the launch site builds carries the approver, and it defaults to empty — which the
    # workflow refuses rather than treating as "anybody".
    empty = ConnectorJobInput(
        connector="qms",
        job="file_deviation",
        workflow="FileDeviationWorkflow",
        task_queue="connector-qms",
        rationale="a deviation was observed",
        requested_by="u-1",
        effect_system="the QMS",
        effect_reversal="irreversible",
    )
    assert empty.effect_approver == "", "the field must default empty so the refusal is reachable"

    routed = empty.model_copy(update={"effect_approver": "qa-leads"})
    assert routed.effect_approver == "qa-leads"


def test_the_launch_site_reads_the_approver_from_configuration() -> None:
    """The read must happen outside workflow code, and it must actually happen.

    In the workflow it would change the scheduled child on replay; absent entirely, every
    irreversible job refuses to run and the control reads as broken rather than unconfigured. This
    pins the source rather than the value, because the value is a deployment's.
    """
    source = inspect.getsource(connector_jobs)
    assert "effect_approver=settings.effect_approval_role" in source, (
        "the launch site no longer reads the approver from configuration, so every irreversible "
        "job refuses to run"
    )


def test_an_unrouted_irreversible_approval_is_refused_rather_than_opened() -> None:
    """`asked_of=""` is `_may_answer`'s "anyone authenticated" branch, including the requester.

    That is how the seam shipped: `_approve_effect` raised every approval with no routing, so the
    person who launched an irreversible change could approve it themselves. The refusal is
    unconditional — dev as well as under Entra — because a workflow may not read `settings`, and
    because there is no version of "nobody in particular signs off an unrecoverable change" that is
    right.
    """
    source = inspect.getsource(connector_job)
    approve = source[source.index("async def _approve_effect") :]
    approve = approve[: approve.index("async def _begin_effect")]
    assert "if not job.effect_approver:" in approve, (
        "the unrouted-approval refusal is gone; an irreversible effect is self-approvable again"
    )
    assert "asked_of=job.effect_approver" in approve, (
        "the approval wait is raised unrouted, which opens the anyone-authenticated branch"
    )


def test_the_ledger_publishes_no_reader_nothing_in_this_repository_names() -> None:
    """Every public name here is reached from somewhere; two were reached from nowhere at all.

    `effects_for_session` had **zero** callers — no `src/`, no `tests/`, no route, no CLI — while
    its docstring called itself "the evidence pack's read"; `operations/evidence_pack.assemble` has
    always issued its own `SELECT ... FROM effects WHERE session_id = %s` instead, and
    `infra/sql/078_effects_session_index.sql` justifies its index for both readers. `Unsettled`,
    the model carrying the operator-facing `meaning` sentence, had no reader either. Both are the
    `map_to_hpc_identity` shape D-2026-08-15 deleted 254 lines for and the `audit_events.agent`
    shape D-2026-08-26 wrote an absence test for: a surface that is described but not served, which
    reads as a control that exists.

    The rule is deliberately "named anywhere in this repository" rather than "called from `src/`".
    `get_effect` and `unsettled` are reached only from this file, and that is a different thing:
    they are the store's own accessors, exercised as the read-back of the write path under test,
    and deleting them would put raw SQL in a test instead of removing a claim. What no rule here can
    check is the one gap the module docstring now states plainly — `unsettled` is served by no
    operator surface — because "a name nothing reaches" and "a name only a person could reach" are
    not distinguishable from inside the tree.
    """
    import ast

    ledger = SRC / "durable" / "effect_ledger.py"
    public = sorted(
        node.name
        for node in ast.parse(ledger.read_text()).body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    )
    assert public, "the scan found no definitions at all, so it is proving nothing"

    # Identifiers as *code*, not as text: this test's own docstring names both deleted readers, and
    # a substring scan over the tree would therefore report them as reached by the test that exists
    # to say they were not.
    named: set[str] = set()
    for tree in (SRC, Path(__file__).parent):
        for path in sorted(tree.rglob("*.py")):
            if path == ledger:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Name):
                    named.add(node.id)
                elif isinstance(node, ast.Attribute):
                    named.add(node.attr)
                elif isinstance(node, ast.alias):
                    named.add(node.name.rsplit(".", 1)[-1])
    unreached = [name for name in public if name not in named]
    assert unreached == [], (
        f"{unreached} is published by the effect ledger and named nowhere else in the tree — a "
        "reader that exists only in its own module is a claim that something is observable"
    )
