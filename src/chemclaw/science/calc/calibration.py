"""Close the loop between what was predicted and what actually happened.

Predictions keyed on `(calc_type, calc_version, input_hash)` (`input_hash` is over the canonical
SMILES, not the cache's molecule hash) are reconciled against measurements as they arrive. Figures
are bias, MAE and ±1σ coverage, whose target is 0.683, not 1.0. Recording a prediction is
best-effort; recording a measurement and reading the ledger raise on failure. The ledger is off by
default, and `Calibration.verdict` says so.
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

# The measurement itself, kept whether or not anything predicted it, and written before the
# reconciliation so it survives. Keyed by source too: a replicate or second site is a second
# fact, while a second value from the same source replaces (a correction).
_UPSERT_MEASUREMENT = """
INSERT INTO measurements (property, input_hash, subject, value, unit, source, observed_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (property, input_hash, source) DO UPDATE SET
    value = EXCLUDED.value,
    unit = EXCLUDED.unit,
    observed_at = now()
"""

# All measurements for one property of one molecule as one row: the consensus (mean over
# sources), spread and sources. One fragment for every reader, so scored and quoted values agree.
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

# The reverse direction: a prediction made *after* a measurement reconciles against it at once.
_RECONCILE_FROM_MEASUREMENT = f"""
UPDATE predictions p
   SET observed_value = c.value, observed_at = c.observed_at, observed_source = c.reported_by
  FROM ({_CONSENSUS}) AS c
 WHERE p.calc_type = %s
   AND p.input_hash = %s
   AND p.observed_value IS NULL
   AND c.value IS NOT NULL
"""

# Not scoped by version: a measurement is a fact about the molecule and scores every version's
# prediction. Writes the consensus, not the latest value; `c.value IS NOT NULL` guards the
# empty aggregate, which would otherwise blank an observed value.
_RECORD_OBSERVATION = f"""
UPDATE predictions p
   SET observed_value = c.value, observed_at = c.observed_at, observed_source = c.reported_by
  FROM ({_CONSENSUS}) AS c
 WHERE p.calc_type = %s
   AND p.input_hash = %s
   AND c.value IS NOT NULL
"""

# Scoped to one calculator version: pooling versions could average opposite biases into apparent
# good calibration.
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
    # Whether the ledger is recording at all (`calibration_enabled` defaults to False), so "nothing
    # recorded" is distinguishable from "nothing missed".
    enabled: bool = True
    bias: float | None = None
    mean_absolute_error: float | None = None
    rmse: float | None = None
    # Fraction of observations inside the prediction's stated ±1σ. `None` when no prediction carried
    # an uncertainty ("never claimed", not "never covered"). The target is 0.683, not 1.0.
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

        A `computed_field` so it is serialized. A database outage is not a state here:
        `reconciled_for` raises.
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

    # `property_name` because `property` would shadow the builtin decorator in this class body.
    property_name: str
    value: float
    lowest: float
    highest: float
    # One row per source, so this counts reporters rather than reports: a lab that measured three
    # times under one source name contributes one.
    sources: int
    # Distinct units among those rows: `1` in every shipped path, carried rather than assumed.
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

        `False` on about a third of well-calibrated predictions (see the module's 0.683).
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

    Idempotent by `(calc_type, calc_version, input_hash)`, so a repeated prediction does not
    double-weight its input.
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
                # A measurement may already be on file, so score the new prediction against it now.
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

    Values from different sources are kept separately and predictions are scored against their
    consensus; a second value from one source replaces the first. The measurement is stored even if
    nothing predicted it. Raises on failure, since storing is the call's whole deliverable.

    Returns:
        How many predictions were reconciled, or `None` if the ledger is disabled and nothing was
        stored.
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

    Reads the same `_CONSENSUS` fragment the reconciliations write from. Raises on database failure,
    since this read is the caller's whole deliverable.
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

    Pure, so the statistics are testable without a database; `enabled` is the caller's fact about
    the ledger.
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

    The one read of the ledger; unbounded because growth is bounded by bench work. Raises on
    failure, since `[]` would read as a calculator that never missed.
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

    `calc_version` is required: a default would reintroduce pooled readings. Raises whatever
    `reconciled_for` raises.
    """
    residuals = await reconciled_for(calc_type, calc_version)
    return summarize(
        calc_type,
        [(r.predicted, r.uncertainty, r.observed) for r in residuals],
        unit=unit,
        enabled=settings.calibration_enabled,
    )
