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
    # The most hypotheses that reach the tournament after screening. At 10 that is 4 Swiss rounds
    # of 5 pairings — 20 comparisons, plus 5 more for the double-judged first round below, so 25
    # judged model calls at the shipped defaults. `hypotheses.pairing.comparisons_for` computes it,
    # and takes `double_judge_first_round` precisely so this number is not quoted without it.
    #
    # **It also bounds the result payload, and that figure was wrong here once.** The outcome is
    # returned whole in `ConnectorJobResult.data` and read back through `get_durable_job_status`,
    # where `agent_max_tool_result_chars` (60,000) applies to the whole `ToolMessage`. Measured at
    # this default with deliberately verbose content — 200-character statements, three objections
    # each — `data` is 32,378 characters. An earlier version of this comment stopped there and
    # claimed "roughly a factor of two in hand"; it had missed that `report.summarise` re-rendered
    # the same table into `summary`, which rides in the same message: **57,215 combined, a factor
    # of 1.05**. And the failure would not have been silent, as that comment also said —
    # `agent/tool_result_size.py` cuts from the middle and says so, which leaves the `data` JSON
    # unparseable and removes the centre of the ranking.
    #
    # `report._SUMMARY_ROWS` now bounds the prose at the top rows, so the summary no longer grows
    # with the field. Re-measure both halves before raising this, not just `data`.
    hypothesis_max_field: int = Field(default=10, ge=2)
    # Judge the first round in both presentation orders, so position bias is measured on every run
    # rather than assumed absent. Costs `field/2` extra calls once. Turning this off makes
    # `TournamentOutcome.position_bias` `None` — absent, not zero, which is the honest reading.
    hypothesis_double_judge_first_round: bool = True
    # One structured model call: generation, critique, a comparison, or deriving a check.
    hypothesis_call_timeout_seconds: float = Field(default=120.0, gt=0)
    # One evidence sweep across every internal source, for one hypothesis.
    hypothesis_evidence_timeout_seconds: float = Field(default=180.0, gt=0)
    # A computable check's result, as text, in the outcome a chemist reads. Bounded because it
    # is a tool's own output landing in a note body and in the job envelope: a site-reactivity
    # panel over a large molecule runs to thousands of characters, and the result payload is
    # already shared with the ranked table.
    hypothesis_result_max_chars: int = Field(default=2000, ge=200)
    # The most `experiment-proposal` notes one tournament writes, taken from the top of the table. A
    # run that filed one note per hypothesis would bury the corpus under proposals nobody asked for,
    # and the ranking is precisely what says which are worth writing down.
    hypothesis_max_proposals: int = Field(default=3, ge=0)
    # Durable calculations one tournament may start on its own. **Two, deliberately low.** These
    # are the jobs a manifest marks `expensive: true`: a solvent screen is one conformer search per
    # solvent per species, so a single check can be minutes of compute and a tournament that ran
    # one per hypothesis would spend a chemist's budget on a question they asked in passing. A
    # check past the cap is reported as not run *for budget* rather than dropped, so a thin result
    # never reads as a complete one. Raising it is a deployment's call about its own cluster.
    hypothesis_max_calculations: int = Field(default=2, ge=0)
    # How wide a swept axis may be. **`max_calculations` does not bound this and cannot** — it
    # counts checks, and one check that sweeps twelve solvents is twelve conformer searches inside
    # a single child workflow, which is the budget escaping through the one argument the model is
    # allowed to choose. An over-wide axis is refused rather than truncated: the swept values are
    # reported beside the answer, so silently dropping some would make that report wrong.
    hypothesis_max_sweep_values: int = Field(default=6, ge=1)
