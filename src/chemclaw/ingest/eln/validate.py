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

   **What this therefore does not catch, stated plainly** because it reads stronger than it
   is: any fabrication whose product is built from elements the inputs already supply.
   `aniline + methanol >> paracetamol` validates. So does `methane >> eicosane`, and
   `glucose >> cholesterol`. Only a product introducing a *new element* is rejected — which
   is why the reviewer, not this function, is the gate on whether a reaction is real.

   Two stronger checks were considered and neither is available on this data. Comparing
   heavy-atom counts needs a ceiling on how many times an input may repeat in a product, and
   without coefficients that ceiling is arbitrary — it would reject a genuine oligomerization
   to catch an invented one. Checking that products cannot outweigh inputs is sound at any
   stoichiometry, but measured across every shipped fixture **no outcome records a mass**, so
   it would be a no-op wearing the appearance of a control. Both need the export to carry
   something it does not; see `docs/planning/DEFERRED.md`.

Returns a list of human-readable problems (empty = valid), so the sync can log exactly why
an entry was rejected and the CLI can report them.
"""

import asyncio
from datetime import UTC, datetime

from rdkit import Chem

from chemclaw.ingest.eln.adapter import ElnAdapter, ElnMappingError
from chemclaw.ingest.eln.ord import OrdReaction


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
    # Species introduced by a procedure step (a mid-run reagent, a quench, a wash) can supply
    # elements too, so they count on the input side of the balance — otherwise a product atom
    # legitimately coming from a workup reagent would be falsely flagged.
    input_smiles = [c.smiles for c in (*reaction.inputs, *reaction.step_components())]
    input_elements, bad_inputs = _elements(input_smiles)
    output_elements, bad_outputs = _elements([c.smiles for c in reaction.outcomes])

    for smiles in [*bad_inputs, *bad_outputs]:
        problems.append(f"unparseable SMILES: {smiles!r}")
    if bad_inputs or bad_outputs:
        return problems  # cannot check balance without valid structures

    for element in sorted(output_elements):
        if element not in input_elements:
            problems.append(f"mass balance: products contain {element} but no input supplies it")
    return problems


def _validate_source(adapter: ElnAdapter, label: str) -> int:
    """Map + validate every entry an adapter offers; print problems, return their count.

    **A source that offers nothing counts as one problem.** The counter used to be the only signal,
    so an empty source produced a success line — "OK: 0 entr(ies) ... are valid" — whose own text
    was the tell nobody reads in CI, and a directory that does not exist read identically because
    an adapter yields nothing rather than raising. A typo'd `export_dir`, or an ORD export missing
    from the image, therefore reported OK while the structure and mass-balance gate on everything
    entering the graph and the fingerprint index had quietly stopped running.

    Zero is not legitimate here, and the difference from `main`'s empty *enabled set* is worth
    stating: no sources enabled is a configuration a deployment chose and can read off
    `CHEMCLAW_DATA_SOURCES`, so that is announced and exits 0. A source that is attached and
    supplies nothing is a claim that failed — and nothing available here distinguishes a genuinely
    empty ELN from a mis-mounted one, which is exactly why it must not pass quietly.
    """
    entries = asyncio.run(adapter.fetch_new_entries(datetime.min.replace(tzinfo=UTC)))
    if not entries:
        print(
            f"{label}: no entries found — this half of the gate did not run. Check the source's "
            "configuration (its export directory or query) before reading this as a pass."
        )
        return 1
    problems = 0
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
    """CLI: map and validate every entry from the *enabled* ingest sources (plan 4.4).

    Run as `python -m chemclaw.ingest.eln.validate`. Exits non-zero if any entry is unmappable or
    fails structure/mass-balance validation.

    **It asks the registry which adapters are attached rather than naming two of them.** This used
    to construct `JsonExportAdapter` and `OrdJsonAdapter` by name and validate those, which was
    right while they were the only two — and became a gate looking somewhere other than where the
    data comes in the moment an ELN could be attached through a manifest (D-120). A site whose ELN
    arrives that way was outside the only check that maps and mass-balances entries before they
    land, and this printed `OK` regardless: the shape `CLAUDE.md` records as "a README is not a
    gate", in the one file whose entire job is being one.

    The source's *name* is the label, so a failure names the manifest an operator has to go and fix
    rather than a format. Sources are resolved one at a time through `make_data_source`, not by
    zipping the names list against the halves list: two independently-built lists of the same length
    mispair silently, which for a validator would attribute one source's rejections to another.
    """
    from chemclaw.ingest.sources.registry import active_ingest_source_names, make_data_source

    names = active_ingest_source_names()
    if not names:
        # Not a failure — a retrieve-only deployment is a legitimate configuration — but it must not
        # read as a pass. "OK" over an empty set is exactly what this rewrite exists to stop.
        print(
            "No ingest sources are enabled (CHEMCLAW_DATA_SOURCES), so no ELN entries were "
            "validated. This is not a pass: nothing was checked."
        )
        return 0
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
