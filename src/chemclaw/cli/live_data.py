"""`python -m chemclaw.cli.live_data` — prove the seeded corpus survives the pipeline *by value*.

Every other live lane asks whether a capability is **reachable**. `live_jobs` asks whether a
durable job runs; `live_probes` asks whether a model uses the right tool. Neither asks the
question a chemist actually cares about: **is the number in the answer the number in the paper?**

That question was never asked here, and the 2026-08-17 four-repo run shows what the gap costs. It
reported "638 note proposals" and called ingestion proven. 638 is a count. A count cannot tell you
that 57% of the seeded corpus never entered the system at all, which is what this lane found on its
first run.

## The ground truth is the paper, not the previous stage

`Chemclaw3_mock` seeds ~10,000 ORD records from **real, published, cited** HTE screens, and it
commits the raw factor tables it expanded them from as CSVs in `app/eln/real_data/`. Those CSVs are
the only honest reference point: they are upstream of the mock's seeding code *and* upstream of
this repo's adapter, so a check against them cannot be satisfied by two stages agreeing on the same
mistake. Every assertion below compares against the CSV, never against the stage before it.

## What it measures

One ledger, per dataset, following the published rows down the pipeline:

    published (CSV) -> seeded (ORD JSON) -> mapped (OrdReaction) -> note -> proposal -> index

A stage that drops rows is not automatically a failure — one dataset is refused *deliberately*,
and the refusal is correct (see `_DATASETS`). It is a failure when reality disagrees with the
declaration, **in either direction**: a dataset that starts being refused has regressed, and a
dataset declared unreachable that suddenly ingests means somebody taught the adapter to invent a
structure it cannot know. Both are red here, which is what makes this a regression detector rather
than a number to admire.

## Why no model is involved

The same argument `cli/live_jobs.py` makes for the durable half. Grading an answer conflates a
corpus that never held the data with a model that did not look for it — and the corpus half is the
one nothing was checking. `make live-probes` is the strictly later question, and it is only
interpretable once this lane is green: the 2026-08-17 grounded probes were graded against a corpus
that contained **none** of the ORD data they name.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.db import _redact
from chemclaw.core.db import connection as db_connection
from chemclaw.core.logging import configure_logging
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.note import note_from_ord_reaction
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.ord_adapter import OrdJsonAdapter
from chemclaw.kg.note import note_id_for_reaction

logger = logging.getLogger(__name__)

# The epoch every corpus read starts from. The ORD exports carry payload timestamps years in the
# past and one shared mtime, so any later floor silently reads an empty corpus — the exact failure
# the backfill below exists to undo.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Yields are compared at this precision unless a dataset declares its own. Four of the five
# published tables carry two decimals, so six places is exact for them while still catching a
# stage that rounds, truncates or rescales a measurement.
_YIELD_PLACES = 6


@dataclass(frozen=True)
class Dataset:
    """One published dataset, and how its CSV columns line up with the ORD records seeded from it.

    This is a *binding*, in the sense `D-2026-08-04-the-schema-is-a-file` gives the word: the
    published tables and the ORD input keys are both facts about somebody else's data, so they are
    declared here rather than inferred. Inferring them is how a check ends up asserting that a
    corpus agrees with itself.

    `reachable` is the declaration this lane is really built around. `False` says the adapter is
    *expected* to refuse every record — and the check fails if it stops refusing them, because the
    only way to accept them is to invent the structure the source never published.
    """

    csv_name: str
    # ORD `datasetId` for the whole file, or the column that partitions it into several.
    dataset_id: str | None = None
    partition_column: str | None = None
    partitions: dict[str, str] = field(default_factory=dict)
    # (ORD input key, CSV column) for each experimental factor that identifies a row.
    factors: tuple[tuple[str, str], ...] = ()
    yield_column: str = ""
    # Decimal places the seeded record is expected to carry. Declared rather than assumed: one
    # published table reports a UV-area yield at full float precision (`4.76410921845962`) and the
    # mock rounds it to ORD's reporting precision on the way in. Pinning the rounding here keeps
    # the check strict — a stage that truncated further, or that seeded the mass-ion column
    # instead, still fails — where a loosened global tolerance would have hidden both.
    yield_places: int = _YIELD_PLACES
    reachable: bool = True
    # Why a dataset is refused, in one line, so a red run explains itself without a git archaeology
    # session. Empty for reachable datasets.
    refusal: str = ""

    def dataset_ids(self) -> tuple[str, ...]:
        """Every ORD `datasetId` this CSV was seeded into."""
        if self.dataset_id is not None:
            return (self.dataset_id,)
        return tuple(sorted(set(self.partitions.values())))


# The five published screens, bound to the ORD keys `Chemclaw3_mock` seeds them under. Counts are
# deliberately absent: a hard-coded row count is a second source of truth that drifts, and the CSV
# is right there.
_DATASETS: tuple[Dataset, ...] = (
    Dataset(
        csv_name="bh_amination_hte.csv",
        partition_column="plate",
        partitions={
            "P2Et": "bh-amination-plate-p2et",
            "MTBD": "bh-amination-plate-mtbd",
            "BTMG": "bh-amination-plate-btmg",
        },
        factors=(
            ("ligand", "ligand_smiles"),
            ("isoxazole additive", "additive_smiles"),
            ("base", "base_smiles"),
            ("aryl halide", "aryl_halide_smiles"),
        ),
        yield_column="yield_percent",
    ),
    Dataset(
        csv_name="suzuki_miyaura_flow_hte.csv",
        dataset_id="suzuki-miyaura-flow-hte",
        factors=(
            ("quinoline coupling partner", "r1_smiles"),
            ("ligand", "ligand_smiles"),
            ("base", "base_smiles"),
        ),
        yield_column="yield_pct_uv",
        yield_places=2,
        reachable=False,
        refusal=(
            "the source spreadsheet (Perera, Science 2018, 359, 429) publishes the second "
            "coupling partner only as its own shorthand (`2a, Boronic Acid`), so no structure "
            "exists to map. "
            "`ord_adapter._smiles` refuses it rather than inventing one, and "
            "`test_ord_compound_with_no_resolvable_identifier_is_still_refused` pins that refusal"
        ),
    ),
    Dataset(
        csv_name="santanilla_amidation_screen.csv",
        dataset_id="santanilla-amidation-screen",
        factors=(
            ("aryl halide", "aryl_halide_smiles"),
            ("nucleophile", "nucleophile_smiles"),
            ("precatalyst", "catalyst_smiles"),
            ("base", "base_smiles"),
        ),
        yield_column="yield_percent",
    ),
    Dataset(
        csv_name="santanilla_sulfonamidation_screen.csv",
        dataset_id="santanilla-sulfonamidation-screen",
        factors=(
            ("aryl halide", "aryl_halide_smiles"),
            ("nucleophile", "nucleophile_smiles"),
            ("precatalyst", "catalyst_smiles"),
            ("base", "base_smiles"),
        ),
        yield_column="yield_percent",
    ),
    Dataset(
        csv_name="nielsen_deoxyfluorination.csv",
        dataset_id="nielsen-deoxyfluorination-screen",
        factors=(
            ("alcohol", "alcohol_smiles"),
            ("sulfonyl fluoride", "sulfonyl_fluoride_smiles"),
            ("base", "base_smiles"),
        ),
        yield_column="product_yield",
    ),
)


@dataclass
class Check:
    """One assertion about the seeded corpus, and what was actually observed.

    `observed` is kept even when the check passes, for the reason `cli/live_jobs.py` gives: a green
    run that cannot say what it saw is a green run nobody can audit.
    """

    name: str
    passed: bool
    observed: str


@dataclass
class Reach:
    """How far one dataset's published rows actually got down the pipeline."""

    dataset: str
    published: int
    seeded: int
    mapped: int
    refused: int
    proposed: int | None = None


@dataclass
class DataRun:
    """Everything one pass produced: the per-dataset ledger and every check over it."""

    checks: list[Check] = field(default_factory=list)
    reach: list[Reach] = field(default_factory=list)
    seconds: float = 0.0
    backfilled: str = ""

    @property
    def ok(self) -> bool:
        """True when every check passed — the exit code follows this and nothing else."""
        return all(check.passed for check in self.checks)


# --- the published tables -------------------------------------------------------------------


def _published_rows(real_data: Path, dataset: Dataset) -> list[dict[str, str]]:
    """Every row of one published factor table, verbatim."""
    path = real_data / dataset.csv_name
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _published_key(dataset: Dataset, row: dict[str, str]) -> tuple[Any, ...]:
    """The (dataset id, factors…, yield) tuple that identifies one published measurement.

    A *multiset* of these is compared against the seeded corpus rather than a set, so a duplicated
    row and a dropped one are both visible. Real screens repeat factor combinations across
    replicates, so requiring uniqueness would be a claim about the chemistry, not about the code.
    """
    if dataset.partition_column is not None:
        dataset_id = dataset.partitions[row[dataset.partition_column]]
    else:
        assert dataset.dataset_id is not None
        dataset_id = dataset.dataset_id
    # A blank cell is an omitted reagent (a real control), and it has to compare equal to the
    # seeded record's *absent* input rather than to an empty string.
    factors = tuple(row[column].strip() or None for _, column in dataset.factors)
    return (dataset_id, *factors, round(float(row[dataset.yield_column]), dataset.yield_places))


# --- the seeded ORD exports -----------------------------------------------------------------


def _identifier(payload: dict[str, Any], key: str) -> str | None:
    """The first identifier value on one named ORD input, whatever its type — None if absent.

    Deliberately type-agnostic: one real dataset identifies a coupling partner by `NAME` because
    no structure was ever published for it, and a reader that demanded SMILES here could not even
    *count* those records — which is the whole thing this lane exists to make visible.

    **An absent input is data, not an error.** The Perera flow screen runs real no-ligand (480) and
    no-base (720) control conditions, and the published table records them as blank cells. Raising
    here would have made this lane unable to read a fifth of that dataset; treating the absence as
    a distinct key value is what lets it check the controls arrived as controls.
    """
    entry = payload.get("inputs", {}).get(key)
    if entry is None:
        return None
    for identifier in entry["components"][0].get("identifiers", ()):
        value = identifier.get("value")
        if value:
            return str(value)
    return None


def _seeded_yield(payload: dict[str, Any], places: int = _YIELD_PLACES) -> float | None:
    """The headline yield percentage on an ORD export, or None when it records none.

    `is not None` and never a truthiness test. 236 of the 3,955 published Buchwald-Hartwig wells
    are exactly 0.00% — a real, informative result (that combination failed) that a falsy check
    silently converts into "unknown". This is not hypothetical: the first draft of this lane's own
    verification script had that bug and mis-reported 21 records.
    """
    for outcome in payload.get("outcomes", ()):
        for product in outcome.get("products", ()):
            for measurement in product.get("measurements", ()):
                if measurement.get("type") == "YIELD":
                    value = measurement.get("percentage", {}).get("value")
                    if value is not None:
                        return round(float(value), places)
    return None


def _seeded_payloads(export_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every seeded ORD export, grouped by `datasetId` (curated fixtures under an empty key)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(export_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        grouped.setdefault(str(payload.get("datasetId") or ""), []).append(payload)
    return grouped


def _seeded_key(dataset: Dataset, payload: dict[str, Any]) -> tuple[Any, ...]:
    """The same (dataset id, factors…, yield) tuple, read off a seeded export."""
    factors = tuple(_identifier(payload, key) for key, _ in dataset.factors)
    return (
        str(payload.get("datasetId") or ""),
        *factors,
        _seeded_yield(payload, dataset.yield_places),
    )


# --- checks ----------------------------------------------------------------------------------


def check_seeding_is_faithful(
    real_data: Path, seeded: dict[str, list[dict[str, Any]]]
) -> list[Check]:
    """Every published measurement is seeded exactly once, unchanged — and nothing else is.

    Multiset equality in both directions. A one-directional check would pass a corpus that
    duplicated every row, and a count check would pass one that swapped two yields.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
        want = Counter(_published_key(dataset, row) for row in _published_rows(real_data, dataset))
        got: Counter[tuple[Any, ...]] = Counter()
        for dataset_id in dataset.dataset_ids():
            got.update(_seeded_key(dataset, payload) for payload in seeded.get(dataset_id, ()))
        missing = want - got
        extra = got - want
        checks.append(
            Check(
                name=f"seeding faithful · {dataset.csv_name}",
                passed=not missing and not extra,
                observed=(
                    f"{sum(want.values())} published, {sum(got.values())} seeded, "
                    f"{sum(missing.values())} missing, {sum(extra.values())} unpublished"
                ),
            )
        )
    return checks


def check_zero_yields_survive(real_data: Path, seeded: dict[str, list[dict[str, Any]]]) -> Check:
    """A 0% yield is evidence, and it has to arrive as 0% rather than as silence.

    Counted across every dataset at once because the failure mode is a shared one — a single falsy
    test anywhere on the path erases all of them, and one aggregate number makes that unmissable.
    """
    published = 0
    for dataset in _DATASETS:
        published += sum(
            1
            for row in _published_rows(real_data, dataset)
            if round(float(row[dataset.yield_column]), dataset.yield_places) == 0.0
        )
    seeded_zeroes = sum(
        1
        for dataset in _DATASETS
        for dataset_id in dataset.dataset_ids()
        for payload in seeded.get(dataset_id, ())
        if _seeded_yield(payload) == 0.0
    )
    return Check(
        name="zero yields survive seeding",
        passed=published == seeded_zeroes,
        observed=f"{published} published at exactly 0.00%, {seeded_zeroes} seeded",
    )


def check_adapter_matches_its_declaration(
    mapped: dict[str, list[OrdReaction]], refused: dict[str, int]
) -> list[Check]:
    """Each dataset is accepted, or refused, exactly as `_DATASETS` declares — no drift either way.

    The asymmetry matters. A reachable dataset that starts being refused is a plain regression. A
    dataset declared unreachable that starts being *accepted* is worse than a regression: the only
    way to accept it is to have invented a structure the source never published, which propagates
    into a fingerprint index and eventually into a proposed note.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
        accepted = sum(len(mapped.get(name, ())) for name in dataset.dataset_ids())
        rejected = sum(refused.get(name, 0) for name in dataset.dataset_ids())
        if dataset.reachable:
            passed = accepted > 0 and rejected == 0
            observed = f"{accepted} mapped, {rejected} refused"
        else:
            passed = accepted == 0 and rejected > 0
            observed = (
                f"{accepted} mapped, {rejected} refused (declared unreachable: {dataset.refusal})"
            )
        checks.append(
            Check(
                name=f"adapter matches declaration · {dataset.csv_name}",
                passed=passed,
                observed=observed,
            )
        )
    return checks


def check_adapter_preserves_values(
    real_data: Path, mapped: dict[str, list[OrdReaction]], seeded: dict[str, list[dict[str, Any]]]
) -> list[Check]:
    """For every record the adapter accepts, its published factors and yield are still there.

    The comparison is against the CSV, so this is not "the adapter agrees with the file it read".
    Structures are compared as the strings both sides publish: the adapter is asserted to preserve
    what it was given, and any canonicalisation it applied would be a change this must see.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
        if not dataset.reachable:
            continue
        want = Counter(_published_key(dataset, row) for row in _published_rows(real_data, dataset))
        # Rebuild each mapped reaction's key from the OrdReaction itself: the factor SMILES must
        # appear among its inputs, and the yield must be the published one.
        checked = intact = 0
        losses: list[str] = []
        by_id = {
            str(payload.get("reactionId")): payload
            for dataset_id in dataset.dataset_ids()
            for payload in seeded.get(dataset_id, ())
        }
        for dataset_id in dataset.dataset_ids():
            for reaction in mapped.get(dataset_id, ()):
                payload = by_id.get(reaction.reaction_id)
                if payload is None:
                    losses.append(f"{reaction.reaction_id}: no seeded payload")
                    continue
                checked += 1
                key = (
                    dataset_id,
                    *(_identifier(payload, k) for k, _ in dataset.factors),
                    round(reaction.yield_percent, dataset.yield_places)
                    if reaction.yield_percent is not None
                    else None,
                )
                structures = {component.smiles for component in reaction.inputs}
                factor_smiles = {value for value in key[1:-1] if isinstance(value, str)}
                if key not in want:
                    losses.append(f"{reaction.reaction_id}: not a published measurement")
                elif not factor_smiles <= structures:
                    losses.append(
                        f"{reaction.reaction_id}: lost {sorted(factor_smiles - structures)}"
                    )
                else:
                    intact += 1
        checks.append(
            Check(
                name=f"adapter preserves values · {dataset.csv_name}",
                passed=checked > 0 and intact == checked,
                observed=(
                    f"{intact}/{checked} reactions carry their published factors and yield"
                    + (f" · first loss: {losses[0]}" if losses else "")
                ),
            )
        )
    return checks


def check_note_carries_the_number(mapped: dict[str, list[OrdReaction]]) -> Check:
    """The rendered note states the yield — including when the yield is zero.

    The note body is what reaches the index, the retriever and eventually the answer, so a value
    that survives every model above and is dropped here is a value the chemist never sees. A 0%
    record is chosen deliberately: it is the one a truthiness test loses.
    """
    zero: OrdReaction | None = None
    nonzero: OrdReaction | None = None
    for reactions in mapped.values():
        for reaction in reactions:
            if reaction.yield_percent == 0.0 and zero is None:
                zero = reaction
            elif reaction.yield_percent not in (None, 0.0) and nonzero is None:
                nonzero = reaction
            if zero is not None and nonzero is not None:
                break
    if zero is None or nonzero is None:
        return Check(
            name="note carries the number",
            passed=False,
            observed=(
                f"needed one 0% and one non-zero record, found "
                f"{zero is not None}/{nonzero is not None}"
            ),
        )
    bodies = {r.reaction_id: note_from_ord_reaction(r).body for r in (zero, nonzero)}
    states_zero = "yield: 0.0%" in bodies[zero.reaction_id]
    states_nonzero = f"yield: {nonzero.yield_percent}%" in bodies[nonzero.reaction_id]
    return Check(
        name="note carries the number",
        passed=states_zero and states_nonzero,
        observed=(
            f"{zero.reaction_id} states 0%: {states_zero} · "
            f"{nonzero.reaction_id} states {nonzero.yield_percent}%: {states_nonzero}"
        ),
    )


# A procedure step states its conditions like "stirred at 82 °C for 4.0 h". Anchored on the two
# units so a mass in mg or a 1H NMR shift cannot be read as a temperature.
_PROSE_TEMPERATURE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°\s*C")
_PROSE_TIME = re.compile(r"for\s+(\d+(?:\.\d+)?)\s*h\b")


async def check_prose_yields_its_numbers(eln_export_dir: Path) -> Check:
    """Conditions stated only in a procedure's prose still reach the structured record.

    **The one place in this corpus where a value is *derived* rather than copied**, which is why it
    is worth a check of its own: everything else here asserts that a number survived a hop, and
    this asserts that a number was recovered from a sentence at all.

    The free-text fixtures are deliberately paired for exactly this. A `-1` record states "stirred
    at 82 °C for 4.0 h" in step 2 and carries **no** `temperature_c`/`time_h` fields; its `-2` twin
    carries both fields and its prose says only "stirred under nitrogen". So the `-1` half has no
    structured value to fall back on — if the extraction fails, the condition is simply gone, and
    nothing downstream can tell the difference between "ran at 82 °C" and "temperature unrecorded".

    Only records whose prose states both are checked, and the count is reported, because a silent
    denominator is how a check that stopped matching anything keeps passing.
    """
    adapter = JsonExportAdapter(str(eln_export_dir))
    raws = await adapter.fetch_new_entries(_EPOCH)
    checked = 0
    wrong: list[str] = []
    for raw in raws:
        prose = str(raw.payload.get("procedure") or "")
        temperature = _PROSE_TEMPERATURE.search(prose)
        time_h = _PROSE_TIME.search(prose)
        if temperature is None or time_h is None:
            continue
        try:
            reaction = adapter.map_to_ord(raw)
        except Exception:
            continue
        checked += 1
        # `is not None` on both, and not a truthiness test: one fixture reads "cooled to 0 °C",
        # and `0.0 or None` would report the extraction as a failure that it is not.
        want = (float(temperature.group(1)), float(time_h.group(1)))
        got = (reaction.temperature_c, reaction.time_h)
        if got != want:
            wrong.append(f"{raw.entry_id}: prose says {want}, record carries {got}")
    return Check(
        name="prose yields its numbers",
        passed=checked > 0 and not wrong,
        observed=(
            f"{checked - len(wrong)}/{checked} procedures state a temperature and a time in prose "
            f"and carry both" + (f" · first miss: {wrong[0]}" if wrong else "")
        ),
    )


async def check_corpus_is_reachable(mapped: dict[str, list[OrdReaction]]) -> Check:
    """The mapped records actually reached the PR-gate — asked of Postgres, not of a log line.

    This is the check the 2026-08-17 run had no way to fail: it counted 638 proposals without ever
    asking *which* records they were, and the answer was none of the ~4,200 ORD ones.
    """
    expected = [
        note_id_for_reaction(reaction.reaction_id)
        for reactions in mapped.values()
        for reaction in reactions
    ]
    if not expected:
        return Check(
            name="corpus is reachable", passed=False, observed="nothing mapped to look for"
        )
    async with db_connection(settings.postgres_dsn) as connection:
        cursor = await connection.execute(
            "SELECT count(DISTINCT note_id) FROM note_proposals WHERE note_id = ANY(%s)",
            (expected,),
        )
        row = await cursor.fetchone()
    found = 0 if row is None else int(row[0])
    return Check(
        name="corpus is reachable",
        passed=found == len(expected),
        observed=f"{found}/{len(expected)} mapped ORD records have a note proposal",
    )


# --- the backfill ----------------------------------------------------------------------------


async def backfill(timeout_seconds: float) -> str:
    """Run one `ElnSyncWorkflow` from the epoch, so the seeded corpus is reachable at all.

    Every ORD export shares a single mtime — the moment the repo was cloned — and carries an older
    payload timestamp, so the incremental sync's cursor passes all of them on its first scheduled
    firing and no later run can ever qualify them again. `adapter.warn_late_arrivals` says exactly
    this and names the remedy; nothing took it, so the four-repo lane ran with 0 of ~10,000 ORD
    records ingested while `/readyz` was green and the sync log read normally.

    Deliberately the real workflow on the real broker rather than `sync_entries` in-process: a
    backfill that bypassed Temporal would prove the adapter works and leave the thing that actually
    runs in production untested.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from chemclaw.core.temporal_client import connect as temporal_connect
    from chemclaw.durable.eln_sync import ElnSyncWorkflow

    client = await temporal_connect()
    # **A fixed id, so a second invocation rejoins the running drain instead of racing it.**
    # This is D-011's argument applied to the harness: the drain takes hours, `up.sh` starts one on
    # every bring-up and a human may run the lane meanwhile, and two concurrent syncs over one
    # corpus contend on the PR-gate's git repository while producing no row the first would not.
    # Measured while writing this: calling it twice did start two.
    workflow_id = "eln-backfill-epoch"
    try:
        handle = await client.start_workflow(
            ElnSyncWorkflow.run,
            _EPOCH,
            id=workflow_id,
            task_queue=settings.background_task_queue,
        )
        logger.info("backfill %s started on %s", workflow_id, settings.background_task_queue)
    except WorkflowAlreadyStartedError:
        # Already running: take a handle to it and wait on that. Rejoining is the whole point, so
        # this is the ordinary path on every bring-up after the first, not an error to report.
        handle = client.get_workflow_handle(workflow_id)
        logger.info("backfill %s already running — waiting on it", workflow_id)
    try:
        summary = await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
    except TimeoutError:
        # **A drain still running is a state, not an error.** Every proposal costs a PR-gate git
        # branch and commit — measured at ~1.8 s/record against this corpus, so the mock's 4,251
        # ingestible records take a little over two hours. Failing here would make the lane red for
        # a reason that is not a defect; the reachability check below reports how far it got, which
        # is the honest number and the one that converges on its own.
        return (
            f"{workflow_id}: still draining after {timeout_seconds:.0f}s — the workflow keeps "
            "running on the broker, so re-running this lane later reads the finished corpus"
        )
    return (
        f"{workflow_id}: ingested {len(summary.ingested)}, "
        f"skipped {len(summary.skipped_existing)}, rejected {len(summary.rejected)}"
    )


# --- the run ---------------------------------------------------------------------------------


async def _map_corpus(
    export_dir: Path,
) -> tuple[dict[str, list[OrdReaction]], dict[str, int]]:
    """Run this repo's real ORD adapter over the whole seeded corpus; group results by dataset.

    The *real* adapter, from the configured export directory, for the reason `cli/live_jobs.py`
    gives for using the real job tool: a lane that reimplemented the mapping would keep passing
    while the mapping that ships broke.
    """
    adapter = OrdJsonAdapter(str(export_dir))
    raws = await adapter.fetch_new_entries(_EPOCH)
    dataset_of = {
        str(payload.get("reactionId")): str(payload.get("datasetId") or "")
        for payloads in _seeded_payloads(export_dir).values()
        for payload in payloads
    }
    mapped: dict[str, list[OrdReaction]] = {}
    refused: dict[str, int] = {}
    for raw in raws:
        dataset_id = dataset_of.get(raw.entry_id, "")
        try:
            mapped.setdefault(dataset_id, []).append(adapter.map_to_ord(raw))
        except Exception as exc:
            refused[dataset_id] = refused.get(dataset_id, 0) + 1
            logger.debug("refused %s: %s", raw.entry_id, exc)
    return mapped, refused


async def run_data_checks(
    real_data: Path,
    export_dir: Path,
    *,
    with_database: bool,
    do_backfill: bool,
    timeout: float,
    checks_enabled: bool = True,
) -> DataRun:
    """Check the published tables, the seeded corpus and the live database against each other.

    One pass: optionally start the backfill, read both corpora, then run every check over them.
    """
    run = DataRun()
    started = time.monotonic()

    if do_backfill:
        run.backfilled = await backfill(timeout)
        logger.info("backfill: %s", run.backfilled)
    if not checks_enabled:
        # A bring-up only has to *start* the drain. Running the checks here would make its exit
        # code report whether the corpus is currently correct — which, mid-drain, it is not.
        run.seconds = time.monotonic() - started
        return run

    seeded = _seeded_payloads(export_dir)
    mapped, refused = await _map_corpus(export_dir)

    run.checks.extend(check_seeding_is_faithful(real_data, seeded))
    run.checks.append(check_zero_yields_survive(real_data, seeded))
    run.checks.extend(check_adapter_matches_its_declaration(mapped, refused))
    run.checks.extend(check_adapter_preserves_values(real_data, mapped, seeded))
    run.checks.append(check_note_carries_the_number(mapped))
    run.checks.append(await check_prose_yields_its_numbers(Path(settings.eln_export_dir)))
    if with_database:
        run.checks.append(await check_corpus_is_reachable(mapped))

    for dataset in _DATASETS:
        published = len(_published_rows(real_data, dataset))
        ids = dataset.dataset_ids()
        run.reach.append(
            Reach(
                dataset=dataset.csv_name.removesuffix(".csv"),
                published=published,
                seeded=sum(len(seeded.get(name, ())) for name in ids),
                mapped=sum(len(mapped.get(name, ())) for name in ids),
                refused=sum(refused.get(name, 0) for name in ids),
            )
        )
    run.seconds = time.monotonic() - started
    return run


def report(run: DataRun) -> str:
    """The run as two tables, in the same shape `cli/live_jobs.py` reports its own."""
    lines = [
        "# Live corpus-fidelity pass\n",
        f"Ground truth: the published factor tables · Postgres `{_redact(settings.postgres_dsn)}`",
        f"· {run.seconds:.1f}s\n",
    ]
    if run.backfilled:
        lines.append(f"Backfill: {run.backfilled}\n")
    if not run.checks:
        # `--backfill-only`: the drain was started and nothing was asked. Empty tables here would
        # read as "every check returned nothing", which is a different and much worse claim.
        lines.append("No checks run (`--backfill-only`). `make live-data` reads what arrived.")
        return "\n".join(lines)
    lines += [
        "| dataset | published | seeded | mapped | refused |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for reach in run.reach:
        lines.append(
            f"| {reach.dataset} | {reach.published} | {reach.seeded} "
            f"| {reach.mapped} | {reach.refused} |"
        )
    lines += ["", "| check | result | observed |", "| --- | --- | --- |"]
    for check in run.checks:
        lines.append(
            f"| {check.name} | {'PASS' if check.passed else '**FAIL**'} | {check.observed} |"
        )
    passed = sum(1 for check in run.checks if check.passed)
    lines.append(f"\n**{passed}/{len(run.checks)} checks passed.**")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the corpus checks and write their report; exit non-zero if any check failed."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--real-data",
        type=Path,
        default=None,
        help="the mock's published factor tables (default: alongside the ORD export directory)",
    )
    parser.add_argument(
        "--corpus-only",
        action="store_true",
        help="skip every check that needs Postgres — the corpus half runs with no infrastructure",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="run one ElnSyncWorkflow from the epoch first, so the seeded corpus is reachable",
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help=(
            "start the backfill and run no checks — what a bring-up wants, so that its exit code "
            "reports whether the drain started and never whether the corpus is currently correct"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds to wait for the backfill workflow before reporting it as still draining",
    )
    parser.add_argument("--report", type=Path, default=None, help="where to write the report")
    args = parser.parse_args(argv)

    configure_logging()
    export_dir = Path(settings.ord_export_dir)
    real_data = args.real_data or export_dir.parent.parent.parent / "app" / "eln" / "real_data"
    if not real_data.is_dir():
        parser.error(
            f"no published factor tables at {real_data} — pass --real-data pointing at "
            "Chemclaw3_mock/app/eln/real_data (this lane has no ground truth without them)"
        )

    run = asyncio.run(
        run_data_checks(
            real_data,
            export_dir,
            with_database=not (args.corpus_only or args.backfill_only),
            do_backfill=args.backfill or args.backfill_only,
            checks_enabled=not args.backfill_only,
            timeout=args.timeout,
        )
    )
    text = report(run)
    print(text)

    destination = args.report or Path(settings.live_probe_transcript_dir) / "corpus-fidelity.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    print(f"\nwritten to {destination}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
