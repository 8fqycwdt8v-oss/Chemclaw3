"""A concrete adapter for native Open Reaction Database messages (plan step 4.3, second source).

The **structured-recipe** counterpart to `chemclaw.ingest.eln.json_adapter`: it reads
human-readable ORD
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
from rdkit import Chem

from chemclaw.core.config import settings
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.ingest.eln.adapter import (
    ElnMappingError,
    RawEntry,
    entry_window,
    is_late_arrival,
    parse_iso_utc,
    warn_late_arrivals,
)
from chemclaw.ingest.eln.ord import Component, OrdReaction, ReactionStep, Role, StepKind
from chemclaw.ingest.rejections import record_refusals

logger = logging.getLogger(__name__)

# The source name a refusal is filed under when nobody says otherwise — a *fallback* for an
# adapter built by hand (the CLI's one-shot import, and tests), not this adapter's identity.
#
# **Every deployment path is told its name.** The ledger's `source` says whose data quality a row
# is a statement about, and the eviction cap is per source, so two ORD sources filing under one
# name would share a bucket and mis-attribute each other's refusals. This used to be a constant
# with no way in: the ingest half was built from `manifest.config` alone and never told which
# source it was, which made a hardcoded string the only thing that could be right — and only while
# exactly one manifest named this adapter. `registry._build_ingest_half` now passes the manifest's
# name, the same rule it already stated for a retrieve half and for a commitments half.
DEFAULT_LEDGER_SOURCE = "eln-ord"

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

    def __init__(self, export_dir: str | None = None, name: str | None = None) -> None:
        """Read from the given directory, or the configured `ord_export_dir`.

        `name` is the data source this adapter *is*, passed by the registry from the manifest and
        used as the rejection ledger's `source`. See `DEFAULT_LEDGER_SOURCE` for what an omitted
        one means.
        """
        self._dir = Path(export_dir if export_dir is not None else settings.ord_export_dir)
        self._source = name or DEFAULT_LEDGER_SOURCE

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return ORD messages created at or after `since`, oldest first.

        A file that cannot be read/parsed at all, or that carries no usable creation
        timestamp, is skipped (not raised): one broken file must not abort the batch (the
        same skip-and-continue stance as the free-text adapter). Such a file never reaches
        the sync report, so it is logged at WARNING here. Mapping failures on an
        otherwise-readable message surface later, per-entry, through the sync report.

        A message whose creation time predates `since` but whose file *arrived* after it is a late
        arrival: it is filtered out here and on every later run, so it is reported in one
        aggregated WARNING (`warn_late_arrivals`) instead of vanishing silently.

        **The refusals only this fetch can see are recorded in the rejection ledger**, so a chemist
        asking about a record that never arrived gets the reason instead of "I have no such record"
        (`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask`). Those are the two above:
        a file this adapter could not read, and a file that arrived too late to ever be fetched.
        Neither becomes a `RawEntry`, so nothing downstream can know they existed — which is the
        whole reason the recording happens here.

        **A message that cannot be *mapped* is recorded by the sync instead**, and that is a fix
        rather than a division of labour (`D-2026-08-29-a-bound-derived-twice-is-two-bounds`). This
        fetch is handed the *floor* — `since` minus the overlap window — and knows neither the run's
        cursor nor the chunk limit, so a pre-flight here can only guess which entries its caller
        will actually process. It guessed `entries[:eln_sync_batch_size]` and was short by the size
        of the overlap window on every chunk that had one, losing the ledger row for entries the
        drain refused and whose cursor it had already advanced past. `durable/eln_sync.py` records
        what `sync_entries` actually refused, which is the same set by construction and costs no
        second mapping pass at all.
        """
        entries: list[RawEntry] = []
        late: list[str] = []
        # entry id -> why it was refused. A dict, because one file is refused once per fetch and
        # the ledger is keyed the same way.
        refused: dict[str, str] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    logger.warning("skipping ORD export %s: not a JSON object", path.name)
                    refused[path.stem] = f"{path.name} is not a JSON object, so it is not a record"
                    continue
                created = _created_at(payload)
                # ORD's own `record_modified` list, if the exporter populates it: the record is
                # amended in place and `record_created` does not move, so creation time alone can
                # never bring a correction back into the fetch window.
                modified = _modified_at(payload)
            # `UnicodeDecodeError` is listed explicitly and is not covered by anything else here.
            # It derives from `ValueError`, not from `OSError`, and `json.JSONDecodeError` is a
            # *sibling* subclass rather than a parent — so one export written by a tool that emitted
            # latin-1 aborted the whole batch, contradicting this method's own skip-and-continue
            # contract and losing every later file in the directory along with it.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, OrdFormatError) as exc:
                logger.warning("skipping unreadable ORD export %s: %s", path.name, exc)
                # The file stem is the only id there is: the payload never parsed, so nothing in
                # it can be trusted to name the record.
                refused[path.stem] = f"unreadable ORD export {path.name}: {exc}"
                continue
            if entry_window(created, modified) >= since:
                entries.append(
                    RawEntry(
                        entry_id=str(_get(payload, "reaction_id", "reactionId") or path.stem),
                        created_at=created,
                        modified_at=modified,
                        payload=payload,
                    )
                )
            elif is_late_arrival(path, since):
                late.append(path.name)
                refused[path.stem] = (
                    f"{path.name} arrived after the sync cursor but carries an older timestamp "
                    f"({created.isoformat()}), so no scheduled run will fetch it; re-run the sync "
                    "from an explicit earlier `since` to backfill it"
                )
        warn_late_arrivals(logger, "ORD export", late)
        entries.sort(key=lambda e: e.created_at)
        await record_refusals(self._source, refused)
        return entries

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one ORD message to the canonical `OrdReaction` (structured, step-linked).

        Any shape violation becomes an `OrdFormatError`, so the sync treats one bad message
        as a rejection rather than a crash (G4). `TypeError` is caught alongside because a
        quantity whose `value` is an object/list fails inside `float`, not as a ValueError.
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
    return OrdReaction(
        reaction_id=raw.entry_id,
        inputs=inputs,
        outcomes=outcomes,
        temperature_c=temperature_c,
        yield_percent=yield_percent,
        purity_percent=purity_percent,
        # **No `performed_at`, deliberately** — see `json_adapter._build` for the argument. An
        # ORD record's only date is `provenance.record_created.time`, which is when the record was
        # written; mapping it here filled the field while leaving `date_source` claiming the source
        # had stated an experiment date. `adapter.DatedIngest` supplies both together.
        #
        # ORD models impurity profiles only indirectly (as further products or analyses), so
        # `impurities` stays empty here rather than guessing which co-product was unwanted — a
        # fabricated impurity profile would be worse than an absent one.
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
    if not isinstance(reaction_input, dict):
        return []
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


def _outcomes(payload: dict[str, Any]) -> tuple[list[Component], float | None, float | None]:
    """Map ORD `outcomes[].products[]` to components + the headline product's YIELD and PURITY."""
    products: list[Component] = []
    raw: list[dict[str, Any]] = []
    for outcome in _optional_list(payload, "outcomes"):
        if not isinstance(outcome, dict):
            raise OrdFormatError(f"outcome is not an object: {outcome!r}")
        for product in _as_list(outcome.get("products")):
            if not isinstance(product, dict):
                raise OrdFormatError(f"product is not an object: {product!r}")
            products.append(Component(smiles=_smiles(product), role=Role.PRODUCT))
            raw.append(product)
    if not products:
        raise OrdFormatError("ORD reaction has no products")
    headline = _headline_product(raw)
    if headline is None:
        return products, None, None
    return products, _percentage(headline, "YIELD"), _percentage(headline, "PURITY")


def _headline_product(products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one product the reaction's `yield_percent`/`purity_percent` are about, or `None`.

    **The source's own marking beats a positional one, which is why this function exists.** This
    used to be "the first product that states a YIELD", and with several products that is a
    position in an export's array being read as a claim about chemistry: an ORD record listing the
    des-ethyl by-product at 12% ahead of the desired ester at 85% transcribed the reaction's yield
    as 12% — into `conditions.yield_percent`, the number every comparison renders, and into the
    body's `- yield:` bullet. `record.py::_principal_product` already refuses to *name* a compound
    in exactly this situation ("a wrong `compound_smiles` is worse than none: it is what a
    by-compound search would return, and it would look right"); the number carried no such guard,
    so the record named no product while confidently asserting one product's figure. A chemist
    reads that figure as precedent.

    `ProductCompound.is_desired_product` is ORD's field for this and this adapter ignored it
    entirely. Reading it is a **transcription** — the source stating which compound the run was
    for — while ordering by array position is an *inference* this seam has no standing to make, and
    `D-2026-08-25-an-eln-transcription-is-data-not-a-claim` is the rule that separates the two.

    Only an actual JSON `true` is a marking; an explicit `false` and an absent field are both
    "unmarked", which is what ORD's own default means, and any other value falls through to the
    rules below rather than being coerced — the failure mode of that strictness is `None`, never a
    wrong number.

    Unmarked, the honest answers in order:

    - **one product** — unambiguous by construction, and the overwhelmingly common export;
    - **exactly one product stating a YIELD** — the other products are by-products the source did
      not measure, so there is still only one candidate;
    - **anything else** — `None`, and the record carries no headline yield. A reader then sees that
      the figure was not recorded, which is true, rather than a figure belonging to a compound
      nobody chose.

    One product decides **both** figures rather than each being resolved on its own. Read
    separately, a two-product record where A states the YIELD and B the PURITY produced a headline
    pair describing two different compounds — the same fabrication one level down, and harder to
    see because each half is individually a real measurement.
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
    measured = [p for p in products if _percentage(p, "YIELD") is not None]
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


def _smiles(compound: dict[str, Any]) -> str:
    """Resolve a compound to SMILES from any identifier ORD allows, or raise.

    ORD's `CompoundIdentifier` is a union — a real submission may carry `INCHI` or only a `NAME`,
    and requiring `SMILES` discarded whole reactions over one component. Measured against the
    public corpora: of 10,011 ORD records, **5,761 were refused**, all of them the Perera
    Suzuki–Miyaura flow set (Science 2018, 359, 429), whose second coupling partner the source
    spreadsheet publishes only as a `NAME`. That is 57% of a real corpus lost, including the yield
    data on components that *were* resolvable.

    The order is by decreasing certainty, and every step is an exact lookup:

    1. `SMILES` — the structure, stated.
    2. `INCHI` — also the structure, in another notation. RDKit is already a dependency and the
       conversion is exact, so refusing it was never a safety property, only a missing branch.
    3. `NAME` / `IUPAC_NAME` — resolved through `chemclaw.core.reagents`, the same table
       `resolve_compound` serves the agent from. It returns `None` on an unknown spelling rather
       than guessing, which is what keeps this a lookup and not an inference.

    Still raises when nothing resolves. Refusing to invent a structure is the point (a fabricated
    one propagates silently into a fingerprint index, a similarity hit and eventually a proposed
    note); what changes is that refusal now follows an actual attempt.
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
        if kind in {"NAME", "IUPAC_NAME"}:
            resolved = resolve_compound_name(value)
            if resolved is not None:
                return resolved.smiles

    raise OrdFormatError(f"compound has no resolvable structure identifier: {compound!r}")


def _role(compound: dict[str, Any], default: Role) -> Role:
    """Map a compound's ORD `reaction_role` to our subset (defaulting only when unstated).

    A *stated* role outside the subset (WORKUP, INTERNAL_STANDARD, AUTHENTIC_STANDARD)
    collapses to REAGENT, per the `_ROLES` rationale: an auxiliary species must never read
    as a true REACTANT — `chemclaw.memory.chains` keys causal product→reactant edges on REACTANT,
    so mis-labeling an internal standard would fabricate handoffs that never happened.
    """
    name = str(_get(compound, "reaction_role", "reactionRole") or "").upper()
    if not name:
        return default
    return _ROLES.get(name, Role.REAGENT)


def _percentage(product: dict[str, Any], measurement_type: str) -> float | None:
    """Read the first `ProductMeasurement` of `measurement_type` as a percentage, if present.

    Generalized from the YIELD-only reader so PURITY rides the identical path (gap KNW-2, DRY):
    ORD models both as a `ProductMeasurement` with a `percentage`, so one reader is correct for
    both and a third measurement type costs one call site.
    """
    wanted = measurement_type.upper()
    for measurement in _as_list(product.get("measurements")):
        if isinstance(measurement, dict) and str(measurement.get("type", "")).upper() == wanted:
            percentage = measurement.get("percentage")
            if isinstance(percentage, dict) and percentage.get("value") is not None:
                return float(percentage["value"])
    return None


def _amount(amount: dict[str, Any]) -> tuple[float | None, float | None]:
    """Convert an ORD `Amount` to (mass_mg, amount_mmol); either or both may be absent."""
    if not isinstance(amount, dict):
        return None, None
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


def _modified_at(payload: dict[str, Any]) -> datetime | None:
    """The newest `provenance.record_modified[*].time.value`, or None when the record has none.

    ORD models amendments as a *list*, so the newest entry is the one that decides whether this
    record has changed since the sync last looked. Unparseable members are ignored rather than
    raised: an exporter that writes a malformed modification stamp should not make the whole
    record unreadable, and the overlap replay still catches the change on its own timescale.
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
