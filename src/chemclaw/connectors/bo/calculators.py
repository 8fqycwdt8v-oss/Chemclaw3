"""The calculators a BO campaign consults, bound to the calculation server and the D-011 cache.

`science/bo` states *what* it needs — electronic descriptors for a categorical option, a predicted
log S for a candidate — as two injected callables (`PropertiesFor`, `LogSFor`). This module is where
those are bound, and it exists because of a layering rule rather than a preference: the physics
moved to `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`), the client that
reaches it lives in `connectors/calc/remote.py`, and `chemclaw.science` may import `chemclaw.core`
and nothing else. Excusing an edge from `science` up into `connectors` would declare a
`science <-> connectors` cycle to save one argument at three call sites.

Both bindings go through `cached_remote`, so the property both callers have always advertised is
unchanged: a molecule seen before is served from the calculation store and never recomputed. What a
campaign pays on a repeat is one `calculation_key` round trip, not an SCF.
"""

from chemclaw.connectors.calc.remote import cached_remote
from chemclaw.science.bo.featurize import PropertiesFor
from chemclaw.science.bo.objectives import LogSFor
from chemclaw.science.calc.models import ElectronicProperties, SolubilityResult
from chemclaw.science.calc.store import ResultStore


def properties_for(store: ResultStore) -> PropertiesFor:
    """Bind `science.bo.featurize`'s `PropertiesFor` to `store` and the calculation server."""

    async def lookup(smiles: str) -> tuple[ElectronicProperties, str]:
        """The electronic properties of one molecule, and the `calc_ref` they can be cited by.

        The reference is read off the payload rather than derived: the server stamps every result
        with its own `calc_key`, and a stored row keeps that stamp — which is what lets a
        `experiment-proposal` note point at the calculations that shaped the space its surrogate
        searched, on a cache hit as well as a miss.
        """
        payload, _ = await cached_remote(
            store, "compute_electronic_properties", {"smiles": smiles, "solvent": None}
        )
        calc_ref = payload.get("calc_key")
        if not isinstance(calc_ref, str) or not calc_ref:
            raise ValueError(
                f"the electronic properties of {smiles!r} came back without a calc_key, so a "
                "suggestion built on them could not cite its evidence"
            )
        return ElectronicProperties.model_validate(payload), calc_ref

    return lookup


def log_s_for(store: ResultStore) -> LogSFor:
    """Bind `science.bo.objectives`'s `LogSFor` to `store` and the calculation server."""

    async def score(smiles: str) -> float:
        payload, _ = await cached_remote(store, "predict_solubility", {"smiles": smiles})
        return SolubilityResult.model_validate(payload).log_s_mol_per_l

    return score
