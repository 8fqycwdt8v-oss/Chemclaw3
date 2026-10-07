"""The shape of one turn's spend — the record, not the machinery that writes it.

`agent/turn_cost.py` owns writing it (the sink, scheduling, booking a disconnected turn), and
`chemclaw.evals` scores it (`turn_cost_ratio`). The eval layer may not import `chemclaw.agent` (the
scorer must not depend on the scored), so the shared data shape lives in `core`, keeping case files
from drifting from what the ledger produces.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TurnCost"]


class TurnCost(BaseModel):
    """What one completed turn spent, and the identity to bill it to.

    **`turn_id` is the key and `correlation_id` is the join**, which used to be one field doing
    both (`D-2026-09-06-an-id-a-caller-chooses-is-not-a-key`). The correlation id already identifies
    a turn, already keys `audit_events` and is already on every log line — all true, and all true of
    an id *this system mints*. The front door also **adopts** one off the request when the caller
    sends a well-formed `X-Chemclaw-Correlation-Id`, on purpose, so a click is traceable from the
    browser inwards; with the ledger keyed on it and written `ON CONFLICT … DO UPDATE`, a caller
    that repeated one header collapsed its whole history to one row. Measured: two turns of 900,000
    and 1,000 input tokens left a single row reading 1,000.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Minted per record, never read off a request: no caller-supplied value would be more correct. A
    # retried write of this record still replaces rather than doubles; two turns cannot share an id.
    turn_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    # The turn's correlation id: the join to `audit_events`, `session_messages` and every log line,
    # and possibly a string the caller chose. Indexed, not unique (migration 088).
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
    # Tokens billed but never reported (usage arrives only on the terminal chunk, so an abandoned
    # turn's prompt is estimated by `agent/turn_usage.InFlightPrompts`). Kept apart from the
    # measured four so inference never passes for a provider's number; the budget meters the sum.
    # `0` on completed turns.
    estimated_tokens: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    # False when the turn was torn down before it answered (`completed=answered`). Such turns are
    # recorded, not filtered, because they spent real tokens. Derived from `outcome` and kept for
    # existing readers; `outcome` is the precise field.
    completed: bool = True
    # How the turn ended (`chemclaw.api.runner._OUTCOMES`). Written by `api.runner._settle_outcome`
    # for chat turns and by `durable.template_activities._book_step_spend` for harness steps (which
    # spells values as literals, since it cannot import `api`). A turn whose process died is booked
    # `interrupted` by `api.runner.settle_interrupted_turns`. `unknown` is the column default and
    # also `_book_turn_spend`'s logged fallback.
    outcome: str = "unknown"
    # The user-facing classification of a failed turn (`chemclaw.api.runner._classify`), so a code a
    # chemist quotes can be found server-side. Empty for every other outcome.
    error_code: str = ""
    # The model id the turn's agent route resolved to; the reason the spend counters carry no
    # `model` label. A turn can span models (the verifier's judge has its own route); this names the
    # one that answered.
    model: str = ""
    # What the turn did, which `duration_seconds` cannot separate. `None` where the writer did not
    # count.
    tool_calls: int | None = Field(default=None, ge=0)
    tool_failures: int | None = Field(default=None, ge=0)
    # Calls a governance gate stopped (the plan gate today) — the control working, which must not
    # be read as a failure.
    tool_refusals: int | None = Field(default=None, ge=0)
    # The jobs this turn left *running*, not every launch: fed by `JobStartedEvent`, which
    # `connectors/jobs.py` announces only when the run is still going after the inline wait.
    # `chemclaw_jobs_started_total` counts launches. The name stays because the schema only goes
    # forward.
    jobs_started: int | None = Field(default=None, ge=0)
    # Seconds to the first streamed token, the latency a chemist experiences. `None` when no token
    # was produced, which differs from zero.
    ttft_seconds: float | None = Field(default=None, ge=0)
    # What the context policy did to this turn, joinable with its cost. `context_unreducible` is the
    # one to alert on: a call went out over budget with nothing left to reclaim, the state just
    # before a provider context-length failure (`agent/compaction.py`). Both may be true of one
    # turn.
    compacted: bool = False
    context_unreducible: bool = False
    # What this turn looked at, cited and wrote back; not recoverable afterwards from
    # `session_messages`. `retrieval_calls == 0` on a turn making claims about the programme's
    # chemistry is what the retrieval obligation exists to move; `capture_calls` is the write
    # direction. Consultations, not attempts: `api/runner._TurnLedger.note_event` removes refused or
    # failed calls.
    retrieval_calls: int = 0
    capture_calls: int = 0
    # `score_answer` runs on every turn; these persist its result. `answer_confidence` stays `None`
    # when the verifier did not run, which is not a low score: `review_required` can be True with no
    # confidence, when the answer-shape gate fired.
    answer_confidence: float | None = None
    review_required: bool = False
    notes_cited: int = 0
    # Which skills shaped this turn, from both tiers (the self-confirmation guard reads it; a
    # counter cannot be joined to a turn). Sorted so equal sets give equal rows. A `list` because
    # psycopg adapts lists to arrays and tuples to composites; the column is `TEXT[]`.
    skills_loaded: list[str] = Field(default_factory=list)
    recorded_at: datetime | None = None
