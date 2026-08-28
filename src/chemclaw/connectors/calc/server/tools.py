"""The `calc` connector's MCP tool surface: cache, compose, and read the ledger.

Fifteen tools, and after `D-2026-08-16-the-physics-leaves-the-cache-stays` not one of them computes
anything. The physics is in `Chemclaw3-mcp`'s `servers/calc`, exposed as individually-keyed
primitives; what happens here is the three things that stayed:

- **The D-011 cache.** Every compute tool goes through `connectors/calc/remote.py::cached_remote` —
  ask the server for the key, look it up, cross the wire only on a miss. A persisted result is still
  never recomputed; the miss path just got longer.
- **Composition.** `compute_thermochemistry` and `predict_logd` are not shipped by the server at
  all, because their keys would name an output. They are assembled here from parts that *are* keyed
  (`connectors/calc/compose.py`), which is what keeps their warm path warm.
- **The calibration ledger and the store's read side.** `report_measurement`, `calculator_trust`,
  `calculator_outliers`, `find_calculations`, `list_artifacts`, `fetch_artifact` — none of which the
  server can answer, because it holds no state at all.

`default_store` names the production backend and is the seam tests swap for an in-memory store.

**The agent-facing surface did not move.** Every signature, docstring and return type below is what
it was before the split, because profiles, eval probes and skills name these tools by string. What
changed is one layer down.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, computed_field
from rdkit import Chem

from chemclaw.connectors.calc import compose
from chemclaw.connectors.calc.remote import cached_remote, remote_version
from chemclaw.core.chem import canonical_smiles, require_canonical_smiles, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.calibration import (
    Calibration,
    PredictionRecord,
    Residual,
    calibration_for,
    reconciled_for,
    record_observation,
    record_prediction,
)
from chemclaw.science.calc.geometry import without_geometry
from chemclaw.science.calc.logd import logd_from_pka
from chemclaw.science.calc.models import (
    AtomicDescriptorResult,
    DescriptorProfile,
    ElectronicProperties,
    FukuiMode,
    LogdResult,
    OptimizationSummary,
    PkaResult,
    SiteReactivityResult,
    SolubilityResult,
    Structure,
    SurfacePotentialResult,
    ThermochemistryResult,
    XtbResult,
)
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.postgres_structures import default_structure_store
from chemclaw.science.calc.store import CalculationQuery, ResultPayload, ResultStore, StoredResult
from chemclaw.science.calc.structures import require_structure
from chemclaw.science.calc.thermo import ThermoSettings

server = FastMCP("calc")

logger = logging.getLogger(__name__)


def default_store() -> ResultStore:
    """Return the production result store (Postgres). Overridden in tests."""
    return PostgresStore()


def _version_of(payload: ResultPayload, tool: str) -> str:
    """The `calc_version` a result was computed under, read off the payload rather than derived.

    Every result the server returns is stamped with its own version, and a stored row keeps that
    stamp — so on a cache hit this is still the version that produced the number, not the version
    that is current. That is exactly what the calibration ledger wants: `predictions` records what
    was predicted *by what*, and a row logged under today's version for a value computed under
    yesterday's would put two incomparable calculators into one bias figure (REV-12).

    Raises rather than defaulting, because the failure it would otherwise cause is silent: an empty
    or missing version degenerates the ledger's unique index `(calc_type, calc_version, input_hash)`
    to `(calc_type, input_hash)`, and one calculator version's prediction quietly overwrites
    another's.
    """
    version = payload.get("calc_version")
    if not isinstance(version, str) or not version:
        raise ValueError(
            f"{tool} returned a result with no calc_version, so its prediction cannot be logged "
            "against a calibration ledger keyed on one"
        )
    return version


async def _log_prediction(
    calc_type: str,
    calc_version: str,
    smiles: str,
    value: float,
    uncertainty: float | None,
    unit: str,
) -> None:
    """Record a prediction for later reconciliation against a measurement (gap IDEA-2).

    Hooked at the *tool* layer rather than inside the calculators, because this is the boundary
    where a prediction becomes advice a chemist acts on — a cache hit deep in a workflow does not
    need re-logging, and the ledger is keyed on the input, not on how often it was read.

    The subject key is the canonical SMILES, the same identity the calculation cache uses, so a
    measurement of the same molecule meets its prediction without a second naming scheme.
    """
    canonical = canonical_smiles(smiles)
    await record_prediction(
        PredictionRecord(
            calc_type=calc_type,
            # Without this the unique index `(calc_type, calc_version, input_hash)` degenerated to
            # `(calc_type, input_hash)`, because every row carried the default `""` — so upgrading a
            # calculator silently overwrote the previous version's prediction and `calculator_trust`
            # reported a bias averaged across versions that were never comparable (REV-12).
            calc_version=calc_version,
            input_hash=stable_hash(canonical),
            subject=canonical,
            predicted_value=value,
            predicted_uncertainty=uncertainty,
            unit=unit,
        )
    )


@server.tool()
async def report_measurement(property_name: str, smiles: str, measured_value: float) -> str:
    """Record a *measured* property value, so predictions can be scored against reality.

    Call this when a chemist reports an experimental measurement for a property the system also
    predicts (`solubility` as log S, or `pka`). It closes the prediction loop: `calculator_trust`
    then reports how far that calculator has actually been off, instead of the agent having to
    reason about trust from prose.

    Args:
        property_name: Which predicted property was measured — "solubility" or "pka".
        smiles: The molecule measured, as SMILES.
        measured_value: The experimental value, in the property's own unit (log S, or pKa).

    Returns:
        Whether the measurement matched an existing prediction. "No prediction on file" is a normal
        answer — say so rather than implying the measurement was scored. If the reply says the
        measurement was **not** recorded, report exactly that: it was not kept, and repeating the
        call will not help.
    """
    canonical = canonical_smiles(smiles)
    matched = await record_observation(
        property_name,
        stable_hash(canonical),
        measured_value,
        source="chemist-reported",
        subject=canonical,
    )
    if matched is None:
        # Not a failure to report as an error — the deployment turned the ledger off on purpose —
        # but emphatically not "Recorded" either. `calibration_enabled` is False by *default*, so
        # this was the answer every unconfigured deployment gave while storing nothing at all.
        return (
            f"NOT recorded. The calibration ledger is disabled in this deployment, so the "
            f"measurement for {canonical} was not stored and nothing will be scored against it. "
            "Tell the chemist the value was not kept, and that an operator must enable "
            "`calibration_enabled` before measurements can be reported."
        )
    if matched:
        return f"Recorded; it reconciled {matched} prediction(s) for {canonical}."
    # This branch used to say "Recorded" and be wrong: the write was a bare UPDATE against
    # `predictions`, so a measurement nothing had predicted matched no row and was discarded
    # (DARK-9). It is now stored on its own, and the next prediction of the same thing scores
    # against it — which is worth saying, because it is the reason reporting it was not wasted.
    return (
        f"Recorded for {canonical}. Nothing had predicted {property_name} for it yet, so no "
        "prediction was scored — the measurement is kept and the next prediction of it will be "
        "scored against this value."
    )


class CalculationRecord(BaseModel):
    """One stored calculation, flattened to what an agent can reason about.

    Not `StoredResult` itself: that carries a `CalculationKey` whose two hashes mean nothing to a
    reader and would spend the model's attention on them. `calc_ref` is the same key in the flat
    form a knowledge note cites (`type@version:hash:hash`), so a result found here can be quoted
    as evidence by the reference that resolves back to it.

    **`result` is bounded, and `result_omitted` says when the bound bit.** This model was the
    largest unbounded model-facing payload in the system and nothing said so: a stored
    `xtb.conformers` row holds *every* member the search found, untruncated — measured at 66,520
    characters for one 40-atom molecule — and `calc_find_max_results` is 50. That is ~830,000
    tokens from one read-only call on two agent profiles, which is not a degraded answer but a
    hard context-limit failure (`agent/compaction.py`). Geometries now project to their addresses
    (D-2026-08-21) and what is still oversized after that is withheld rather than silently cut,
    because a truncated JSON payload that still parses reads as a complete one — the same rule
    `ArtifactContent.truncated` and the substructure scan's verdict already follow.
    """

    calc_ref: str
    calc_type: str
    calc_version: str
    result: dict[str, Any]
    # True when `result` is empty because the stored payload was over `calc_find_max_result_chars`,
    # as opposed to a calculation that genuinely stored nothing. Ask for that one calculation
    # directly to see it.
    result_omitted: bool = False
    provenance: str
    computed_at: datetime | None = None
    compute_seconds: float | None = None


@server.tool()
async def find_calculations(
    smiles: str = "",
    structure_id: str = "",
    calc_type: str = "",
    calc_version: str = "",
    since: str = "",
    until: str = "",
    limit: int = 20,
) -> list[CalculationRecord]:
    """Look up calculations this system has already run, instead of running them again.

    Every calculation ever computed is kept forever and keyed by (calculator, version, input,
    parameters) — including the expensive DFT jobs — but until now the only way to reach one was
    to ask for the exact same calculation and get a cache hit. This is the other question: *what
    do we already know about this molecule*, which is what a chemist actually asks before
    committing hours of compute.

    Use it before submitting anything expensive, and to answer "have we looked at this before?".
    An empty result is a real answer — say the store has nothing rather than implying the
    calculation was tried and failed.

    A returned `calc_ref` can be cited directly in a knowledge note, so an answer built on a
    stored value stays traceable to the run that produced it.

    Args:
        smiles: Restrict to one molecule (any valid SMILES for it — matching is on the canonical
            form, so "CCO" and "OCC" find the same rows). Empty means every molecule. This reaches
            the molecule-keyed calculators — pka, solubility, descriptors, dft. The xTB task
            results and geometry pointers are keyed by a 3-D structure, which a molecule alone
            does not determine, so address those with `structure_id`; asking for a molecule and a
            structure-keyed `calc_type` together is refused rather than answered with a
            misleading empty list.
        structure_id: Restrict to calculations that *ran on* one specific geometry, as the
            `st_...` address reported by `optimize_geometry`, `sample_conformers`,
            `scan_coordinate` or `compute_thermochemistry`. This is the question "what do we
            already know about *this conformer*" — the relaxation started from it, its properties,
            its Hessian — which a molecule cannot ask, because a molecule does not determine a
            geometry. It matches the calculation's input, so a relaxation is found by the geometry
            it started from rather than by the minimum it reached. Empty means every geometry.
        calc_type: Restrict to one kind of calculation, e.g. "xtb", "pka", "dft". Empty means all.
        calc_version: Restrict to one calculator version. Empty means every version — useful
            precisely when asking whether an older version's number is still what is on file.
        since: ISO-8601 date or timestamp; only results computed at or after it.
        until: ISO-8601 date or timestamp; only results computed at or before it.
        limit: How many to return, newest first.

    Returns:
        The matching calculations, newest first. Each carries the result payload the calculator
        produced, so no second call is needed to read a value.
    """
    query = CalculationQuery(
        smiles=smiles or None,
        structure_id=structure_id or None,
        calc_type=calc_type or None,
        calc_version=calc_version or None,
        since=_timestamp(since),
        until=_timestamp(until),
        limit=max(1, min(limit, settings.calc_find_max_results)),
    )
    return [_record(stored) for stored in await default_store().find(query)]


def _timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 date or timestamp, or None for an empty string.

    A malformed date raises rather than being dropped: silently ignoring "last Tuesday" would
    answer a question about a window with results from outside it, which reads as an authoritative
    "nothing else exists" — the failure mode this tool is least able to afford.
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def _record(stored: StoredResult) -> CalculationRecord:
    """Flatten one stored result into the agent-facing record, bounded.

    Two reductions, in the order that matters. Geometries become addresses first, because that is
    lossless for a reader — a `structure_id` is what the *next* calculation takes, where the
    coordinates were something no tool accepted — and on the shapes that motivated the bound it is
    most of the reduction. Only what is still over the ceiling afterwards is dropped, whole, with
    `result_omitted` set.

    The ceiling is measured on the rendered JSON rather than on the parsed object, because
    characters are what the bound is about: this payload is going into a model's context.
    """
    projected = without_geometry(stored.result)
    rendered = len(json.dumps(projected, default=str))
    omitted = rendered > settings.calc_find_max_result_chars
    if omitted:
        logger.info(
            "%s renders %d characters, over the %d-character listing budget; its result is "
            "reported as omitted",
            stored.key.as_str(),
            rendered,
            settings.calc_find_max_result_chars,
        )
    return CalculationRecord(
        calc_ref=stored.key.as_str(),
        calc_type=stored.key.calc_type,
        calc_version=stored.key.calc_version,
        result={} if omitted else projected,
        result_omitted=omitted,
        provenance=stored.provenance,
        computed_at=stored.created_at,
        compute_seconds=stored.compute_seconds,
    )


class StoredArtifact(BaseModel):
    """One by-product a calculation left behind, described without being read.

    `artifact_ref` is the same `<calculation key>#<name>` string a knowledge note's `artifact_refs`
    cites, so a listing and a citation name the same thing and `fetch_artifact` takes either.
    """

    artifact_ref: str
    name: str
    media_type: str
    byte_size: int


class ArtifactContent(BaseModel):
    """One artifact's contents, as text, bounded.

    `byte_size` is the artifact's *full* size and `truncated` says whether `text` is all of it —
    together they are what keeps a partial read from being quoted as a complete one.
    """

    artifact_ref: str
    name: str
    media_type: str
    byte_size: int
    text: str
    truncated: bool


@server.tool()
async def list_artifacts(calc_ref: str) -> list[StoredArtifact]:
    """List the packed arrays a stored calculation left behind.

    A calculation's *answer* is a small set of numbers, and `find_calculations` returns it. This is
    the bulk data a run produced and the system kept — today that means the second derivatives a
    Hessian computed, held out of the result row because they are megabytes.

    **Not geometries.** A computed structure is addressed by its `structure_id`, which every
    geometry calculation reports and which `optimize_geometry`, `compute_thermochemistry`,
    `compute_electronic_properties`, `scan_coordinate` and `sample_conformers` all *take* — that is
    how a conformer is carried from one calculation into the next, and it needs no file. This tool
    predates that and used to be the only route to one.

    An empty list is a real answer and usually the right one: most calculations produce no
    by-products worth keeping. It does not mean the calculation is missing — ask
    `find_calculations` for that.

    Args:
        calc_ref: A calculation key as `find_calculations` returns it, or as a note cites it
            (`calc_type@version:input_hash:params_hash`).

    Returns:
        One entry per stored by-product, with its size and type, ordered by name. Read one with
        `fetch_artifact`; check `byte_size` first, because a Hessian is megabytes and is meant to
        seed another calculation rather than to be read.
    """
    refs = await default_artifact_store().list_for(calc_ref)
    return [
        StoredArtifact(
            artifact_ref=ref.as_str(),
            name=ref.name,
            media_type=ref.media_type,
            byte_size=ref.byte_size,
        )
        for ref in refs
    ]


@server.tool()
async def fetch_artifact(artifact_ref: str, max_chars: int = 0) -> ArtifactContent:
    """Read a stored calculation by-product a knowledge note cites in its `artifact_refs`.

    It refuses a binary artifact (a packed `.npy` array, an SCF restart) instead of returning
    something unreadable — those exist to seed a further calculation, not to be read — and it
    truncates at a configured ceiling, reporting `truncated` and the full `byte_size`, so a large
    file costs a bounded amount of context; if `truncated` is set, say the value came from part of
    the file.

    **In this release every artifact is one of those binary arrays, so this refuses more often
    than it answers.** The text by-products it was written for — an `xtbopt.xyz`, a `vibspectrum` —
    have no producer since the calculators moved to their own server. For a geometry, use the
    `structure_id` a calculation reports: it names the structure exactly and the next calculation
    takes it directly, which is what quoting coordinates was ever a substitute for.

    Args:
        artifact_ref: `<calculation key>#<name>`, as `list_artifacts` returns it and as a note's
            `artifact_refs` cites it.
        max_chars: Read at most this many characters. 0 uses the configured ceiling, which also
            caps any larger request.

    Returns:
        The artifact's text with its type and full size, and whether the text is all of it.
    """
    calc_key, separator, name = artifact_ref.rpartition("#")
    if not separator or not name:
        raise ValueError(
            f"{artifact_ref!r} is not an artifact reference "
            "(expected '<calculation key>#<name>', as list_artifacts returns)"
        )
    store = default_artifact_store()
    stored = {ref.name: ref for ref in await store.list_for(calc_key)}
    ref = stored.get(name)
    if ref is None:
        known = ", ".join(sorted(stored)) or "none"
        raise ValueError(
            f"no artifact {name!r} is stored for calculation {calc_key!r} "
            f"(stored under it: {known}). By-products are eviction-managed, so a reference from "
            "an older note may point at something that has since been reclaimed."
        )
    data = await store.open(ref.content_hash)
    if data is None:  # evicted between the listing and the read
        raise ValueError(f"artifact {artifact_ref!r} is no longer stored")
    try:
        # Decoding is the test, rather than a table of readable media types: the store accepts any
        # producer-given name and `media_type_for` falls back to opaque bytes for one it does not
        # know, so a type-based rule would refuse perfectly readable output from any tool added
        # later. What actually matters is whether the bytes are text, and this asks them.
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{artifact_ref!r} is binary ({ref.media_type}, {ref.byte_size} bytes), not text. "
            "It is stored to seed a further calculation, not to be read."
        ) from exc
    # `max(1, ...)` because a negative argument would otherwise slice from the *end*
    # (`text[:-5]` is all but the last five characters) and report itself as truncated — a
    # near-complete read dressed as a bounded one, which is the reading this tool least affords.
    limit = max(
        1, min(max_chars or settings.calc_artifact_max_chars, settings.calc_artifact_max_chars)
    )
    return ArtifactContent(
        artifact_ref=ref.as_str(),
        name=ref.name,
        media_type=ref.media_type,
        byte_size=ref.byte_size,
        text=text[:limit],
        truncated=len(text) > limit,
    )


# The calculators whose predictions are logged to the ledger, and how to read them back: the
# version that is current *now* and the unit its values are in.
#
# A table rather than a conditional. `calculator_trust` used to be
# `solubility = property_name == "solubility"` followed by two ternaries, so every name that was
# not "solubility" was answered as though it were pKa — an unknown property got a confident report
# about the wrong calculator, in the wrong unit, and the model had no way to tell. Adding a
# calibrated calculator is now one row, and asking about an uncalibrated one is an error that
# names what does exist.
_CALIBRATED: dict[str, tuple[str, str]] = {
    "solubility": ("predict_solubility", "log S"),
    "pka": ("predict_pka", "pKa"),
}


async def _calibrated(property_name: str) -> tuple[str, str]:
    """The current version and unit for a calibrated property, or raise naming the alternatives.

    **The version comes from the server, and that is the single most load-bearing line in this
    module.** It used to be derived here, from `xtb --version` and seven calibration settings this
    process can no longer see. `binary_version()` answered the literal string `"absent"` rather than
    raising when a binary was missing, so a locally-derived version would be *well-formed*, match
    **zero** rows in a ledger keyed exactly on `(calc_type, calc_version, input_hash)` (D-139, no
    pooling), and `calculator_trust("pka")` would report a confident `UNCALIBRATED` — the state that
    machinery exists to distinguish, reached by a route it never anticipated, with every historical
    residual unreachable at the same moment. Nothing would look broken.
    `tests/test_calc_remote.py` asserts statically that no derivation has crept back.

    The *current* version, not a pooled figure: the chemist is asking how far to trust the
    calculator that is about to answer them, and a v1 that ran high averaged with a v2 that ran low
    reads as well-calibrated while neither is (REV-12).
    """
    entry = _CALIBRATED.get(property_name)
    if entry is None:
        raise ValueError(
            f"{property_name!r} is not a calibrated property (known: "
            f"{', '.join(sorted(_CALIBRATED))}). Only calculators that log their predictions can "
            "be scored against measurements."
        )
    tool, unit = entry
    version = await remote_version(tool, {"smiles": settings.calc_version_probe_smiles})
    return version, unit


@server.tool()
async def calculator_trust(property_name: str) -> Calibration:
    """Report how far a calculator's predictions have actually been off, measured not asserted.

    Use this before leaning on a predicted value in an answer, and quote it: "the solubility model
    has run about 0.4 log units low over 18 measurements" is a far more useful caveat than a generic
    "predictions are uncertain".

    **Read `verdict` first**, then `n`. A disabled ledger, an empty one and too few points are all
    "the accuracy is unknown", never "the calculator is accurate" — and the figures are `None`
    rather than 0.0 in those states so a zero cannot be misread as a measurement.
    `uncertainty_coverage` is the subtle one: a low value means the stated error bars are too
    narrow, so the *uncertainty* is misleading even when the values look close.

    These are averages over every molecule measured. When the answer matters, follow up with
    `calculator_outliers`: a calculator can be well-behaved overall and badly wrong on one class of
    molecule, and an average cannot show that.

    Args:
        property_name: A calibrated property — "solubility" or "pka". Anything else is an error
            rather than a guess, and the message names what is available.

    Returns:
        Bias, mean absolute error, RMSE, and uncertainty coverage, with the observation count.
    """
    version, unit = await _calibrated(property_name)
    return await calibration_for(property_name, version, unit=unit)


class OutlierResidual(BaseModel):
    """One molecule where a calculator's prediction and the measurement disagreed."""

    smiles: str
    predicted: float
    observed: float
    # Signed (predicted − observed), matching the reported bias: direction is half the information.
    error: float
    unit: str
    # Whether the measurement fell inside the prediction's own stated ±1σ. `None` when the
    # calculator claimed no uncertainty — deliberately not `False`, which would read as a miss.
    within_uncertainty: bool | None = None


class OutlierReport(BaseModel):
    """The worst misses, **and whether the ledger was in a position to name any**.

    Why this is not a bare `list[OutlierResidual]`: an empty list meant three things a chemist must
    never see conflated — the ledger is disabled, nothing has been measured yet, and the filter
    matched nothing. `calibration_enabled` defaults to **False**, so the shipped deployment served
    the first of those as an empty list, and this tool's own docstring tells the model that a short
    list means few measurements. That is the same collapse `Calibration` carries a verdict for; the
    listing beside it was left without one.

    `measured` is the ledger's size *before* `matching` filtered it, which is what separates "no
    measurement exists" from "no measured molecule contains that fragment" — two answers a chemist
    testing a hypothesis about a class acts on very differently.
    """

    calc_type: str
    enabled: bool
    measured: int
    residuals: list[OutlierResidual] = Field(default_factory=list)
    matching: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before concluding anything from the length of this list.

        `computed_field` rather than a plain property for the reason `Calibration.verdict` and
        `FingerprintSearch.verdict` are: a bare property is not serialized, so the sentence would
        never reach the model that writes the answer.
        """
        if not self.enabled:
            return (
                "CALIBRATION NOT RECORDED: the prediction ledger is disabled, so no "
                f"{self.calc_type} prediction has ever been scored against a measurement. An "
                "empty list here is NOT evidence that the calculator has no outliers — nothing "
                "was measured. Say its accuracy is unknown and that an operator must enable the "
                "ledger."
            )
        if not self.measured:
            return (
                f"UNCALIBRATED: no measurement has been reconciled against a {self.calc_type} "
                "prediction of this calculator version, so there is nothing to be wrong about "
                "yet. Its accuracy is unknown, not good."
            )
        if not self.residuals:
            return (
                f"No measured molecule contains {self.matching!r}, so this class is untested — "
                f"the ledger holds {self.measured} measurement(s) of other molecules. This is NOT "
                "evidence that the calculator handles the class well."
            )
        return (
            f"{len(self.residuals)} of {self.measured} measured molecule(s), worst first. Every "
            "row is a measurement someone made, so a short list means few measurements, not a "
            "well-behaved calculator — check `calculator_trust`'s `n`."
        )


@server.tool()
async def calculator_outliers(
    property_name: str, matching: str = "", limit: int = 10
) -> OutlierReport:
    """Show where a calculator was most wrong, molecule by molecule — optionally on one class.

    `calculator_trust` answers "how far off is this calculator on average". This answers the
    question a chemist actually acts on: *on what*. A model that is 0.3 log units off overall may
    be fine on neutrals and two units low on every acid, and no aggregate can show that — the two
    populations average into one reassuring number.

    Read it in two passes. Call it with no filter to see the worst misses and look for what they
    have in common; then call it again with `matching` set to that class to test the idea against
    the whole ledger. If the filtered errors are much larger than the unfiltered ones, say so in
    the answer and treat a prediction for that class as weak evidence.

    **Read `verdict` before you read the length of `residuals`.** Every row is a real measurement
    someone made, so a short list means few measurements, not a well-behaved calculator — and an
    empty one may mean the ledger is switched off entirely.

    Args:
        property_name: A calibrated property — "solubility" or "pka".
        matching: Optional SMARTS or SMILES fragment; only molecules containing it are considered.
            Use it to test a hypothesis about a class ("C(=O)O" for carboxylic acids).
        limit: How many to return, largest absolute error first.

    Returns:
        The worst misses — each with what was predicted, what was measured, the signed error, and
        whether the calculator's own uncertainty covered it — with the ledger's state beside them.
    """
    version, unit = await _calibrated(property_name)
    measured = await reconciled_for(property_name, version)
    residuals = await _only_matching(measured, matching) if matching else measured
    worst = sorted(residuals, key=lambda r: abs(r.error), reverse=True)
    return OutlierReport(
        calc_type=property_name,
        enabled=settings.calibration_enabled,
        measured=len(measured),
        matching=matching,
        residuals=[
            OutlierResidual(
                smiles=r.subject,
                predicted=r.predicted,
                observed=r.observed,
                error=r.error,
                unit=unit,
                within_uncertainty=r.within_uncertainty,
            )
            for r in worst[: max(1, min(limit, settings.calc_outliers_max_results))]
        ],
    )


async def _only_matching(residuals: list[Residual], query: str) -> list[Residual]:
    """Keep the residuals whose subject contains `query`, matched off the event loop.

    Bounded by `substructure_match_timeout_seconds` for the same reason the fingerprint index's
    scan is: the ledger is small, but a short adversarial recursive SMARTS matches for minutes
    regardless of corpus size, and this coroutine shares its loop with every other request.
    """
    pattern = substructure_pattern(query)

    def _scan() -> list[Residual]:
        kept = []
        for residual in residuals:
            molecule = Chem.MolFromSmiles(residual.subject)
            # A subject that no longer parses is skipped rather than failing the listing: one bad
            # row must not hide every real outlier.
            if molecule is not None and molecule.HasSubstructMatch(pattern):
                kept.append(residual)
        return kept

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_scan), timeout=settings.substructure_match_timeout_seconds
        )
    except TimeoutError as exc:
        raise ValueError(
            f"substructure match for {query!r} exceeded "
            f"{settings.substructure_match_timeout_seconds}s; use a simpler fragment"
        ) from exc


async def _starting_geometry(smiles: str, structure_id: str) -> Structure:
    """The geometry a calculation starts from: a named one, or a fresh embedding.

    The tool-path twin of `connectors/calc/activities._subject`, and separate from it for the
    reason the two modules are separate at all — an activity resolves inside a durable retry and a
    tool resolves inside a turn. What they share is the rule, and the rule is short enough that one
    home would cost an import from `activities` into the MCP server for four lines.

    Refuses a handle for a different molecule, canonically compared, because a `structure_id`
    addresses a geometry rather than a compound: nothing about the id says which molecule it is of,
    while `smiles` is what the answer is reported and cited under.
    """
    if not structure_id:
        return await compose.embed(smiles)
    structure = await require_structure(default_structure_store(), structure_id)
    named = require_canonical_smiles(smiles)
    if structure.smiles is not None and require_canonical_smiles(structure.smiles) != named:
        raise ValueError(
            f"{structure_id!r} is a geometry of {structure.smiles!r}, not of {smiles!r}. "
            "A structure id addresses one 3D geometry; use one reported by a calculation on the "
            "molecule you are asking about."
        )
    return structure


@server.tool()
async def compute_xtb_energy(smiles: str, charge: int = 0) -> XtbResult:
    """Compute the GFN2-xTB total energy of a molecule (fast, semiempirical).

    Runs a quick GFN2-xTB single point — semiempirical is the ceiling, with no heavier
    method behind this tool. Results are cached, so
    repeating the same molecule and charge is free and returns instantly.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net molecular charge (0 = neutral).

    Returns:
        The method, charge, and total energy in Hartree.
    """
    payload, _ = await cached_remote(
        default_store(), "compute_xtb_energy", {"smiles": smiles, "charge": charge}
    )
    return XtbResult.model_validate(payload)


@server.tool()
async def predict_solubility(smiles: str) -> SolubilityResult:
    """Predict aqueous solubility (log S, mol/L) of a molecule, with uncertainty.

    Uses a fast property model; the result reports an uncertainty that you should
    pass on to the user rather than treating the value as exact. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted log solubility, its uncertainty, and the model used.
    """
    payload, _ = await cached_remote(default_store(), "predict_solubility", {"smiles": smiles})
    result = SolubilityResult.model_validate(payload)
    await _log_prediction(
        "solubility",
        _version_of(payload, "predict_solubility"),
        smiles,
        result.log_s_mol_per_l,
        result.uncertainty_log,
        "log S",
    )
    return result


@server.tool()
async def predict_pka(smiles: str) -> PkaResult:
    """Predict a molecule's pKa via GFN2-xTB — an acid site, or a base's conjugate acid.

    Two domains with different accuracy, and `site` on the result says which one ran.
    **Acids** (`site="acid"`): the most acidic O-H/S-H proton — carboxylic acids, phenols,
    alcohols, thiols — reported with ~1.6 units of uncertainty. **Bases** (`site="base"`),
    when there is no acidic proton: the pKa of the *conjugate acid* (pKaH), the number
    tabulated for amines, reported with +/-1.0. An acid site wins when a molecule has both.

    Base coverage is **aromatic and aryl nitrogen only** — pyridines, imidazoles, azoles,
    anilines. Aliphatic amines raise instead of returning a value, and that refusal is
    load-bearing rather than cautious: over 13 reference amines the method ranks them at
    Spearman -0.17, because a continuum solvent cannot represent the ammonium ion's hydrogen
    bonding to water. Report that the value is not predictable rather than substituting
    another tool's output. Cached.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted pKa, which site it describes, the protonation/deprotonation energy,
        and the uncertainty.
    """
    payload, _ = await cached_remote(default_store(), "predict_pka", {"smiles": smiles})
    result = PkaResult.model_validate(payload)
    await _log_prediction(
        "pka", _version_of(payload, "predict_pka"), smiles, result.pka, result.uncertainty, "pKa"
    )
    return result


@server.tool()
async def compute_electronic_properties(
    smiles: str, solvent: str | None = None, structure_id: str = ""
) -> ElectronicProperties:
    """Compute frontier orbitals, dipole, partial charges and bond orders (GFN2-xTB).

    One fast semiempirical calculation gives the HOMO and LUMO energies and their gap
    (eV), the dipole moment (Debye), Mulliken partial charges per atom, and Wiberg
    bond orders per bonded pair. Use it to compare the electronic character of related
    molecules — a smaller gap means a more easily excited/reactive π system, a larger
    dipole a more polar molecule, and the partial charges show where the electron
    density sits. These are semiempirical values on a force-field geometry: compare
    them across similar structures rather than quoting one as an absolute measurement.
    Cached, so repeats are free.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "toluene") for an ALPB
            solvated calculation; omit for gas phase.
        structure_id: A specific geometry to evaluate at, as the `st_...` address reported by
            `optimize_geometry`, `sample_conformers` or `scan_coordinate`. Empty describes a
            force-field geometry embedded from `smiles` — which is the default and is fine for
            comparing related molecules, and is the wrong answer when the question is about a
            particular conformer.

    Returns:
        The total energy, HOMO/LUMO/gap in eV, dipole in Debye, per-atom charges, Wiberg and free
        valences, and the bond orders. Atom indices match the heavy atoms of the canonical SMILES,
        with hydrogens following them — `describe_sites` on `chem` maps those indices to positions a
        chemist can read, and is free.
    """
    # Two routes to one answer, and which one runs is decided by whether a geometry was named.
    #
    # The SMILES route stays byte-identical rather than being folded into the other, and that is
    # deliberate: `compute_electronic_properties` keys on the geometry the *server* embeds, and
    # routing it through `compute_properties_at` would key on the geometry embedded here. The two
    # agree today — both are `structure_from_smiles(smiles, optimize=True)` for every molecule this
    # tool accepts — but "agree today" is not a property a cache may rest on, and forking it would
    # orphan every `xtb.properties` row on disk for no gain the named-geometry route does not
    # already deliver.
    if not structure_id:
        payload, _ = await cached_remote(
            default_store(),
            "compute_electronic_properties",
            {"smiles": smiles, "solvent": solvent},
        )
        return ElectronicProperties.model_validate(payload)
    structure = await _starting_geometry(smiles, structure_id)
    payload, _ = await cached_remote(
        default_store(),
        "compute_properties_at",
        {"structure": structure.model_dump(mode="json"), "solvent": solvent},
    )
    return ElectronicProperties.model_validate(payload)


@server.tool()
async def compute_atomic_descriptors(
    smiles: str, solvent: str | None = None
) -> AtomicDescriptorResult:
    """Per-atom polarisability, dispersion and multipole descriptors (GFN2-xTB, binary only).

    Answers what a partial charge cannot: which atom is **polarisable** — a soft, dispersion-driven
    or halogen-bonding site — and how anisotropic its own electron density is. For where the
    potential is most positive or negative, which is where a halogen's sigma-hole shows up, call
    `compute_surface_potential`: a second calculation with its own cost and its own cache entry.

    Unlike Fukui indices, nothing in this panel is normalised per molecule, so these values **do**
    compare between molecules. Use `describe_sites` on `chem` to report them by position.

    **Needs the `xtb` binary and refuses by name where a deployment has none.** It does not
    approximate: the in-process library exposes no atomic multipoles and no polarisability, so there
    is nothing to fall back to. Partial charges, bond orders, frontier orbitals and site rankings
    all come from `compute_electronic_properties` and `predict_site_reactivity`, neither of which
    needs it.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell.
        solvent: Optional ALPB implicit solvent name; omit for gas phase.

    Returns:
        One entry per atom, indexed as `compute_electronic_properties` and `predict_site_reactivity`
        index them for the same structure, so the three panels join. Atomic units throughout.
    """
    payload, _ = await cached_remote(
        default_store(),
        "compute_atomic_descriptors",
        {"smiles": smiles, "solvent": solvent},
        # Pinned to the `xtb` binary (see above), whose own timeout can run to 3600 s — see
        # `calc_atomic_timeout_seconds`'s docstring for why the client must wait at least that long.
        timeout_seconds=settings.calc_atomic_timeout_seconds,
    )
    return AtomicDescriptorResult.model_validate(payload)


@server.tool()
async def compute_surface_potential(
    smiles: str, solvent: str | None = None
) -> SurfacePotentialResult:
    """Where a molecule's electrostatic potential is most positive and most negative (GFN2-xTB).

    The **maximum** is where an electrophilic patch sits — an acidic hydrogen, or a heavy halogen's
    sigma-hole, which is what makes a halogen bond and which a partial charge cannot show at all.
    The **minimum** marks the most electron-rich patch: a lone pair, a pi face. Both in kcal/mol.

    Extrema over a grid, not a map: compare analogues with them (does the bromo congener still have
    a positive sigma-hole?), do not use them to locate a patch in space.

    **Needs the `xtb` binary and refuses by name where a deployment has none**, and it is a separate
    calculation from `compute_atomic_descriptors` costing its own single point — an `--esp` run
    cannot also produce the atomic multipoles. Cached, so repeats are free.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell.
        solvent: Optional ALPB implicit solvent name; omit for gas phase.

    Returns:
        The minimum and maximum potential in kcal/mol and the grid size they were taken over.
    """
    payload, _ = await cached_remote(
        default_store(),
        "compute_surface_potential",
        {"smiles": smiles, "solvent": solvent},
        # Pinned to the `xtb` binary (see above); see `calc_atomic_timeout_seconds`'s docstring.
        timeout_seconds=settings.calc_atomic_timeout_seconds,
    )
    return SurfacePotentialResult.model_validate(payload)


@server.tool()
async def predict_site_reactivity(
    smiles: str,
    mode: FukuiMode = "electrophilic",
    top_n: int = 0,
    structure_id: str = "",
) -> SiteReactivityResult:
    """Rank the atoms of a molecule by how susceptible they are to attack (GFN2-xTB).

    Answers regioselectivity questions — which position of a ring is substituted,
    which site is oxidized, where a nucleophile adds — using condensed Fukui indices
    from three fast semiempirical calculations. Choose `mode` by what attacks the
    molecule: "electrophilic" for attack by an electrophile (e.g. aromatic
    nitration/halogenation), "nucleophilic" for attack by a nucleophile (e.g. addition
    to a carbonyl), "radical" for radical chemistry.

    Read the ranking as a hypothesis, not a prediction of yield: it ranks sites
    *within* this molecule only (never between molecules), it describes electronic
    susceptibility alone — sterics, the specific reagent and the solvent are not in
    the model — and a heteroatom often tops the list because of its lone pair, so for
    a ring-substitution question compare the ring carbons with each other. Cached, and
    asking a second mode for the same molecule is free.

    **Call `describe_sites` (on `chem`) first and report the answer by position, never by
    index.** It is free, and it is what makes this ranking readable: it groups the atoms into
    symmetry classes, names each one the way a chemist does ("the para aromatic carbon"), and says
    which scope each belongs to. Two things follow that this tool cannot do on its own. Ranking
    *within a scope* is the fix for the measured failure here — on phenol the para carbon, which is
    the answer, ranks 6th of 13 behind the hydroxyl oxygen and four hydrogens, and no value of
    `top_n` changes that because 15 already exceeds 13. And **the spread across a symmetry class is
    this calculation's own error bar**: phenol's two equivalent ortho carbons differ by 0.0088
    purely because its planar O-H makes one syn and the other anti, which is the same size as the
    ortho-to-meta difference somebody would otherwise report as chemistry.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell (no radicals).
        mode: Which attack to rank for.
        top_n: How many atoms to return, most susceptible first. 0 uses the configured
            default; pass a larger number to see the whole molecule.
        structure_id: A specific 3D geometry to rank at, as reported by `optimize_geometry`,
            `sample_conformers`, `scan_coordinate` or `compute_thermochemistry`. Empty describes a
            force-field geometry embedded from `smiles` — fine for comparing related molecules, and
            the wrong answer to "which site is reactive *in this conformer*", which is the question
            a chemist asks right after a conformer search.

    Returns:
        The ranked sites with all three Fukui indices per atom, and the total number
        of atoms the ranking was drawn from. Atom indices match the heavy atoms of the
        canonical SMILES, with hydrogens following them.
    """
    # Two routes to one answer, exactly as `compute_electronic_properties` above. The SMILES route
    # stays byte-identical rather than being folded into the other: it keys on the geometry the
    # *server* embeds, and routing it through `compute_fukui_at` would key on the geometry embedded
    # here. Forking it would orphan every `xtb.fukui` row on disk for no gain the named-geometry
    # route does not already deliver.
    if structure_id:
        structure = await _starting_geometry(smiles, structure_id)
        payload, _ = await cached_remote(
            default_store(),
            "compute_fukui_at",
            # `mode` and `top_n` are withheld here for the same reason as below — the server keys
            # `xtb.fukui` without them and `ranked_for` re-sorts locally, so one row serves every
            # mode and every slice of one geometry.
            {"structure": structure.model_dump(mode="json")},
        )
        result = SiteReactivityResult.model_validate(payload).ranked_for(mode)
        limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
        return result.model_copy(update={"sites": result.sites[:limit]})
    payload, _ = await cached_remote(
        default_store(),
        "predict_site_reactivity",
        # Neither `mode` nor `top_n` is sent, and both omissions matter. The three single points do
        # not depend on the mode — the server keys them without it (measured: all three modes on
        # phenol derive one key) and re-ranks on the way out, which a cache *hit* here would never
        # reach. So the row is stored in whatever order the server's default produces and
        # `ranked_for` re-ranks it locally; sending `mode` would only make the stored ordering look
        # authoritative. `top_n` is left off for the same reason in the other direction: the row
        # holds every atom, so asking for more sites re-slices a cached result instead of running
        # three more single points.
        {"smiles": smiles},
    )
    result = SiteReactivityResult.model_validate(payload).ranked_for(mode)
    limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
    return result.model_copy(update={"sites": result.sites[:limit]})


@server.tool()
async def optimize_geometry(
    smiles: str, solvent: str | None = None, structure_id: str = ""
) -> OptimizationSummary:
    """Relax a molecule to its nearest stable 3D shape with GFN2-xTB.

    Every other fast calculation here describes whichever conformer was embedded from
    the SMILES and cleaned up with a force field. This one finds an actual minimum of
    the quantum-mechanical surface, which is what the energy and the frequencies are
    computed on. Use it before comparing energies that need to be trustworthy, and to
    see how far a starting guess was from a real structure — a large `relaxation_kcal`
    on a molecule means the unrelaxed numbers for it were describing a strained shape.

    It finds the *nearest* minimum, not the best one: a flexible molecule has many
    conformers and this relaxes into whichever basin it started in. Cached, so repeats
    are free, and the thermochemistry and reaction tools reuse the same result.

    **To relax a conformer you have already found, pass its `structure_id`.** That is the
    cheap-search-then-careful-optimization sequence: run `sample_conformers` first, then bring
    each member's `structure_id` here. Without it this starts from a fresh force-field embedding,
    which discards whichever conformer the search settled on.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "thf"); omit for gas phase.
        structure_id: A specific geometry to relax, as the `st_...` address reported by
            `sample_conformers`, `scan_coordinate` or an earlier call here. Empty starts from a
            fresh embedding of `smiles`. A geometry of a different molecule is refused.

    Returns:
        The converged energy, how much the relaxation lowered it, how far the atoms
        moved, and the id of the resulting geometry.
    """
    # Embed and relax as two calls rather than through the server's own one-shot
    # `optimize_geometry`, and the reason is a collision found by measurement rather than by
    # reading. That tool and `relax_structure` derive the **same** key —
    # `xtb.opt@…:389b625b3220108a:56dca3aa944bd3da` for `CCO` on both — while returning different
    # payloads: a summary without coordinates, and the full result with them. Caching either under
    # that one key poisons the other, and the failure is a
    # validation error on a *hit* deep inside a reaction job. One key, one payload shape: the full
    # result is stored, and the summary is derived from it here, where dropping the geometry costs
    # nothing.
    relaxed, _ = await compose.relax(
        default_store(), await _starting_geometry(smiles, structure_id), solvent
    )
    return OptimizationSummary.of(relaxed)


@server.tool()
async def compute_thermochemistry(
    smiles: str,
    solvent: str | None = None,
    symmetry_number: int = 1,
    temperature_k: float = 0.0,
    top_bands: int = 0,
    structure_id: str = "",
) -> ThermochemistryResult:
    """Compute vibrational frequencies, an IR spectrum, and free energy (GFN2-xTB).

    Optimizes the molecule, then takes its second derivatives. That gives three things:
    whether the structure is a genuine minimum (`is_minimum`, with any imaginary
    frequencies listed), a predicted IR spectrum with band positions and intensities,
    and ideal-gas thermochemistry — zero-point energy, enthalpy, entropy and Gibbs free
    energy. Use the spectrum to test a proposed structure against a measured one, and
    the free energy for equilibrium questions that an electronic energy cannot answer.

    Read it with three limits in mind. Frequencies are semiempirical and systematically
    a few percent off, so compare *patterns and orderings* with a measured spectrum
    rather than expecting positions to match. Everything describes one conformer, not
    the molecule's real population. And the entropy depends on the rotational symmetry
    number, which defaults to 1 — pass the true value (2 for water, 3 for ammonia, 6
    for ethane, 12 for benzene) when the molecule is symmetric, or the entropy comes
    out too high by R·ln(symmetry number).

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name; omit for gas phase.
        symmetry_number: Rotational symmetry number; 1 if the molecule has no symmetry.
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        top_bands: How many IR bands to report, strongest first. 0 uses the configured
            default; imaginary modes are always reported in full.
        structure_id: A specific conformer to describe, as the `st_...` address reported by
            `sample_conformers` or `optimize_geometry`. Empty starts from a fresh embedding.
            Since everything here describes *one* conformer, naming which one is usually the
            difference between a free energy that means something and one that does not.

    Returns:
        Frequencies with IR intensities, whether the geometry is a minimum, and the
        thermochemistry with the uncertainty to quote alongside it.
    """
    # Composed rather than called: remote optimise, remote Hessian, local RRHO. The key of a
    # thermochemistry would have to name the geometry the refinement loop settles on, which is an
    # output, so it has no cache row of its own and never had one — its economy is entirely the two
    # nested entries, and a single remote call would swallow both.
    structure = await _starting_geometry(smiles, structure_id)
    thermo = ThermoSettings(
        symmetry_number=symmetry_number,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
    )
    _, result, _ = await compose.relax_to_minimum(default_store(), structure, solvent, thermo)
    limit = top_bands if top_bands > 0 else settings.xtb_ir_bands_top_n
    # The imaginary mode's 3N-vector is refinement machinery, not something a model can
    # read; the frequency itself is already in `imaginary_frequencies_cm`.
    return result.model_copy(
        update={"modes": result.strongest_bands(limit), "imaginary_displacement": None}
    )


@server.tool()
async def predict_developability_profile(smiles: str) -> DescriptorProfile:
    """Compute a developability descriptor panel: MW, LogP, TPSA, H-bond counts, Ro5/Veber flags.

    Use this to triage a candidate before committing bench time — Lipinski's Rule-of-Five
    (`lipinski_violations`) and Veber's rule (`veber_pass`) are widely used oral-bioavailability
    heuristics, not developability verdicts. Report them as flags to weigh alongside everything
    else known about the molecule, never as a pass/fail gate on their own. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The descriptor panel plus the two rule-of-thumb flags.
    """
    payload, _ = await cached_remote(
        default_store(), "predict_developability_profile", {"smiles": smiles}
    )
    return DescriptorProfile.model_validate(payload)


@server.tool()
async def predict_logd(smiles: str, ph: float | None = None) -> LogdResult:
    """Predict the pH-dependent distribution coefficient (logD) of a singly-ionisable molecule.

    Answers "how lipophilic is this at the pH I actually work at?" — useful for HPLC
    mobile-phase pH selection, extraction, and formulation, where the pH-independent LogP alone
    is not the number that matters.

    Built on `predict_pka`, but its domain is **strictly narrower** than that tool's rather than
    the same, so a working pKa is not a promise of a logD. `predict_pka` reports one pKa and one
    Henderson-Hasselbalch term consumes exactly one, so this is defined only where a single
    equilibrium describes the molecule at the pH asked for.

    Served: one O-H/S-H acid (carboxylic acid, phenol, alcohol, thiol) **or** one aromatic/aryl
    nitrogen base — bases are supported and corrected in the opposite direction, which is why the
    result names the site. Further sites are fine while they stay un-ionised at that pH, so a
    diol or sugar (pKa ~15) is served at any ordinary pH and a diacid is served well below its
    pKa.

    Refused, with a `ValueError` naming the reason rather than a guess: aliphatic amines and
    charged or unparseable inputs (inherited from `predict_pka`); anything **amphoteric**, an
    acid site plus a base site, since `predict_pka` always answers with the acid and never
    evaluates the base; and any **polyprotic** molecule substantially ionised at that pH. The
    second pKa is not computable here at all, so the alternative would be a number wrong by 2-5
    log units carrying a ±1.6 uncertainty. Relay the refusal; do not fall back to logP or retry
    at a pH chosen to get past it.

    Args:
        smiles: The molecule as a SMILES string.
        ph: The pH to evaluate at. Defaults to 7.4 (physiological pH) if omitted.

    Returns:
        logD at the given pH, plus the LogP and pKa it was derived from and the pKa model's
        uncertainty (state it — this is not an exact value).
    """
    # The expensive half is a *cached* pKa on the server's own key; the rest is a Crippen sum and
    # one Henderson-Hasselbalch term, both pure RDKit and both local. Shipping the composite whole
    # would have made every repeat a full recompute of the most expensive tool in the set —
    # measured, pyridine 20.603 s cold against 0.005 s warm — which is a D-011 violation reached by
    # moving code rather than by changing a rule. `predict_logd` has no cache row of its own here
    # and never had one, so `logd_from_pka` is called on the pKa the cache just served.
    payload, _ = await cached_remote(default_store(), "predict_pka", {"smiles": smiles})
    return logd_from_pka(PkaResult.model_validate(payload), ph)
