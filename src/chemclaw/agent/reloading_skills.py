"""The skills middleware this repository composes: upstream's, re-narrowed on every run.

Two subclasses of `deepagents` types, in their own module so that importing the graph builder
does not import `deepagents`; `langgraph_agent._skills_middleware` imports them when it
compiles a graph.
"""

from typing import Annotated, NotRequired

from deepagents.middleware.skills import SkillMetadata, SkillsMiddleware, SkillsState
from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.channels.untracked_value import UntrackedValue

from chemclaw.agent.langgraph_agent import NO_SKILLS


class ReloadingSkillsState(SkillsState):
    """`SkillsState` with its cached listing moved to a channel the checkpointer cannot restore.

    One redeclared field, and it is the whole reloading mechanism. Upstream annotates
    `skills_metadata` with `PrivateStateAttr` only, so it resolves to a checkpointed `LastValue`;
    `UntrackedValue` is never written to a checkpoint (`checkpoint()` returns `MISSING`), so the
    key is absent at the start of every run of the graph and upstream's own
    `if "skills_metadata" in state: return None` short-circuit simply does not fire.

    Measured over three turns on one thread, counting whether the key was already in state when
    `before_agent` ran: `[False, True, True]` on upstream's channel and `[False, False, False]` on
    this one.

    The same mechanism `ChemclawState.loop_capped` uses, and for the same reason: "per turn" is a
    property of the channel, not of a caller who remembers to clear it.
    """

    skills_metadata: NotRequired[Annotated[list[SkillMetadata], UntrackedValue, PrivateStateAttr]]


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """`SkillsMiddleware` that re-narrows its listing every turn instead of caching it.

    The role gate reads the turn's identity, so a listing cached from one caller or an earlier role
    would be wrong. The subclass is one redeclared state field (`ReloadingSkillsState`), depending
    only on the field name pinned by `tests/test_upstream_surface.py`. A staleness fix, not the
    gate: `NarrowedSkillsBackend` refuses reads on every call regardless.
    """

    state_schema = ReloadingSkillsState

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Upstream's listing, except when there is nothing to list — see `NO_SKILLS`.

        Upstream's empty-listing text invites writing a skill, which `NarrowedSkillsBackend`
        refuses. A private-method override, pinned by `tests/test_upstream_surface.py`.
        """
        if not skills:
            return NO_SKILLS
        return str(super()._format_skills_list(skills))
