# D-2026-09-05-a-ratchet-that-re-derives-half-its-basis-bounds-half-a-request — the prefix was already over the ceiling

## Status

Accepted. Revisits `D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget`, whose
arithmetic is unchanged and whose *input* was wrong. Neither that ADR nor
`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for` is edited.

## Context

`tests/test_context_floor.py` is the ratchet that bounds what a turn costs before the user has said
anything. Its module docstring states the rule it exists to enforce:

> a basis that is re-derived rather than observed will agree with itself forever.

That sentence was written about `_bound_tools`, which had been converting the raw callables out of
`_capability_tools` rather than the `BaseTool`s the graph binds, and under-measuring the `default`
profile by 8,126 tokens while calling itself "the payload rather than an approximation of it". The
fix read the surface off the compiled graph's `ToolNode`, and the file recorded the lesson.

**It applied the lesson to one half of its own basis and not to the other.** `_floor` composed a
prose half — `instructions_for(profile)` plus `_skills_listing(profile, ...)` — and a tool half read
off the graph. The prose half is a re-derivation of the system message, not the system message.

Three independent fresh-context measurements agree on what that cost, at `aed402c`:

| | tokens |
| --- | --- |
| `_floor("default")` — what the ratchet asserts on | 43,063 |
| the prefix `MeasureRequestPrefix` publishes — what the provider bills | **43,521** |
| `CEILINGS["__default__"]` — the bound | 43,500 |

**The real prefix was already 21 tokens over the ceiling, with every assertion in the file green.**

The 458-token gap is one thing, and it is not the thing the first write-up of this finding named
(which said "skills, todo, subagent and filesystem sections"). Measured, it is entirely
deepagents' `SKILLS_SYSTEM_PROMPT` — the wrapper upstream puts *around* the listing `_skills_listing`
already measures. So the gap is not a middleware the ratchet forgot; it is prose belonging to a
listing it thought it had.

That matters beyond the 21 tokens, because the ceiling is load-bearing elsewhere.
`agent_tool_result_clear_trigger`'s shipped default is derived as *the ceiling plus 30,000 of thread*
— deliberately the bound rather than a measurement, so the setting does not move every time a tool
schema does. A ceiling that is not an upper bound on the prefix makes that derivation say something
its author did not mean, and `test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged`
could not catch it: it asserts only that the trigger exceeds the prefix, so a default of 44,000
leaves it green while the thread allowance collapses to 319 tokens. The 30,000 the whole derivation
rests on was asserted nowhere; the band was asserted only as `> 1`.

## Decision

**1. The ratchet observes both halves.** `_observed_prefix` builds the graph, reads the `ToolNode`,
then invokes it against a capturing model and takes the `SystemMessage` off the wire. `_floor`'s
total is that observed system message plus the bound tool schemas. The three prompt lines survive as
a *split* of the observed number plus a named remainder (`prompt:middleware-sections`), so `_report`
still says which half grew and the previously-invisible middleware text is a line item rather than a
silence.

**2. `_bound_tools` stays, and that was measured rather than assumed.** `request.tools` is one step
closer to the wire, and switching to it would have orphaned three upstream pins in
`tests/test_upstream_surface.py` that name `_bound_tools` in their failure messages. The two hold the
same 61 names and differ by 20 tokens on `grep` alone — `FilesystemMiddleware` trims a line before
binding, so the node's copy is the *larger*, and charging it over-counts in the safe direction for a
ratchet. A new test pins that direction, so a sign flip is red rather than quiet.

**3. The ceiling and the trigger move together, and the derivation is unchanged.** Ceiling
43,500 → **44,500**; `agent_tool_result_clear_trigger` 73,500 → **74,500**, still ceiling plus
30,000. The alternative — re-deriving the setting from the *measured* prefix so no deployment's
behaviour changed — was put to the owner and declined, because the config's stated reason for using
the bound rather than a measurement is worth more than avoiding a 1,000-token step.

**This is a real behavioural change and is stated as one**: every deployment's lossless edit now
fires 1,000 tokens later than it did.

**4. The band is asserted.** `test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged` now
requires `trigger − ceiling ≥ 30,000` and, at the day's measured prefix,
`effective_trigger(trigger) ≥ 30,000`.

## Consequences

`default` measures **43,701** on the observed basis against a 44,500 ceiling — **799** of headroom,
under what one `propose_knowledge_note` costs (1,126 re-derived the same day), which is the property
every raise in that file has been chosen for. The other profiles are far below it.

**Nothing was added.** The number grew because the measurement got honest, for the second time in
this file's life and by the same mechanism.

The load-bearing consequence is what is now catchable: lengthening upstream's `SKILLS_SYSTEM_PROMPT`
by 20 lines moves the observed floor 43,701 → 44,026 and leaves the re-derived basis **unchanged at
43,223**; at 120 lines the ratchet fails and names `prompt:middleware-sections` in its report. A
`deepagents` bump that grows what every deployment pays was invisible to this file and is not now.

Four present-tense sentences elsewhere said 73,500 or 43,500 and are corrected in the same commit —
`CLAUDE.md`, `docs/guides/runbook.md`'s `context.trigger_floored` row, `docs/planning/BACKLOG.md`,
and `agent/context_budget.py`. `.env.example` moved with the default because
`test_config.py::test_env_example_ships_the_code_defaults` compares parsed values and would
otherwise be red.

**The numbers here are about this commit.** The observed floor was 43,521 at `aed402c` and 43,681
two commits later, drifted by work in this same sweep that never touched a tool schema on purpose.
The figure worth reading is the ceiling, because a ceiling only moves when somebody decides it
should; the live measurement is whatever `_report` prints.
