"""A concrete adapter for native Open Reaction Database messages.

Reads human-readable ORD `Reaction` JSON (`*.json` in `settings.ord_export_dir`) into the canonical
`OrdReaction`. ORD records the procedure structurally (ordered `inputs` with
`addition_order`/`addition_time`, `conditions`, `workups[]`), so steps here are component-linked,
which prose segmentation cannot achieve.

Only the subset Chemclaw consumes is read. `_get` accepts both camelCase (protobuf JSON) and
snake_case (pbtxt-derived) field names. Shares only the `ElnAdapter` contract with the free-text
adapter.
"""

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError
from rdkit import Chem

from chemclaw.core.config import settings
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.ingest.eln.adapter import (
    ElnMappingError,
    RawEntry,
    entry_id_or_stem,
    entry_window,
    is_late_arrival,
    parse_iso_utc,
    refuse_colliding_ids,
    warn_late_arrivals,
)
from chemclaw.ingest.eln.ord import (
    Component,
    OrdReaction,
    ReactionStep,
    Role,
    StepKind,
    UnstructuredComponent,
)
from chemclaw.ingest.rejections import record_refusals

logger = logging.getLogger(__name__)

# The source name a refusal is filed under when no name is passed — a fallback for hand-built
# adapters (CLI one-shot import, tests). Deployments pass the manifest's name via
# `registry._build_ingest_half`, so two ORD sources keep separate ledgers and eviction caps.
DEFAULT_LEDGER_SOURCE = "eln-ord"

# ORD reaction-role -> our Role subset. WORKUP, INTERNAL_STANDARD and AUTHENTIC_STANDARD collapse to
# REAGENT: auxiliary species that only need to be a valid non-product.
_ROLES: dict[str, Role] = {
    "REACTANT": Role.REACTANT,
    "REAGENT": Role.REAGENT,
    "SOLVENT": Role.SOLVENT,
    "CATALYST": Role.CATALYST,
    "PRODUCT": Role.PRODUCT,
}

# ORD ReactionWorkup.type -> step label. Unlisted types (WAIT, TEMPERATURE, STIRRING, ...) are
# ordinary process steps and default to WORKUP.
_WORKUP_KINDS: dict[str, StepKind] = {
    "FILTRATION": StepKind.PURIFICATION,
    "DISTILLATION": StepKind.PURIFICATION,
}

# Unit conversions to the canonical units (temperature °C, duration h, mass mg, amount mmol).
_TO_CELSIUS: dict[str, Any] = {
    "CELSIUS": lambda v: v,
    "FAHRENHEIT": lambda v: (v - 32.0) * 5.0 / 9.0,
    "KELVIN": lambda v: v - 273.15,
}
_TO_HOURS: dict[str, float] = {"HOUR": 1.0, "MINUTE": 1 / 60, "SECOND": 1 / 3600, "DAY": 24.0}
_TO_MG: dict[str, float] = {"KILOGRAM": 1e6, "GRAM": 1e3, "MILLIGRAM": 1.0, "MICROGRAM": 1e-3}
_TO_MMOL: dict[str, float] = {"MOLE": 1e3, "MILLIMOLE": 1.0, "MICROMOLE": 1e-3, "NANOMOLE": 1e-6}
_TO_ML: dict[str, float] = {"LITER": 1e3, "MILLILITER": 1.0, "MICROLITER": 1e-3, "NANOLITER": 1e-6}

# The keys ORD's `Amount` may carry (a `oneof` over `mass | moles | volume | unmeasured`, plus the
# volume qualifier). All are read; anything else is refused by name rather than becoming no amount.
_AMOUNT_KINDS = frozenset({"mass", "moles", "volume", "unmeasured", "volume_includes_solutes"})


# A species as this adapter maps it: a structure, or the name the source gave in place of one.
Species = Component | UnstructuredComponent

# The identifier kinds that are a *name* — resolved through the reagent table when it knows the
# spelling, and otherwise carried verbatim as an `UnstructuredComponent`.
_NAME_KINDS = frozenset({"NAME", "IUPAC_NAME"})


class OrdFormatError(ElnMappingError):
    """A file did not match the ORD `Reaction` JSON shape (G4)."""


class OrdJsonAdapter:
    """Map a directory of ORD `Reaction` JSON files to `OrdReaction` records (an ELN adapter)."""

    def __init__(self, export_dir: str | None = None, name: str | None = None) -> None:
        """Read from the given directory, or the configured `ord_export_dir`.

        `name` is the data source this adapter is, used as the rejection ledger's `source`; see
        `DEFAULT_LEDGER_SOURCE`.
        """
        self._dir = Path(export_dir if export_dir is not None else settings.ord_export_dir)
        self._source = name or DEFAULT_LEDGER_SOURCE

    async def fetch_new_entries(
        self, since: datetime, limit: int | None = None, *, report_late_arrivals: bool = True
    ) -> list[RawEntry]:
        """Return ORD messages created at or after `since`, oldest first.

        An unreadable file, or one without a usable creation timestamp, is skipped with a WARNING
        and written to the rejection ledger, as is a late arrival (created before `since`, arrived
        after it); neither becomes a `RawEntry`, so this is the only place they can be recorded.
        Mapping failures are recorded by `durable/eln_sync.py` from what `sync_entries` actually
        refused, since this fetch does not know the run's cursor or chunk limit.

        The directory read runs in a thread, off the worker's event loop. `limit` is ignored for the
        reason `JsonExportAdapter.fetch_new_entries` gives.

        Args:
            since: The window floor; messages at or after it are returned.
            limit: Accepted for the protocol and unused — see above.
            report_late_arrivals: Whether `since` is the run's floor, so a late-arriving file behind
            it may be reported. False on a continuation chunk — see `is_late_arrival`.
        """
        entries, late, refused = await asyncio.to_thread(self._scan, since, report_late_arrivals)
        # Named by the source, not the format, so two ORD drop directories log distinguishable
        # lines.
        warn_late_arrivals(logger, self._source, late)
        await record_refusals(self._source, refused)
        return entries

    def _scan(
        self, since: datetime, report_late_arrivals: bool
    ) -> tuple[list[RawEntry], list[str], dict[str, str]]:
        """The whole blocking read, in one synchronous function so one thread can hold it.

        Args:
            report_late_arrivals: whether `since` is the run's floor — see `is_late_arrival`.
            since: the window floor; messages at or after it are returned.

        Returns:
            The entries in the window oldest first, the names of the late arrivals, and the refusals
            to file.
        """
        entries: list[RawEntry] = []
        late: list[str] = []
        # entry id -> why it was refused. A dict, because one file is refused once per fetch and
        # the ledger is keyed the same way.
        refused: dict[str, str] = {}
        # Every id this directory claims, and the files claiming it — see `refuse_colliding_ids`.
        files_by_id: dict[str, list[str]] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    logger.warning("skipping ORD export %s: not a JSON object", path.name)
                    refused[path.stem] = f"{path.name} is not a JSON object, so it is not a record"
                    continue
                created = _created_at(payload)
                # ORD's own `record_modified` list: an amended record keeps `record_created`, so
                # creation time alone would never re-fetch a correction.
                modified = _modified_at(payload)
                entry_id = entry_id_or_stem(
                    _get(payload, "reaction_id", "reactionId"), path, "reaction_id"
                )
            # `UnicodeDecodeError` must be listed: it is neither an `OSError` nor a
            # `JSONDecodeError`, and a non-UTF-8 file would otherwise abort the batch.
            # `ElnMappingError` covers `entry_id_or_stem`'s refusal.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ElnMappingError) as exc:
                logger.warning("skipping ORD export %s: %s", path.name, exc)
                # The file stem is the only id there is: the payload never parsed, so nothing in
                # it can be trusted to name the record.
                refused[path.stem] = f"refused ORD export {path.name}: {exc}"
                continue
            files_by_id.setdefault(entry_id, []).append(path.name)
            if entry_window(created, modified) >= since:
                entries.append(
                    RawEntry(
                        entry_id=entry_id,
                        created_at=created,
                        modified_at=modified,
                        payload=payload,
                    )
                )
            elif report_late_arrivals and is_late_arrival(path, since):
                late.append(path.name)
                refused[path.stem] = (
                    f"{path.name} arrived after the sync cursor but carries an older timestamp "
                    f"({created.isoformat()}), so no scheduled run will fetch it; re-run the sync "
                    "from an explicit earlier `since` to backfill it"
                )
        collisions = refuse_colliding_ids(logger, self._source, files_by_id)
        entries = [entry for entry in entries if entry.entry_id not in collisions]
        refused.update(collisions)
        entries.sort(key=lambda e: e.created_at)
        return entries, late, refused

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one ORD message to the canonical `OrdReaction` (structured, step-linked).

        Any shape violation becomes an `OrdFormatError`, so the sync rejects the message rather than
        crashing. `TypeError` is caught too, since an object where a number belongs fails inside
        `float`.
        """
        try:
            return _build(raw)
        except OrdFormatError:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise OrdFormatError(f"entry {raw.entry_id!r}: cannot map ORD reaction: {exc}") from exc


def _build(raw: RawEntry) -> OrdReaction:
    """Assemble the canonical reaction from an ORD message (inputs, outcomes, steps)."""
    payload = raw.payload
    reaction_inputs = _inputs(payload)
    inputs = [component for _, components in reaction_inputs for component in components]
    if not inputs:
        raise OrdFormatError("ORD reaction has no input components")
    outcomes, yield_percent, purity_percent = _outcomes(payload)
    temperature_c = _temperature(_get(_conditions(payload), "temperature") or {})
    species = [*inputs, *outcomes]
    return OrdReaction(
        reaction_id=raw.entry_id,
        inputs=[c for c in inputs if isinstance(c, Component)],
        outcomes=[c for c in outcomes if isinstance(c, Component)],
        # A species named without a structure goes here whatever its role, which is what makes the
        # record citation-only — see `UnstructuredComponent` for why nothing resolves it further.
        unstructured=[c for c in species if isinstance(c, UnstructuredComponent)],
        temperature_c=temperature_c,
        yield_percent=yield_percent,
        purity_percent=purity_percent,
        # No `performed_at`: ORD's only date is when the record was written; `adapter.DatedIngest`
        # supplies it with `date_source="entry"`.
        # ORD models impurities only indirectly, so `impurities` stays empty rather than guessing
        # which co-product was unwanted.
        provenance=_provenance(payload),
        steps=_steps(reaction_inputs, temperature_c, payload),
        procedure_text=_procedure_text(payload),
    )


def _steps(
    reaction_inputs: list[tuple[dict[str, Any], list[Species]]],
    temperature_c: float | None,
    payload: dict[str, Any],
) -> list[ReactionStep]:
    """Build the ordered recipe: additions (by ORD order), the setpoint, then the workups."""
    steps: list[ReactionStep] = []
    for raw_input, components in sorted(reaction_inputs, key=_addition_order):
        names = ", ".join(_label(c) for c in components)
        steps.append(
            ReactionStep(
                index=len(steps) + 1,
                kind=StepKind.ADDITION,
                text=f"Add {names}",
                # A step links the species it can name *as a structure*; a named-only one is in the
                # step's text as the source gave it and in `OrdReaction.unstructured`.
                components=[c for c in components if isinstance(c, Component)],
                duration_h=_duration(_get(raw_input, "addition_time", "additionTime")),
            )
        )
    if temperature_c is not None:
        steps.append(
            ReactionStep(
                index=len(steps) + 1,
                kind=StepKind.TEMPERATURE,
                text=f"Hold at {temperature_c} °C",
                temperature_c=temperature_c,
            )
        )
    for workup in _optional_list(payload, "workups"):
        steps.append(_workup_step(workup, len(steps) + 1))
    return steps


def _workup_step(workup: dict[str, Any], index: int) -> ReactionStep:
    """Map one ORD `ReactionWorkup` to a step (its type, detail text, reagents, timing)."""
    if not isinstance(workup, dict):
        raise OrdFormatError(f"workup is not an object: {workup!r}")
    kind_name = str(workup.get("type", "")).upper()
    details = str(workup.get("details", "")) or kind_name.title() or "Workup"
    components: list[Component] = []
    for species in _components(_get(workup, "input") or {}):
        if not isinstance(species, Component):
            # A workup reagent is not a reaction species and carries no tier, so a named-only one
            # stays the refusal it always was rather than widening what a step may hold.
            raise OrdFormatError(
                f"workup species {species.name!r} has no resolvable structure identifier"
            )
        components.append(species)
    return ReactionStep(
        index=index,
        kind=_WORKUP_KINDS.get(kind_name, StepKind.WORKUP),
        text=details,
        components=components,
        temperature_c=_temperature(_get(workup, "temperature") or {}),
        duration_h=_duration(_get(workup, "duration")),
    )


def _inputs(payload: dict[str, Any]) -> list[tuple[dict[str, Any], list[Species]]]:
    """Parse the `inputs` map into (raw ReactionInput, its components) pairs.

    The raw input is kept so `_steps` can read `addition_order`/`addition_time`.
    """
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise OrdFormatError("ORD reaction missing non-empty 'inputs'")
    pairs: list[tuple[dict[str, Any], list[Species]]] = []
    for value in raw_inputs.values():
        if not isinstance(value, dict):
            raise OrdFormatError(f"ReactionInput is not an object: {value!r}")
        pairs.append((value, _components(value, default_role=Role.REACTANT)))
    return pairs


def _components(reaction_input: dict[str, Any], default_role: Role = Role.REAGENT) -> list[Species]:
    """Map an ORD `ReactionInput`'s `components` to canonical species (empty if none)."""
    if not isinstance(reaction_input, dict):
        return []
    components: list[Species] = []
    for compound in _as_list(reaction_input.get("components")):
        if not isinstance(compound, dict):
            raise OrdFormatError(f"component is not an object: {compound!r}")
        components.append(_species(compound, _role(compound, default_role)))
    return components


def _species(compound: dict[str, Any], role: Role, *, charged: bool = True) -> Species:
    """One ORD `Compound` as a structured `Component`, or as the name the source gave it.

    A structure when any identifier resolves (`_smiles`); otherwise an `UnstructuredComponent`
    carrying the source's `NAME` verbatim, making the reaction citation-only
    (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable). A compound with neither
    is refused. `charged=False` for a product, which records measurements, not a charge.
    """
    common: dict[str, Any] = {"role": role}
    if charged:
        amount = _amount(_get(compound, "amount") or {})
        common.update(
            mass_mg=amount.mass_mg,
            amount_mmol=amount.amount_mmol,
            volume_ml=amount.volume_ml,
            # Not under `amount`: no amount was recorded. This carries the source's reason it was
            # deliberately not measured.
            attributes=({"amount_unmeasured": amount.unmeasured} if amount.unmeasured else {}),
        )
    smiles = _smiles(compound)
    if smiles is not None:
        return Component(smiles=smiles, **common)
    name = _given_name(compound)
    if name is None:
        raise OrdFormatError(f"compound has no resolvable structure identifier: {compound!r}")
    return UnstructuredComponent(name=name, **common)


def _outcomes(payload: dict[str, Any]) -> tuple[list[Species], float | None, float | None]:
    """Map ORD `outcomes[].products[]` to species + the headline product's YIELD and PURITY."""
    products: list[Species] = []
    raw: list[dict[str, Any]] = []
    for outcome in _optional_list(payload, "outcomes"):
        if not isinstance(outcome, dict):
            raise OrdFormatError(f"outcome is not an object: {outcome!r}")
        for product in _as_list(outcome.get("products")):
            if not isinstance(product, dict):
                raise OrdFormatError(f"product is not an object: {product!r}")
            products.append(_species(product, Role.PRODUCT, charged=False))
            raw.append(product)
    if not products:
        raise OrdFormatError("ORD reaction has no products")
    headline = _headline_product(raw)
    if headline is None:
        return products, None, None
    return products, _percentage(headline, "YIELD"), _percentage(headline, "PURITY")


def _headline_product(products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one product the reaction's `yield_percent`/`purity_percent` are about, or `None`.

    The source's marking beats position: array order is not a claim about chemistry, and reading a
    by-product's yield as the reaction's would mislead as precedent. In order:

    - a product with `is_desired_product` exactly JSON `true` (a transcription, not an inference);
    - the only product, when there is one;
    - the only product the source measured (YIELD or PURITY);
    - otherwise `None`, and the record carries no headline figures.

    One product decides both figures, so a yield and a purity never describe two different
    compounds.
    """
    marked = [p for p in products if _get(p, "is_desired_product", "isDesiredProduct") is True]
    if len(marked) == 1:
        return marked[0]
    if marked:
        # Several marked desired: the source contradicts itself, so it has stated nothing this can
        # read. Falling through to the yield count would silently re-introduce the positional pick.
        return None
    if len(products) == 1:
        return products[0]
    measured = [
        p
        for p in products
        if _percentage(p, "YIELD") is not None or _percentage(p, "PURITY") is not None
    ]
    return measured[0] if len(measured) == 1 else None


def _identifiers(compound: dict[str, Any]) -> list[tuple[str, str]]:
    """The compound's `(TYPE, value)` identifier pairs, uppercased and non-empty."""
    pairs: list[tuple[str, str]] = []
    for identifier in _as_list(compound.get("identifiers")):
        if not isinstance(identifier, dict):
            continue
        value = identifier.get("value")
        if value:
            pairs.append((str(identifier.get("type", "")).upper(), str(value)))
    return pairs


def _smiles(compound: dict[str, Any]) -> str | None:
    """Resolve a compound to SMILES from any identifier ORD allows, or `None` when none resolves.

    Requiring `SMILES` would discard real records that carry only `INCHI` or `NAME`. In decreasing
    certainty, each an exact lookup:

    1. `SMILES` — the structure, stated.
    2. `INCHI` — the structure in another notation, converted exactly by RDKit.
    3. `NAME` / `IUPAC_NAME` — resolved through `chemclaw.core.reagents`, which returns `None` on an
       unknown spelling rather than guessing.

    A structure is never invented; `_species` carries an unresolved compound as its name.
    """
    identifiers = _identifiers(compound)
    for wanted in ("SMILES",):
        for kind, value in identifiers:
            if kind == wanted:
                return value

    for kind, value in identifiers:
        if kind == "INCHI":
            mol = Chem.MolFromInchi(value)
            if mol is not None:
                return str(Chem.MolToSmiles(mol))

    for kind, value in identifiers:
        if kind in _NAME_KINDS:
            resolved = resolve_compound_name(value)
            if resolved is not None:
                return resolved.smiles
    return None


def _given_name(compound: dict[str, Any]) -> str | None:
    """The first name the source gave the compound, verbatim, or `None` when it gave none."""
    return next((value for kind, value in _identifiers(compound) if kind in _NAME_KINDS), None)


def _label(species: Species) -> str:
    """How a species is written in a procedure line: its SMILES, or the name the source gave it."""
    return species.smiles if isinstance(species, Component) else species.name


def _role(compound: dict[str, Any], default: Role) -> Role:
    """Map a compound's ORD `reaction_role` to our subset (defaulting only when unstated).

    A stated role outside the subset collapses to REAGENT, never REACTANT: `chemclaw.memory.chains`
    keys product->reactant edges on REACTANT, so a mislabelled standard would fabricate handoffs.
    """
    name = str(_get(compound, "reaction_role", "reactionRole") or "").upper()
    if not name:
        return default
    return _ROLES.get(name, Role.REAGENT)


def _percentage(product: dict[str, Any], measurement_type: str) -> float | None:
    """Read the first `ProductMeasurement` of `measurement_type` as a percentage, if present.

    ORD models YIELD and PURITY alike, so one reader serves both.
    """
    wanted = measurement_type.upper()
    for measurement in _as_list(product.get("measurements")):
        if isinstance(measurement, dict) and str(measurement.get("type", "")).upper() == wanted:
            percentage = measurement.get("percentage")
            if isinstance(percentage, dict) and percentage.get("value") is not None:
                return float(percentage["value"])
    return None


class _Charged(NamedTuple):
    """What one ORD `Amount` says was charged, in this record's canonical units."""

    mass_mg: float | None
    amount_mmol: float | None
    volume_ml: float | None
    #: The source's statement that the amount was deliberately not measured (its
    #: `UnmeasuredAmount.type`), carried into `Component.attributes`.
    unmeasured: str | None


def _amount(amount: dict[str, Any]) -> _Charged:
    """Convert an ORD `Amount` to what this record keeps of it, in mg, mmol and mL.

    Every kind of the `oneof` is read: a volumetric charge (neat liquids, solvents) must not become
    "no amount", which would under-report the record's scale. `unmeasured` is carried as an
    attribute rather than refused, since a catalytic or saturated charge is a real statement. An
    `Amount` with none of the known kinds is refused by name, so a kind ORD adds later is never
    silently dropped.
    """
    if not isinstance(amount, dict):
        return _Charged(None, None, None, None)
    unmeasured = amount.get("unmeasured")
    stated = _Charged(
        mass_mg=_measure(amount.get("mass"), _TO_MG),
        amount_mmol=_measure(amount.get("moles"), _TO_MMOL),
        volume_ml=_measure(amount.get("volume"), _TO_ML),
        unmeasured=(
            str(unmeasured.get("type") or "unspecified").lower()
            if isinstance(unmeasured, dict)
            else None
        ),
    )
    if amount and not any(stated) and not (set(amount) & _AMOUNT_KINDS):
        raise OrdFormatError(
            f"amount {sorted(amount)} states none of {sorted(_AMOUNT_KINDS)}, so what was charged "
            "cannot be read. A recorded amount this ingest drops is a run that reads as smaller "
            "than it was"
        )
    return stated


def _measure(value: Any, factors: dict[str, float]) -> float | None:
    """Convert an ORD `{value, units}` quantity to its canonical unit via `factors`."""
    if not isinstance(value, dict) or value.get("value") is None:
        return None
    units = str(value.get("units", "")).upper()
    if units not in factors:
        raise OrdFormatError(f"unknown units {units!r}")
    return float(value["value"]) * factors[units]


def _temperature(temperature: dict[str, Any]) -> float | None:
    """Convert an ORD temperature (`{setpoint|value, units}`) to °C, or `None` if absent."""
    setpoint = temperature.get("setpoint") if "setpoint" in temperature else temperature
    if not isinstance(setpoint, dict) or setpoint.get("value") is None:
        return None
    units = str(setpoint.get("units", "")).upper()
    if units not in _TO_CELSIUS:
        raise OrdFormatError(f"unknown temperature units {units!r}")
    return float(_TO_CELSIUS[units](float(setpoint["value"])))


def _duration(duration: Any) -> float | None:
    """Convert an ORD `Time` (`{value, units}`) to hours, or `None` if absent."""
    return _measure(duration, _TO_HOURS)


def _conditions(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the `conditions` sub-message (empty dict if absent)."""
    conditions = payload.get("conditions")
    return conditions if isinstance(conditions, dict) else {}


def _procedure_text(payload: dict[str, Any]) -> str | None:
    """Return the free-text `notes.procedure_details`, preserved verbatim, if present."""
    notes = payload.get("notes")
    if not isinstance(notes, dict):
        return None
    details = _get(notes, "procedure_details", "procedureDetails")
    return str(details) if details else None


def _provenance(payload: dict[str, Any]) -> str:
    """Build the provenance string from the record's creator, or a stable fallback."""
    created = _get(_provenance_msg(payload), "record_created", "recordCreated") or {}
    person = created.get("person") if isinstance(created, dict) else None
    if isinstance(person, dict):
        who = person.get("name") or person.get("username") or person.get("orcid")
        if who:
            return f"ord:{who}"
    return "ord:unknown"


def _provenance_msg(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the `provenance` sub-message (empty dict if absent)."""
    provenance = payload.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _modified_at(payload: dict[str, Any]) -> datetime | None:
    """The newest `provenance.record_modified[*].time.value`, or None when the record has none.

    Unparseable members are ignored rather than raised, so a bad stamp does not make the record
    unreadable; the overlap replay still catches the change.
    """
    records = _get(_provenance_msg(payload), "record_modified", "recordModified")
    if not isinstance(records, list):
        return None
    stamps: list[datetime] = []
    for record in records:
        time = record.get("time") if isinstance(record, dict) else None
        value = time.get("value") if isinstance(time, dict) else None
        if not isinstance(value, str):
            continue
        try:
            stamps.append(parse_iso_utc(value))
        except ValueError:
            continue
    return max(stamps) if stamps else None


def _created_at(payload: dict[str, Any]) -> datetime:
    """Parse the ORD record's creation time (`provenance.record_created.time.value`) as UTC.

    A naive timestamp is read as UTC. A missing or unparseable time raises `OrdFormatError`, so the
    file is skipped rather than mis-ordered.
    """
    created = _get(_provenance_msg(payload), "record_created", "recordCreated") or {}
    time = created.get("time") if isinstance(created, dict) else None
    value = time.get("value") if isinstance(time, dict) else None
    if not isinstance(value, str):
        raise OrdFormatError("ORD reaction missing 'provenance.record_created.time'")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        raise OrdFormatError(f"bad record_created time {value!r}: {exc}") from exc


def _addition_order(pair: tuple[dict[str, Any], list[Species]]) -> tuple[int, str]:
    """Sort key for input additions: ORD `addition_order` first, then component SMILES (or name).

    Inputs without an order sort last, deterministically, so charge order is stable run to run.
    """
    raw_input, components = pair
    order = _get(raw_input, "addition_order", "additionOrder")
    label = _label(components[0]) if components else ""
    return (int(order) if isinstance(order, int) else 1_000_000, label)


def _get(mapping: dict[str, Any], *names: str) -> Any:
    """First present key among `names` (tolerates ORD's snake_case vs. camelCase JSON)."""
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _optional_list(payload: dict[str, Any], key: str) -> list[Any]:
    """Return an optional list field as a list (empty when absent), else raise on a non-list."""
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise OrdFormatError(f"{key!r} is not a list")
    return value


def _as_list(value: Any) -> Iterable[Any]:
    """Yield items of a list field, or nothing when it is absent/not a list."""
    return value if isinstance(value, list) else []
