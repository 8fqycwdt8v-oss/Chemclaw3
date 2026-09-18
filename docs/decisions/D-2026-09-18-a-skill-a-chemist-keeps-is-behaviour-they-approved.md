# D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved — the per-actor skills tier, and the writer its design was blocked on

**Status:** accepted · **Date:** 2026-09-18

## Context

The owner asked for skills that evolve per user under a human gate.
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` §3 designed exactly that tier — a chemist's
own skills, live for their turns immediately and for nobody else's, with promotion into `skills/`
as the admin gate above — and recorded it **unbuilt**, with the reason stated plainly: *"there is no
distiller, so nothing writes into a per-actor directory and nobody can populate one."*

That reason is still true of a *distiller*. `make trajectory-census` reports zero on both arms
against a database that has never served a user, and this repository's standing rule is that a
miner over an empty table is a mechanism whose only caller is its own test — the `reject_widening`
shape `D-2026-08-15` deleted 254 lines for.

**But the distiller is not the only writer that design admits, and building the tier around it was
the omission.** A chemist asking for a procedure to be remembered is a writer, and the one this
system most obviously owes: the agent drafts the judgment into its answer, and a person decides
whether it becomes judgment.

## Decision

The tier ships with that writer.

**The write is an HTTP route and never a tool.** `POST /skills/mine` is the same shape
`api/routes/plan.py` uses, for the same reason: a model must never be able to authorize its own
behaviour change. A skill is injected into the prompt and reshapes every later answer with no
citation trail — the exact inverse of the property that makes ungated knowledge safe — so
`agent/skill_backend.SkillsReadOnlyRefusal` is untouched and no agent path writes a `SKILL.md`,
local or shared.

**Storage is the store, not a directory, and this is the one place that ADR's text is departed
from.** It says "the chemist's own skills directory", and a directory is wrong for the deployment
this ships into: a pod's filesystem is ephemeral and the chart runs `serverReplicas` of them, so a
local skill written to disk would vanish on the next restart and differ between replicas answering
the same chemist. `StoreBackend` over the `AsyncPostgresStore` that already serves `/memories/` is
multi-replica-safe, and — the reason that matters most — its namespace is the erasure key
`agent/leaver.py` already sweeps by prefix, so a departing person's skills leave with their
memories rather than needing a second mechanism that could disagree.

**The four invariants that ADR names are each answered somewhere findable.** Per-actor and resolved
per turn: the namespace closes over `get_current_actor()` at backend construction, which happens
inside `build_langgraph_agent` — per turn, because the graph is. Never a source of shared truth:
structural rather than gated, since another chemist's turn is mounted on a namespace that does not
contain it. Inspectable and removable: `GET`/`DELETE /skills/mine`, and that ADR calls this a
requirement rather than a nicety — an inspectable behaviour change nobody can withdraw is the worse
bargain, because the person has learned something is acting on them and still cannot stop it.
`SkillsReadOnlyRefusal` unchanged: above.

**The tier is advertised from the routes the backend really has**, not from the configured skill
trees, because it is mounted on two conditions the middleware cannot see. A source naming a path
with no route resolves to the composite's default `StateBackend` — an empty tier published to the
model on every turn of every deployment without a store.

**Availability rides the memory store and the coupling is stated rather than switched.** The tier is
available exactly when `/memories/` is: `agent_memory_enabled` with a Postgres session store. A
second flag would be a second switch for one resource, and
`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` is why it is not defaulted off
besides. `turn_store` is public for this — one function answering "is this available" for both
surfaces, rather than two spellings of two conditions where one later grows a third.

## Consequences

**None of the shared tree's four narrowings applies here, and three of them must not.**
`EnabledSkills`, `ProfileScopedSkills` and `RoleScopedSkills` answer governance questions about a
*shared* corpus, and the first would delete the tier outright: a deployment that sets
`CHEMCLAW_SKILLS_ENABLED` is naming shared skills, so every local one falls out of a list it was
never going to be in.

**`ToolScopedSkills` is the one that would have carried, and it is not applied.** Stated here
because the alternative is prose claiming a narrowing that is not there. It is absent because the
shared tree gets it from `declared_tools` reading frontmatter off a *directory*, and this tier is
stored: applying it would mean parsing every local skill's frontmatter out of the store
synchronously, inside a backend whose reason for existing is that it is not a filesystem. The cost
is bounded and one-sided — a chemist may be offered their own skill about a tool this profile
lacks, and the worst outcome is judgment they wrote being unhelpful to them. `BACKLOG.md` carries
the row.

**A skill is replaced rather than versioned.** "What is acting on my turns" must be a question with
one answer per name; a tier that accumulated drafts would answer it with a list. The earlier body is
not recoverable, which is why the route echoes what it stored.

**`MAX_LOCAL_SKILL_CHARS` is 16,000, derived rather than picked.** The largest skill this repository
ships is `protocol-generation` at 12,896 characters, so a chemist can write judgment as substantial
as anything reviewed into `skills/` while the shape that is not a skill at all — a transcript, a
pasted dataset — is refused. It is load-bearing for the reason `AgentProfile.instructions`' bound
is: a body is read into a model's context on demand, so an unbounded one is unbounded spend, and
this is the one tier where a *person* rather than this repository decides the text.
`tests/test_local_skills.py` checks the bound against the shipped tree, so a skill landing in
`skills/` that this tier could not hold turns it red.

**What the tier still cannot do, so nobody reads this as the whole feature**: nothing proposes a
skill automatically, nothing promotes a local skill into `skills/`, and nothing measures whether one
helped. The first is the distiller, still blocked on deployment history. The second needs the
promotion thresholds `D-2026-09-05` sketches. The third is `chemclaw_skill_loads_total{skill}`
(`D-2026-09-16-a-skill-nothing-counts-is-a-skill-nobody-can-retire`) plus a corpus, and the counter
already covers this tier because the label is the first path segment and the mount is `/mine/` —
which means a local skill's *name* reaches the exposition. That is a per-person label on a shared
metric and it is the thing to watch if this tier is ever busy.

**And one defect found by building it, recorded because the reasoning is transferable.**
`agent/skill_backend.py` states that the async write verbs need no override, because
`FilesystemBackend` implements them as `asyncio.to_thread(self.write, …)`. That is true of that base
class and **false of `StoreBackend`**, whose `awrite`/`aedit`/`adelete` are natively async — so four
sync overrides left the async path open, which is the path an async agent takes. Measured before the
fix: three of the four succeeded against a read-only tier, and the fourth refused, which is worse
than none refusing because it is what a spot check passes. The general rule: **an inherited-refusal
argument is about one base class and does not travel with the refusal.**

## What keeps it true

- `tests/test_local_skills.py::test_a_chemists_own_skill_reaches_their_turn` — through the mount,
  not the store, since rows existing is not a turn reaching them.
- `::test_one_chemists_skill_never_reaches_anothers_turn` — the isolation, which is structural.
- `::test_no_turn_may_write_its_owners_skills`
- `::test_every_method_this_tier_exposes_is_either_a_read_or_a_refusal` — the derived enumeration
  over `BackendProtocol` *and* `StoreBackend`, which is what found the async hole.
- `::test_the_tier_is_advertised_only_when_it_is_mounted` — all three arms.
- `::test_the_two_store_tiers_do_not_share_a_namespace`
- `::test_a_departing_chemists_own_skills_are_erased_with_their_memories` — through
  `leaver.store_prefixes`, the function `erase_actor` calls.
- `::test_a_skill_is_replaced_rather_than_versioned`,
  `::test_a_chemist_can_remove_what_is_acting_on_them`
- `::test_the_size_bound_is_larger_than_anything_this_repository_ships`
- `::test_the_label_carries_no_identity`
- `tests/test_route_auth_coverage.py` — that the four routes are gated like every other.
