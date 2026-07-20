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

from agents.calc_tools import compute_xtb_energy, predict_solubility
from agents.qm_tools import get_qm_job_status, submit_qm_job
from chemclaw.config import settings

_INSTRUCTIONS = (
    "You are Chemclaw, an assistant for pharmaceutical/chemical process R&D. "
    "For fast questions use compute_xtb_energy (semiempirical GFN2-xTB single "
    "point) — it runs inline and caches, so comparing related molecules is cheap. "
    "Heavy quantum-mechanical jobs are slow: submit them with submit_qm_job, which "
    "returns a job id immediately; report that id instead of waiting, and use "
    "get_qm_job_status to check progress. Consult a loaded skill for the judgment "
    "on which calculator fits the question and how far to trust the result."
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
        tools=[compute_xtb_energy, predict_solubility, submit_qm_job, get_qm_job_status],
        context_providers=[skills],
    )


def _default_chat_client() -> Any:
    """Build the configured chat client (imported lazily to keep the provider optional)."""
    from agent_framework.anthropic import AnthropicClient

    return AnthropicClient(model=settings.agent_model)
