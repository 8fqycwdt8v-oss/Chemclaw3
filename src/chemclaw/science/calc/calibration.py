"""Close the loop between what was predicted and what actually happened (gap IDEA-2).

The stack predicts (xTB, pKa, solubility, BO surrogates) and, separately, ingests what actually
happened (the ELN). Nothing connected the two. `evals/metrics.py::prediction_error` exists but
scores against a *held-out reference in a committed case file* — not against reality as it arrives —
so "how far should I trust this calculator?", which is the entire job of the `calculation-selection`
skill, was answerable only in prose.

This is the ledger that makes it answerable in numbers: every prediction is recorded against the
same `(calc_type, input_hash)` identity the calculation cache already keys on, so a later
measurement of the same thing meets it without a second naming scheme.

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

**Deliberately advisory.** Nothing here changes a prediction. Recording is best-effort and a
calibration failure never fails a calculation — a broken ledger must degrade the *advice about*
predictions, never the predictions themselves.
"""

import logging

from pydantic import BaseModel, Field

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

_RECORD_OBSERVATION = """
UPDATE predictions
   SET observed_value = %s, observed_at = now(), observed_source = %s
 WHERE calc_type = %s AND input_hash = %s
"""

_SELECT_RECONCILED = """
SELECT predicted_value, predicted_uncertainty, observed_value
  FROM predictions
 WHERE calc_type = %s AND observed_value IS NOT NULL
"""


class Calibration(BaseModel):
    """How well one calculator's predictions have matched reality so far.

    `n` is reported alongside every figure because a bias computed from three points is not a bias;
    a surface that shows the number without the count invites exactly that mistake.
    """

    calc_type: str
    n: int
    bias: float = 0.0
    mean_absolute_error: float = 0.0
    rmse: float = 0.0
    # Fraction of observations that fell inside the prediction's stated ±1σ interval. `None` when
    # no prediction carried an uncertainty — deliberately not 0.0, which would read as "never
    # covered" rather than "never claimed".
    uncertainty_coverage: float | None = None
    unit: str = ""

    @property
    def is_meaningful(self) -> bool:
        """Whether enough observations exist for the figures to mean anything."""
        return self.n >= settings.calibration_min_observations


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
        async with db.connection(
            settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
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
            await conn.commit()
    except Exception:
        # A calibration ledger is advice *about* predictions; losing a row must never cost the
        # prediction itself. Logged, not raised.
        logger.warning(
            "could not record prediction %s/%s", record.calc_type, record.input_hash, exc_info=True
        )


async def record_observation(
    calc_type: str, input_hash: str, observed_value: float, source: str
) -> int:
    """Attach a measured value to any matching prediction; return how many rows it reconciled.

    Zero is a normal and informative answer: it means nothing predicted this yet, which is exactly
    the case where a measurement is *most* worth having and least worth silently discarding — the
    caller logs it rather than this pretending success.
    """
    if not settings.calibration_enabled:
        return 0
    try:
        async with db.connection(
            settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RECORD_OBSERVATION, (observed_value, source, calc_type, input_hash)
                )
                matched = cur.rowcount
            await conn.commit()
        return int(matched)
    except Exception:
        logger.warning("could not record observation for %s", calc_type, exc_info=True)
        return 0


def summarize(
    calc_type: str,
    pairs: list[tuple[float, float | None, float]],
    *,
    unit: str = "",
) -> Calibration:
    """Compute the calibration figures from `(predicted, uncertainty, observed)` triples.

    Pure, so the statistics are testable without a database — the same split the eval harness uses.
    """
    if not pairs:
        return Calibration(calc_type=calc_type, n=0, unit=unit)
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
        bias=sum(errors) / n,
        mean_absolute_error=sum(abs(e) for e in errors) / n,
        rmse=(sum(e * e for e in errors) / n) ** 0.5,
        uncertainty_coverage=coverage,
        unit=unit,
    )


async def calibration_for(calc_type: str, *, unit: str = "") -> Calibration:
    """Read the reconciled rows for one calculator and summarize them."""
    if not settings.calibration_enabled:
        return Calibration(calc_type=calc_type, n=0, unit=unit)
    try:
        async with db.connection(
            settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_RECONCILED, (calc_type,))
                rows = await cur.fetchall()
    except Exception:
        logger.warning("could not read calibration for %s", calc_type, exc_info=True)
        return Calibration(calc_type=calc_type, n=0, unit=unit)
    return summarize(calc_type, [(row[0], row[1], row[2]) for row in rows], unit=unit)
