"""A concrete adapter for native Open Reaction Database messages (plan step 4.3, second source).

The **structured-recipe** counterpart to `eln.json_adapter`: it reads human-readable ORD
`Reaction` JSON (`*.json` in `settings.ord_export_dir`) and maps it into the same canonical
`OrdReaction`. Where the free-text adapter recovers a procedure from prose, ORD already
records it structurally — ordered `inputs` (with `addition_order`/`addition_time`),
`conditions`, and a `workups[]` sequence — so this adapter produces genuinely
**component-linked** steps: each addition and workup step knows exactly which species it
introduces, which prose segmentation cannot.

Only the subset Chemclaw consumes is read (structures, roles, amounts, headline
temperature + yield, the step sequence, the free-text procedure note). ORD JSON exported via
protobuf uses camelCase field names, while pbtxt-derived JSON uses snake_case; `_get` accepts
either so both round-trip. Nothing above this adapter knows ORD's shape (G6). One adapter per
source: this and the free-text adapter share only the `ElnAdapter` contract, not code.
"""

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.config import settings
from eln.adapter import ElnMappingError, RawEntry, parse_iso_utc
from eln.ord import Component, OrdReaction, ReactionStep, Role, StepKind

logger = logging.getLogger(__name__)

# ORD reaction-role -> our Role subset. Roles outside the subset (WORKUP,
# INTERNAL_STANDARD, AUTHENTIC_STANDARD) collapse to REAGENT: they are auxiliary species,
# and the reaction-input role only needs to be a valid non-product for the schema.
_ROLES: dict[str, Role] = {
    "REACTANT": Role.REACTANT,
    "REAGENT": Role.REAGENT,
    "SOLVENT": Role.SOLVENT,
    "CATALYST": Role.CATALYST,
    "PRODUCT": Role.PRODUCT,
}

# ORD ReactionWorkup.type -> step label. Types absent here (WAIT, TEMPERATURE, STIRRING,
# ADDITION, PH_ADJUST, DISSOLUTION, CUSTOM, ...) are ordinary process steps, not the
# distinctive purification/isolation actions, so they default to WORKUP.
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


class OrdFormatError(ElnMappingError):
    """A file did not match the ORD `Reaction` JSON shape (G4)."""


class OrdJsonAdapter:
    """Map a directory of ORD `Reaction` JSON files to `OrdReaction` records (an ELN adapter)."""

    def __init__(self, export_dir: str | None = None) -> None:
        """Read from the given directory, or the configured `ord_export_dir`."""
        self._dir = Path(export_dir if export_dir is not None else settings.ord_export_dir)

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return ORD messages created at or after `since`, oldest first.

        A file that cannot be read/parsed at all, or that carries no usable creation
        timestamp, is skipped (not raised): one broken file must not abort the batch (the
        same skip-and-continue stance as the free-text adapter). Such a file never reaches
        the sync report, so it is logged at WARNING here. Mapping failures on an
        otherwise-readable message surface later, per-entry, through the sync report.
        """
        entries: list[RawEntry] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    logger.warning("skipping ORD export %s: not a JSON object", path.name)
                    continue
                created = _created_at(payload)
            except (OSError, json.JSONDecodeError, OrdFormatError) as exc:
                logger.warning("skipping unreadable ORD export %s: %s", path.name, exc)
                continue
            if created >= since:
                entries.append(
                    RawEntry(
                        entry_id=str(_get(payload, "reaction_id", "reactionId") or path.stem),
                        created_at=created,
                        payload=payload,
                    )
                )
        entries.sort(key=lambda e: e.created_at)
        return entries

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one ORD message to the canonical `OrdReaction` (structured, step-linked).

        Any shape violation becomes an `OrdFormatError`, so the sync treats one bad message
        as a rejection rather than a crash (G4).
        """
        try:
            return _build(raw)
        except OrdFormatError:
            raise
        except (ValueError, ValidationError) as exc:
            raise OrdFormatError(f"entry {raw.entry_id!r}: cannot map ORD reaction: {exc}") from exc


def _build(raw: RawEntry) -> OrdReaction:
    """Assemble the canonical reaction from an ORD message (inputs, outcomes, steps)."""
    payload = raw.payload
    reaction_inputs = _inputs(payload)
    inputs = [component for _, components in reaction_inputs for component in components]
    if not inputs:
        raise OrdFormatError("ORD reaction has no input components")
    outcomes, yield_percent = _outcomes(payload)
    temperature_c = _temperature(_get(_conditions(payload), "temperature") or {})
    return OrdReaction(
        reaction_id=raw.entry_id,
        inputs=inputs,
        outcomes=outcomes,
        temperature_c=temperature_c,
        yield_percent=yield_percent,
        provenance=_provenance(payload),
        steps=_steps(reaction_inputs, temperature_c, payload),
        procedure_text=_procedure_text(payload),
    )


def _steps(
    reaction_inputs: list[tuple[dict[str, Any], list[Component]]],
    temperature_c: float | None,
    payload: dict[str, Any],
) -> list[ReactionStep]:
    """Build the ordered recipe: additions (by ORD order), the setpoint, then the workups."""
    steps: list[ReactionStep] = []
    for raw_input, components in sorted(reaction_inputs, key=_addition_order):
        names = ", ".join(c.smiles for c in components)
        steps.append(
            ReactionStep(
                index=len(steps) + 1,
                kind=StepKind.ADDITION,
                text=f"Add {names}",
                components=components,
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
    components = _components(_get(workup, "input") or {})
    return ReactionStep(
        index=index,
        kind=_WORKUP_KINDS.get(kind_name, StepKind.WORKUP),
        text=details,
        components=components,
        temperature_c=_temperature(_get(workup, "temperature") or {}),
        duration_h=_duration(_get(workup, "duration")),
    )


def _inputs(payload: dict[str, Any]) -> list[tuple[dict[str, Any], list[Component]]]:
    """Parse the `inputs` map into (raw ReactionInput, its components) pairs.

    The pair is kept so `_steps` can read each input's `addition_order`/`addition_time`
    while `_build` flattens the components into the reaction's input list.
    """
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise OrdFormatError("ORD reaction missing non-empty 'inputs'")
    pairs: list[tuple[dict[str, Any], list[Component]]] = []
    for value in raw_inputs.values():
        if not isinstance(value, dict):
            raise OrdFormatError(f"ReactionInput is not an object: {value!r}")
        pairs.append((value, _components(value, default_role=Role.REACTANT)))
    return pairs


def _components(
    reaction_input: dict[str, Any], default_role: Role = Role.REAGENT
) -> list[Component]:
    """Map an ORD `ReactionInput`'s `components` to canonical `Component`s (empty if none)."""
    components: list[Component] = []
    for compound in _as_list(reaction_input.get("components")):
        if not isinstance(compound, dict):
            raise OrdFormatError(f"component is not an object: {compound!r}")
        mass_mg, amount_mmol = _amount(_get(compound, "amount") or {})
        components.append(
            Component(
                smiles=_smiles(compound),
                role=_role(compound, default_role),
                mass_mg=mass_mg,
                amount_mmol=amount_mmol,
            )
        )
    return components


def _outcomes(payload: dict[str, Any]) -> tuple[list[Component], float | None]:
    """Map ORD `outcomes[].products[]` to product components + the first YIELD measurement."""
    products: list[Component] = []
    yield_percent: float | None = None
    for outcome in _optional_list(payload, "outcomes"):
        if not isinstance(outcome, dict):
            raise OrdFormatError(f"outcome is not an object: {outcome!r}")
        for product in _as_list(outcome.get("products")):
            if not isinstance(product, dict):
                raise OrdFormatError(f"product is not an object: {product!r}")
            products.append(Component(smiles=_smiles(product), role=Role.PRODUCT))
            yield_percent = yield_percent if yield_percent is not None else _yield(product)
    if not products:
        raise OrdFormatError("ORD reaction has no products")
    return products, yield_percent


def _smiles(compound: dict[str, Any]) -> str:
    """Return the compound's SMILES identifier, or raise if none is present."""
    for identifier in _as_list(compound.get("identifiers")):
        if isinstance(identifier, dict) and str(identifier.get("type", "")).upper() == "SMILES":
            value = identifier.get("value")
            if value:
                return str(value)
    raise OrdFormatError(f"compound has no SMILES identifier: {compound!r}")


def _role(compound: dict[str, Any], default: Role) -> Role:
    """Map a compound's ORD `reaction_role` to our subset (defaulting when unstated)."""
    name = str(_get(compound, "reaction_role", "reactionRole") or "").upper()
    return _ROLES.get(name, default) if name else default


def _yield(product: dict[str, Any]) -> float | None:
    """Read the first YIELD `ProductMeasurement`'s percentage value, if present."""
    for measurement in _as_list(product.get("measurements")):
        if isinstance(measurement, dict) and str(measurement.get("type", "")).upper() == "YIELD":
            percentage = measurement.get("percentage")
            if isinstance(percentage, dict) and percentage.get("value") is not None:
                return float(percentage["value"])
    return None


def _amount(amount: dict[str, Any]) -> tuple[float | None, float | None]:
    """Convert an ORD `Amount` to (mass_mg, amount_mmol); either or both may be absent."""
    return _measure(amount.get("mass"), _TO_MG), _measure(amount.get("moles"), _TO_MMOL)


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


def _created_at(payload: dict[str, Any]) -> datetime:
    """Parse the ORD record's creation time (`provenance.record_created.time.value`) as UTC.

    ORD stamps creation under `provenance`; a naive timestamp is read as UTC (see the
    free-text adapter for the same rationale). A missing/unparseable time raises
    `OrdFormatError`, so `fetch_new_entries` skips the file rather than mis-ordering it.
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


def _addition_order(pair: tuple[dict[str, Any], list[Component]]) -> tuple[int, str]:
    """Sort key for input additions: ORD `addition_order` first, then component SMILES.

    An input without an explicit order sorts last (a large sentinel) but stays deterministic
    via the SMILES tiebreak, so charge order is stable run to run.
    """
    raw_input, components = pair
    order = _get(raw_input, "addition_order", "additionOrder")
    smiles = components[0].smiles if components else ""
    return (int(order) if isinstance(order, int) else 1_000_000, smiles)


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
