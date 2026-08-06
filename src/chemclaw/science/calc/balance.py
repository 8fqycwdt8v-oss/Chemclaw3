"""Whether an equation conserves atoms and charge, and the launch-time check on it (VIBE-1a).

**Why this is its own module, and a leaf one.** A chemist asked for a reaction energy over an
unbalanced equation. `CalcJobWorkflow` correctly refused it — and the tool raised
`WorkflowFailureError: Workflow execution failed`, while the actionable message ("reaction is not
atom-balanced (reactants minus products): C +2, H +4, O +2") stayed in the worker log. The model
could not repair its own input from that, and Temporal retried the refusal five times on the way
(live full-stack pass, 2026-08-04).

Balance is a *precondition* in the sense `JobSpec.preconditions` means: a fact about the request
that is knowable before any durable work starts and can only ever refuse it. Running it at launch
relays the message through `surface_domain_errors` in the same turn and stops the retries. The
identical argument, and the identical shape, as `chemclaw.science.calc.solvents` — which is why
this sits beside it rather than inside it.

The rule itself lived in `reaction.py` and is *moved* here, not copied: that module imports
`xtb_engine` and therefore `tblite`, and a precondition is resolved by importing it in the **chat
service's** process (D-118, `tests/test_connector_isolation.py`). `reaction.py` imports it back, so
the workflow and the launch check enforce one definition — a second copy is how the two would come
to disagree about what balanced means.

RDKit is core's own dependency (`core/chem.py`), so it is fair game here; the SMILES parse is
local for the same reason `science/safety/screen.py` keeps its own, which is that a leaf may not
reach into a bundle's heavy module for a four-line helper.
"""

from collections import Counter
from typing import Any

from rdkit import Chem


def _composition(smiles: str) -> tuple[Counter[str], int]:
    """Element counts (hydrogens explicit) and formal charge of one species."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    with_hydrogens = Chem.AddHs(mol)
    counts: Counter[str] = Counter(atom.GetSymbol() for atom in with_hydrogens.GetAtoms())
    return counts, Chem.GetFormalCharge(with_hydrogens)


def check_balance(reactants: list[str], products: list[str]) -> None:
    """Raise unless the equation conserves atoms and charge (gate G4).

    Named the failure, not just detected: the message says which element is short and
    by how much, because the usual cause is a forgotten water or proton and that is
    immediately fixable once stated.

    An unbalanced equation produces a difference that includes whatever atoms the two sides do not
    share — a number that is meaningless rather than merely imprecise, and one that looks entirely
    ordinary.
    """
    if not reactants or not products:
        raise ValueError("a reaction needs at least one reactant and one product")
    left: Counter[str] = Counter()
    right: Counter[str] = Counter()
    left_charge = right_charge = 0
    for smiles in reactants:
        counts, charge = _composition(smiles)
        left += counts
        left_charge += charge
    for smiles in products:
        counts, charge = _composition(smiles)
        right += counts
        right_charge += charge
    if left != right:
        difference = {
            element: left[element] - right[element]
            for element in sorted(set(left) | set(right))
            if left[element] != right[element]
        }
        raise ValueError(
            "reaction is not atom-balanced (reactants minus products): "
            + ", ".join(f"{element} {count:+d}" for element, count in difference.items())
        )
    if left_charge != right_charge:
        raise ValueError(
            f"reaction is not charge-balanced: reactants {left_charge:+d}, "
            f"products {right_charge:+d}"
        )


def require_balanced_equation(spec: Any) -> None:
    """Refuse a durable calc job whose equation does not conserve atoms and charge, before it runs.

    Duck-typed over the params object for the reason
    `chemclaw.science.calc.solvents:require_supported_solvents` is: the specs live in a leaf the
    manifest points at, and reading the two attributes is what lets one rule cover every job that
    carries an equation without importing them.

    A spec with no `reactants`/`products` is not this rule's business and passes untouched — the
    same shape as gas phase passing the solvent rule. That is what lets a job declare both rules
    without either one having to know which jobs the other applies to.

    Args:
        spec: The validated params object, whatever the job declared.

    Raises:
        ValueError: The equation is unbalanced in atoms or charge, or a SMILES does not parse. The
            message names the imbalance element by element, which is what the model needs to
            correct its own call in the same turn instead of reading
            `WorkflowFailureError: Workflow execution failed`.
    """
    reactants = list(getattr(spec, "reactants", None) or [])
    products = list(getattr(spec, "products", None) or [])
    if not reactants and not products:
        return
    check_balance(reactants, products)
