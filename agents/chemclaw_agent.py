"""The Chemclaw MAF agent (plan step 1.5).

`build_agent` wires the conversation agent: the QM job tools plus a
`SkillsProvider` that discovers `SKILL.md` files under the configured skills
directory (progressive disclosure — the model sees skill names/descriptions and
loads a skill body only when it needs the judgment). The chat client is
injectable so the wiring can be built and tested without live credentials; the
default builds the configured Anthropic client, which reads its own API key from
the environment at call time.
"""

from typing import Any

from agent_framework import Agent, FileSkillsSource, SkillsProvider

from agents.calc_tools import compute_xtb_energy, predict_pka, predict_solubility
from agents.graph_tools import expand_note, find_notes, propose_knowledge_note
from agents.qm_tools import get_qm_job_status, submit_qm_job
from agents.research_tools import gather_evidence
from agents.search_tools import (
    find_similar_molecules,
    find_similar_reactions,
    find_substructure_matches,
)
from chemclaw.config import settings

_INSTRUCTIONS = (
    "You are Chemclaw, a research assistant for pharmaceutical/chemical process R&D. Your job "
    "is to answer open-ended questions — about any output (yield, purity, impurities), any "
    "process detail or observation, and general protocol guidance — by drawing on every data "
    "source and tool available, and to help design new conditions/protocols grounded in that "
    "evidence.\n"
    "Research loop: (1) gather_evidence sweeps all internal sources at once (the knowledge "
    "graph — reactions, optimization campaigns, playbooks, reports — plus similar reactions "
    "when you pass a reaction SMILES); expand_note/find_notes drill into any cited note for "
    "the full step-by-step recipe, conditions, and outcomes. (2) For cross-learning by "
    "structure, find_similar_reactions gathers past runs of a transformation, "
    "find_similar_molecules/find_substructure_matches find analogous substrates or a "
    "functional group (then find_notes on a hit's SMILES to reach the reactions using it). "
    "(3) For properties use compute_xtb_energy / predict_pka / predict_solubility (inline, "
    "cached); heavy QM goes through submit_qm_job (returns a job id — report it, poll with "
    "get_qm_job_status).\n"
    "Discipline: cite the note id behind every claim; keep evidenced history separate from "
    "transferred analogy; say plainly when the data is silent rather than inventing it. "
    "Anything new worth keeping — a distilled rule, a proposed protocol or set of conditions — "
    "goes through propose_knowledge_note, which opens a PR for human review; never assert "
    "agent-written notes as established fact until merged. Load the deep-research skill for how "
    "to run this loop, and the calculation/search skills for which tool fits and how far to "
    "trust it."
)


def build_agent(chat_client: Any | None = None) -> Agent:
    """Construct the Chemclaw agent with its tools and skills.

    Args:
        chat_client: A MAF chat client. Injected in tests; when omitted, the
            configured Anthropic client is built (needs an API key at run time,
            not here).

    Returns:
        A ready-to-run `Agent`. No LLM call happens at construction.
    """
    client = chat_client if chat_client is not None else _default_chat_client()
    skills = SkillsProvider(FileSkillsSource([settings.skills_dir]))
    return Agent(
        client=client,
        name="chemclaw",
        instructions=_INSTRUCTIONS,
        tools=[
            compute_xtb_energy,
            predict_solubility,
            predict_pka,
            submit_qm_job,
            get_qm_job_status,
            find_notes,
            expand_note,
            gather_evidence,
            find_similar_reactions,
            find_similar_molecules,
            find_substructure_matches,
            propose_knowledge_note,
        ],
        context_providers=[skills],
    )


def _default_chat_client() -> Any:
    """Build the configured chat client (imported lazily to keep the provider optional)."""
    from agent_framework.anthropic import AnthropicClient

    return AnthropicClient(model=settings.agent_model)
