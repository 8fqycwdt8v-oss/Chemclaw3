"""The hypothesis tournament: field size, judging budget and what it will propose.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
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

    # Independent framings a question is attacked from, one generator call each. Four rather than a
    # fixed persona list, because the angles are drafted for the question
    # (`D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared`: a declared list "is
    # wrong in both directions").
    hypothesis_angles: int = Field(default=4, ge=1)
    # Candidates one angle may propose. The product with `hypothesis_angles` is what the screen
    # sees, and the screen usually removes some, so this is deliberately above the field cap.
    hypothesis_per_angle: int = Field(default=3, ge=1)
    # The most hypotheses that reach the tournament after screening. At 10 the tournament is 20
    # comparisons over 4 rounds; at 20 it is 50 over 5.
    #
    # **It also bounds the result payload, which is the constraint that bites first.** The outcome
    # is returned whole in `ConnectorJobResult.data` and read back through `get_durable_job_status`,
    # where `agent_max_tool_result_chars` (60,000) applies. Measured at this default with
    # deliberately verbose content — 200-character statements, three objections each — a full field
    # serialises to **32,378 characters**, so there is roughly a factor of two in hand. A deployment
    # doubling this should re-measure rather than assume: the growth is linear in the field, so 20
    # lands around the cap and the loss would be silent truncation of the ranking a chemist is
    # reading, not an error.
    hypothesis_max_field: int = Field(default=10, ge=2)
    # Judge the first round in both presentation orders, so position bias is measured on every run
    # rather than assumed absent. Costs `field/2` extra calls once. Turning this off makes
    # `TournamentOutcome.position_bias` `None` — absent, not zero, which is the honest reading.
    hypothesis_double_judge_first_round: bool = True
    # One structured model call: generation, critique, a comparison, or deriving a check.
    hypothesis_call_timeout_seconds: float = Field(default=120.0, gt=0)
    # One evidence sweep across every internal source, for one hypothesis.
    hypothesis_evidence_timeout_seconds: float = Field(default=180.0, gt=0)
    # The most `experiment-proposal` notes one tournament writes, taken from the top of the table. A
    # run that filed one note per hypothesis would bury the corpus under proposals nobody asked for,
    # and the ranking is precisely what says which are worth writing down.
    hypothesis_max_proposals: int = Field(default=3, ge=0)
