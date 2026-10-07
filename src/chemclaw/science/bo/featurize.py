"""Turn categorical BO choices into a continuous descriptor space via GFN2-xTB.

Each category becomes a position in descriptor space so the surrogate can interpolate to untried
options. Descriptors: HOMO, LUMO, dipole and the extreme partial charges; the gap is omitted as
`lumo - homo` (collinear). Sterics are not captured.
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
# the `calc_ref` they can be cited by. Injected because the client lives in `connectors/calc`,
# which `science` may not import; `connectors/bo/calculators.py` supplies it.
PropertiesFor = Callable[[str], Awaitable[tuple[ElectronicProperties, str]]]

# The descriptor names, in reported order. Fixed: values are stored in the campaign spec, and a
# fixed vocabulary keeps campaigns comparable.
DESCRIPTOR_NAMES = (
    "homo_ev",
    "lumo_ev",
    "dipole_debye",
    "max_atomic_charge",
    "min_atomic_charge",
)


def descriptors_from_properties(properties: ElectronicProperties) -> dict[str, float]:
    """Project one molecule's electronic properties onto `DESCRIPTOR_NAMES`.

    Raises `ValueError` when the molecule has no LUMO: a missing entry would make the matrix ragged,
    and a placeholder would put a fictional molecule into the surrogate's input space.
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

    The keys let a suggestion cite the calculations that shaped its space. Sorted and deduplicated,
    since two categories may resolve to one molecule.
    """

    problem: OptimizationProblem
    calc_refs: list[str]


async def featurize_parameter(
    properties_for: PropertiesFor, parameter: CategoricalParameter
) -> tuple[CategoricalParameter, list[str]]:
    """Return `parameter` with `descriptors` computed from its `structures`.

    Also returns the calculation keys used. A parameter without `structures` is returned unchanged.

    Raises:
        ValueError: When one of the structures cannot be featurized; the category is named.
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

    Call once before the campaign starts so descriptors travel with the spec.
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
