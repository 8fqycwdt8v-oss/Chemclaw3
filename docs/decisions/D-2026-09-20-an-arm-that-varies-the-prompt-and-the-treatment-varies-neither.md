# D-2026-09-20-an-arm-that-varies-the-prompt-and-the-treatment-varies-neither — the delegation experiment's baseline is one of three symmetric profiles, not one control against `default`

**Status:** accepted · **Date:** 2026-09-20 · Builds the run half
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` asked for and
`docs/planning/BACKLOG.md` specified. Applies
`D-2026-09-14-tools-were-never-the-variable`'s finding to a measurement that was about to repeat it.
Does **not** reopen `D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`:
the peer arm observes a handoff, it does not turn one on.

## Context

The BACKLOG row for this experiment named what the run half owed, and one item of it was "a
`no-helper` profile whose system prompt asks the model not to call `task`". Read literally that is one
new file: the baseline arm supplies its own `instructions:`, and the delegating arms run the shipped
`default` agent.

That instrument is confounded, and this repository has already paid for the confound once. A
profile's `instructions:` **replace** the deployment's domain prose rather than adding to it
(`agent/chemclaw_agent.instructions_for`: *"A profile's `instructions:` replace the domain guidance
of `_INSTRUCTIONS`, which is the point of a specialist"*), and `data/evals/profiles/no-tools.yaml`'s
own header records what that measured out at — **a 13,895-character difference in the system prompt**
between that arm and `default`, which is why `D-2026-09-14-tools-were-never-the-variable` exists and
why `tools-removed.yaml` had to be written beside it.

The delegation experiment is worse placed than the tool-utility A/B was, because its headline axis is
**cost**. A ~14,000-character prompt difference is on the order of 3,500 tokens *per model call*
against a `median_token_ratio` that is being asked to detect what a helper's context isolation saves.
The prompt delta would not merely confound the token axis; it would dominate it. A baseline profile
plus a `default` treatment arm therefore answers "does replacing the system prompt change the bill",
with `delegated` along for the ride.

## Decision

**Every arm supplies prose, the prose is one shared body, and the arms differ only in the paragraph
after `Delegation:`.** `data/evals/profiles/` gains three files —`no-helper.yaml`,
`with-helper.yaml`, `with-peer.yaml` — whose instruction bodies are byte-identical and whose asks are
not. `tests/test_delegation_run.py::test_the_three_arm_profiles_differ_only_in_their_delegation_ask`
holds both directions, because either alone is satisfied by doing nothing: identical bodies, and
three asks that differ from one another.

Three files rather than one is the cost of a one-variable contrast, and the `Delegation:` heading is
what makes "the body" a mechanical notion rather than a reviewer's impression. A profile file cannot
include another, so the duplication is real; the test is what makes it safe, and it is the same trade
`tests/test_identity_contract.py` takes when it transcribes header spellings as literals rather than
importing the constants it is checking.

**What is given up is named rather than left to be discovered.** These arms do not carry the
deployment's own domain guidance, so the comparison is *internally valid* — it says what delegation
does to a task under this instrument's prose — and it is **not** a measurement of the shipped prompt.
A control arm that overstates itself is worse than none, which is the rule `no-tools.yaml`'s header
states and this decision inherits.

**Two other choices are taken here because a later reader will ask about both.**

**`delegated` is read off `audit_events`.** Four things in this system know that a turn called `task`
and only one of them is a record: the SSE stream's `tool_call` event is what the browser was told and
has been wrong about a turn before (`STREAM-1`, events carrying no arguments); `turn_costs.tool_calls`
is a count and cannot say which tool; `ChemclawState` is keyed by thread and holds no per-tool record.
`agent/audit.py` wraps every registered tool from one place, writes the tool's own name, and
`api/runner.py` drains the sink *before* the answer event — so the row is queryable the moment the
stream closes. It also carries `outcome`, which is the distinction a tool name alone cannot make: a
`refused` row means a gate stopped the call above the tool body, so no helper was spawned, while a
call that entered and failed is a delegation that happened and dilutes the effect toward zero, which
is the conservative direction under intention-to-treat.

**The arm's name and its prompt are never read.** That is the whole reason the baseline is
*behavioural*: `task` cannot be removed, so a baseline that delegated anyway is `contaminated`, and
inferring compliance from the label would make the one observation the measurement depends on a
restatement of its own design.

## Consequences

- **The measurement's own prose is now a variable nobody can vary by accident, and the shipped
  prompt is out of reach.** Bringing it back needs an `AgentProfile` dimension that *appends* to the
  default blocks instead of replacing them — a new field on the manifest surface, with its own
  bounding argument, since `instructions` is the system prompt
  (`D-2026-09-06-a-manifest-is-data-in-every-field-that-executes`). That is its own decision and is
  not taken here.
- **The peer arm's act has never been observed.** `treatment_tools("handoff", …)` resolves a name
  `agent/handoff.handoff_tool_name` mints, and no run has recorded a real `transfer_to_<peer>` row:
  `available_tool_names()` does not carry the handoff name space, so `cli/mock_llm._validate` refuses
  a scripted behaviour that calls one. That absence is a **finding about
  `available_tool_names()`** — its docstring claims "every tool name the agent can resolve, across
  all six name spaces" and a peer's handoff tool is resolvable and absent — and it is left as a
  BACKLOG row rather than widened here, because that function is also what the skill, template and
  prose validators read.
- **A run against `cli.mock_llm --catalogue delegation` exits non-zero on purpose.** The double
  supplies the *decision* to delegate, which is the one thing a credential-free lane cannot get from
  a model, so such a run is evidence about the runner. It drove every compliance bucket the
  comparator carries — `delegated`, `undelegated`, `partially_delegated`, `contaminated` and
  `incomplete` — none of which any run had exercised before. **No figure it produced is quoted
  anywhere**, here or in `BACKLOG.md`, and that is the point rather than an omission.
- **Nothing is claimed about whether delegation pays.** `D-2026-08-29`'s question is still open and
  the row still says so.

**Revisit when:** a gateway run exists and its report shows the token axis dominated by something
other than delegation — or when an `AgentProfile` field that appends to the shipped prose lands, at
which point the three bodies should become that field's value and this file's shared-body test should
be deleted with them. The file that would show the first is any
`tasks/live-test/transcripts/delegation/*/summary.md` whose provenance line names a real gateway; the
second is `src/chemclaw/agent/profiles.py`.

## What keeps it true

- `tests/test_delegation_run.py::test_the_three_arm_profiles_differ_only_in_their_delegation_ask`
- `tests/test_delegation_run.py::test_the_baseline_profile_asks_the_model_not_to_call_task`
- `tests/test_delegation_run.py::test_a_refused_task_is_not_a_delegation`
- `tests/test_delegation_run.py::test_the_treatment_is_read_off_the_producers_rather_than_a_literal`
- `tests/test_delegation_run.py::test_a_repeat_that_did_not_delegate_is_recorded_rather_than_dropped`
