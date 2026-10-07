"""Settings for the hypothesis tournament: field size, judging budget and what it will propose.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class HypothesisSettings(BaseSettings):
    """Bounds on a hypothesis tournament — how wide the field is and how much judging it buys.

    Grouped because every knob here prices one durable job: the field size decides the number of
    generator calls, and it decides the comparison count too, since Swiss pairing runs
    `field/2 · ceil(log2(field))` of them. A deployment raising `hypothesis_max_field` is buying
    more than it may expect, which is why `hypotheses/pairing.comparisons_for` exists to state the
    number before a run starts.
    """

    # Independent framings a question is attacked from, one generator call each; drafted per
    # question rather than a fixed persona list.
    hypothesis_angles: int = Field(default=4, ge=1)
    # Candidates one angle may propose; deliberately above the field cap since screening removes
    # some.
    hypothesis_per_angle: int = Field(default=3, ge=1)
    # Most hypotheses reaching the tournament after screening (10 → 25 judged calls with the
    # double-judged first round). It also bounds the result payload, which must fit under
    # `agent_max_tool_result_chars` together with its summary (`report._SUMMARY_ROWS` caps the
    # latter). Re-measure both before raising.
    hypothesis_max_field: int = Field(default=10, ge=2)
    # Judge the first round in both orders so position bias is measured every run (`field/2` extra
    # calls). Off makes `TournamentOutcome.position_bias` `None`: absent, not zero.
    hypothesis_double_judge_first_round: bool = True
    # One structured model call: generation, critique, a comparison, or deriving a check.
    hypothesis_call_timeout_seconds: float = Field(default=120.0, gt=0)
    # One evidence sweep across every internal source, for one hypothesis.
    hypothesis_evidence_timeout_seconds: float = Field(default=180.0, gt=0)
    # Bound on one `computable` check: open the connectors, then call one tool, which may be a
    # semiempirical calculation on a cache miss. 915 = `calc_server_timeout_seconds` (900) +
    # `connector_open_timeout_seconds` (15), so a connector's `request_timeout` trips first;
    # `tests/test_hypothesis_tournament.py` holds the ordering. Raise it with either.
    hypothesis_check_timeout_seconds: float = Field(default=915.0, gt=0)
    # Characters of a computable check's result kept in the outcome; a tool's output lands in a note
    # body and the job envelope.
    hypothesis_result_max_chars: int = Field(default=2000, ge=200)
    # Most `experiment-proposal` notes one tournament writes, from the top of the ranking.
    hypothesis_max_proposals: int = Field(default=3, ge=0)
    # Durable `expensive: true` calculations one tournament may start on its own; deliberately low.
    # A check past the cap is reported as not run for budget, so a thin result never reads as
    # complete.
    hypothesis_max_calculations: int = Field(default=2, ge=0)
    # Widest swept axis allowed; `max_calculations` counts checks, not the calculations one sweep
    # starts. Over-wide axes are refused, not truncated, because the swept values are reported.
    hypothesis_max_sweep_values: int = Field(default=6, ge=1)
