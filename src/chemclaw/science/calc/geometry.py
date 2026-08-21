"""One walk over a calculation payload, used twice: keep the geometries, then take them out.

**The measurement this exists for.** A conformer search on a 40-atom drug molecule returns twenty
geometries, and the whole of that — 2,400 Cartesian coordinates, ~29,000 characters, ~7,300 tokens
— reached the model's context three times over: as the inline-wait return value, as the mid-turn
resume message, and as `get_durable_job_status`'s result. The model can do nothing with a
coordinate. Worse, the *stored* payload is untruncated, so one `xtb.conformers` row is 66,520
characters and `find_calculations` will return fifty of them — **~830,000 tokens** from one
read-only call, past every provider's context limit, where `agent/compaction.py` records that the
failure is a hard error rather than a degradation.

The rule was already written down one module over and applied to exactly one result shape
(`OptimizationSummary`): *a model cannot read 3N Cartesians; `structure_id` is what makes a geometry
referable*. This module is that rule applied everywhere, and the reason it is one generic walker
rather than a `.of()` per model is that the third caller is `find_calculations`, which holds a
**stored payload of unknown type** — a `dict` out of a JSONB column. A per-model projection cannot
help it at all.

**The two halves are one act, and that is what makes the handle trustworthy.** `structures_in`
finds the geometries so they can be persisted; `without_geometry` replaces those same geometries
with their addresses. Driven from one shape test, so a payload whose geometry is projected away is
a payload whose geometry was kept — the invariant `chemclaw.science.calc.structures` states as
"every `structure_id` the agent is shown resolves".

**Shape, not type.** A geometry is recognised by carrying `elements` and `positions`, and is then
*validated* as a `Structure` before anything is done with it — so a payload field that happens to
share those names but is not a geometry is left alone rather than mangled. Validation is also what
derives the address: `structure_id` is a property of the normalised coordinates, not of whatever
the caller happened to send.
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

# What survives a projection, beyond the address. Each answers a question a chemist asks of a
# geometry they cannot see: *of what molecule* (`smiles`), *in what electronic state*
# (`charge`/`multiplicity`), and *produced by what* (`origin`, the key of the calculation that
# relaxed it — which is also what `propose_knowledge_note` takes as a `calc_ref`).
_KEPT_FIELDS = ("smiles", "charge", "multiplicity", "origin")

# The values of those fields that say nothing, and are therefore omitted. A neutral closed-shell
# singlet is what every reader assumes, and stating it costs a line per geometry — twenty of them in
# one ensemble, which is the shape this projection exists to bound. The same rule
# `CalcJobWorkflow`'s `exclude_none` follows one line up: a field whose value is the default is a
# field the model has to read past.
#
# Charge and multiplicity are omitted **together or not at all**: a `[CH3]` radical is
# `charge=0, multiplicity=2`, and reporting only the multiplicity would read as a partial statement
# about an electronic state rather than as a complete one about an unusual half of it.
_DEFAULT_STATE = {"charge": 0, "multiplicity": 1}


def _as_structure(node: Any) -> Structure | None:
    """`node` as a `Structure` when it is one, else None.

    A payload that *looks* like a geometry and does not validate as one is not a geometry — a
    mismatched array length or an impossible electron count is exactly what `Structure`'s validator
    refuses — so it is left untouched rather than replaced by an address derived from nonsense.
    """
    if not isinstance(node, dict) or any(field not in node for field in _GEOMETRY_FIELDS):
        return None
    try:
        return Structure.model_validate(node)
    except ValidationError:
        return None


def structures_in(payload: Any) -> Iterator[Structure]:
    """Every geometry embedded anywhere in `payload`, in the order it is reached.

    Recursive because a geometry is as likely to be the `structure` of the fourteenth member of an
    ensemble as a top-level field, and because the payload shapes differ per calculation — an
    optimization holds one, a scan holds one, an ensemble holds as many as the search found.

    Duplicates are not removed: `put` is content-addressed and idempotent, so de-duplicating here
    would only move the same work to a set.
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

    The model-facing projection. What replaces a geometry is not a bare string: a chemist reading
    "the lowest conformer" needs to know which molecule and which charge state it is, and a
    `structure_id` alone says neither. So the replacement carries the address plus the four fields
    that answer those questions, and drops the 3N numbers that answer none of them.

    An ordinary neutral closed-shell state is left out rather than restated — see `_DEFAULT_STATE`.

    `geometry_omitted` is set on the replacement rather than left implied. This repository's rule is
    that a silent truncation reads as completeness (`D-2026-08-08-a-partial-answer-must-say-so`),
    and without it a reader cannot tell a geometry that was projected away from one the calculation
    never produced.

    Pure, and it has to be: `CalcJobWorkflow` applies it in workflow code, where a replay must
    produce byte-identical output from the same activity result.
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

    **A silent divergence here is a cache that misses forever.** `structure_id` is half of every
    `xtb.*` key, and the two derivations agree only while two things stay equal: the payload
    `stable_hash` sees, and the decimal place coordinates are rounded to before it. This repository
    froze its rounding at a constant precisely because an operator who changed it "was not
    re-addressing a local cache… they were making every relaxation, Hessian, scan point and CREST
    search in that deployment miss forever, silently" (`science/calc/models.py`). The server did
    **not** freeze its side: `xtb_geometry_decimals` is an ordinary ENV-overridable field there. So
    the cross-repository agreement the constant protects holds on one side only, and nothing
    anywhere compares the two.

    The server's `Structure.structure_id` is a `computed_field`, so its authoritative answer arrives
    on every payload — and pydantic drops it on validation, because ours is a plain property and
    unknown fields are ignored. This reads it before it is dropped.

    Counted through `degraded` rather than through a counter of its own, because that is what
    the helper is for and because the subsystem label set is pinned (`tests/test_degraded.py`) —
    which is how this stays visible on a dashboard instead of only in a log search nobody runs.

    **It logs rather than raises, and the local derivation wins.** A divergence means the two sides
    would key differently from here on; it does not mean the numbers in hand are wrong. Raising
    would turn one operator's configuration mistake into a total outage of every calculation, which
    is a worse answer than a loud counter and a degraded line — and the local id is what this
    deployment's own rows, handles and geometry store are keyed by, so preferring it keeps this side
    self-consistent while the disagreement is fixed.
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
