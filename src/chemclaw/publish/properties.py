"""The property registry: every quantity this system may publish, with its canonical unit.

**This is the extension point, and it is why the schema does not move when a tool ships.** A new
calculator adds rows here; the DDL is untouched. That is the whole difference between this design
and a typed table per result type, which would need a migration per tool forever — and where the
failure is asymmetric across deployments, because a site that has not run this quarter's migration
is missing the table the new writer needs, so the new tool writes *nothing*, silently.

**It is also what keeps the fact layer from degrading into EAV.** Every fact's `property` is a
foreign key into this registry, so a value cannot be written under a name nobody defined. Without
that, names drift — `pka`, `pka_acid`, `pKa` — and every query silently under-returns while looking
entirely correct. The foreign key does not prevent a *synonym* being registered; only review does.
`tests/test_publish_registry.py` narrows that gap by failing on two properties that share a
dimension and appear on the same subject.

**Canonical unit per property, not one global unit.** Absolute energies stay in hartree because
their only use is being differenced, and six decimal places of kcal/mol on a -76 Ha number is a
rounding trap. Every *difference* is kcal/mol, because that is the unit every threshold a chemist
states is in: `value < -10` has to be literally what the question says. A single SI unit would mean
nobody could write a predicate without dividing by 4184; a unit column with no canonicalization
would mean one mis-tagged row falls silently out of a range filter.

`dimension` exists so a test can assert that every property sharing one is expressed in a unit that
converts to its canonical unit — the check that catches a row shipped with `kcal/mol` under
`molar_entropy`.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# What kind of value a property carries. `PropertyFact` enforces that exactly one of its three
# value columns is filled; this says which one is correct for a given name, so a projection that
# writes `converged` as the float 1.0 is a registry violation rather than a plausible number.
ValueKind = Literal["number", "integer", "boolean", "text"]

# Where a property may attach. A registry-level statement, so a projection that writes a
# per-atom quantity as a calculation-scope scalar is caught rather than stored.
ScopeKind = Literal["calculation", "member", "site", "point", "conformer", "candidate"]


class PropertyDefinition(BaseModel):
    """One registered quantity: what it is, what unit it is kept in, and where it attaches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    property: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    canonical_unit: str = ""
    value_kind: ValueKind = "number"
    scope_kind: ScopeKind = "calculation"
    definition: str = Field(min_length=1)


def _d(
    name: str,
    dimension: str,
    unit: str,
    definition: str,
    *,
    kind: ValueKind = "number",
    scope: ScopeKind = "calculation",
) -> PropertyDefinition:
    """Terse constructor, so the table below reads as data rather than as constructor calls."""
    return PropertyDefinition(
        property=name,
        dimension=dimension,
        canonical_unit=unit,
        value_kind=kind,
        scope_kind=scope,
        definition=definition,
    )


# Every conversion the registry needs, as (from, to) -> factor. Deliberately small: a unit appears
# here only because some tool reports in it and the registry keeps another. `hartree -> kcal/mol` is
# the one that matters, and it is the constant `science/calc/thermo.py` already uses.
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("hartree", "kcal/mol"): 627.5094740631,
    ("kcal/mol", "hartree"): 1.0 / 627.5094740631,
    ("kj/mol", "kcal/mol"): 1.0 / 4.184,
    ("kcal/mol", "kj/mol"): 4.184,
    ("cal/(mol*K)", "j/(mol*K)"): 4.184,
    ("j/(mol*K)", "cal/(mol*K)"): 1.0 / 4.184,
    ("ev", "kcal/mol"): 23.060547830619026,
}

# The shipped registry. Grouped by what the quantity is, because that is how a reader looking for
# "is there already a property for this" scans it.
#
# **Absolute energies are hartree; every difference is kcal/mol.** The split is not stylistic — see
# the module docstring. A new absolute energy joins the first block, a new difference the second.
_DEFINITIONS: tuple[PropertyDefinition, ...] = (
    # --- absolute energies: kept in hartree, because they exist to be differenced ---------
    _d("total_energy", "energy", "hartree", "Total electronic energy of the system as computed."),
    _d("electronic_energy", "energy", "hartree", "Electronic energy underlying a thermochemistry."),
    _d(
        "initial_energy",
        "energy",
        "hartree",
        "Energy of the geometry an optimization started from.",
    ),
    _d("enthalpy", "energy", "hartree", "Absolute enthalpy H at the stated temperature."),
    _d("gibbs_free_energy", "energy", "hartree", "Absolute Gibbs free energy G at the stated T."),
    _d("complex_energy", "energy", "hartree", "Total energy of an associated complex."),
    _d(
        "conformer_energy",
        "energy",
        "hartree",
        "Absolute energy of one ensemble member.",
        scope="conformer",
    ),
    # --- energy differences: kcal/mol, because that is the unit a threshold is stated in ---
    _d(
        "reaction_delta_e",
        "energy_difference",
        "kcal/mol",
        "Electronic energy change, products minus reactants.",
    ),
    _d(
        "reaction_delta_h",
        "energy_difference",
        "kcal/mol",
        "Enthalpy change, products minus reactants.",
    ),
    _d(
        "reaction_delta_g",
        "energy_difference",
        "kcal/mol",
        "Gibbs free energy change, products minus reactants. Negative is downhill at equilibrium; "
        "it says nothing about rate.",
    ),
    _d(
        "relaxation",
        "energy_difference",
        "kcal/mol",
        "How much a geometry optimization lowered the energy. Large means the starting guess was "
        "strained.",
    ),
    _d("zero_point_energy", "energy_difference", "kcal/mol", "Vibrational zero-point energy."),
    _d(
        "thermal_enthalpy_correction",
        "energy_difference",
        "kcal/mol",
        "Thermal correction from the electronic energy to H.",
    ),
    _d(
        "gibbs_correction",
        "energy_difference",
        "kcal/mol",
        "Correction from the electronic energy to G.",
    ),
    _d(
        "interaction_energy",
        "energy_difference",
        "kcal/mol",
        "Non-covalent interaction energy of a complex relative to its separated monomers.",
    ),
    _d(
        "deprotonation_energy",
        "energy_difference",
        "kcal/mol",
        "Energy of removing a proton, underlying a predicted pKa.",
    ),
    _d(
        "conformational_entropy_correction",
        "energy_difference",
        "kcal/mol",
        "The -T*S_conf term an ensemble contributes.",
    ),
    _d(
        "ensemble_correction",
        "energy_difference",
        "kcal/mol",
        "Correction from the lowest conformer to the Boltzmann-weighted ensemble.",
    ),
    _d(
        "solvent_spread",
        "energy_difference",
        "kcal/mol",
        "Range of a reaction energy across the solvents screened. Compare against the method "
        "uncertainty before reading a ranking.",
    ),
    _d(
        "max_relative_energy",
        "energy_difference",
        "kcal/mol",
        "Highest point of a relaxed scan relative to its minimum. An upper bound on a ground- "
        "state barrier, not a transition state.",
    ),
    _d(
        "relative_energy",
        "energy_difference",
        "kcal/mol",
        "Energy relative to the reference member of a set.",
        scope="conformer",
    ),
    _d(
        "point_relative_energy",
        "energy_difference",
        "kcal/mol",
        "Scan point energy relative to the scan minimum.",
        scope="point",
    ),
    _d("point_energy", "energy", "hartree", "Absolute energy at one scan point.", scope="point"),
    # --- entropy ---------------------------------------------------------------------------
    _d(
        "entropy",
        "molar_entropy",
        "cal/(mol*K)",
        "Total molar entropy S. Depends on the rotational symmetry number; too high by "
        "R*ln(sigma) if that was not stated.",
    ),
    _d(
        "conformational_entropy",
        "molar_entropy",
        "cal/(mol*K)",
        "Entropy contributed by the accessible conformer population.",
    ),
    # --- orbital and electronic properties --------------------------------------------------
    _d("homo", "orbital_energy", "ev", "Highest occupied molecular orbital energy."),
    _d("lumo", "orbital_energy", "ev", "Lowest unoccupied molecular orbital energy."),
    _d("homo_lumo_gap", "orbital_energy", "ev", "HOMO-LUMO gap."),
    _d("dipole", "dipole_moment", "debye", "Total dipole moment magnitude."),
    _d("partial_charge", "charge", "e", "Atomic partial charge.", scope="site"),
    _d("bond_order", "bond_order", "", "Wiberg/Mayer bond order between two atoms.", scope="site"),
    _d(
        "fukui_minus",
        "fukui",
        "",
        "Fukui index for electrophilic attack (the site donates electrons). Normalized per "
        "molecule, so comparable within one molecule only.",
        scope="site",
    ),
    _d(
        "fukui_plus",
        "fukui",
        "",
        "Fukui index for nucleophilic attack (the site accepts electrons). Comparable within one "
        "molecule only.",
        scope="site",
    ),
    _d(
        "fukui_zero",
        "fukui",
        "",
        "Fukui index for radical attack, the mean of the other two. Comparable within one "
        "molecule only.",
        scope="site",
    ),
    # --- vibrational -------------------------------------------------------------------------
    _d(
        "wavenumber",
        "wavenumber",
        "cm^-1",
        "Vibrational mode frequency. Semiempirical frequencies are systematically a few percent "
        "off; compare patterns, not positions.",
        scope="point",
    ),
    _d(
        "ir_intensity",
        "ir_intensity",
        "km/mol",
        "Infrared intensity of a vibrational mode.",
        scope="point",
    ),
    _d(
        "imaginary_frequency",
        "wavenumber",
        "cm^-1",
        "An imaginary mode, reported as a negative wavenumber. Any means the geometry is not a "
        "minimum.",
    ),
    # --- predicted physicochemical properties -------------------------------------------------
    _d("pka", "log_unit", "", "Predicted acid dissociation constant, as a pKa."),
    _d("log_s", "log_unit", "", "Predicted aqueous solubility, log10 of mol/L."),
    _d("log_d", "log_unit", "", "Predicted distribution coefficient at the stated pH."),
    _d("clogp", "log_unit", "", "Calculated octanol/water partition coefficient (Crippen)."),
    _d("tpsa", "area", "angstrom^2", "Topological polar surface area."),
    _d("molecular_weight", "molar_mass", "g/mol", "Average molecular weight."),
    _d(
        "fraction_csp3",
        "dimensionless",
        "",
        "Fraction of carbons that are sp3-hybridised; a shape/saturation measure.",
    ),
    _d("qed", "dimensionless", "", "Quantitative estimate of drug-likeness, 0 to 1."),
    # --- geometry and convergence --------------------------------------------------------------
    _d(
        "max_gradient",
        "gradient",
        "hartree/bohr",
        "Largest residual force component at the converged geometry.",
    ),
    _d("displacement_rms", "length", "angstrom", "RMS atomic displacement over an optimization."),
    _d("optimization_steps", "count", "", "Optimizer iterations taken.", kind="integer"),
    _d("atom_count", "count", "", "Number of atoms.", kind="integer"),
    _d(
        "symmetry_number",
        "count",
        "",
        "Rotational symmetry number used for the entropy. 1 when none was stated, which makes the "
        "entropy too high by R*ln(sigma_true).",
        kind="integer",
    ),
    _d("mode_count", "count", "", "Number of vibrational modes computed.", kind="integer"),
    _d(
        "total_conformers",
        "count",
        "",
        "Conformers the search found, before any reporting cap.",
        kind="integer",
    ),
    _d(
        "binding_modes",
        "count",
        "",
        "Distinct complex geometries the NCI search located.",
        kind="integer",
    ),
    _d(
        "degeneracy",
        "count",
        "",
        "Rotamer degeneracy of an ensemble member.",
        kind="integer",
        scope="conformer",
    ),
    _d(
        "population",
        "dimensionless",
        "",
        "Boltzmann population of an ensemble member at the stated temperature. Meaningless "
        "without that temperature.",
        scope="conformer",
    ),
    _d("hydrogen_bond_donors", "count", "", "Lipinski hydrogen-bond donor count.", kind="integer"),
    _d(
        "hydrogen_bond_acceptors",
        "count",
        "",
        "Lipinski hydrogen-bond acceptor count.",
        kind="integer",
    ),
    _d("rotatable_bonds", "count", "", "Rotatable bond count.", kind="integer"),
    _d("aromatic_rings", "count", "", "Aromatic ring count.", kind="integer"),
    _d(
        "lipinski_violations",
        "count",
        "",
        "Number of Lipinski rule-of-five violations.",
        kind="integer",
    ),
    _d(
        "cache_hits",
        "count",
        "",
        "How many of a composite's constituent calculations were already stored.",
        kind="integer",
    ),
    # --- booleans: a fixed 0..1 attribute of every result of its kind ----------------------------
    _d(
        "converged",
        "flag",
        "",
        "Whether the calculation reached its convergence criterion.",
        kind="boolean",
    ),
    _d(
        "is_minimum",
        "flag",
        "",
        "Whether the geometry is a true minimum (no imaginary frequencies).",
        kind="boolean",
    ),
    _d(
        "veber_pass",
        "flag",
        "",
        "Whether the molecule satisfies the Veber oral-bioavailability criteria.",
        kind="boolean",
    ),
    _d(
        "is_strongly_exothermic",
        "flag",
        "",
        "Whether the reaction energy crosses the configured exotherm threshold.",
        kind="boolean",
    ),
    _d(
        "exotherm_threshold",
        "energy_difference",
        "kcal/mol",
        "The threshold is_strongly_exothermic was judged against. Published beside the flag "
        "because it is deployment configuration: without it, a stored boolean cannot be re- "
        "interpreted after the setting changes.",
    ),
    _d(
        "scan_minimum_coordinate",
        "dimensionless",
        "",
        "The coordinate value at which a relaxed scan found its minimum, in the scan's own unit.",
    ),
    # --- coded text ------------------------------------------------------------------------------
    _d(
        "pka_site",
        "category",
        "",
        "Which site the predicted pKa describes: 'acid' or 'base'.",
        kind="text",
    ),
    _d(
        "best_solvent",
        "category",
        "",
        "The solvent a screen ranked first. Read with solvent_spread: a spread inside the method "
        "uncertainty is not a ranking.",
        kind="text",
    ),
    _d("fukui_mode", "category", "", "Which Fukui index the sites were ranked by.", kind="text"),
    _d(
        "scan_coordinate",
        "category",
        "",
        "Which internal coordinate a relaxed scan drove.",
        kind="text",
    ),
    _d(
        "conformer_treatment",
        "category",
        "",
        "How conformers were treated: a single geometry, or the lowest plus a conformational "
        "entropy.",
        kind="text",
    ),
    _d(
        "reaction_level",
        "category",
        "",
        "The effort tier a reaction energy was run at: quick, standard or thorough.",
        kind="text",
    ),
    _d(
        "search_effort",
        "category",
        "",
        "How hard a conformer search worked: quick, normal or extensive. Published because two "
        "ensembles are not comparable without it - a quick search finds fewer conformers than an "
        "extensive one on the same molecule, so a population difference may be an effort "
        "difference.",
        kind="text",
    ),
    _d(
        "search_kind",
        "category",
        "",
        "What a CREST search enumerated: conformers, tautomers, protomers or deprotomers.",
        kind="text",
    ),
    _d(
        "produced_structure",
        "identifier",
        "",
        "The address of the geometry this calculation *produced* - an optimization's relaxed "
        "structure, or the minimum along a relaxed scan. Distinct from the record's own "
        "structure_id, which is the geometry it ran ON (migration 048's meaning) and answers a "
        "different question. A property rather than a column because that is exactly the extension "
        "this schema is built for: a new fact about a calculation is a registry row and an INSERT, "
        "never an ALTER.",
        kind="text",
    ),
    # --- similarity and ranking -------------------------------------------------------------------
    _d(
        "tanimoto",
        "similarity",
        "",
        "Tanimoto (Jaccard) similarity over a fingerprint. Comparable only between fingerprints "
        "of one definition.",
        scope="candidate",
    ),
    _d(
        "predicted_value",
        "dimensionless",
        "",
        "A surrogate model's predicted objective value for a candidate.",
        scope="candidate",
    ),
    _d(
        "predicted_sd",
        "dimensionless",
        "",
        "The surrogate's standard deviation on that prediction. Large means the model has not "
        "learned this region.",
        scope="candidate",
    ),
)

# Indexed once at import: the lookup every write does, and the thing a validator scans.
REGISTRY: dict[str, PropertyDefinition] = {d.property: d for d in _DEFINITIONS}


class UnknownPropertyError(ValueError):
    """A fact named a property the registry does not define.

    A `ValueError`, so `durable/publish.py` treats it as non-retryable: an unregistered name will
    fail identically on every retry, and the fix is a registry row, not a wait.
    """


def definition_for(name: str) -> PropertyDefinition:
    """The registered definition of `name`, or raise naming the closest thing to a fix."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownPropertyError(
            f"{name!r} is not a registered property. Add it to "
            "`chemclaw.publish.properties._DEFINITIONS` with its canonical unit — a value stored "
            "under an unregistered name is a value no query will find."
        ) from None


def to_canonical(name: str, value: float, unit: str) -> float:
    """Convert `value` into the registry's canonical unit for `name`.

    The one place a unit conversion happens on the publish path, so a predicate over
    `value_canonical` is sound. An empty `unit` means the caller is already canonical and says so;
    a unit with no conversion path is an error rather than a silent pass-through, because passing
    it through is exactly how a mis-tagged row falls out of a range filter with nothing raising.
    """
    definition = definition_for(name)
    if not unit or unit == definition.canonical_unit:
        return value
    try:
        factor = UNIT_CONVERSIONS[(unit, definition.canonical_unit)]
    except KeyError:
        raise UnknownPropertyError(
            f"no conversion from {unit!r} to {definition.canonical_unit!r} for property {name!r}; "
            "add one to `UNIT_CONVERSIONS` or report the value in the canonical unit"
        ) from None
    return value * factor
