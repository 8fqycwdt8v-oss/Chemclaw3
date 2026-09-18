"""A chemist's own skills: judgment that shapes their turns and nobody else's.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` §3 designed this tier and recorded it
unbuilt, with the reason it was blocked: *"there is no distiller, so nothing writes into a per-actor
directory and nobody can populate one."* That is still true of a *distiller* — `make
trajectory-census` reports zero on a database that has never served a user — so the tier ships with
the other writer that argument overlooked: **the chemist**. They ask for a procedure to be
remembered, the agent drafts the text into its answer, and the person posts it through a route.

**No agent path writes a skill, and that is unchanged rather than relaxed.** `SkillsReadOnlyRefusal`
still refuses every write verb, here as on the shared tree — `_LOCAL_READ_ONLY` below is the same
refusal worded for this root. The write is an HTTP route a person calls, which is the same shape
`api/routes/plan.py` uses for exactly the same reason: a model must never be able to authorize its
own behaviour change.

**The invariants this tier owes**, from that ADR, and where each one lives:

- **Per-actor, resolved per turn.** The namespace closes over `get_current_actor()` at backend
  construction, which happens inside `build_langgraph_agent` — per turn, because the graph is. A
  directory resolved once at startup would be one chemist's skills served to everyone.
- **Never a source of shared truth.** Nothing promotes one, nothing cites one, and no other actor's
  turn can name the namespace: `local_skills_namespace` takes the actor and the route is only
  mounted when a turn has one.
- **The user can see what it does.** `api/routes/skills.py` lists, reads and deletes; a behaviour
  change nobody can inspect is the property that makes the *shared* tree need a gate, and this tier
  earns its exemption by being visible to the one person it affects.
- **`SkillsReadOnlyRefusal` unchanged.** See above.

**Storage is the store rather than a directory, and this is the one place that ADR's text is
departed from.** It says "the chemist's own skills directory", and a directory is wrong for the
deployment this ships into: a pod's filesystem is ephemeral and the chart runs `serverReplicas` of
them, so a local skill written to disk would vanish on the next restart and differ between replicas
answering the same chemist. `StoreBackend` over the `AsyncPostgresStore` that already serves
`/memories/` is multi-replica-safe, and — the reason that matters most here — its namespace is the
erasure key `agent/leaver.py` already sweeps by prefix, so a departing person's local skills leave
with their memories instead of needing a second mechanism that could disagree.

**None of the four narrowings applies here, and three of them must not.** `EnabledSkills`,
`ProfileScopedSkills` and `RoleScopedSkills` answer governance questions about a *shared* corpus —
did this deployment turn it on, is this agent about it, may this caller see it — and none has an
answer for a skill whose only reader is the person who wrote it. The first would delete the tier
outright: a deployment that sets `CHEMCLAW_SKILLS_ENABLED` is naming shared skills, so every local
one would fall out of a list it was never going to be in. What bounds this tier instead is
structural and stronger than a predicate — the namespace closes over one actor, so another chemist's
turn cannot reach it at all.

**`ToolScopedSkills` is the one that would have been worth keeping, and it is not applied. Said
plainly because the alternative is prose claiming a narrowing that is not there.** It asks whether
this agent can do any of what a skill teaches, which is as true of a personal skill as a shipped
one, and a local skill about tools the turn cannot reach is misleading in exactly the way that
narrowing exists to prevent. It is absent because the shared tree gets it from `declared_tools`
reading frontmatter off a *directory*, and this tier is stored rather than filed: applying it would
mean parsing every local skill's frontmatter out of the store synchronously, inside a backend whose
whole reason for existing is that it is not a filesystem. The cost of the gap is bounded and
one-sided — a chemist may be offered their own skill about a tool this profile lacks, and the worst
outcome is judgment they wrote being unhelpful to them. `docs/planning/BACKLOG.md` carries the row.
"""

from typing import Any

from deepagents.backends import StoreBackend

from chemclaw.agent.refusal_route import routed
from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal
from chemclaw.core.ids import stable_hash

#: The root a chemist's own skills are mounted at, and the label the model sees in their paths.
#:
#: `mine` rather than the actor's digest, which is what the namespace uses: a digest in a path is a
#: stable identifier for a person appearing in the system prompt of every turn and in every log line
#: that quotes one. The route is per-actor by construction — the namespace closes over the turn's
#: own actor — so the path needs to carry no identity at all.
LOCAL_SKILLS_ROOT = "/mine/"

#: The same label without its slashes, for the skills middleware's source list.
LOCAL_SKILLS_LABEL = "mine"

#: What a refused *write* to this tier says. The shared tree's wording names a reviewed commit as
#: the way in, which is wrong here — a local skill changes through a route its owner calls — so the
#: sanctioned path differs while the refusal does not.
_LOCAL_READ_ONLY = routed(
    "your own skills are read-only to a turn — a skill is judgment that reshapes later answers, "
    "so it changes only when you decide it does, not when a turn decides. Nothing was changed.",
    code="local_skills_read_only",
    boundary="the chemist's own skills tier, which no turn may write",
    who_can_act="the chemist who owns it, through the skills route",
    sanctioned_path="draft the skill in your answer and say it can be saved from there",
)


def local_skills_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's own skills live under.

    Digested for the two reasons `scratchpad.memory_namespace` gives and shares its shape with:
    `store` validates namespace components against a character class that rejects most punctuation,
    and an actor has more than one spelling in this system (`unverified:<id>` is the other), so a
    digest is always legal and collapses neither spelling into the other.

    A **different first component** from `memories`, so the two tiers are separately erasable and
    separately countable, and so a bug in one cannot serve the other's rows.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return ("local-skills", stable_hash(actor))


def local_skills_prefix(actor: str) -> str:
    """The `store.prefix` value naming one person's own skills, for the erasure sweep.

    Exposed so `agent/leaver.py` builds the same string this module writes under rather than
    re-deriving the join — the defect class where two modules agree about a key until one is edited.

    Args:
        actor: The departing person's id.

    Returns:
        The dotted prefix under which every local skill of theirs is stored.
    """
    return ".".join(local_skills_namespace(actor))


class ReadOnlyStoreBackend(StoreBackend):
    """A `StoreBackend` whose write half is refused, for the tier a turn may read and not change.

    The shared tree gets this property from `NarrowedSkillsBackend`, which is a `FilesystemBackend`;
    this tier is stored rather than filed, so the same refusal has to be stated against the other
    base class. Both raise `SkillsReadOnlyRefusal`, which is what makes a refusal read to the model
    as an access-control decision rather than a fault, and what lands it in the audit trail as a
    refusal (`agent/skill_backend.py` carries the argument).

    **Derived rather than listed, because the write half grows.** deepagents 0.7 added `delete` to
    the protocol and the shared tree inherited a working one until a test caught it. Here the same
    risk is answered the same way: `tests/test_local_skills.py` enumerates the protocol's write
    verbs and asserts each is refused, so a bump that adds a seventh turns red rather than quietly
    handing a turn a way to rewrite its owner's judgment.
    """

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a turn may read its chemist's skills and may not change them."""
        raise SkillsReadOnlyRefusal(_LOCAL_READ_ONLY)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(_LOCAL_READ_ONLY)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        Deleting is the verb a chemist most plausibly wants and least plausibly wants *a turn* to
        have: the route is where they do it, and a turn that could would be one bad inference away
        from removing judgment its owner still relies on.
        """
        raise SkillsReadOnlyRefusal(_LOCAL_READ_ONLY)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(_LOCAL_READ_ONLY)


#: The document inside a skill directory that makes it a skill, mirroring the shared tree's shape so
#: one chemist's own tier is discovered by the same rule as `skills/` and reads the same to a model.
LOCAL_SKILL_FILENAME = "SKILL.md"


def _key(name: str) -> str:
    """The store key one local skill's body lives under.

    Leading slash and trailing `SKILL.md`, because that is the shape `StoreBackend` writes and reads
    — measured rather than assumed, since a key this module invented would be a second definition
    that upstream could walk away from on a bump.
    """
    return f"/{name}/{LOCAL_SKILL_FILENAME}"


def _writer(store: Any, actor: str) -> StoreBackend:
    """A writable backend over one person's own skills, for the route that saves them.

    **Plain `StoreBackend`, deliberately, and this is the only place one is built.** The turn's
    backend is `ReadOnlyStoreBackend` over the same namespace; this is the other side of the same
    tier, reached from an HTTP route a person calls rather than from anything a model holds. Using
    upstream's writer rather than putting rows in directly is what keeps the stored shape — the
    key, the `content`/`encoding` pair, the two timestamps — with exactly one definition, so a
    bump that changes it changes both halves together.
    """
    namespace = local_skills_namespace(actor)
    return StoreBackend(namespace=lambda _runtime: namespace, store=store)


async def save_local_skill(store: Any, actor: str, name: str, body: str) -> None:
    """Write one of a chemist's own skills, replacing any earlier version of that name.

    No merge and no version history: a skill is judgment its owner is asserting *now*, and a tier
    that accumulated every draft would make "what is acting on my turns" a question with a list for
    an answer. The route validates `body` as a `SkillManifest` before calling this, so a malformed
    skill is a 422 rather than a file the listing then skips.

    Args:
        store: The process's store.
        actor: Whose tier to write, in the turn's own actor spelling.
        name: The skill's name, which is also its directory.
        body: The whole `SKILL.md`, frontmatter included.
    """
    await _writer(store, actor).awrite(_key(name), body)


async def list_local_skills(store: Any, actor: str) -> list[str]:
    """The names of one chemist's own skills, sorted.

    Read off the store rather than off the mounted backend, because this answers a *route's*
    question — "what is acting on my turns" — and the route has no turn and therefore no mount.
    """
    items = await store.asearch(local_skills_namespace(actor))
    suffix = f"/{LOCAL_SKILL_FILENAME}"
    return sorted(item.key[1 : -len(suffix)] for item in items if item.key.endswith(suffix))


async def read_local_skill(store: Any, actor: str, name: str) -> str | None:
    """One of a chemist's own skills, verbatim, or `None` if they have no skill by that name."""
    item = await store.aget(local_skills_namespace(actor), _key(name))
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


async def delete_local_skill(store: Any, actor: str, name: str) -> bool:
    """Remove one of a chemist's own skills. Returns whether there was one to remove.

    The half of "the user can see what it does" that makes the rest worth having: an inspectable
    behaviour change nobody can withdraw is a worse bargain than one nobody can see, because the
    person has learned there is something acting on them and still cannot stop it.
    """
    namespace = local_skills_namespace(actor)
    if await store.aget(namespace, _key(name)) is None:
        return False
    await store.adelete(namespace, _key(name))
    return True
