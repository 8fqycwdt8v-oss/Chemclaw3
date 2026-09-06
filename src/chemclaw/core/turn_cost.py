"""The shape of one turn's spend — the record, not the machinery that writes it.

**In `core/` because two layers read it and only one writes it.** `agent/turn_cost.py` owns the
sink, the fire-and-forget scheduling and the `finally`-block discipline that makes a disconnected
turn still book its cost; those are behaviours and they belong beside the turn. This is the *record*
those behaviours move, and `chemclaw.evals` scores it — `turn_cost_ratio` reads a list of these out
of a case file.

Splitting them is what keeps `tests/test_layering.py` honest rather than what works around it. The
eval layer is deliberately not allowed to import `chemclaw.agent`: an eval scores output, the agent
is the thing under test, and a dependency from the scorer to the scored is the one edge that would
let a change to the agent silently change what "correct" means. `core` is the shared kernel both may
read, and a data shape is exactly the kind of thing it exists to hold.

The alternative — a second model in `evals/` describing the same five counters — was rejected for
the reason the reuse is worth having: a case file's shape would then be free to drift from what the
ledger actually produces, and nothing would say so.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TurnCost"]


class TurnCost(BaseModel):
    """What one completed turn spent, and the identity to bill it to.

    `correlation_id` is the key rather than a fresh id because it already identifies the turn
    uniquely, already keys `audit_events`, and is already on every log line — so the ledger joins to
    the trail and the logs with no new correspondence to maintain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: str = Field(min_length=1)
    session_id: str = ""
    actor: str = ""
    # `default` rather than empty for a session on no profile, matching the metric label exactly, so
    # a sum here and a sum there answer the same question the same way.
    profile: str = "default"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    # False when the turn was torn down *before it answered* — `chemclaw.api.runner` books
    # `completed=answered`, so a disconnect or wall-clock deadline that lands after the answer is a
    # completed turn that keeps its history, and only one that lands before it is not. Recorded
    # rather than filtered: those turns spent real tokens, and a ledger that kept only the tidy ones
    # would be wrong in the direction that hides a runaway.
    #
    # **Derived from `outcome` since 2026-08-27, and kept because things already read it.** It is
    # one boolean over a six-value enum: a capped turn, a silent turn, a raised turn, a timed-out
    # turn and an abandoned turn were all simply `False`, and a partial answer after the runaway cap
    # was `True` beside a clean one.
    completed: bool = True
    # How the turn ended (`chemclaw.api.runner._OUTCOMES`). Two producers, because a step is not
    # a chat turn: `api.runner._settle_outcome` for a chat turn, and
    # `durable.template_activities._book_step_spend` for a harness step — which cannot import
    # `api` (`tests/test_layering.py`), so it spells four of the six values as literals.
    # `unknown` is the column default a row written before this field existed carries, and it is
    # *also* written live, by `_book_turn_spend`'s caught-exception fallback, which logs when it
    # does.
    outcome: str = "unknown"
    # The user-facing classification of a failed turn (`chemclaw.api.runner._classify`), empty for
    # every other outcome. It was computed, sent to the chemist and discarded server-side, so a
    # chemist quoting a code named something the deployment had no record of.
    error_code: str = ""
    # The model id the turn's *agent* route resolved to. `core/metrics.py` and the runbook both
    # said this table carried model attribution — it is the stated reason the spend counters
    # deliberately omit a `model` label — while the table had no such column. One turn can span
    # models (the verifier's judge runs on its own route); this names the one that answered.
    model: str = ""
    # What the turn actually did, which `duration_seconds` alone cannot separate: a slow turn that
    # made two tool calls and a slow turn that made forty are different problems. `None` where the
    # writer did not count, which is every row written before these existed.
    tool_calls: int | None = Field(default=None, ge=0)
    tool_failures: int | None = Field(default=None, ge=0)
    # Calls a governance gate stopped (the plan gate today) — the control working, which must not
    # be read as a failure.
    tool_refusals: int | None = Field(default=None, ge=0)
    jobs_started: int | None = Field(default=None, ge=0)
    # Seconds to the turn's first streamed token — the latency a chemist actually experiences, as
    # opposed to `duration_seconds`, which includes every tool call after it. `None` when the turn
    # produced no token at all, which is a different fact from zero.
    ttft_seconds: float | None = Field(default=None, ge=0)
    # **What the context policy did to this turn**, and the join nothing could make before it.
    # `chemclaw_context_compactions_total` says the policy fired somewhere in the fleet;
    # `input_tokens` above says what a turn cost. Neither could answer "what is compaction costing
    # us, and is it working", because the counter carries no identity and the ledger had never
    # heard of the policy.
    #
    # `context_unreducible` is the one to alert on: a model call went out over the conversation
    # budget with the policy unable to reduce it further, which is the state immediately before a
    # context-length failure at the provider. Measured, the compaction counters cannot see it —
    # both edits run and reclaim nothing — so a turn like that used to look exactly like a quiet
    # one (`agent/compaction.py::_record_overrun`).
    #
    # Both may be true of one turn: an early model call reduced the thread, a later one had
    # nothing left to reclaim.
    compacted: bool = False
    context_unreducible: bool = False
    # **The knowledge dimensions — what this turn looked at, what it cited, what it wrote back.**
    #
    # This row already carried what a turn spent and how it ended. It could not say whether the
    # turn consulted the record at all, and that is the question two separate reviews of this
    # system's knowledge loop had to answer with bespoke scripts because no series and no table
    # held it. None of it is recoverable afterwards: the events are gone, and `session_messages`
    # holds prose rather than which tool ran.
    #
    # `retrieval_calls == 0` on a turn that made a claim about this programme's chemistry is the
    # signal the retrieval obligation in the system prompt exists to move, and the only way to
    # know whether it worked. `capture_calls` is the same question in the write direction.
    retrieval_calls: int = 0
    capture_calls: int = 0
    # `score_answer` computes these on **every** production turn and they were streamed to the
    # client and discarded — `api/schemas.py` records that they are not persisted either, so the
    # richest answer-quality signal this system produces was retained nowhere.
    #
    # `answer_confidence` stays `None` when the verifier did not run. That is not a low score and
    # must never be stored as one: `review_required` can be True with `confidence is None`,
    # because the answer-shape gate found something and that is not a score.
    answer_confidence: float | None = None
    review_required: bool = False
    notes_cited: int = 0
    recorded_at: datetime | None = None
