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

**Two bounds, because either alone is unbounded in the other**, and the second one was missing
from the first version of this record.

`agent_local_skill_max_chars` is 16,000, derived rather than picked: the largest skill this
repository ships is `protocol-generation` at 12,896 characters, so a chemist can write judgment as
substantial as anything reviewed into `skills/` while the shape that is not a skill at all — a
transcript, a pasted dataset — is refused. That bounds a **body**, which is read into context on
demand, and `tests/test_local_skills.py` checks it against the shipped tree so a skill landing in
`skills/` that this tier could not hold turns it red.

`agent_local_skills_max` is 20, and it bounds something else: **the prefix of every model call its
owner makes.** A skill's name and description are listed in the system message unconditionally —
only the body is on demand — so the row count is a multiplier on the one part of a request nothing
can compact. This paragraph originally said `agent_memory_max_files` held that, and it does not:
that cap is enforced by `scratchpad.BoundedStoreBackend`, which mounts `/memories/` and not this
root, so nothing on either half of this tier ever counted a row. Measured on a compiled graph with
the connector surface bound, an empty mounted tier costs **6** tokens and a maximal one — 20 rows
at deepagents' 1,024-character description limit — costs **5,571**;
`tests/test_context_floor.py::test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows`
is the ratchet, and it is deliberately *not* folded into `CEILINGS`: that file bounds what this
repository ships to everybody, and charging every deployment 5,600 tokens of thread allowance for a
tier almost none of them will fill is the wrong trade. Nothing is lost by keeping it out, because
`agent/context_budget.prefix_tokens` charges the prefix of the call actually in flight.

The row cap is **refused rather than evicted**, which is the one place this tier departs from the
memory tier's shape on purpose: a memory a turn wrote may be dropped silently, and judgment a person
authored may not vanish because they wrote one more. A *replacement* is allowed at the cap, or a
chemist who filled it could not correct any of them.

**A third bound rides on `SkillManifest`, and it is upstream's own.** deepagents truncates a name or
a description past the Agent Skills spec's limits and carries on, so an over-long description is
served half with nothing but a log saying so, and a name over the limit is stored under one spelling
and listed under another. `agent/skill_manifest.py` imports those two constants and declares them as
pydantic bounds, which turns both into a CI failure for the reviewed tree and a 422 for this one.
`tests/test_upstream_surface.py` holds the assumption that upstream truncates, because a bump that
made it *refuse* would put a person's skill in the store and out of the listing with no error
anywhere.

**What the tier still cannot do, so nobody reads this as the whole feature**: nothing proposes a
skill automatically, nothing promotes a local skill into `skills/`, and nothing measures whether one
helped. The first is the distiller, still blocked on deployment history. The second needs the
promotion thresholds `D-2026-09-05` sketches.

**The third is a counter, and this paragraph shipped wrong about it in both directions at once.**
It said `chemclaw_skill_loads_total{skill}`
(`D-2026-09-16-a-skill-nothing-counts-is-a-skill-nobody-can-retire`) "already covers this tier
because the label is the first path segment", and warned that a local skill's *name* therefore
reaches the exposition. Measured: a shipped skill and a personal one read through the same mount in
one process left the labelled series at 1 for the shipped one and **no series at all** for the
other. That counter lives on `NarrowedSkillsBackend`, a `FilesystemBackend`; this tier is a
`StoreBackend`. So the coverage claim was false and the privacy warning was a warning about
nothing — which is the same base-class mistake as the async write verbs below, one method over, and
the reason the rule that finding leaves behind is written about *inheritance* rather than about
writes.

`chemclaw_local_skill_loads_total` is the tier's own signal, on `read` and `aread` both, counted
off the result rather than the path so a failed read and a zero-line read book nothing. **Bare
rather than labelled**, and that is why it is a second series rather than a label on the first: a
local skill's name is a person's own words, clamped by nothing, and a label would mint a series per
private project name in a shared exposition. `Chemclaw3-mcp` states that rule for its own fleet;
this repository had no occasion to state it until a caller-named skill existed. What an operator
needs here is whether the tier is used at all; who used which is a question for that person's own
listing route.

**A write outside the tool-call chain is not a write without a record.**
`tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly` holds that every
store write arrives as a `write_file` tool call, because that is what crosses the audit row, the
authorization gate, the dry-run refusal and the repeat guard. Three of those four have no subject
here — there is no turn — and the fourth is answered by the route being `CurrentUser`-gated with a
namespace *derived from* the caller rather than taken from them. But that guard's stated reason
does apply in its own words, that a direct write "would do so silently: nothing fails, the memory
is simply written with no record that it was", so both halves of this tier's lifecycle log one.
The delete also goes through the same backend the write uses rather than reaching past it to
`store.adelete`, which keeps the stored shape's single definition and satisfies that guard on the
merits rather than by exemption.

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
- `::test_the_listing_answers_for_a_tier_larger_than_one_page` — `BaseStore.asearch` defaults to
  `limit=10`, so the un-paged listing answered ten of twelve while the prompt carried twelve, which
  falsifies this record's own licence condition: the two beyond the page were undeletable through
  the only route that deletes.
- `::test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims` — the collision order, driven
  through the loader rather than read off the source list.
- `tests/test_api_local_skills.py` — the four routes: owner scoping driven as a second principal,
  the 422 family (frontmatter, traversal, NUL, whitespace, an invented key), the 409s (a shipped
  name, the row cap), the 503 on all four routes, and the listing past one page.
- `tests/test_context_floor.py::test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows`
- `tests/test_upstream_surface.py::test_an_oversized_skill_description_is_still_truncated_rather_than_refused`
- `::test_the_label_carries_no_identity`
- `::test_a_local_skill_load_is_counted_and_carries_no_persons_words` — both arms, since the
  coverage claim and the privacy claim were each false.
- `::test_the_outer_permission_rules_deny_a_write_under_this_root_too` — the second layer, which
  covers this root by *absence* and so is the one most likely to be widened by a rule added for
  another root.
- `::test_a_write_outside_the_tool_chain_is_not_a_write_without_a_record` — including that the
  body never reaches the log.
- `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly` — passed on
  the merits, with no exemption added for this tier.
- `tests/test_route_auth_coverage.py` — that the four routes are gated like every other.
