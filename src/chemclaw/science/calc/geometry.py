"""One walk over a calculation payload, used twice: keep the geometries, then take them out.

A model cannot read 3N Cartesians, and an ensemble's coordinates would flood its context (a
`find_calculations` listing of stored payloads most of all); `structure_id` is what makes a geometry
referable. One generic walker rather than a projection per model, because `find_calculations` holds
stored payloads of unknown type.

`structures_in` finds the geometries so they can be persisted; `without_geometry` replaces those
same geometries with their addresses, so every `structure_id` shown resolves. A geometry is
recognised by shape (`elements` and `positions`) and then validated as a `Structure`, which also
derives the address from normalised coordinates; anything that fails validation is left alone.
"""

import logging
from collections.abc import Iterator
from typing import Any

from pydantic import ValidationError

from chemclaw.core.metrics_bridge import degraded
from chemclaw.science.calc.models import Structure

logger = logging.getLogger(__name__)

# The two fields that make a mapping a geometry. Both, because either alone appears elsewhere:
# `elements` is a plausible name for a composition, and a scan carries `positions` of its own kind.
_GEOMETRY_FIELDS = ("elements", "positions")

# What survives a projection beyond the address: which molecule (`smiles`), which electronic state
# (`charge`/`multiplicity`), and which calculation produced it (`origin`, usable as a `calc_ref`).
_KEPT_FIELDS = ("smiles", "charge", "multiplicity", "origin")

# Default values omitted from a projection, since a neutral singlet is what every reader assumes.
# Charge and multiplicity are omitted together or not at all, so a radical's state is always stated
# whole.
_DEFAULT_STATE = {"charge": 0, "multiplicity": 1}


def _as_structure(node: Any) -> Structure | None:
    """`node` as a `Structure` when it is one, else None.

    Something shaped like a geometry that fails `Structure` validation is left untouched rather than
    addressed.
    """
    if not isinstance(node, dict) or any(field not in node for field in _GEOMETRY_FIELDS):
        return None
    try:
        return Structure.model_validate(node)
    except ValidationError:
        return None


def structures_in(payload: Any) -> Iterator[Structure]:
    """Every geometry embedded anywhere in `payload`, in the order it is reached.

    Recursive because payload shapes differ per calculation. Duplicates are kept: `put` is
    content-addressed and idempotent.
    """
    structure = _as_structure(payload)
    if structure is not None:
        yield structure
        return
    if isinstance(payload, dict):
        for value in payload.values():
            yield from structures_in(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from structures_in(item)


def without_geometry(payload: Any) -> Any:
    """`payload` with every embedded geometry replaced by its address and its identifying fields.

    The model-facing projection: the address plus the fields that say which molecule and state
    (default state omitted, see `_DEFAULT_STATE`), with `geometry_omitted` set so a projected
    geometry is distinguishable from one never produced. Pure, because `CalcJobWorkflow` applies it
    in workflow code where replay must be byte-identical.
    """
    structure = _as_structure(payload)
    if structure is not None:
        dumped = structure.model_dump(mode="json")
        ordinary = all(dumped.get(field) == value for field, value in _DEFAULT_STATE.items())
        projected: dict[str, Any] = {"structure_id": structure.structure_id}
        projected.update(
            {
                field: dumped[field]
                for field in _KEPT_FIELDS
                if dumped.get(field) is not None and not (ordinary and field in _DEFAULT_STATE)
            }
        )
        projected["atom_count"] = len(structure.elements)
        projected["geometry_omitted"] = True
        return projected
    if isinstance(payload, dict):
        return {key: without_geometry(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [without_geometry(item) for item in payload]
    return payload


def check_server_address(payload: Any) -> None:
    """Count, and say out loud, any geometry whose address we derive differently from the server's.

    `structure_id` is half of every `xtb.*` key, so a divergence means a cache that misses forever.
    The server's rounding is configurable while ours is fixed, so its `computed_field`
    `structure_id` is read here before validation drops it. Counted through `degraded` (pinned
    subsystem labels) so it shows on a dashboard. Logs rather than raises, and the local id wins:
    the numbers in hand are not wrong, and this deployment's rows are keyed by the local id.
    """
    if not isinstance(payload, dict | list):
        return
    structure = _as_structure(payload)
    if structure is not None:
        reported = payload.get("structure_id") if isinstance(payload, dict) else None
        if isinstance(reported, str) and reported and reported != structure.structure_id:
            degraded(
                logger,
                "structure_id",
                "the calculation server addressed a geometry as %s and this deployment derives "
                "%s; every calculation keyed on it will miss from here on. The usual cause is "
                "CHEMCLAW_XTB_GEOMETRY_DECIMALS set on the server, which this side holds at a "
                "constant",
                reported,
                structure.structure_id,
                exc_info=False,
            )
        return
    values = payload.values() if isinstance(payload, dict) else payload
    for value in values:
        check_server_address(value)
