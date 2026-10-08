"""Warn-and-degrade sites leave a number behind, and the subsystem label set stays enumerable.

Each swallow is individually right (a preference that did not persist must not fail a turn),
which is why each must leave a count: from outside, a silently failing store and a healthy service
look identical. `core/metrics_bridge.degraded` counts through the swallowing bridge, then logs
under a stable `degraded[<subsystem>]` marker with the caller's logger. Driven against the real
registry, not by asserting a call was made.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from chemclaw.core.metrics import _COUNTER_LABELS, _COUNTERS, METRICS
from chemclaw.core.metrics_bridge import degraded

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"
_COUNTER = "chemclaw_degraded_total"

# Every subsystem name a `degraded()` call site passes. Pinned as a set rather than a count so the
# failure message names what changed, and pinned at all because this is the metric's label value
# space: a label whose values are not enumerable is how a registry ends up with unbounded series.
_EXPECTED_SUBSYSTEMS = {
    # `core/db.py`, where a DSN libpq cannot parse is swallowed: the error message degrades to
    # `<postgres>` and the DSN's `options` are dropped, invisible even on a connect that succeeds.
    "db_dsn",
    # `connectors/calc/remote.py`, on the paths that raise `CalcServerError` (backend unreachable,
    # internal error, dropped mid-call). `CalcToolError` is a refusal of a chemist's input and is
    # not counted, so a typo cannot look like a down pod.
    "calc_server",
    # `agent/checkpointer.SchemaStampedSaver.aput`. The one site that does **not** swallow: the
    # caller still fails, and the counter makes a checkpointer outage visible.
    "checkpointer",
    # `agent/compaction`. A raising context edit used to kill the turn as a generic internal error;
    # it now continues uncompacted, which is the safe direction — a request over budget still has a
    # chance of being answered, where a failed turn has none.
    "compaction",
    # `agent/context_budget.MeasureRequestPrefix`. An unmeasurable prefix is budgeted as zero (the
    # generous direction), which silently reverts the budget to the configured constant.
    "context_budget",
    # `api/budget.py`, both halves of the durable spend window. An unreadable meter degrades to this
    # pod's own counters, so the cap silently binds per process again
    # (`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`).
    "budget_window",
    # `core/bookkeeping.drain`: writes a turn owed the record that a shutdown could not wait for.
    "bookkeeping",
    # `api/budget.check_thread_size`, whose read of a thread's stored size admits the turn when the
    # database cannot answer — the load that follows reads the same database. Silent otherwise: the
    # memory bound it enforces would simply stop binding.
    "thread_size",
    "cost_ledger",
    # A retrieval source that could not be asked — the degradation a chemist feels as evidence
    # missing from an answer.
    "evidence_source",
    # `agent/exhibit_notes.exhibit_turn_note`. An artefact store that cannot be read at turn start
    # means the turn runs without the note — no listing and no announcement of a chemist's edit —
    # which is silent from the chemist's side: the agent simply does not know the table changed.
    "exhibits",
    # `durable/connector_job.py::ConnectorJobWorkflow._record_run`: a swallowed write means the run
    # survives only in Temporal's history.
    "job_record",
    "job_resume",
    # `durable/job_metrics.refresh_open_jobs`, swallowed so a broker hiccup cannot stop the refresh
    # loop; counted because a gauge that stops moving reads as "no durable work".
    "jobs_in_flight",
    # `agent/protocol_design_tools.recorded_failures`, on an unreadable corpus. The check reports
    # "no recorded failure" either way, so a broken lookup would pass every draft clean; swallowed
    # so an outage cannot refuse a design, and counted for that reason.
    "failure_memory",
    "log_redaction",
    # `durable/deliver_message.deliver_message_activity`. `deliver()` swallows a per-channel failure
    # so one broken webhook is not everyone's outage; without a count, dropped and delivered
    # messages look identical. Named for the seam, which every outbound kind shares.
    "message_delivery",
    # `deliver/message._connector_secret_envs`. If connector bearer-token names cannot be resolved,
    # tokens quoted inside a tool error stop being scrubbed from outbound webhook bodies for the
    # life of the process — a security degradation.
    "deliver_redaction",
    # `deliver/registry.deliver`, on a channel that cannot be *built* (bad `config:`, unimportable
    # callable, a destination `entra_required` forbids). Separate from
    # `chemclaw_delivery_failures_total` because a build failure is a permanent misconfiguration, a
    # send failure usually transient.
    "delivery_channel_config",
    # `ingest/commitments/json_export.fetch_commitments`, on an export path that does not exist. The
    # sync succeeds, nothing is mirrored, and `review_commitments` would present an empty portfolio
    # as truthful.
    "commitment_mirror",
    # The same module, on an export present but unparseable or invalid. Separate from the one above
    # because "the knob points nowhere" and "none of it parsed" need different operator actions.
    "commitment_export",
    # `api/runner.py::_escalate_exhausted_review`, on a review request that could not be opened. The
    # answer still ships (a degradation, not an error), but it goes out marked for review with
    # nobody asked to read it, indistinguishable from the escalation being off.
    "answer_review_escalation",
    "plan_approval",
    # `agent/protocol_design_tools.uncited_precedent`, on an unreachable reaction index — ordinary
    # on a laptop. The passing text then says nothing was offered, not that no precedent exists.
    "precedent_lookup",
    "preferences",
    # `publish/outbox`, on a row left `pending` at the attempt ceiling with no error after an
    # interrupted delivery: unclaimable and invisible to the dead-letter counter. The reaper
    # dead-letters it; this counter is how an operator learns it happened.
    "result_outbox_orphaned",
    # `publish/drivers/sql`, on a result store one migration behind the writer: missing columns
    # would otherwise be dropped while the delivery reports success. Per (sink, table), since a
    # lagging schema is a deployment fact.
    "result_sink_schema_lag",
    # `publish/drivers/sql`, when an `executemany` group is refused and the sink replays it a row at
    # a time so the error can name the row. The delivery still lands or raises `SinkRejectedError`;
    # what is lost is the batching, which should be counted rather than inferred from a slower
    # drain.
    "result_sink_batch_replayed",
    # `agent.condense`, per protocol: no reachable `"protocol-digest"` route, or one extraction that
    # failed. Otherwise a down endpoint looks like protocols with empty procedures.
    "protocol_digest",
    # `agent/session_store.message_from_row`. Its catch is `Exception`, which also swallows a
    # converter *bug*; the counter separates that from one unreadable legacy row.
    "session_transcript",
    "skill_manifest",
    "spend_cap",
    # `agent/stored_skill_tools._unreadable`: a stored `SKILL.md` whose frontmatter cannot be read
    # is scoped to nothing. Separate from `skill_manifest` (the filed trees) because the remedy
    # differs: a stored body is fixable only by its owner or an administrator. The message carries
    # the exception type, never its text, which would quote a person's own words.
    "stored_skill_manifest",
    # `science/calc/geometry.check_server_address`
    # (D-2026-08-21-a-geometry-is-an-address-not-a-payload). Visible *only* as a counter: a
    # `structure_id` derived differently here and on the calculation server makes every lookup miss
    # while the service looks healthy.
    "structure_id",
    # `core/temporal_client.telemetry_runtime`, when the SDK's Prometheus exporter cannot bind; it
    # must not be reported as Temporal being unreachable.
    "temporal_sdk_metrics",
    # `api/runner._earlier_user_texts`, the transcript read a `basis="stated"` quote is checked
    # against. An unreachable store degrades to no earlier words (the strict direction: a true quote
    # is refused, a fabricated one never accepted); counted because the turn otherwise looks normal.
    "stated_quote_history",
    # `api/runner.run_turn`'s review-revision loop, around the two checkpointer reads and the one
    # write that keep the thread ending on the answer that ships. The answer is already in hand, so
    # a checkpointer error there costs the thread its tidiness (or the round), never the turn.
    "review_revision_thread",
    "tool_result_store",
    "transcript_projection",
}


def _subsystem_argument(node: ast.Call) -> ast.expr | None:
    """The `subsystem` argument of a `degraded(...)` call, in either form it can be written.

    Keyword before positional, because a call may pass `logger` positionally and `subsystem=` by
    name. Returns None only for a call that passes no subsystem at all, which `mypy` rejects.
    """
    keyword = next((kw.value for kw in node.keywords if kw.arg == "subsystem"), None)
    if keyword is not None:
        return keyword
    return node.args[1] if len(node.args) > 1 else None


def _is_degraded_call(node: ast.AST) -> bool:
    """Whether `node` calls the helper — `degraded(...)` or `<module>.degraded(...)`.

    Both spellings, or a computed label through `metrics_bridge.degraded` would pass. Matched on the
    attribute name alone, like `test_metric_declarations.py` matches `increment`, so any import form
    is covered.
    """
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Name) and node.func.id == "degraded") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "degraded"
    )


def _call_site_subsystems() -> dict[str, list[str]]:
    """Read the literal subsystem argument of every `degraded(...)` call under `src/chemclaw`."""
    found: dict[str, list[str]] = {}
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not _is_degraded_call(node):
                continue
            assert isinstance(node, ast.Call)
            subsystem = _subsystem_argument(node)
            assert isinstance(subsystem, ast.Constant) and isinstance(subsystem.value, str), (
                f"{f}:{node.lineno}: degraded()'s subsystem must be a literal — it is a metric "
                "label value, and a computed one cannot be bounded by reading the source"
            )
            found.setdefault(subsystem.value, []).append(
                f"{f.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"
            )
    return found


def test_the_subsystem_label_space_is_exactly_what_is_declared() -> None:
    """Both directions: a new subsystem is a deliberate addition, a removed one loses its row."""
    observed = _call_site_subsystems()
    assert set(observed) == _EXPECTED_SUBSYSTEMS, (
        f"the `subsystem` label value set changed. Sites: {dict(sorted(observed.items()))}"
    )


def test_the_enumeration_sees_both_call_spellings_and_both_argument_forms() -> None:
    """The extractor sees both call spellings and both argument forms.

    A computed subsystem passed positionally through `metrics_bridge.degraded` or as
    `subsystem=f"..."` must be caught: nothing a request carries may reach this label.
    """
    calls = [
        node
        for node in ast.walk(
            ast.parse(
                'degraded(logger, "a", "m")\n'
                'metrics_bridge.degraded(logger, "b", "m")\n'
                'degraded(logger, subsystem="c", message="m")\n'
            )
        )
        if _is_degraded_call(node)
    ]
    assert len(calls) == 3, "a call spelling the source walk cannot see"
    seen = [_subsystem_argument(call) for call in calls if isinstance(call, ast.Call)]
    assert [s.value for s in seen if isinstance(s, ast.Constant)] == ["a", "b", "c"]


def test_the_counter_is_declared_with_its_label() -> None:
    """The registry refuses an undeclared label, and `record_metric` would swallow that refusal."""
    assert _COUNTER in _COUNTERS
    assert _COUNTER_LABELS[_COUNTER] == ("subsystem",)


def test_a_degradation_is_counted_under_its_own_subsystem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point: the swallow leaves a number, attributed to what lost function.

    Read off the registry rather than off a mock, so this fails against a helper that logs and
    forgets — which is precisely the state 32 of those 35 modules were in.
    """
    logger = logging.getLogger("chemclaw.test.degraded")
    before = METRICS.value(_COUNTER)
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise RuntimeError("the sink is down")
        except RuntimeError:
            degraded(
                logger, "preferences", "could not persist preference %r for %s", "units", "ana"
            )

    assert METRICS.value(_COUNTER) == before + 1
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.name == "chemclaw.test.degraded", "the caller's logger names the failing module"
    assert record.getMessage() == (
        "degraded[preferences]: could not persist preference 'units' for ana"
    )
    assert record.exc_info is not None, "the active exception travels with the record"


def test_the_series_are_separate_per_subsystem() -> None:
    """An alert has to name the failing subsystem, which one undifferentiated total cannot do."""
    logger = logging.getLogger("chemclaw.test.degraded")
    rendered_before = METRICS.render()
    degraded(logger, "cost_ledger", "ledger down", exc_info=False)
    rendered = METRICS.render()
    assert 'chemclaw_degraded_total{subsystem="cost_ledger"}' in rendered
    assert rendered != rendered_before


def _boom(*_args: object, **_kwargs: object) -> None:
    """Stand in for a registry that refuses the update (an undeclared name or label)."""
    raise KeyError("undeclared counter")


def test_a_registry_failure_cannot_replace_the_degradation_it_reports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registry failure cannot replace the degradation it reports.

    `degraded` is called inside `except` blocks, and `Metrics.increment` raises on an undeclared
    name or label, so the update is swallowed. Proved by breaking the registry and requiring the log
    line anyway.
    """
    logger = logging.getLogger("chemclaw.test.degraded")
    original = METRICS.increment
    try:
        METRICS.increment = _boom  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger=logger.name):
            degraded(logger, "preferences", "the store is unreachable", exc_info=False)
    finally:
        METRICS.increment = original  # type: ignore[method-assign]

    assert caplog.records[-1].getMessage() == "degraded[preferences]: the store is unreachable"
