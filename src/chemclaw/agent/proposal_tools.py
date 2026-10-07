"""The agent's one way to suggest a change to its own behaviour — a proposal, never a skill.

`propose_skill` writes a row in `behaviour_proposals` and nothing else; a person accepts it through
`POST /proposals/...`, so a model never authorizes its own work, and no turn writes a skill
(`agent/skill_backend.SkillsReadOnlyRefusal`). It is in `authz.STATE_CHANGING_TOOLS` so the plan
gate sees a turn proposing a behaviour change, and so it is subtracted from every helper's surface.
"""

from __future__ import annotations

import frontmatter

from chemclaw.agent.authz import require_actor
from chemclaw.agent.behaviour_proposals import (
    Proposal,
    content_hash,
    default_proposal_store,
)
from chemclaw.agent.local_skills import SkillRefused, validated_skill
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool


@tool
async def propose_skill(name: str, body: str, rationale: str) -> str:
    """Propose a procedure this chemist should keep, for them to accept or decline.

    Use this when a turn has worked out something reusable — a workup that has now gone wrong twice
    the same way, an order of operations that matters, a rule for choosing between two methods. Do
    not use it for facts: what is *true* belongs in the knowledge graph through the note tools, and
    is read with its citations beside it. This is for judgment that would change how later answers
    are written.

    Nothing here changes any behaviour. The proposal waits until the chemist accepts it, and only
    then does it act on their turns and nobody else's. Say in your answer that you have proposed it.

    Args:
        name: A short lowercase-and-hyphens name, which becomes the skill's directory.
        body: The whole `SKILL.md`, frontmatter included, exactly as it should be kept.
        rationale: Why this is worth keeping, in one or two sentences — what a reviewer reads first.

    Returns:
        What became of it: recorded as open, or the decision already standing for this exact text.

    Raises:
        ChemclawError: The body is not a valid `SKILL.md`, the name in the frontmatter disagrees
            with `name`, or the text is over `agent_local_skill_max_chars`. Refused rather than
            stored, because a proposal a person accepts must be a document that can actually be
            written — a malformed one would be accepted and then fail at the write, which is the
            worst place to discover it.
    """
    actor = require_actor()
    declared = _validated(name, body)
    store = default_proposal_store()
    digest = content_hash(body)
    # The row before this call, so the answer can say whether this proposed something or repeated
    # itself. Tested by the row's state, not its hash: only an open row makes this a repeat, since
    # `propose` revives a superseded body.
    before = await store.one(actor, "skill", declared, digest)
    repeated = before is not None and before.state == "open"
    outcome = await store.propose(
        Proposal(
            kind="skill",
            name=declared,
            content_hash=digest,
            content=body,
            rationale=rationale.strip(),
            actor=actor,
            session_id=get_current_session_id() or "",
            correlation_id=get_current_correlation_id() or "",
            state="open",
        )
    )
    return _what_became_of_it(declared, outcome, proposed_now=not repeated)


def _validated(name: str, body: str) -> str:
    """The name this body declares, checked against `name` and against the tier's own bounds.

    Validated at proposal time through `validated_skill` (the same checks `POST /skills/mine`
    makes), so an accepted proposal can always be written. It adds the check only this tool can
    make: the frontmatter's name and the `name` argument agree.

    Raises:
        ChemclawError: Worded for the model, naming what is wrong and what to send instead.
    """
    # Checked before the body, because a name the model got wrong is the cheaper thing to say and
    # `validated_skill` can only report the name the *frontmatter* declares.
    try:
        parsed = frontmatter.loads(body)
    except Exception as bad:
        raise ChemclawError(
            f"the body is not a `SKILL.md`: its YAML frontmatter could not be parsed ({bad}). It "
            "must open with `---`, a `name:` and a `description:`, then `---`."
        ) from bad
    declared = parsed.metadata.get("name") if isinstance(parsed.metadata, dict) else None
    if isinstance(declared, str) and declared != name.strip():
        raise ChemclawError(
            f"the frontmatter declares the name {declared!r} and the `name` argument is "
            f"{name.strip()!r}. Send the same name in both, since the frontmatter is what a later "
            "turn reads."
        )
    try:
        return validated_skill(body, expected_name=name.strip())
    except SkillRefused as refused:
        # Re-raised as `ChemclawError` so the tool's contract is one exception type, worded for the
        # model.
        raise ChemclawError(str(refused)) from refused


def _what_became_of_it(name: str, outcome: Proposal, *, proposed_now: bool) -> str:
    """What to tell the model: proposed, already open, or already decided.

    Each calls for a different next move: mention it to the chemist; stop repeating it; or respond
    to the decision's reason rather than retry. A revived (previously superseded) proposal is
    waiting again, so it reads as proposed.
    """
    if outcome.decided:
        return (
            f"{name!r} was already {outcome.state} by this chemist"
            + (f": {outcome.reason}" if outcome.reason else "")
            + ". The same text cannot reopen a decision, so do not propose it again. If the "
            "wording should change in the light of that, propose the new wording as a new skill."
        )
    if not proposed_now:
        return (
            f"{name!r} is already waiting for this chemist to decide — you proposed this exact "
            "text before and nothing changed. Mention it once and move on."
        )
    return (
        f"proposed {name!r}. It is waiting for this chemist to accept or decline, and changes "
        "nothing until they do. Say in your answer that you have proposed it and what it says."
    )
