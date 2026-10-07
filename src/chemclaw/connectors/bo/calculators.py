"""The calculators a BO campaign consults, bound to the calculation server and the D-011 cache.

`science/bo` declares what it needs as injected callables (`PropertiesFor`, `LogSFor`); they are
bound here because `chemclaw.science` may import only `chemclaw.core`, while the client lives in
`connectors/calc/remote.py`. Both go through `cached_remote`, so a molecule seen before is served
from the calculation store and never recomputed.
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

        The reference is the `calc_key` the server stamps on every result, read off the payload so
        an `experiment-proposal` note can cite it on a cache hit as well as a miss.
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
