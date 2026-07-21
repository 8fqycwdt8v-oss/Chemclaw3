"""The Chemclaw MAF agent (plan step 1.5; harness backbone: docs/harness-konzept.md).

`build_agent` wires the conversation agent: the domain tools plus a `SkillsProvider`
that discovers `SKILL.md` files under the configured skills directory (progressive
disclosure — the model sees skill names/descriptions and loads a skill body only when
it needs the judgment). The chat client is injectable so the wiring can be built and
tested without live credentials; the default builds the configured Anthropic client,
which reads its own API key from the environment at call time.

Two backbones behind one factory (D-020):

- **Classic** (`harness_enabled=False`, the tested default): a plain `Agent` — one
  reasoning step per turn, no self-planning.
- **Harness** (`harness_enabled=True`): `create_harness_agent` adds a self-managed todo
  list (`TodoProvider`) and an explicit plan/execute mode (`AgentModeProvider`) over the
  *same* tools and skills, so open multi-step requests are decomposed into a visible,
  checkable plan and worked through autonomously. The generic file-memory / file-access /
  shell / web-search batteries the harness enables by default are turned OFF here:
  Chemclaw's capability is its explicit tools and skills, not a generic filesystem or
  shell (architektur.md §6, plan G6). Long/expensive work still hands off fire-and-forget
  to Temporal (D-002) — the harness only sequences the short reasoning steps.
"""

from typing import Any

from agent_framework import (
    Agent,
    FileSkillsSource,
    SkillsProvider,
    create_harness_agent,
    todos_remaining,
)

from agents.calc_tools import compute_xtb_energy, predict_pka, predict_solubility
from agents.graph_tools import expand_note, find_notes, propose_knowledge_note
from agents.qm_tools import get_qm_job_status, submit_qm_job
from chemclaw.config import settings

_INSTRUCTIONS = (
    "You are Chemclaw, an assistant for pharmaceutical/chemical process R&D. "
    "For fast questions use compute_xtb_energy (semiempirical GFN2-xTB single "
    "point) — it runs inline and caches, so comparing related molecules is cheap. "
    "Heavy quantum-mechanical jobs are slow: submit them with submit_qm_job, which "
    "returns a job id immediately; report that id instead of waiting, and use "
    "get_qm_job_status to check progress. Before computing, check what is already "
    "known: find_notes then expand_note traverse the knowledge graph (cite the note "
    "ids you use). New findings worth keeping go through propose_knowledge_note, "
    "which opens a PR for human review — never assert agent-written notes as "
    "established fact until merged. Consult a loaded skill for the judgment on which "
    "calculator or note fits the question and how far to trust the result."
)

# The domain tools the agent advertises, in both backbones. The QM tools are the thin
# fire-and-forget adapter to Temporal (D-002); the rest run inline (calc are cached,
# graph tools are file I/O / the PR-gate). This one list is the single source for both
# the classic Agent and the harness agent (DRY).
_TOOLS = [
    compute_xtb_energy,
    predict_solubility,
    predict_pka,
    submit_qm_job,
    get_qm_job_status,
    find_notes,
    expand_note,
    propose_knowledge_note,
]


def build_agent(chat_client: Any | None = None) -> Agent:
    """Construct the Chemclaw agent with its tools and skills.

    Selects the classic or harness backbone from `settings.harness_enabled`. Either way
    the same tools and skills are wired and no LLM call happens at construction.

    Args:
        chat_client: A MAF chat client. Injected in tests; when omitted, the
            configured Anthropic client is built (needs an API key at run time,
            not here).

    Returns:
        A ready-to-run `Agent`.
    """
    client = chat_client if chat_client is not None else _default_chat_client()
    skills = SkillsProvider(FileSkillsSource([settings.skills_dir]))

    if not settings.harness_enabled:
        return Agent(
            client=client,
            name="chemclaw",
            instructions=_INSTRUCTIONS,
            tools=_TOOLS,
            context_providers=[skills],
        )

    return create_harness_agent(
        client=client,
        name="chemclaw",
        agent_instructions=_INSTRUCTIONS,
        tools=_TOOLS,
        skills_provider=skills,
        # Chemclaw's capability is its explicit tools/skills, not a generic sandbox —
        # keep only the todo + plan/execute providers; drop the file/shell/web batteries.
        disable_file_memory=True,
        disable_file_access=True,
        disable_web_search=True,
        loop_should_continue=_loop_predicate(),
        loop_max_iterations=settings.harness_max_loop_iterations,
    )


def _loop_predicate() -> Any:
    """Return the completion-loop predicate for the configured autonomy level, or None.

    "execute" autonomy loops the agent through its open todos, but only while it is in
    *execute* mode — so a plan is still made (and can be approved) in plan mode before any
    autonomous work begins. "plan_only" returns None, so no loop middleware is added and
    the agent stays interactive.
    """
    if settings.harness_autonomy == "execute":
        return todos_remaining(looping_modes=["execute"])
    return None


def _default_chat_client() -> Any:
    """Build the configured chat client (imported lazily to keep the provider optional)."""
    from agent_framework.anthropic import AnthropicClient

    return AnthropicClient(model=settings.agent_model)
