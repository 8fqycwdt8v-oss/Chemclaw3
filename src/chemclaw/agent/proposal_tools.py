"""The agent's one way to suggest a change to its own behaviour — a proposal, never a skill.

**Standing, stated first because it is what this file is about.** `propose_skill` writes a row in
`behaviour_proposals` and nothing else. It does not write a `SKILL.md`, it does not reach either
skills tier, and nothing in any later prompt contains what it wrote until a *person* accepts it
through `POST /proposals/...`. `agent/skill_backend.SkillsReadOnlyRefusal` is untouched: a turn
still reads both tiers and writes neither.

That is the same separation `agent/pending_tools.py` draws and for the same reason — a model must
never be able to authorize its own work. The asymmetry there is a tool that *asks* beside a route
that *answers*; here it is a tool that *proposes* beside a route that *decides*.

**Why the agent gets to propose at all**, given that
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` refuses it a skill outright: because
the refusal was about the *write*, not about the *suggestion*,
and the tier this proposal lands in when accepted is one person's own
(`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`). Before this tool the agent's
only channel was prose in an answer, which a chemist had to notice, copy and post. That is a worse
control than it looks: what a person approves should be a document they were shown, and a
copy-paste is where a document changes without anybody deciding it did.

**State-changing, and for the gate rather than the row.** `propose_skill` is in
`authz.STATE_CHANGING_TOOLS`. Writing a row is the small reason; the real one is that a turn
proposing a change to what the agent does is something the plan gate should see. It also takes the
tool out of every helper's surface by arithmetic — `side_effecting_tools()` is subtracted on both
halves — which is correct and is the `ask_clarifying_question` argument exactly: a helper proposing
behaviour changes from a context the chemist cannot see is worse than a helper that cannot.
"""

from __future__ import annotations

import frontmatter

from chemclaw.agent.authz import require_actor
from chemclaw.agent.behaviour_proposals import (
    Proposal,
    content_hash,
    default_proposal_store,
)
from chemclaw.agent.skill_manifest import SkillManifest
from chemclaw.core.config import settings
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
    # What was there *before* this call, so the answer can tell the model whether it has just
    # proposed something or repeated itself. The store knows — it books the distinction on
    # `chemclaw_behaviour_proposals_total` — but `propose` returns the standing row either way, so
    # the two are indistinguishable from the result alone.
    before = await store.one(actor, "skill", declared, digest)
    standing = before.content_hash if before is not None else ""
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
    return _what_became_of_it(declared, outcome, proposed_now=outcome.content_hash != standing)


def _validated(name: str, body: str) -> str:
    """The name this body declares, checked against `name` and against the tier's own bounds.

    **Validated here rather than at acceptance, because a proposal a person accepts must be a
    document that can actually be written.** `POST /skills/mine` refuses a malformed `SKILL.md`, so
    a proposal that skipped this check would be reviewed, accepted, and then fail at the write —
    which is the worst place to discover it, since the person has already decided and the failure
    looks like the system losing their decision.

    The same three checks the route makes, plus one the route cannot: that the frontmatter's name
    and the tool's `name` argument agree. Two sources of one name can disagree, and the one a reader
    believes is whichever the code happens to consult — the reason `agent/profile_discovery.py`
    refuses a `name:` key beside a filename. Here the model writes both, so a mismatch is a model
    error worth surfacing rather than a precedence rule worth inventing.

    Raises:
        ChemclawError: Worded for the model, naming what is wrong and what to send instead.
    """
    if len(body) > settings.agent_local_skill_max_chars:
        raise ChemclawError(
            f"a skill may be at most {settings.agent_local_skill_max_chars} characters and this "
            f"one is {len(body)}. A skill is judgment, not a transcript — write the rule, not the "
            "worked example that produced it."
        )
    try:
        parsed = frontmatter.loads(body)
    except Exception as bad:
        raise ChemclawError(
            f"the body is not a `SKILL.md`: its YAML frontmatter could not be parsed ({bad}). It "
            "must open with `---`, a `name:` and a `description:`, then `---`."
        ) from bad
    try:
        manifest = SkillManifest.model_validate(parsed.metadata)
    except Exception as bad:
        raise ChemclawError(
            f"the frontmatter is not a valid skill manifest: {bad}. It takes `name`, "
            "`description`, and optionally `tools` and `tags` — nothing else."
        ) from bad
    if manifest.name != name.strip():
        raise ChemclawError(
            f"the frontmatter declares the name {manifest.name!r} and the `name` argument is "
            f"{name.strip()!r}. Send the same name in both, since the frontmatter is what a later "
            "turn reads."
        )
    if "/" in manifest.name or manifest.name.startswith("."):
        raise ChemclawError("a skill name may not contain '/' or start with '.'")
    if any(character.isspace() or not character.isprintable() for character in manifest.name):
        raise ChemclawError(
            "a skill name may not contain whitespace or control characters — use hyphens."
        )
    return manifest.name


def _what_became_of_it(name: str, outcome: Proposal, *, proposed_now: bool) -> str:
    """What to tell the model, which is three different things and was one.

    **A proposer can learn what became of its proposal**, and that is a requirement rather than a
    courtesy: without it the only strategy available to a model is to propose again, which is the
    behaviour this queue's idempotence exists to make harmless and its counters exist to make
    visible. Three answers, because the three situations call for different next moves:

    - *proposed* — say so in the answer, so the chemist knows there is something to look at.
    - *already open* — it is already waiting; repeating it adds nothing and the model should stop.
    - *already decided* — a person answered. Proposing the same text cannot reopen it, and the
      model is told the verdict and the reason so it can respond to the reason rather than retry.
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
