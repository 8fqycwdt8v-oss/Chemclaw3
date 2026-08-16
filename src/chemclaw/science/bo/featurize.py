"""Turn categorical BO choices into a continuous descriptor space via GFN2-xTB (U1).

The problem this solves. A BoFire campaign over "which ligand / base / solvent" treats the
choice as a bare **category**: the surrogate learns a separate effect per label and can say
nothing about an option nobody has run yet. With eight ligands and a budget of twelve
experiments that is most of the budget spent discovering that the model has no opinion.

Giving each category a numeric descriptor vector replaces the label with a *position in
chemical space*, so the surrogate interpolates: evidence about two electron-rich phosphines
informs a third. This module computes those descriptors from the electronic properties
the calculation server already provides — no new xTB capability, only wiring.

Descriptor choice, and one deliberate omission. The five descriptors below are the electronic
axes a reagent choice usually turns on: donor strength (HOMO), acceptor strength (LUMO),
polarity (dipole), and the electrostatic extremes (most positive / most negative partial
charge, which carry H-bond donor and acceptor character). The HOMO-LUMO **gap is deliberately
excluded**: it is exactly `lumo - homo`, so including it alongside both would hand the GP a
perfectly collinear column — redundancy that costs kernel conditioning and buys nothing.

What this does not capture: sterics. Cone angles and buried volume need a 3D geometry, so a
purely electronic featurization cannot distinguish two ligands that differ mainly in bulk.
That is a real limitation for phosphine selection specifically, and it is what the geometry
tasks (plan X3) would add.
"""

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from chemclaw.science.bo.problem import (
    CategoricalParameter,
    OptimizationProblem,
    Parameter,
)
from chemclaw.science.calc.models import ElectronicProperties

# How this module obtains one molecule's electronic properties: given a SMILES, the properties and
# the `calc_ref` they can be cited by.
#
# **Injected rather than imported, because of where the physics went.** The engines moved to
# `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`) and the client that reaches
# them lives in `connectors/calc/remote.py` — one package *above* this one. `science` may depend on
# `core` and nothing else (`tests/test_layering.py`), and excusing an edge here would declare a
# `science <-> connectors` cycle to save one argument. So the caller passes the calculator in;
# `connectors/bo/calculators.py` is the one that does, for all three call sites.
PropertiesFor = Callable[[str], Awaitable[tuple[ElectronicProperties, str]]]

# The descriptor names, in the order they are reported. Fixed rather than configurable: the
# values are stored in the campaign spec, so a campaign always sees the set it was built with,
# and a fixed vocabulary keeps two campaigns comparable.
DESCRIPTOR_NAMES = (
    "homo_ev",
    "lumo_ev",
    "dipole_debye",
    "max_atomic_charge",
    "min_atomic_charge",
)


def descriptors_from_properties(properties: ElectronicProperties) -> dict[str, float]:
    """Project one molecule's electronic properties onto `DESCRIPTOR_NAMES`.

    Raises `ValueError` when the molecule has no virtual orbital and therefore no LUMO —
    a descriptor row with a missing entry would make the matrix ragged, and substituting a
    placeholder would put a fictional molecule into the surrogate's input space (gate G4).
    """
    if properties.lumo_ev is None:
        raise ValueError(
            f"{properties.smiles!r} has no virtual orbital, so it has no LUMO descriptor"
        )
    charges = [atom.charge for atom in properties.atom_charges]
    return {
        "homo_ev": properties.homo_ev,
        "lumo_ev": properties.lumo_ev,
        "dipole_debye": properties.dipole_debye,
        "max_atomic_charge": max(charges),
        "min_atomic_charge": min(charges),
    }


class Featurized(NamedTuple):
    """A featurized problem and the calculation keys its descriptors came from.

    The keys are what lets a suggestion cite its own evidence. Descriptors are real xTB results
    from the shared calculation cache, and until now their identity was derived inside
    the calculator and discarded — so an `experiment-proposal` note could describe the
    conditions a surrogate recommended and could not point at the calculations that shaped the
    space it searched. D-158 plumbed exactly this out of the QM activity for the same reason.

    Sorted and deduplicated, because two categories may resolve to one molecule and the order a
    dict happens to iterate in is not a property of the campaign.
    """

    problem: OptimizationProblem
    calc_refs: list[str]


async def featurize_parameter(
    properties_for: PropertiesFor, parameter: CategoricalParameter
) -> tuple[CategoricalParameter, list[str]]:
    """Return `parameter` with `descriptors` computed from its `structures`.

    A parameter with no `structures` is returned unchanged — featurization is opt-in, and a
    campaign over categories that are not molecules (a stirrer type, a vendor) has nothing to
    compute. Results come from the calculation cache, so re-featurizing a parameter whose
    molecules were seen before costs nothing.

    Also returns the calculation keys the descriptors came from, so a suggestion built on them can
    say so (`Featurized`).

    Raises:
        ValueError: When one of the structures cannot be featurized. The category is named,
            because "which one" is the only useful part of that message.
    """
    if parameter.structures is None:
        return parameter, []
    descriptors: dict[str, dict[str, float]] = {}
    calc_refs: list[str] = []
    for category in parameter.categories:
        smiles = parameter.structures[category]
        try:
            properties, calc_ref = await properties_for(smiles)
            descriptors[category] = descriptors_from_properties(properties)
        except ValueError as error:
            raise ValueError(
                f"parameter {parameter.name!r}: cannot featurize category {category!r} "
                f"({smiles!r}): {error}"
            ) from error
        calc_refs.append(calc_ref)
    return parameter.model_copy(update={"descriptors": descriptors}), calc_refs


async def featurize_problem(
    properties_for: PropertiesFor, problem: OptimizationProblem
) -> Featurized:
    """Return `problem` with every structure-carrying categorical parameter featurized.

    The one entry point a caller needs: continuous parameters and categoricals without
    structures pass through untouched, so it is safe to call on any problem. Call it once,
    before the campaign starts — the descriptors then travel with the spec, which is what
    keeps a durable campaign's featurization stable across rounds and worker restarts.

    Returns the calculation keys alongside, so a persisted suggestion can cite the evidence its
    decision space was built from.
    """
    parameters: list[Parameter] = []
    calc_refs: set[str] = set()
    for parameter in problem.parameters:
        if not isinstance(parameter, CategoricalParameter):
            parameters.append(parameter)
            continue
        featurized, keys = await featurize_parameter(properties_for, parameter)
        parameters.append(featurized)
        calc_refs.update(keys)
    return Featurized(
        problem=problem.model_copy(update={"parameters": parameters}),
        calc_refs=sorted(calc_refs),
    )
