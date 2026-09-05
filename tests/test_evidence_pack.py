"""The evidence pack: assembly, not capture.

Every component has existed since it was written — the audit trail, `job_records`, `note_proposals`,
`plan_approvals`, `effects`. What was missing is the read that puts them beside each other, which is
the artefact a regulated deployment is asked for and the one an engineer wants after an incident.

Three properties are asserted rather than described, because each is a claim the pack makes about
itself: it draws from all five stores, an empty pack says so rather than reading as "nothing
happened", and refusals are part of the record rather than a list of faults.
"""

import asyncio

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.operations.evidence_pack import LIMITS, assemble
from tests.pg import migrated_db_or_skip

SESSION = "pack-test-session"

#: Distinctive enough that no other test's search can match it. `test_job_record_postgres.py`
#: searches `job_records` by the *reason a run was launched*, and this file's fixture writes one —
#: so a rationale sharing any ordinary word with another file's query makes that file fail in a full
#: run and pass alone, which is exactly how it first showed up.
RATIONALE = "zzz-evidence-pack-fixture-rationale-zzz"


async def _clear() -> None:
    """Remove this file's rows from the five shared tables.

    Called before *and* after each seeding test. These tables are shared by the whole suite — the
    isolation schema is per process, not per file — so a fixture that only cleans on the way in
    leaves rows for every later test to trip over.
    """
    async with await connect(settings.postgres_dsn) as conn:
        for table in ("audit_events", "job_records", "note_proposals", "effects", "plan_approvals"):
            await conn.execute(f"DELETE FROM {table} WHERE session_id = %s", (SESSION,))
        await conn.commit()


async def _seed() -> None:
    """One row in each of the five stores, all for one session."""
    await _clear()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO audit_events (correlation_id, session_id, actor, tool, arguments,"
            " outcome, detail, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("c-1", SESSION, "u-1", "gather_evidence", "{}", "ok", "", 12.0),
        )
        await conn.execute(
            "INSERT INTO audit_events (correlation_id, session_id, actor, tool, arguments,"
            " outcome, detail, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("c-2", SESSION, "u-1", "file_deviation", "{}", "refused", "plan not approved", 1.0),
        )
        await conn.execute(
            "INSERT INTO job_records (job_id, connector, job, rationale, requested_by, session_id,"
            " summary) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                "pack-j-1",
                "calc",
                "compute_thermochemistry",
                RATIONALE,
                "u-1",
                SESSION,
                "dG = -12.3 kJ/mol",
            ),
        )
        await conn.execute(
            "INSERT INTO note_proposals (note_id, note_type, content_hash, content, branch, actor,"
            " session_id, state, decided_at, decided_by) VALUES"
            " (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)",
            ("note-1", "playbook", "h", "body", "b", "agent", SESSION, "merged", "u-review"),
        )
        await conn.execute(
            "INSERT INTO effects (effect_id, connector, job, system, reversal, requested_by,"
            " session_id, approved_by, state, external_ref) VALUES"
            " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "e-1",
                "qms",
                "file_deviation",
                "the QMS",
                "irreversible",
                "u-1",
                SESSION,
                "u-qa",
                "applied",
                "DEV-2291",
            ),
        )
        await conn.execute(
            "INSERT INTO plan_approvals (session_id, plan_hash, actor, approved)"
            " VALUES (%s, %s, %s, %s)",
            (SESSION, "plan-abc", "u-1", True),
        )
        await conn.commit()


def test_the_pack_draws_from_every_store_that_holds_part_of_the_record() -> None:
    """Five reads rather than one join.

    The stores are independent by design — an effect is recorded whether or not a note was
    proposed, and a proposal survives the session's messages being pruned — so a join would
    silently drop a row whose partner had been disposed of under a different retention rule.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed()
        pack = await assemble(SESSION)

        assert [call.tool for call in pack.tool_calls] == ["gather_evidence", "file_deviation"]
        assert [job.job for job in pack.jobs] == ["compute_thermochemistry"]
        assert pack.jobs[0].rationale == RATIONALE
        assert [(p.note_id, p.state, p.decided_by) for p in pack.proposals] == [
            ("note-1", "merged", "u-review")
        ]
        assert [(e.system, e.approved_by, e.external_ref) for e in pack.effects] == [
            ("the QMS", "u-qa", "DEV-2291")
        ]
        assert [(a.plan_hash, a.approved) for a in pack.approvals] == [("plan-abc", True)]
        assert not pack.is_empty

    asyncio.run(_run())
    asyncio.run(_clear())


def test_a_refusal_is_part_of_the_record_rather_than_a_fault() -> None:
    """A gate refusing is the control operating.

    Surfaced as a property of the calls rather than as a separate section, so a pack cannot be read
    as a list of things that went wrong — and the reason is carried, because "refused" without one
    is indistinguishable from a broken tool.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed()
        pack = await assemble(SESSION)
        assert [call.tool for call in pack.refusals] == ["file_deviation"]
        assert pack.refusals[0].detail == "plan not approved"
        # And a successful call carries no detail, so the field means "why it was stopped".
        assert [c.detail for c in pack.tool_calls if c.outcome == "ok"] == [""]

    asyncio.run(_run())
    asyncio.run(_clear())


def test_an_empty_pack_says_so_rather_than_reading_as_nothing_happened() -> None:
    """The one thing a caller must check before presenting a pack.

    An empty pack is a statement about the *record* — a window outside retention reads identically
    to a session in which nothing was done — which is the same distinction `Coverage` exists to
    make one module over.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        pack = await assemble("pack-test-session-that-never-existed")
        assert pack.is_empty
        assert pack.tool_calls == [] and pack.effects == []

    asyncio.run(_run())


def test_the_pack_carries_the_three_things_a_reader_must_not_supply_themselves() -> None:
    """`limits` is on the object, not in a docstring.

    The first of the three is the one that matters most and is the easiest to overstate: the trail
    is append-only by *database privilege*, which is not tamper-evidence. The system prompt already
    says this to chemists, and a pack presented to an auditor must not say more than the prompt
    says to the person doing the work.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        pack = await assemble(SESSION)
        assert pack.limits == LIMITS
        joined = " ".join(pack.limits).lower()
        assert "not tamper-evidence" in joined
        assert "not the whole record of the decision" in joined
        assert "not the same as nothing" in joined

    asyncio.run(_run())
    asyncio.run(_clear())


def test_the_effects_a_truncated_pack_shows_are_the_recent_ones() -> None:
    """A capped section must keep the rows an incident is about, which are the newest.

    The pack's own `truncated` field was added because "every read is `ORDER BY <ts> LIMIT 200` and
    nothing said so", and it names effects specifically: "because effects are ordered oldest-first
    the rows dropped were the most recent — the ones an incident is actually about". The field
    disclosed the truncation and left the ordering that makes it wrong.

    Two definitions of that read existed at the same time.
    `durable/effect_ledger.effects_for_session` was documented as "the evidence pack's read",
    ordered `attempted_at DESC`, and had **no caller anywhere in `src/`**; the pack carried an
    inline copy with the opposite ordering, which is the one that ran. The dead one is deleted
    rather than adopted — `operations` may not import `durable` — so the pack's own read is the
    single definition, and it is the ordering that survives.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        async with await connect(settings.postgres_dsn) as conn:
            for index, stamp in enumerate(("2026-01-01", "2026-06-01", "2026-09-01")):
                await conn.execute(
                    "INSERT INTO effects (effect_id, connector, job, system, reversal,"
                    " session_id, state, attempted_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        f"pack-eff-{index}",
                        "lims",
                        "file_deviation",
                        "LIMS",
                        "irreversible",
                        SESSION,
                        "attempting",
                        f"{stamp}T00:00:00+00:00",
                    ),
                )
            await conn.commit()
        try:
            pack = await assemble(SESSION, limit=2)
            assert [effect.effect_id for effect in pack.effects] == [
                "pack-eff-2",
                "pack-eff-1",
            ], "a capped effects section keeps the newest, not the oldest"
            assert "effects" in pack.truncated
        finally:
            await _clear()

    asyncio.run(_run())


def test_the_effect_ledgers_read_path_has_a_caller() -> None:
    """The absence test this repository uses when a claim has no producer.

    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` is the pattern: three
    docstrings said in the present tense that the audit trail names the agent, and the writer had
    no caller. `durable/effect_ledger` was in that shape from the other side — `unsettled()`,
    `effects_for_session()` and `class Unsettled` had zero references in `src/`, the CLI, the API,
    `deploy/` or `infra/`, while the module docstring said "`unsettled` is the query an incident
    starts from" and `Unsettled.meaning` called itself "the sentence an operator needs beside it".

    So: every public read this module still declares is one somebody calls. All three are gone
    rather than given a caller. `unsettled` and `Unsettled` because a reader nothing reads is the
    `map_to_hpc_identity` shape, and the incident query survives as
    `infra/sql/075_effects.sql`'s `effects_unsettled_idx`, which is what an operator actually runs.
    `effects_for_session` because the pack **may not call it**: `tests/test_layering.py` makes
    `operations` a leaf on the kernel so that a reading of the record cannot reach the capability
    that wrote it, and this module is what writes `effects`. Two definitions of one query where
    the layering permits exactly one is how the live reader came to be the wrongly-ordered one, and
    the surviving definition is the pack's own, now ordered newest-first.
    """
    import re
    from pathlib import Path

    from chemclaw.durable import effect_ledger

    source = Path(effect_ledger.__file__).resolve()
    package = source.parents[1]
    #: The one reviewed exemption. `get_effect` is a plain accessor by primary key that claims
    #: nothing in its docstring — it exists so the write-path tests can read back what
    #: `begin_effect`/`settle_effect` wrote without a second copy of `_row`. It is named here so
    #: that adding a *second* such function is a decision somebody takes rather than a silence.
    test_only = {"get_effect"}
    declared = {
        name
        for name in vars(effect_ledger)
        if not name.startswith("_")
        and getattr(getattr(effect_ledger, name), "__module__", "") == effect_ledger.__name__
    }
    word = re.compile(r"\b({})\b".format("|".join(sorted(declared))))
    referenced: set[str] = set()
    for path in package.rglob("*.py"):
        if path != source:
            referenced.update(word.findall(path.read_text(encoding="utf-8")))
    unreferenced = sorted(declared - referenced - test_only)
    assert unreferenced == [], (
        f"{unreferenced} is declared here and called nowhere in src/. A read path with no caller "
        "is a claim that a control exists; give it one or delete it."
    )
