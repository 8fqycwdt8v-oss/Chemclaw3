"""A credential-free walkthrough of the agent's research loop (no live LLM).

The MAF agent needs a provider API key to actually converse, which this sandbox does not have.
This script instead drives the *same tools the agent calls* over a seeded in-memory corpus, so
you can watch the loop turn a question into a cited, computed, and optimization-backed answer
without any credentials or database. It answers one concrete question end to end:

    "For the ethyl-acetate esterification, what moved the yield, would 2-MeTHF (untried) be a
     reasonable solvent to try, and what conditions should I run next?"

Every store is in-memory and the knowledge graph is a temp directory, seeded here; the tools
(`gather_evidence`, `find_similar_reactions`, `predict_solubility`, `suggest_next_experiment`)
are the real ones, wired through the module seams tests use.

Run: `python -m examples.research_demo`.
"""

import asyncio
import tempfile
from pathlib import Path

import agents.calc_tools as calc_tools
import agents.research_tools as research_tools
import agents.search_tools as search_tools
from agents.bo_tools import suggest_next_experiment
from agents.calc_tools import predict_solubility
from agents.research_tools import gather_evidence
from agents.search_tools import find_similar_reactions
from bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from calc.store import InMemoryStore
from chemclaw.config import settings
from mcp_servers.fpstore import InMemoryFingerprintStore
from mcp_servers.rxnfp.search import record_for_reaction

# The transformation under study and two of its recorded runs.
_ESTER = "CCO.CC(=O)O>>CCOC(C)=O"
_SOLVENTS = {"THF": "C1CCOC1", "2-MeTHF": "CC1CCCO1"}


def _seed_graph(directory: Path) -> None:
    """Write a mini knowledge graph: two runs and the optimization-campaign that compares them."""
    (directory / "rxn-cold.md").write_text(
        "---\nid: reaction-ester-40c\ntype: reaction\n---\n"
        "Esterification in THF at 40 C, 6 h. Yield 55%. Slow conversion, unreacted acid.\n",
        encoding="utf-8",
    )
    (directory / "rxn-hot.md").write_text(
        "---\nid: reaction-ester-80c\ntype: reaction\n---\n"
        "Esterification in THF at 80 C, 4 h. Yield 85%. Trace diethyl ether impurity.\n",
        encoding="utf-8",
    )
    (directory / "campaign.md").write_text(
        "---\nid: optimization-ester\ntype: optimization-campaign\n---\n"
        "Raising temperature from 40 to 80 C moved yield 55% -> 85%; time was the lesser lever. "
        "[[reaction-ester-40c]] [[reaction-ester-80c]]\n",
        encoding="utf-8",
    )


def _seed_reaction_store() -> InMemoryFingerprintStore:
    """A reaction fingerprint index holding the esterification run."""
    store = InMemoryFingerprintStore()
    asyncio.run(store.add(record_for_reaction("ester-80c", _ESTER)))
    return store


async def _investigate() -> str:
    """Run the loop and return a narrated, cited transcript of the agent's answer."""
    lines: list[str] = ["# Chemclaw research-loop demo (no LLM)\n"]

    # 1) Gather cited evidence across every internal source in one sweep.
    evidence = await gather_evidence("yield", reaction_smiles=_ESTER)
    lines.append("## 1. Evidence gathered (cited)")
    for chunk in evidence:
        lines.append(f"- [{chunk.retriever}] [[{chunk.source_note_id}]]: {chunk.content}")

    # 2) Cross-learn by structure: past runs of this exact transformation.
    hits = await find_similar_reactions(_ESTER)
    lines.append("\n## 2. Structurally similar past reactions")
    for hit in hits:
        lines.append(f"- [[{hit.reaction_note_id}]] (Tanimoto {hit.similarity:.2f})")

    # 3) Proactively compute a property the record is silent on: is the untried solvent
    #    2-MeTHF comparable to the tested THF? Real ESOL model, no database.
    lines.append("\n## 3. Proactive computation (untried solvent, unprompted)")
    for name, smiles in _SOLVENTS.items():
        result = await predict_solubility(smiles)
        tested = "tested" if name == "THF" else "UNTRIED"
        lines.append(
            f"- {name} ({tested}): log S = {result.log_s_mol_per_l:.2f} "
            f"± {result.uncertainty_log:.2f} ({result.model})"
        )

    # 4) Design the next experiment from the runs on file (Bayesian optimization).
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=40.0, upper=110.0),
            CategoricalParameter(name="solvent", categories=list(_SOLVENTS)),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )
    observations = [
        Observation(params={"temperature": 40.0, "solvent": "THF"}, value=55.0),
        Observation(params={"temperature": 80.0, "solvent": "THF"}, value=85.0),
    ]
    candidates = await suggest_next_experiment(problem, observations, count=1)
    nxt = candidates[0].params
    lines.append("\n## 4. Suggested next experiment (a proposal a human runs)")
    lines.append(f"- temperature {nxt['temperature']:.0f} C, solvent {nxt['solvent']}")

    lines.append("\n## Answer the agent would compose")
    lines.append(
        "Temperature was the yield lever: 40->80 C moved 55%->85% ([[optimization-ester]], "
        "[[reaction-ester-40c]], [[reaction-ester-80c]]). 2-MeTHF is a reasonable solvent to "
        "try — its predicted aqueous solubility is close to THF's (section 3), and it is a "
        "common greener THF replacement (analogy, not on file). Next run to try: section 4."
    )
    return "\n".join(lines)


def run_demo() -> str:
    """Seed the in-memory corpus, wire the tool seams, and return the transcript.

    Restores every global it swaps on the way out, so it is safe to call in-process (a test)
    as well as from the command line.
    """
    saved_dir = settings.knowledge_dir
    saved_research = research_tools._reaction_store
    saved_search = search_tools._reaction_store
    saved_calc = calc_tools.default_store
    try:
        with tempfile.TemporaryDirectory() as tmp:
            settings.knowledge_dir = tmp
            _seed_graph(Path(tmp))
            reaction_store = _seed_reaction_store()
            calc_store = InMemoryStore()
            # The in-memory stores satisfy the same contracts as the production Postgres ones;
            # the ignores are only because these attributes are typed to the concrete backend.
            research_tools._reaction_store = lambda: reaction_store  # type: ignore[assignment,return-value]
            search_tools._reaction_store = lambda: reaction_store  # type: ignore[assignment,return-value]
            calc_tools.default_store = lambda: calc_store
            return asyncio.run(_investigate())
    finally:
        settings.knowledge_dir = saved_dir
        research_tools._reaction_store = saved_research
        search_tools._reaction_store = saved_search
        calc_tools.default_store = saved_calc


if __name__ == "__main__":
    print(run_demo())
