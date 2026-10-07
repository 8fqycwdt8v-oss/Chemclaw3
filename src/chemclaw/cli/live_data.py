"""`python -m chemclaw.cli.live_data` — prove the seeded corpus survives the pipeline *by value*.

Other live lanes ask whether a capability is reachable; this asks whether the number in the
answer is the number in the paper. `Chemclaw3_mock` seeds ORD records from real, published HTE
screens and commits the raw factor tables as CSVs in `app/eln/real_data/`. Every assertion compares
against those CSVs, never against the previous stage, so two stages agreeing on one mistake cannot
pass. One ledger per dataset:

    published (CSV) -> seeded (ORD JSON) -> mapped (OrdReaction) -> reaction_records

Each dataset declares the tier its records must arrive in; one is citation-only because the source
names a coupling partner without its structure
(`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable`). Disagreement in either
direction is red: a refused or demoted record has regressed, and a citation-only record arriving
structured means a structure was invented.

No model is involved (as in `cli/live_jobs.py`); `make live-probes` is only interpretable once this
lane is green.
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
from chemclaw.core.markdown import render_table
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.ord import OrdReaction, RecordTier, Role
from chemclaw.ingest.eln.ord_adapter import OrdJsonAdapter
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.warehouse.expr import PatternBudgetError, pattern_budget

logger = logging.getLogger(__name__)

# The epoch every corpus read starts from. ORD exports carry old payload timestamps and one shared
# mtime, so any later floor silently reads an empty corpus.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Default yield comparison precision: exact for the two-decimal published tables, while still
# catching a stage that rounds, truncates or rescales.
_YIELD_PLACES = 6


@dataclass(frozen=True)
class Dataset:
    """One published dataset, and how its CSV columns line up with the ORD records seeded from it.

    A binding: published columns and ORD input keys are facts about someone else's data, declared
    here
    rather than inferred so the check cannot assert a corpus agrees with itself. `tier` is the
    declaration the lane is built around; `CITATION_ONLY` fails if a record arrives structured.
    """

    csv_name: str
    # ORD `datasetId` for the whole file, or the column that partitions it into several.
    dataset_id: str | None = None
    partition_column: str | None = None
    partitions: dict[str, str] = field(default_factory=dict)
    # (ORD input key, CSV column) for each experimental factor that identifies a row.
    factors: tuple[tuple[str, str], ...] = ()
    yield_column: str = ""
    # Decimal places the seeded record is expected to carry. Declared per dataset because one table
    # reports full float precision and the mock rounds it to ORD's reporting precision; a global
    # tolerance would hide further truncation or a swapped column.
    yield_places: int = _YIELD_PLACES
    tier: RecordTier = RecordTier.STRUCTURED
    # (ORD input key, CSV column) for each species the source *names* without a structure. Checked
    # as names, verbatim: the published cell must arrive as an `UnstructuredComponent.name`.
    named_only: tuple[tuple[str, str], ...] = ()
    # Why a dataset is citation-only, in one line, so a red run explains itself without a git
    # archaeology session. Empty for structured datasets.
    tier_reason: str = ""

    def dataset_ids(self) -> tuple[str, ...]:
        """Every ORD `datasetId` this CSV was seeded into."""
        if self.dataset_id is not None:
            return (self.dataset_id,)
        return tuple(sorted(set(self.partitions.values())))


# The five published screens, bound to the ORD keys `Chemclaw3_mock` seeds them under. No row
# counts:
# the CSV is the source of truth.
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
        tier=RecordTier.CITATION_ONLY,
        named_only=(("second coupling partner", "r2_name"),),
        tier_reason=(
            "the source spreadsheet (Perera, Science 2018, 359, 429) publishes the second "
            "coupling partner only as its own shorthand (`2a, Boronic Acid`), so no structure "
            "exists to map; the name is carried verbatim and the record stays out of every "
            "structure index"
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

    `observed` is kept even on a pass, so the record says what was seen.
    """

    name: str
    passed: bool
    observed: str


@dataclass
class Reach:
    """How far one dataset's published rows actually got down the pipeline, and in which tier."""

    dataset: str
    published: int
    seeded: int
    mapped: int
    refused: int
    # The part of `mapped` that arrived citation-only — the tier column of the reach table.
    citation_only: int = 0
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


def _default_real_data(ord_export_dir: Path) -> Path | None:
    """Where the mock's published factor tables sit, given its ORD export directory.

    Both ends are `Chemclaw3_mock`'s layout: exports at `<repo>/data/eln/exports/ord` and tables at
    `<repo>/app/eln/real_data`, hence `parents[3]`. Returns `None` when the export directory is too
    shallow to derive from — the shipped relative default — so `main` can name the flag to pass.
    """
    try:
        root = ord_export_dir.parents[3]
    except IndexError:
        return None
    return root / "app" / "eln" / "real_data"


def _published_rows(real_data: Path, dataset: Dataset) -> list[dict[str, str]]:
    """Every row of one published factor table, verbatim."""
    path = real_data / dataset.csv_name
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _published_key(dataset: Dataset, row: dict[str, str]) -> tuple[Any, ...]:
    """The (dataset id, factors…, yield) tuple that identifies one published measurement.

    Compared as a multiset so duplicated and dropped rows are both visible; replicates legitimately
    repeat factor combinations.
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

    Type-agnostic because one dataset identifies a partner only by `NAME`. An absent input is data:
    no-ligand and no-base control conditions are blank cells, and treating absence as a key value
    checks that controls arrived as controls.
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

    `is not None`, never truthiness: a 0.00% yield is a real result, not "unknown".
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

    Multiset equality in both directions: one direction would pass duplication, a count would pass
    swapped yields.
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

    Counted across all datasets, since one falsy test anywhere on the path erases all of them.
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
        if _seeded_yield(payload, dataset.yield_places) == 0.0
    )
    return Check(
        name="zero yields survive seeding",
        passed=published == seeded_zeroes,
        observed=f"{published} published at exactly 0.00%, {seeded_zeroes} seeded",
    )


def check_adapter_matches_its_declaration(
    mapped: dict[str, list[OrdReaction]], refused: dict[str, int]
) -> list[Check]:
    """Each dataset maps, whole, into exactly the tier `_DATASETS` declares — no drift either way.

    A refused or demoted record is a regression; a citation-only record arriving structured is
    worse,
    since an invented structure would reach fingerprint indexes and similarity hits.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
        reactions = [r for name in dataset.dataset_ids() for r in mapped.get(name, ())]
        rejected = sum(refused.get(name, 0) for name in dataset.dataset_ids())
        tiers = Counter(reaction.tier for reaction in reactions)
        passed = bool(reactions) and rejected == 0 and set(tiers) == {dataset.tier}
        observed = f"{len(reactions)} mapped, {rejected} refused, " + ", ".join(
            f"{count} {tier.value}" for tier, count in sorted(tiers.items())
        )
        if dataset.tier_reason:
            observed += f" (declared {dataset.tier.value}: {dataset.tier_reason})"
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

    Compared against the CSV, with structures as the published strings, so any canonicalisation the
    adapter applied is a change this sees.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
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


def check_named_species_arrive_verbatim(
    real_data: Path, mapped: dict[str, list[OrdReaction]]
) -> list[Check]:
    """Every species the source only *names* arrives as that name, character for character.

    For the citation-only tier the paper's own text stands in for a structure, so any normalisation
    would change what the source said. Multiset equality against the published column; products are
    excluded because the published tables name none.
    """
    checks: list[Check] = []
    for dataset in _DATASETS:
        for _, column in dataset.named_only:
            want = Counter(row[column] for row in _published_rows(real_data, dataset))
            got = Counter(
                component.name
                for name in dataset.dataset_ids()
                for reaction in mapped.get(name, ())
                for component in reaction.unstructured
                if component.role is not Role.PRODUCT
            )
            checks.append(
                Check(
                    name=f"named species arrive verbatim · {dataset.csv_name} · {column}",
                    passed=bool(want) and want == got,
                    observed=(
                        f"{sum(want.values())} published names, {sum(got.values())} carried, "
                        f"{sum((want - got).values())} missing, {sum((got - want).values())} "
                        "not published"
                    ),
                )
            )
    return checks


def check_note_carries_the_number(mapped: dict[str, list[OrdReaction]]) -> Check:
    """The rendered note states the yield — including when the yield is zero.

    The note body is what reaches the index and the answer; a 0% record is chosen because a
    truthiness test loses it.
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
    bodies = {r.reaction_id: record_from_ord_reaction(r).body for r in (zero, nonzero)}
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
    """A condition stated only in prose reaches the record — as a **step**, not as a setpoint.

    `D-2026-08-26-a-transcription-may-not-infer-a-setpoint` forbids deriving a headline
    `temperature_c`/`time_h` from a procedure (the first number is usually the addition
    temperature),
    while `_segment_steps` records per-step values losslessly. Both halves are asserted: no step
    temperature means the prose was lost, and a headline setpoint never stated means the fallback is
    back.

    Only records whose prose states both a temperature and a time are checked, and the count is
    reported so a check matching nothing cannot pass silently.
    """
    adapter = JsonExportAdapter(str(eln_export_dir))
    raws = await adapter.fetch_new_entries(_EPOCH)
    checked = 0
    wrong: list[str] = []
    # One budget for the whole check, as a real sync runs it: the cost bounded is the page's total.
    with pattern_budget():
        for raw in raws:
            prose = str(raw.payload.get("procedure") or "")
            temperature = _PROSE_TEMPERATURE.search(prose)
            time_h = _PROSE_TIME.search(prose)
            if temperature is None or time_h is None:
                continue
            try:
                reaction = adapter.map_to_ord(raw)
            except PatternBudgetError:
                # Re-raised before the broad arm below: an exhausted page budget is a finding, and
                # skipping
                # entries would shrink the denominator silently.
                raise
            except Exception:
                continue
            checked += 1
            # `is not None`, not truthiness: "cooled to 0 °C" is a valid extraction.
            steps_carry = any(step.temperature_c is not None for step in reaction.steps) and any(
                step.duration_h is not None for step in reaction.steps
            )
            # A setpoint may be present only if the entry stated it in its own field, read from the
            # payload.
            stated = (
                raw.payload.get("temperature_c") is not None,
                raw.payload.get("time_h") is not None,
            )
            setpoint_invented = (
                reaction.temperature_c is not None and not stated[0],
                reaction.time_h is not None and not stated[1],
            )
            if not steps_carry:
                wrong.append(
                    f"{raw.entry_id}: prose states "
                    f"{(float(temperature.group(1)), float(time_h.group(1)))} and no step "
                    "carries both"
                )
            elif any(setpoint_invented):
                wrong.append(
                    f"{raw.entry_id}: headline setpoint "
                    f"{(reaction.temperature_c, reaction.time_h)} was derived from prose, which "
                    f"D-2026-08-26 forbids"
                )
    return Check(
        name="prose reaches the steps, and never the setpoint",
        passed=checked > 0 and not wrong,
        observed=(
            f"{checked - len(wrong)}/{checked} procedures state a temperature and a time in prose, "
            f"carry both on a step, and invent no setpoint"
            + (f" · first miss: {wrong[0]}" if wrong else "")
        ),
    )


async def check_corpus_is_reachable(mapped: dict[str, list[OrdReaction]]) -> Check:
    """The mapped records actually landed in `reaction_records` — asked of Postgres, not of a log.

    The id is the ELN's own `reaction_id` (the `reaction-` citation prefix is not stored). Counted
    per
    tier, since a record in the wrong tier is wrongly served by, or withheld from, structure search.
    """
    expected = Counter(
        (reaction.reaction_id, reaction.tier.value)
        for reactions in mapped.values()
        for reaction in reactions
    )
    if not expected:
        return Check(
            name="corpus is reachable", passed=False, observed="nothing mapped to look for"
        )
    async with db_connection(settings.postgres_dsn) as connection:
        cursor = await connection.execute(
            "SELECT reaction_id, tier FROM reaction_records WHERE reaction_id = ANY(%s)",
            ([reaction_id for reaction_id, _ in expected],),
        )
        rows = await cursor.fetchall()
    found = Counter((str(row[0]), str(row[1])) for row in rows)
    matched = expected & found
    by_tier = Counter(tier for _, tier in matched.elements())
    return Check(
        name="corpus is reachable",
        passed=matched == expected,
        observed=(
            f"{sum(matched.values())}/{sum(expected.values())} mapped ORD records are stored as "
            "reaction records in their declared tier ("
            + ", ".join(f"{count} {tier}" for tier, count in sorted(by_tier.items()))
            + ")"
        ),
    )


async def check_citation_only_is_not_structure_searchable(
    mapped: dict[str, list[OrdReaction]],
) -> Check:
    """No citation-only record has a row in any index a structure search reads.

    Asked of the tables, not a search: zero `reaction_fingerprints` and `reaction_labels` rows is
    the
    claim itself. A row predating an amendment to citation-only is not visible here;
    `ReactionRecordStore.structurally_withheld` keeps such a row unserved.
    """
    ids = [
        reaction.reaction_id
        for reactions in mapped.values()
        for reaction in reactions
        if reaction.tier is RecordTier.CITATION_ONLY
    ]
    if not ids:
        return Check(
            name="citation-only is not structure-searchable",
            passed=False,
            observed="no citation-only record mapped, so nothing was checked",
        )
    async with db_connection(settings.postgres_dsn) as connection:
        counts = []
        for statement in (
            "SELECT count(*) FROM reaction_fingerprints WHERE id = ANY(%s)",
            "SELECT count(*) FROM reaction_labels WHERE reaction_id = ANY(%s)",
        ):
            cursor = await connection.execute(statement, (ids,))
            row = await cursor.fetchone()
            counts.append(0 if row is None else int(row[0]))
    fingerprints, labels = counts
    return Check(
        name="citation-only is not structure-searchable",
        passed=fingerprints == 0 and labels == 0,
        observed=(
            f"{len(ids)} citation-only records: {fingerprints} reaction-fingerprint rows, "
            f"{labels} label rows"
        ),
    )


async def check_the_corpus_is_findable(mapped: dict[str, list[OrdReaction]]) -> Check:
    """A record that arrived can actually be found — asked through the tool a chemist would use.

    `find_similar_reactions` is the entry point behind the agent's `similar_reactions`. Ingest
    writes
    the record and fingerprint row but mints no note, so an unfiltered search finds wells while one
    filtered by `{"type": "reaction"}` returns none — both correct, and checked because nothing else
    states it. `index_empty` is asserted too: an empty index answering "no precedents" is the defect
    to
    catch.
    """
    subject = next(
        (
            reaction
            for reactions in mapped.values()
            for reaction in reactions
            if reaction.reaction_id.startswith("bh-amination")
        ),
        None,
    )
    if subject is None:
        return Check(
            name="the corpus is findable", passed=False, observed="no mapped record to search for"
        )
    from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
    from chemclaw.science.fingerprints.store import default_reaction_store

    search = await find_similar_reactions(default_reaction_store(), subject.reaction_smiles())
    found = {match.id for match in search.hits}
    return Check(
        name="the corpus is findable",
        passed=bool(search.hits) and not search.index_empty and subject.reaction_id in found,
        observed=(
            f"{len(search.hits)} similar reactions for {subject.reaction_id}, "
            f"index_empty={search.index_empty}, itself returned={subject.reaction_id in found}"
        ),
    )


# --- the backfill ----------------------------------------------------------------------------


async def backfill(timeout_seconds: float) -> str:
    """Run one `ElnSyncWorkflow` from the epoch, so the seeded corpus is reachable at all.

    ORD exports share one mtime and carry older payload timestamps, so the incremental cursor passes
    them on its first firing and never qualifies them again (`adapter.warn_late_arrivals`). Runs the
    real workflow on the real broker, so the production path is what is tested.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from chemclaw.core.temporal_client import connect as temporal_connect
    from chemclaw.durable.eln_sync import ElnSyncWorkflow

    client = await temporal_connect()
    # A fixed id, so a second invocation rejoins the running drain instead of racing it: the drain
    # is
    # long, `up.sh` starts one on every bring-up, and concurrent syncs only contend.
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
        # A drain still running is a state, not an error: it can take hours, and the reachability
        # check
        # below reports how far it got.
        return (
            f"{workflow_id}: still draining after {timeout_seconds:.0f}s — the workflow keeps "
            "running on the broker, so re-running this lane later reads the finished corpus"
        )
    return (
        f"{workflow_id}: ingested {summary.ingested} ({summary.citation_only} citation-only), "
        f"skipped {summary.skipped_existing}, rejected {summary.rejected}"
    )


# --- the run ---------------------------------------------------------------------------------


async def _map_corpus(
    export_dir: Path,
) -> tuple[dict[str, list[OrdReaction]], dict[str, int]]:
    """Run this repo's real ORD adapter over the whole seeded corpus; group results by dataset.

    The real adapter, so the mapping that ships is the one checked.
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
    # The same page-wide regex budget a real sync runs under, so this probe measures the shipped
    # bound rather than an unbounded variant of it — which is the whole point of a live check.
    with pattern_budget():
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

    Optionally starts the backfill, reads both corpora, then runs every check.
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
    run.checks.extend(check_named_species_arrive_verbatim(real_data, mapped))
    run.checks.append(check_note_carries_the_number(mapped))
    run.checks.append(await check_prose_yields_its_numbers(Path(settings.eln_export_dir)))
    if with_database:
        run.checks.append(await check_corpus_is_reachable(mapped))
        run.checks.append(await check_citation_only_is_not_structure_searchable(mapped))
        run.checks.append(await check_the_corpus_is_findable(mapped))

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
                citation_only=sum(
                    1
                    for name in ids
                    for reaction in mapped.get(name, ())
                    if reaction.tier is RecordTier.CITATION_ONLY
                ),
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
    lines.append(
        render_table(
            ["dataset", "published", "seeded", "mapped", "citation-only", "refused"],
            [
                [
                    reach.dataset,
                    str(reach.published),
                    str(reach.seeded),
                    str(reach.mapped),
                    str(reach.citation_only),
                    str(reach.refused),
                ]
                for reach in run.reach
            ],
            align="lrrrrr",
        )
    )
    lines += [
        "",
        render_table(
            ["check", "result", "observed"],
            [
                [check.name, "PASS" if check.passed else "**FAIL**", check.observed]
                for check in run.checks
            ],
        ),
    ]
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
    real_data = args.real_data or _default_real_data(export_dir)
    if real_data is None:
        parser.error(
            f"cannot derive the factor tables from ord_export_dir={str(export_dir)!r} — it is not "
            "inside a Chemclaw3_mock checkout. Pass --real-data pointing at "
            "Chemclaw3_mock/app/eln/real_data (this lane has no ground truth without them)"
        )
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

    # Imported here so this CLI does not load the probe lane's httpx/judge machinery.
    from chemclaw.cli.live_probes import run_output_dir

    # A directory per run, so reports never overwrite each other or a tracked file.
    destination = args.report or run_output_dir("corpus-fidelity") / "corpus-fidelity.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    print(f"\nwritten to {destination}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
