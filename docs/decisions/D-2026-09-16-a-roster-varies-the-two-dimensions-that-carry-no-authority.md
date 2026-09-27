# D-2026-09-16-a-roster-varies-the-two-dimensions-that-carry-no-authority — named helpers without reopening the attenuation invariant

**Status:** accepted · **Date:** 2026-09-16

## Context

The owner asked for specialist agents that work as a team on one question — several views, and
sub-parts worked in parallel — and for the selection between them to happen **automatically**, with
the chemist seeing a chat interface and configuring nothing.

Two things stood in the way, and only one of them was a gap.

**The capability existed and was unreachable.** Seven profiles ship and genuinely narrow, but they
are chosen by a *person* at `POST /sessions` and fixed for the session. The `task` roster held
exactly one unnamed helper. So "which specialist" was a question only a human could answer, and
answering it cost the chemist an infrastructure decision before their first message.

**And the obvious fix is the one this repository already deleted.**
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` removed a five-specialist team,
1,442 lines of agent code, because it shipped off and stayed off.
`D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` explains why it was
never used even when on, and the explanation is structural rather than promptable: specialists were
⊆ the supervisor, so the supervisor was never *missing* the tool that answered a question, and
delegating was always a strictly longer path to a tool already in hand. The model declining it was
the model being right. Two real defects were fixed along the way and changed nothing — 1 of 15, then
2 of 15 once the `task` description was rewritten too.

That ADR names the lever that would change it: make specialists hold what the orchestrator lacks.
**That is a widening, it inverts `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`, and it
is not taken here.**

## Decision

A roster of named helpers, where **every entry is still an attenuation**, and the two dimensions a
name varies are the two that carry no authority: its **instructions** and its **model route**.

`CHEMCLAW_AGENT_HELPER_ROSTER` names agent profiles. A rostered helper's surface is

    what the caller holds  ∩  what the specialist names  −  everything that acts

on **both** halves of the surface — `helper_profile` for the in-process tools and
`helper_connectors` for the open connector tools, because a profile's `tool_names` spans both and a
reader cannot tell from the file which is which, by design. Narrowing only the in-process half would
have left `evidence` and `computation` differing locally and identical across the wire.

Intersection is the whole safety argument, and it is **arithmetic rather than a check**: a caller
that narrowed itself hands in the smaller set and no specialist can name its way past it. So the
merged invariant holds on a roster exactly as it held on one unnamed helper, with nothing to
re-argue and no guard to keep alive — which matters, because `reject_widening` was deleted for being
a guard with no caller kept alive by its own test.

**Selection is the model's ordinary in-context tool-call decision.** There is no router, no
classifier and no pre-turn profile choice. That is deliberate and it is the narrow reading of what
this repository has measured: routing was measured twice and produced nothing transferable, but what
was measured was a *hard route that locks a session into one specialist*. A mis-pick there floors
the answer with no recovery. Here the caller keeps every tool it had, so a mis-pick degrades into a
second opinion.

**Which three names, and why not the other three, is a measurement.** After
`side_effecting_tools()` is subtracted: `reporting` keeps 3 of its 8 names and `property-lookup` 1
of 5, because their job *is* writing; `design` keeps 4 of 8 but loses `suggest_next_experiment`,
which is the whole specialist. What stays coherent is `evidence` (14 of 15), `computation` (12 of
41) and `safety` (4 of 6). A name whose helper is empty is a menu entry whose only possible outcome
is a wasted delegation.

**A description is a written purpose plus a derived tool list** (`describe_helper`). The derived half
is the point: `D-2026-08-12`'s roster built its menu as `instructions.split(". ")[0]` over five
profiles that all open "You are Chemclaw's `<name>` specialist", so the model chose between five
entries differing only in a name. Better sentences fix that for one commit; a sentence plus a list
read off the compiled graph's own `ToolNode` fixes it permanently. It also removes a mistake this
repository would otherwise have made, because **a rostered helper is narrower than the profile it is
named for**: `computation` names 41 tools and its helper binds 12 — the enumerations, topology, the
calibration ledger and calculation lookup. A description written about the profile would advertise a
helper that computes, and the model would delegate a calculation and get back a refusal.

## Consequences

**The prefix grows by a measured 305 tokens**, taking the floor from **69,107 to 69,412**, all of
it the `task` tool's own description going 592 → 897 as upstream folds each entry into
`{available_agents}`. The first version of this section said 68,336 → 68,639 against a headroom of
1,161, and both absolutes were wrong: they came from summing `count_tokens_approximately` over the
observed prefix rather than from `_floor`, which is the basis the ceiling is written in. The real
headroom was **388**, against a file whose own history records a neighbouring merge drifting this
floor by 326. Writing a number from a second basis is the same defect as writing a stale one, and
it is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` twice in three commits.

So `CEILINGS["__default__"]` goes to **70,600** — a raise of 1,188 for a measured 305, and the
difference is not slack. **This ratchet under-charges a roster by construction**: each description
names what that helper *binds*, which depends on the bundles a deployment enables, so here `safety`
is absent entirely and `computation` lists what this repository serves rather than what the fleet
does. Measured against the full declared surface the `task` description is **902** tokens rather
than 710. That excess belongs to a first-party tool inside this ceiling, so
`SERVED_ELSEWHERE_ALLOWANCE` cannot absorb it and was not moved.

**What it costs is the thread, not the budget.** `agent_tool_result_clear_trigger` rises with
`PREFIX_BOUND` because nothing bounds it from above; `agent_context_token_budget` does **not**, and
raising it was tried in an earlier commit here and reverted — the window is the input and the
thread is the dependent number, so a budget that rose with the prefix would spend head-room a
provider decides. `BUDGET_THREAD_ALLOWANCE` falls 37,900 → 37,100.

**Compiling the roster eagerly was a 2.75x tax on every turn, and that is fixed rather than
accepted.** Measured, a turn's graph build went 43 ms → 118 ms against the 250 ms ceiling
`tests/test_langgraph_connectors.py` holds, whose own docstring states the headroom as 1.9x–3x —
and under load that test failed at 882 ms. Nearly all of it was paid for helpers no turn spawns. So
a rostered helper's graph is **compiled on first use**, its surface is *predicted* by the same two
functions the build applies (`predicted_helper_surface`), and the prediction is asserted equal to
what a compiled helper binds so it cannot drift. Two smaller costs went with it: `load_profiles()`
re-reads and re-validates every profile file on each call — 8.06 ms, no warm-up, so "idempotent, so
a turn pays once per process" was false — and is now asked only when a rostered name is missing
from the registry; and resolving the narrowed tool set through `_capability_tools` three times was
redundant, since `helper_profile` builds it by removing from names the registry has already
answered for. **43.3 ms → 47.2 ms, 1.09x.**

**Two things fell out of driving it that no amount of reading would have found.** Every helper binds
`FilesystemMiddleware`'s six scratch verbs whatever its profile says, so with them in the derivation
all four descriptions listed the same file tools — alike in exactly the dimension the model chooses
on — and, worse, the "bound nothing" test was unreachable: a helper whose every capability tool was
missing still bound six verbs and was offered as able to do its job. They are a notepad over a
backend with no store behind it, so they are neither capability nor description.

**What a profile names and what a deployment binds are different sets**, which is why an entry that
binds no capability tool is dropped with a WARNING rather than offered. `safety`'s three screens are
served by a connector bundle; with that bundle off, its helper is offered by no one.

**A rostered helper is sent its specialist's prose over a strictly smaller surface, and that is
structural.** `helper_profile` subtracts everything that acts from the *tools* and subtracts nothing
from the *prose*, so measured against the full declared surface the `computation` helper binds 12
tools and its instructions name **10** it lacks — it is told to record a measurement and poll a job
it cannot start. `describe_helper` fixes the caller's view of exactly this and its own docstring
names the hazard, while the prompt half went on saying it. `specialist_override` is the other half:
appended **last**, so a contradiction resolves in favour of the one sentence that knows about the
narrowing, naming the tools the helper actually holds and telling it to report an absent call as
the caller's to make rather than attempt it.

**A misconfiguration is split deliberately.** An unknown roster name is skipped per turn with a
WARNING — a turn must not die because a deployment misspelled an entry, and skipping loses
delegation, never authority — and refused loudly at startup by `refuse_an_unknown_roster`, because a
capability nobody is told is missing is one nobody restores. That refusal covers three things, not
one: a name nothing backs, a profile with no `description:` (which would reach the model as a bare
tool list — the menu `D-2026-08-12` measured), and the `general-purpose` name itself, which a roster
entry must not take because claiming it is what displaces upstream's ungoverned helper. A repeat is
dropped at build time, since upstream keys its graphs by name and keeps the last.

**The "bound nothing" notice is INFO, not WARNING.** It is the *normal* state of a rostered profile
whose bundle is off, so at WARNING it was a line on every build of every turn forever, which is how
operators learn to ignore WARNINGs. It names both causes, because a narrow caller empties a helper
by intersection and telling that operator to enable a connector is advice that cannot help.

**`general-purpose` stays and must.** It is the string comparison that displaces the ungoverned
helper `create_deep_agent` inserts when no spec claims that name, and a roster is not a reason to
give that up.

**The delegation question is still open and this does not close it.** `evals/delegation.py` and
`data/evals/probes/delegation.yaml` exist, have never been run against a model, and a negative
result remains a legitimate outcome. What changed is not the evidence — it is that the reason the
backlog recorded as missing ("everything a second name would need already exists… what is missing is
the reason") has arrived as a product requirement. That is an honest basis for building it and a
dishonest basis for claiming it pays; those are different claims and only the first is made here.

## What keeps it true

- `tests/test_subagents.py::test_a_rostered_helper_holds_no_tool_its_caller_does_not` — compared
  between two compiled graphs, not two profiles.
- `::test_a_specialist_naming_more_than_its_caller_holds_gets_the_intersection` — the widening
  attempt, driven.
- `::test_a_specialist_that_names_no_tool_narrows_to_nothing_rather_than_to_everything`
- `::test_a_roster_entry_that_binds_nothing_is_not_offered` and
  `::test_a_roster_entry_is_offered_once_its_connectors_are_bound` — both directions, so the drop is
  a measurement rather than a permanent absence.
- `::test_every_roster_description_names_the_surface_its_graph_bound` and
  `::test_the_roster_entries_do_not_read_alike`
- `::test_an_unknown_roster_name_is_skipped_rather_than_raised` and
  `::test_a_misspelled_roster_entry_is_refused_at_startup` — the two halves of the split.
- `::test_a_rostered_helper_still_cannot_spawn_a_helper` and
  `::test_no_rostered_helper_holds_a_tool_that_acts`
- `::test_every_rostered_profile_carries_a_description`
- `::test_the_predicted_surface_is_what_a_compiled_helper_binds` — the assertion the lazy compile
  rests on, driven with connectors bound and without.
- `::test_a_rostered_helper_is_not_compiled_until_it_is_spawned` — the cost fix as a property
  rather than a benchmark, which would encode one machine.
- `::test_a_repeated_roster_name_is_offered_once`,
  `::test_a_roster_entry_cannot_take_the_general_purpose_name`,
  `::test_a_rostered_profile_with_no_description_is_refused_at_startup`,
  `::test_rostering_the_general_purpose_name_is_refused_at_startup`,
  `::test_a_spaced_roster_entry_is_read_as_a_name`
- `::test_a_named_helper_is_told_what_it_actually_holds` and
  `::test_the_override_reaches_a_rostered_helpers_system_message` — the text, and that the model is
  sent it, off the wire and in both arms.
- `::test_a_named_helpers_two_texts_name_the_same_surface` — the roster's version of the pair
  `D-2026-08-12` found disagreeing.
- `tests/test_context_floor.py` — the prefix this costs, re-measured rather than transcribed.
- `tests/test_compaction.py` — that both compaction defaults moved with `PREFIX_BOUND`, and which
  of them was allowed to.
