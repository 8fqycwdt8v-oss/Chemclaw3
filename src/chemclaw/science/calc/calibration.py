"""Close the loop between what was predicted and what actually happened (gap IDEA-2).

The stack predicts (xTB, pKa, solubility, BO surrogates) and, separately, ingests what actually
happened (the ELN). Nothing connected the two. `evals/metrics.py::prediction_error` exists but
scores against a *held-out reference in a committed case file* — not against reality as it arrives —
so "how far should I trust this calculator?", which is the entire job of the `calculation-selection`
skill, was answerable only in prose.

This is the ledger that makes it answerable in numbers: every prediction is recorded against a
`(calc_type, calc_version, input_hash)` identity, so a later measurement of the same thing meets it
without a second naming scheme.

**That `input_hash` is *not* the calculation cache's, and this docstring claimed it was.** The
ledger hashes the canonical SMILES (`stable_hash(canonical)` in
`connectors/calc/server/tools.py::_log_prediction`); the cache hashes a dict around it
(`store.molecule_hash` is `stable_hash({"smiles": ...})`). Measured on ethanol: `f29e20f49d416e54`
against `a7d334ebee616d78`. Nothing joins the two tables today, so the claim cost nothing — but it
is a claim about a key, and whoever writes that join on the strength of this sentence gets zero
rows and no error. The two schemes are independent; a join needs a translation, not a `USING`.

**What calibration means here, and why it is three numbers rather than one.**

- **Bias** (mean signed error) says whether a calculator runs high or low. A calculator that is
  reliably +0.4 log units off is *usable with a correction*; one with the same absolute error
  scattered either way is not.
- **MAE** says how far off it typically is — the number a chemist actually wants when deciding
  whether a prediction can stand in for an experiment.
- **Coverage** says how often the truth fell inside the stated uncertainty. This is the one a mean
  error cannot show: a calculator whose errors are small but whose error bars never contain the
  answer is miscalibrated in a way that makes its uncertainty actively misleading, which is worse
  than reporting none.

  **Its target is 0.683, not 1.0, and nothing here used to say so.** The interval is ±1σ and the
  published uncertainties it is scored against are 1σ RMSEs (`crippen_logp_uncertainty = 0.68` is
  Wildman-Crippen's own reported RMSE), so a *correctly* calibrated calculator with Gaussian errors
  misses roughly a third of the time by construction. Read against an unstated target of 1.0 — the
  only one a reader supplies on their own — a perfectly calibrated calculator reads as 32%
  miscalibrated. Materially below 0.683 means the error bars are too tight; materially above means
  they are too loose, which is its own defect and not a better result.

**Deliberately advisory.** Nothing here changes a prediction. *Recording* is best-effort and a
calibration failure never fails a calculation — a broken ledger must degrade the *advice about*
predictions, never the predictions themselves.

**Reading is not.** The rule above is about the write that rides along with a calculation; it was
also applied to the read, and there it means something else entirely. `reconciled_for` is called
only by the two trust tools, so a swallowed read has no calculation to protect — it just answers
"no measurement has ever missed" when the database is down. It raises now, and `Calibration`
carries a `verdict` that separates a disabled ledger from an empty one from too few points, because
all three used to serialize as bias/MAE/RMSE 0.0 and the ledger is **off by default**.
"""

import logging

from pydantic import BaseModel, Field, computed_field

from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

_UPSERT_PREDICTION = """
INSERT INTO predictions (
    calc_type, calc_version, input_hash, subject,
    predicted_value, predicted_uncertainty, unit, predicted_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (calc_type, calc_version, input_hash) DO UPDATE SET
    predicted_value = EXCLUDED.predicted_value,
    predicted_uncertainty = EXCLUDED.predicted_uncertainty,
    predicted_at = now()
"""

# The measurement itself, kept whether or not anything predicted it (DARK-9, `infra/sql/030`).
# Written *before* the reconciliation below, so a measurement for a molecule nothing has predicted
# survives instead of being discarded by an UPDATE that matches nothing.
#
# **The conflict target is the source too** (`infra/sql/093`). `030` keyed the table on
# `(property, input_hash)` and argued that "two values for one property of one molecule is a
# correction, not two facts" — true of a lab revising its own number, false of a replicate or a
# second site, and this statement is where the difference was destroyed: a second lab's `DO UPDATE`
# overwrote the first lab's value and stamped its own name on the row. It still replaces under one
# source, which is the correction case that argument is right about.
_UPSERT_MEASUREMENT = """
INSERT INTO measurements (property, input_hash, subject, value, unit, source, observed_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (property, input_hash, source) DO UPDATE SET
    value = EXCLUDED.value,
    unit = EXCLUDED.unit,
    observed_at = now()
"""

# Every measurement on file for one property of one molecule, as a single row: the value a
# prediction is scored against, how far the reporters are apart, and who they were.
#
# **One fragment, three readers**, because the number `calculator_trust` reports and the number
# `report_measurement` quotes back to the chemist must be the same number by construction rather
# than by two implementations agreeing. The reconciliation is a mean: with the source in the key
# there can be several rows, and an unaggregated `UPDATE … FROM measurements` would have taken
# whichever one the planner handed it first — a scored value determined by nothing the caller
# could see.
#
# `count(*)` is the number of *sources*, not of reports: the primary key holds one row per source,
# so a lab that measured three times contributes one. `count(DISTINCT unit)` is carried because a
# mean over two units is a number in neither; `report_measurement` reconciles every calibrated
# value into the ledger's own unit before it gets here, so this reads 1 in every shipped path and
# says so rather than being believed.
_CONSENSUS = """
    SELECT avg(value)                                AS value,
           min(value)                                AS lowest,
           max(value)                                AS highest,
           count(*)                                  AS sources,
           count(DISTINCT unit)                      AS units,
           max(observed_at)                          AS observed_at,
           string_agg(source, ', ' ORDER BY source)  AS reported_by
      FROM measurements
     WHERE property = %s AND input_hash = %s
"""

# The reverse direction, and the reason the table is worth having rather than merely honest: a
# prediction made *after* a measurement reconciles against it immediately. Without this, storing
# the measurement would only stop the lie, and the ledger would still learn nothing from the
# measure-then-predict order that new chemistry actually follows.
_RECONCILE_FROM_MEASUREMENT = f"""
UPDATE predictions p
   SET observed_value = c.value, observed_at = c.observed_at, observed_source = c.reported_by
  FROM ({_CONSENSUS}) AS c
 WHERE p.calc_type = %s
   AND p.input_hash = %s
   AND p.observed_value IS NULL
   AND c.value IS NOT NULL
"""

# Deliberately *not* scoped by version: a measurement is a fact about the molecule, not about the
# calculator that guessed at it. One reported value scores every version's prediction of that
# molecule, which is what makes a version-over-version comparison possible at all.
#
# It writes the *consensus* rather than the value just reported, so the figure a chemist reads does
# not move to whichever source wrote last. `c.value IS NOT NULL` guards the empty aggregate: a
# grouping-free aggregate over no rows still yields one row of NULLs, and blanking an observed
# value is worse than doing nothing.
_RECORD_OBSERVATION = f"""
UPDATE predictions p
   SET observed_value = c.value, observed_at = c.observed_at, observed_source = c.reported_by
  FROM ({_CONSENSUS}) AS c
 WHERE p.calc_type = %s
   AND p.input_hash = %s
   AND c.value IS NOT NULL
"""

# Scoped to one calculator *version*. Pooling versions was the other half of REV-12: even with the
# write path fixed, a read that ignored the version would average a v1 that ran high against a v2
# that ran low and report the cancellation as good calibration. A chemist asking "how far off is
# this calculator" means the one that just answered them.
_SELECT_RECONCILED = """
SELECT subject, predicted_value, predicted_uncertainty, observed_value
  FROM predictions
 WHERE calc_type = %s AND calc_version = %s AND observed_value IS NOT NULL
"""


class Calibration(BaseModel):
    """How well one calculator's predictions have matched reality so far.

    `n` is reported alongside every figure because a bias computed from three points is not a bias;
    a surface that shows the number without the count invites exactly that mistake.

    The three error figures are `None` rather than 0.0 when there is nothing to compute them from,
    for the reason `uncertainty_coverage` already was: a zero bias is a *measurement*, and a
    calculator that has never been measured must not be reported as a perfect one.
    """

    calc_type: str
    n: int
    # Whether the ledger is recording at all. `calibration_enabled` defaults to **False**, so a
    # shipped deployment's answer here is "nothing is being recorded", not "nothing has missed" —
    # and those two were the same payload.
    enabled: bool = True
    bias: float | None = None
    mean_absolute_error: float | None = None
    rmse: float | None = None
    # Fraction of observations that fell inside the prediction's stated ±1σ interval. `None` when
    # no prediction carried an uncertainty — deliberately not 0.0, which would read as "never
    # covered" rather than "never claimed".
    #
    # **The target is 0.683, not 1.0**: the interval is one standard deviation and the stated
    # uncertainties are 1σ RMSEs, so a correctly calibrated calculator misses a third of the time.
    # Anyone reading this field against an implied 1.0 reports a well-calibrated calculator as 32%
    # miscalibrated.
    uncertainty_coverage: float | None = None
    unit: str = ""

    @property
    def is_meaningful(self) -> bool:
        """Whether enough observations exist for the figures to mean anything."""
        return self.enabled and self.n >= settings.calibration_min_observations

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before quoting any of these figures.

        A `computed_field` and not a bare property, for the reason `FingerprintSearch.verdict` and
        `ScreenResult.verdict` are: a property is not serialized, so the sentence explaining what
        an all-zero payload means would never leave this process — and this was the last advisory
        model in the package without one. A **database outage** is not among the states below
        because it is no longer a state: `reconciled_for` raises rather than answering `[]`.
        """
        if not self.enabled:
            return (
                "CALIBRATION NOT RECORDED: the prediction ledger is disabled, so no "
                f"{self.calc_type} prediction has ever been scored against a measurement. This is "
                "NOT evidence that "
                "the calculator is accurate — nothing was measured. Say the calculator's accuracy "
                "is unknown here and that an operator must enable the ledger."
            )
        if self.n == 0:
            return (
                f"UNCALIBRATED: no measurement has yet been reconciled against a {self.calc_type} "
                "prediction of this calculator version. Its accuracy is unknown, not good. Do not "
                "quote a bias or an error bar from this result."
            )
        if not self.is_meaningful:
            return (
                f"PROVISIONAL: {self.n} observation(s), too few to be meaningful (the minimum is "
                f"{settings.calibration_min_observations}). Report the count, not the figures — a "
                "bias from a handful of points is noise."
            )
        return (
            f"Measured over {self.n} observation(s) of this calculator version. Quote the figures "
            "with the count, and check `calculator_outliers` before trusting the average on a "
            "specific class of molecule."
        )


class ObservedConsensus(BaseModel):
    """Every measurement on file for one property of one molecule, reduced to what scores it.

    The read half of `infra/sql/093`. `value` is the mean over *sources* — the number
    `_RECORD_OBSERVATION` writes into every matching prediction — and the rest is what a mean
    conceals: how many reporters stand behind it, who they were, and how far apart they are. Two
    labs 0.85 log units apart average to a number neither of them measured, and a chemist told only
    the average cannot tell that from two labs that agree.

    Not a tool payload — `report_measurement` renders one sentence out of it — so `spread` is a
    plain property rather than a `computed_field`: nothing serializes this model, and the rule that
    made `FingerprintSearch.verdict` a `computed_field` is about a payload that leaves the process.
    """

    # `property_name` rather than `property`: the field would shadow the builtin decorator inside
    # this class body, which is a `"str" not callable` error on `spread` below rather than a
    # readability preference.
    property_name: str
    value: float
    lowest: float
    highest: float
    # One row per source, so this counts reporters rather than reports: a lab that measured three
    # times under one source name contributes one.
    sources: int
    # How many distinct units those rows carry. `1` in every shipped path, because
    # `report_measurement` reconciles a calibrated value into the ledger's own unit before writing
    # — carried rather than assumed, because a mean over two units is a number in neither.
    units: int
    reported_by: str

    @property
    def spread(self) -> float:
        """How far the extreme reporters are apart, in the ledger's unit. Zero for one source."""
        return self.highest - self.lowest


class Residual(BaseModel):
    """One reconciled prediction: what was predicted, what was measured, and the gap.

    The aggregate is what a calculator does *on average*; this is where it went wrong. Trust in
    practice is not a single number — "the solubility model runs 0.4 log units low" is useful, and
    "and it is 2 log units low on every carboxylic acid we have measured" is a different and more
    actionable statement about the same six aggregates.

    `error` is signed (predicted − observed), matching `Calibration.bias`, because the direction is
    half the information: consistently high is correctable, scattered is not.
    """

    subject: str
    predicted: float
    observed: float
    error: float
    uncertainty: float | None = None

    @property
    def within_uncertainty(self) -> bool | None:
        """Whether the measurement fell inside the stated ±1σ. `None` when none was claimed.

        **±1σ, so `False` on about a third of well-calibrated predictions.** The stated
        uncertainties are 1σ RMSEs, and a residual outside one of them is the ordinary case rather
        than a miss worth explaining — see the module docstring's 0.683.
        """
        if self.uncertainty is None or self.uncertainty <= 0:
            return None
        return abs(self.error) <= self.uncertainty


class PredictionRecord(BaseModel):
    """One prediction to record, in the calculators' own terms."""

    calc_type: str = Field(min_length=1)
    calc_version: str = ""
    input_hash: str = Field(min_length=1)
    subject: str = ""
    predicted_value: float
    predicted_uncertainty: float | None = None
    unit: str = ""


async def record_prediction(record: PredictionRecord) -> None:
    """Log a prediction for later reconciliation. Best-effort: never fails the calculation.

    Idempotent by `(calc_type, calc_version, input_hash)`: re-predicting the same thing updates the
    row rather than accumulating duplicates, which would silently double-weight that input in the
    calibration.
    """
    if not settings.calibration_enabled:
        return
    try:
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _UPSERT_PREDICTION,
                    (
                        record.calc_type,
                        record.calc_version,
                        record.input_hash,
                        record.subject,
                        record.predicted_value,
                        record.predicted_uncertainty,
                        record.unit,
                    ),
                )
                # A measurement may already be on file — new chemistry is routinely measured before
                # it is predicted — so scoring the prediction it was just written for happens here
                # rather than waiting for a measurement that has already arrived.
                await cur.execute(
                    _RECONCILE_FROM_MEASUREMENT,
                    (record.calc_type, record.input_hash, record.calc_type, record.input_hash),
                )
            await conn.commit()
    except Exception:
        # A calibration ledger is advice *about* predictions; losing a row must never cost the
        # prediction itself. Logged, not raised.
        logger.warning(
            "could not record prediction %s/%s", record.calc_type, record.input_hash, exc_info=True
        )


async def record_observation(
    calc_type: str,
    input_hash: str,
    observed_value: float,
    source: str,
    *,
    subject: str = "",
    unit: str = "",
) -> int | None:
    """Store a measured value, reconcile any matching predictions, and return how many it scored.

    **A measurement is identified by who reported it** (`infra/sql/093`). Two sources measuring one
    property of one molecule — a replicate, a second solvent system, a second site — are two facts,
    and the predictions they score are updated to their *consensus* (the mean over sources) rather
    than to whichever arrived last. Before this, `measurements` was keyed on
    `(property, input_hash)` and the second write deleted the first: measured on one prediction of
    -0.30 log S, `lab-basel`'s -0.10 reported a bias of -0.200 and `lab-shanghai`'s -0.95 then
    reported +0.650 — a sign flip on the arrival of an equally valid number, at `n=1` both times,
    with nothing logged and no counter moved.

    A second value under the **same** source still replaces, because that is one reporter revising
    one number — the case `030_measurements.sql` argued for, kept where it is true. `source` is
    therefore load-bearing at the caller: `report_measurement` defaults it to `chemist-reported`,
    so two chemists who do not name their labs still collapse into one row, and the reply says so.

    **The measurement is kept either way**, which it was not before (DARK-9). This was a bare
    `UPDATE` against `predictions`, so a value for a molecule nothing had predicted matched no row
    and was discarded — while `report_measurement` told the chemist it had been "recorded". That is
    the *common* case, not an edge one: new chemistry is measured before anyone thinks to predict
    it, so the ledger could only ever learn from molecules the agent happened to guess at first.

    Zero is still a normal and informative return, and still means "nothing had predicted this" —
    it no longer means "and so it is gone". A later prediction of the same thing reconciles against
    the stored measurement on write, so the measure-then-predict order works as well as the
    reverse.

    **`None` is not zero, and the difference is the whole contract.** Zero means the value was
    stored and nothing had predicted it; `None` means it was not stored at all, because the ledger
    is disabled. Collapsing the two is what let `report_measurement` tell a chemist their
    measurement was "kept" while `calibration_enabled` was False — which is the **default**, so the
    tool said it every time.

    **This one does not swallow, unlike `record_prediction` above.** That asymmetry is deliberate.
    A prediction row is advice *about* work that already happened, so losing it must never cost the
    calculation — logging and continuing is right there. A measurement is the entire deliverable of
    the call: there is no primary result to protect, and swallowing turns the tool's only job into
    a false claim of success (D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed). A write
    failure raises, and the caller says so.

    Returns:
        How many predictions the measurement reconciled, or `None` if the ledger is disabled and
        nothing was stored.

    Raises:
        Exception: whatever the database raises. The connector's error sanitizer turns it into a
            caller-safe message; what matters is that it is not reported as a success.
    """
    if not settings.calibration_enabled:
        return None
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _UPSERT_MEASUREMENT,
                (calc_type, input_hash, subject or input_hash, observed_value, unit, source),
            )
            await cur.execute(_RECORD_OBSERVATION, (calc_type, input_hash, calc_type, input_hash))
            matched = cur.rowcount
        await conn.commit()
    return int(matched)


async def consensus_for(property_name: str, input_hash: str) -> ObservedConsensus | None:
    """What every source has measured for one property of one molecule, or `None` for nothing.

    The same `_CONSENSUS` fragment the two reconciliations write from, so the value a chemist is
    told their prediction is scored against and the value actually written are the same number by
    construction. `None` means no measurement is on file — not a failure, and not a zero.

    Raises whatever the database raises, for `reconciled_for`'s reason: the caller's whole
    deliverable is this read, so an unreachable database must not answer "nothing was measured".
    """
    if not settings.calibration_enabled:
        return None
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_CONSENSUS, (property_name, input_hash))
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    value, lowest, highest, sources, units, _observed_at, reported_by = row
    return ObservedConsensus(
        property_name=property_name,
        value=float(value),
        lowest=float(lowest),
        highest=float(highest),
        sources=int(sources),
        units=int(units),
        reported_by=reported_by or "",
    )


def summarize(
    calc_type: str,
    pairs: list[tuple[float, float | None, float]],
    *,
    unit: str = "",
    enabled: bool = True,
) -> Calibration:
    """Compute the calibration figures from `(predicted, uncertainty, observed)` triples.

    Pure, so the statistics are testable without a database — the same split the eval harness uses.
    `enabled` is carried through rather than read from config here for the same reason: it is the
    caller's fact about the ledger, and this function must stay a function of its arguments.
    """
    if not pairs:
        return Calibration(calc_type=calc_type, n=0, enabled=enabled, unit=unit)
    errors = [predicted - observed for predicted, _sigma, observed in pairs]
    # Only predictions that actually claimed an uncertainty can be scored for coverage; a
    # calculator that reports none is not "never covered", it made no claim to check.
    with_sigma: list[tuple[float, float, float]] = [
        (predicted, sigma, observed)
        for predicted, sigma, observed in pairs
        if sigma is not None and sigma > 0
    ]
    coverage: float | None = None
    if with_sigma:
        inside = sum(1 for p_, s_, o_ in with_sigma if abs(p_ - o_) <= s_)
        coverage = inside / len(with_sigma)
    n = len(errors)
    return Calibration(
        calc_type=calc_type,
        n=n,
        enabled=enabled,
        bias=sum(errors) / n,
        mean_absolute_error=sum(abs(e) for e in errors) / n,
        rmse=(sum(e * e for e in errors) / n) ** 0.5,
        uncertainty_coverage=coverage,
        unit=unit,
    )


async def reconciled_for(calc_type: str, calc_version: str) -> list[Residual]:
    """Every prediction of this calculator version that a measurement has since answered.

    The one read of the ledger, so the aggregate and the per-molecule listing can never disagree
    about which rows they describe — they are the same rows, summarized or not.

    Unbounded on purpose. The filter is `observed_value IS NOT NULL`, and an observation is a
    measurement somebody made and typed in; the table's growth is bounded by bench work, not by
    how often the calculator runs. A cap here would silently drop measurements from the
    calibration, which is worse than the read it would protect.

    **A read failure raises**, where it used to be logged and answered as `[]`. The module's
    best-effort rule protects a *calculation* from a ledger fault — but nothing calls this during
    a calculation. Its only callers are `calculator_trust` and `calculator_outliers`, whose entire
    deliverable is this read, so there was no primary result the swallow was protecting: an
    unreachable database returned an empty residual list, which the summary then rendered as
    bias/MAE/RMSE 0.0 — a calculator that has never missed. That is the read half of
    D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed, whose write half was fixed in
    `record_observation` for the identical reason.

    Raises:
        Exception: whatever the database raises. The connector's error sanitizer turns it into a
            caller-safe message; what matters is that it is not reported as a clean ledger.
    """
    if not settings.calibration_enabled:
        return []
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_RECONCILED, (calc_type, calc_version))
            rows = await cur.fetchall()
    return [
        Residual(
            subject=subject,
            predicted=predicted,
            observed=observed,
            error=predicted - observed,
            uncertainty=sigma,
        )
        for subject, predicted, sigma, observed in rows
    ]


async def calibration_for(calc_type: str, calc_version: str, *, unit: str = "") -> Calibration:
    """Read the reconciled rows for one calculator *version* and summarize them.

    `calc_version` is required rather than defaulted: a default would silently reproduce the pooled
    reading this exists to remove, and every caller already knows which version answered.

    Raises whatever `reconciled_for` raises — see its note on why a failed read is not an empty one.
    """
    residuals = await reconciled_for(calc_type, calc_version)
    return summarize(
        calc_type,
        [(r.predicted, r.uncertainty, r.observed) for r in residuals],
        unit=unit,
        enabled=settings.calibration_enabled,
    )
