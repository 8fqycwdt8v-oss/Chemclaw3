"""Validate a canonical ORD reaction: parseable structures + mass balance (plan 4.4).

Two independent checks, both necessary before a reaction enters the graph or the
fingerprint index (G4):

1. **Structure** — every component SMILES parses in RDKit; an unparseable structure is a
   corrupt record, not a reaction.
2. **Mass balance** — element conservation only: a product cannot contain an ELEMENT that
   no input supplies (you cannot create atoms). The ELN export carries no stoichiometric
   coefficients (a dimerization lists A once for 2 A → A–A), so comparing per-molecule
   atom *counts* is unsound and falsely rejects valid reactions; element-set subsumption
   is the strongest check that stays a sound necessary condition.

Returns a list of human-readable problems (empty = valid), so the sync can log exactly why
an entry was rejected and the CLI can report them.
"""

import asyncio
import sys
from datetime import UTC, datetime

from rdkit import Chem

from chemclaw.config import settings
from eln.adapter import ElnMappingError
from eln.json_adapter import JsonExportAdapter
from eln.ord import OrdReaction


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

    Checks that every component SMILES parses and that no product contains an element
    absent from all inputs. Atom *counts* are deliberately not compared: the export has
    no stoichiometric coefficients, so a valid dimerization (2 A → A–A with A listed
    once, the normal ELN convention) would fail a per-molecule count check. Provenance
    and role consistency are already enforced by the schema, so this focuses on the
    chemistry.
    """
    problems: list[str] = []
    input_elements, bad_inputs = _elements([c.smiles for c in reaction.inputs])
    output_elements, bad_outputs = _elements([c.smiles for c in reaction.outcomes])

    for smiles in [*bad_inputs, *bad_outputs]:
        problems.append(f"unparseable SMILES: {smiles!r}")
    if bad_inputs or bad_outputs:
        return problems  # cannot check balance without valid structures

    for element in sorted(output_elements):
        if element not in input_elements:
            problems.append(f"mass balance: products contain {element} but no input supplies it")
    return problems


def main() -> int:
    """CLI: map and validate every ELN entry in a directory; report problems (plan 4.4).

    Run as `python -m eln.validate [export_dir]`. Exits non-zero if any entry is unmappable
    (bad ELN shape) or fails structure/mass-balance validation, so it can gate an ELN sync.
    """
    export_dir = sys.argv[1] if len(sys.argv) > 1 else settings.eln_export_dir
    adapter = JsonExportAdapter(export_dir)
    entries = asyncio.run(adapter.fetch_new_entries(datetime.min.replace(tzinfo=UTC)))
    total_problems = 0
    for raw in entries:
        try:
            problems = validate_ord(adapter.map_to_ord(raw))
        except ElnMappingError as exc:
            print(f"{raw.entry_id}: unmappable — {exc}")
            total_problems += 1
            continue
        for problem in problems:
            print(f"{raw.entry_id}: {problem}")
        total_problems += len(problems)
    if total_problems:
        print(f"\n{total_problems} problem(s) across {len(entries)} entr(ies) in {export_dir}")
        return 1
    print(f"OK: {len(entries)} entr(ies) in {export_dir} are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
