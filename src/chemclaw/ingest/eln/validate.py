"""Validate a canonical ORD reaction: parseable structures and element conservation.

1. **Structure** — every component SMILES parses in RDKit.
2. **Mass balance** — element conservation only: a product may not contain an element no input
   supplies. Exports carry no stoichiometric coefficients, so atom counts cannot be compared without
   rejecting valid oligomerizations.

This is a soundness filter, not a check that a reaction is real: any fabrication built from the
inputs' elements passes (`aniline + methanol >> paracetamol`). A transcription is trusted because a
source system recorded it. Stronger checks need data the exports lack; see
`docs/planning/DEFERRED.md`.

Returns a list of human-readable problems (empty = valid).
"""

import asyncio
from datetime import UTC, datetime

from rdkit import Chem

from chemclaw.ingest.eln.adapter import ElnAdapter, ElnMappingError
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.warehouse.expr import pattern_budget


def _elements(smiles_list: list[str]) -> tuple[set[str], list[str]]:
    """Collect the element symbols (with explicit H) over SMILES, plus any unparseable ones."""
    found: set[str] = set()
    bad: list[str] = []
    for smiles in smiles_list:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            bad.append(smiles)
            continue
        found.update(atom.GetSymbol() for atom in Chem.AddHs(mol).GetAtoms())
    return found, bad


def validate_ord(reaction: OrdReaction) -> list[str]:
    """Return the reaction's validation problems (empty list if it is valid).

    Checks that every component SMILES parses and that no product contains an element absent from
    all inputs. Atom counts are not compared because the export has no stoichiometric coefficients.
    """
    problems: list[str] = []
    # Species introduced by a procedure step (mid-run reagent, quench, wash) supply elements too, so
    # they count as inputs.
    input_smiles = [c.smiles for c in (*reaction.inputs, *reaction.step_components())]
    input_elements, bad_inputs = _elements(input_smiles)
    output_elements, bad_outputs = _elements([c.smiles for c in reaction.outcomes])

    for smiles in [*bad_inputs, *bad_outputs]:
        problems.append(f"unparseable SMILES: {smiles!r}")
    if bad_inputs or bad_outputs:
        return problems  # cannot check balance without valid structures
    if reaction.unstructured:
        # A citation-only record: the given structures are checked above, but the balance is
        # uncheckable when a species has no structure, so it is not run.
        return problems

    for element in sorted(output_elements):
        if element not in input_elements:
            problems.append(f"mass balance: products contain {element} but no input supplies it")
    return problems


def _validate_source(adapter: ElnAdapter, label: str) -> int:
    """Map + validate every entry an adapter offers; print problems, return their count.

    A source that offers nothing counts as one problem: an empty or mis-mounted export would
    otherwise report OK while nothing was checked.
    """
    entries = asyncio.run(adapter.fetch_new_entries(datetime.min.replace(tzinfo=UTC)))
    if not entries:
        print(
            f"{label}: no entries found — this half of the gate did not run. Check the source's "
            "configuration (its export directory or query) before reading this as a pass."
        )
        return 1
    problems = 0
    # The same page-wide regex budget an ingest runs under, so a binding whose patterns are too
    # expensive fails here rather than wedging the sync.
    with pattern_budget():
        for raw in entries:
            try:
                issues = validate_ord(adapter.map_to_ord(raw))
            except ElnMappingError as exc:
                print(f"{label}/{raw.entry_id}: unmappable — {exc}")
                problems += 1
                continue
            for issue in issues:
                print(f"{label}/{raw.entry_id}: {issue}")
            problems += len(issues)
    if not problems:
        print(f"OK: {len(entries)} entr(ies) from {label} are valid")
    return problems


def main() -> int:
    """CLI: map and validate every entry from the *enabled* ingest sources.

    Run as `python -m chemclaw.ingest.eln.validate`. Exits non-zero if any entry is unmappable or
    invalid, and also if nothing was checked (no enabled ingest source, or a source offering no
    entries); exit 0 means the gate ran.

    Asks the registry which adapters are attached, so a manifest-attached ELN (D-120) is covered.
    Failures are labelled with the source's name, and sources are resolved one at a time through
    `make_data_source` so rejections are never attributed to the wrong source.
    """
    from chemclaw.ingest.sources.registry import active_ingest_source_names, make_data_source

    names = active_ingest_source_names()
    if not names:
        # Exit 1 to match the message: a known source with no `ingest:` half (e.g. `graph` instead
        # of `graph,eln-json`) would otherwise pass with the gate not running. A deployment with no
        # ELN should remove this target.
        print(
            "No ingest sources are enabled (CHEMCLAW_DATA_SOURCES), so no ELN entries were "
            "validated. This is not a pass: nothing was checked. If this deployment has no ELN, "
            "drop this gate from its pipeline rather than reading a green line off it."
        )
        return 1
    total = 0
    for name in names:
        source = make_data_source(name)
        if source.ingest is None:  # pragma: no cover - `active_ingest_source_names` filters these
            continue
        total += _validate_source(source.ingest, name)
    if total:
        print(f"\n{total} problem(s) found")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
