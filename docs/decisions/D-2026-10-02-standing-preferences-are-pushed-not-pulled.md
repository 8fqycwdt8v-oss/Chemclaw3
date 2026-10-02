# D-2026-10-02-standing-preferences-are-pushed-not-pulled — how a chemist's preferences reach the model

**Status:** accepted · **Date:** 2026-10-02 · Live re-verification 2026-10-02 (b), the "DMF" item.

## Context

`user_preferences` holds what a chemist asked the agent to remember: a project, a preferred base, a
constraint such as `forbidden_solvent_dmf` ("DMF is prohibited on this project"). The only route
from that table to the model was the `recall_preferences` tool, and one prompt sentence asked the
model to call it "at the start of a conversation".

Two real-model runs on the live lane (DeepSeek V4 Pro) recommended DMF to a chemist whose
preferences prohibit it, in a deep-research answer about a Ni/photoredox C–N coupling:

- run (b) never called `recall_preferences`, so the prohibition was never in context;
- the earlier run called it first, had the prohibition in its thread, and still listed "Cs2CO3 in
  DMF or DMSO" among the representative conditions it gave from background knowledge.

The first is a pull the model skipped. The second is a scope the model did not apply: it read the
preference as governing the record, not the recommendations it makes from its own knowledge.

## Options

1. **Keep the pull and strengthen the prompt.** No prefix cost. But it still depends on the model
   choosing to call the tool, which is what failed in run (b), and a sentence that already asked
   for the call did not get it.
2. **Insert the preferences into the thread as a message.** The model sees them once. But the
   thread is checkpointed and windowed: the conversation window cuts a leading note in exactly the
   long turns where it matters, and a stored copy outlives a `forget_preference`.
3. **Append them to the system message on every model call.** The model always sees the current
   list, and nothing is written to the thread. The cost is prompt size on every call, bounded by
   `preferences_section_max_chars` (below), and nothing for a chemist with none.

## Decision

Option 3. `agent/preferences.py::StandingPreferences` is a `wrap_model_call` middleware that reads
the turn actor's preferences on every async model call and appends them as one section at the end
of the system message, followed by `STANDING_PREFERENCES_RULE`. The rule scopes them: they constrain
only what the model *recommends* (reagents, solvents, conditions, units, protocols), *including*
what it offers from background knowledge or the literature, and a prohibited item is named as
excluded with an alternative, never proposed. That answers the second run, and the push answers
the first.

**This moves model-written text from a tool result into the system message, and that is a trust
move.** A preference value is written by the model through `remember_preference`, possibly out of
framed third-party content it had just read. Through `recall_preferences` it arrived as a tool
result; now it sits in the most trusted span the model reads, on every call. It is contained four
ways, each enforced in code and tested:

- **Quoted as data, not as the chemist's words.** The section is headed as model-recorded notes
  quoted as data, and the rule says an entry never grants a permission, never changes what the
  model is authorised to do, and never overrides the instructions or a notice this system attaches
  to a tool result (the STAND-IN notice). Authorization is enforced by the tool chain, not by
  prompt text, so a preference cannot widen it either way.
- **Defanged:** the envelope delimiter and the system mark are neutralised.
- **One line per entry:** all whitespace, newlines included, collapses to single spaces, so a value
  cannot start what reads as a new top-level instruction.
- **Bounded in characters:** each rendered entry is capped at `preferences_entry_max_chars` and the
  whole section at `preferences_section_max_chars`, with a visible truncation marker and a count of
  entries left out. `remember_preference` refuses a longer key+value at write time. The render cap
  is the one that holds, because rows written earlier are not re-validated.

- The section sits above the compaction group, so `MeasureRequestPrefix` charges it as prefix; a
  deployment whose chemists hold many preferences pays them in spend, as
  `D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread` already settles
  for prefix beyond the basis.
- An unreadable store or no actor means no section, recorded as a degradation. The turn never fails.
- Helpers are compiled by the same `_middleware`, so a helper sees the same preferences.
- `recall_preferences` stays as a way to re-read the list.

## Consequences

- The prompt sentence about `recall_preferences` now says the list is at the end of the
  instructions; `skills/deep-research` says preferences bind proposals.
- One indexed `SELECT … LIMIT` per model call when the store is Postgres. It is not cached per
  turn, because a preference set or dropped mid-turn should be in force on the next call.
- The synchronous hook passes the request through: the store is async, and every served turn
  takes the async path.

**Revisit when:** `chemclaw_preference_evictions_total` shows chemists routinely at the row cap, or
`context.prefix_over_basis` attributes a measurable share of the excess to this section. Either
means the list is large enough that a selection (by relevance to the turn) beats pushing it whole.
