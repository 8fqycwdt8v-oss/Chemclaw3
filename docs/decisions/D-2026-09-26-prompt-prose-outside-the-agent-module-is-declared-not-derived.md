# D-2026-09-26-prompt-prose-outside-the-agent-module-is-declared-not-derived — how the prose guards find a prompt constant

**Status:** accepted · **Date:** 2026-09-26 · Closes the `BACKLOG.md` row *"Prose the model is sent
from modules other than `agent/chemclaw_agent.py` is outside the prose guards"* (issue #452), which
`D-2026-09-22-an-exemption-is-a-quote-not-a-file` opened by saying what its widened universe still
could not see.

## Context

`tests/test_prose_contract.py` scans six enumerable classes of model-facing text. A prompt written
as a string constant in an ordinary module is none of them, and the row named a live instance —
`durable/hypothesis_tournament.py`'s check prompt saying *"There is no DFT and no cluster here"* —
plus `_TEMPLATE_HINTS`. The row put the question as how to *find* such constants, with two
candidates: a marker they carry, or a rule derived from what reaches a model call. It asked for the
false-positive rate to be measured first.

Measured over `src/chemclaw` before building either:

- **Derived, sink-local**: module-level string constants read inside a function that constructs a
  message or calls a model (`SystemMessage`, `HumanMessage`, `ainvoke`, `with_structured_output`,
  `_structured`, …). **Six hits, one of them prompt text** (`evals/live_judge._SYSTEM`); the rest
  are a session id, a URL and query filters. It found **none** of the constants that matter most —
  `HELPER_BRIEF`, `PEER_BRIEF`, `NO_SKILLS`, `_SCOPE_GUIDANCE` — because those reach the model
  through a middleware argument or a system-prompt concatenation, never beside the call.
- **Derived, by name** (`*PROMPT*`, `*BRIEF*`, `*HINT*`, `*SYSTEM*`, …): twenty hits, of which
  roughly half are SQL statements and identifiers (`_COPY_MESSAGES`, `_NEWEST_MESSAGE`,
  `TEMPLATE_JOB_FAMILY`).

## Decision

**The declaration is explicit.** A module-level constant a model is sent is a
`core/model_prose.ModelProse` — a `str` subclass, so it survives `.format`, concatenation and a
pydantic `Field(description=…)` unchanged and is found by `isinstance`.
`cli/validate_prose_contract.marked_prose` is the one loader (parse for the names, import for the
values), `tests/test_prose_contract.py::_marked_prose` adds it to the universe as a seventh class,
and rule 11 of `make prose-validate` refuses a marker anywhere the loader cannot reach it.

The first carriers: every prompt in `durable/hypothesis_tournament.py`, hoisted from inline
f-strings into `str.format` templates, and `_TEMPLATE_HINTS`; the verifier's and the condenser's
instruction heads, hoisted the same way; and the constants already at module scope that a model is
sent — `HELPER_BRIEF`, `PEER_BRIEF`, `NO_SKILLS`, `_SCOPE_GUIDANCE`, `TOOL_REMEDY`, `STEP_REMEDY`,
compaction's placeholder, the skills backend's `REFUSED`, the two `calc` spec field descriptions
and the live judge's system prompt. The check prompt's DFT sentence is correct and is the fourth
`_TRUE_ABOUT_WHAT_IS_GONE` entry. Hoisting it found a stale count in the same prompt — "filled in
one of two ways" above a numbered list of three — which is fixed.

## Alternatives

- **A derived rule over what reaches a model call.** Declined on the measurement above: its recall
  misses the briefs that are most of the class, and widening the sink set to middleware arguments
  is a dataflow analysis across modules for a set a reviewer can mark in one word.
  **Revisit when:** model-facing prose converges on one call seam — every prompt reaching the model
  through a single function whose argument can be traced — at which point the rule becomes local
  and the marker becomes redundant.
- **A naming convention checked by a test.** Declined: half its hits are SQL, and a convention
  forced onto names is the same "only as good as whoever remembers it" as the marker, with worse
  precision.

## Consequences

- **The limit is recall.** Nothing refuses an *unmarked* prompt constant; the tests hold that every
  marker is read and that the marked text passes the guards, not that every prompt is marked.
- **Prose written inline in a function body stays outside** — refusal messages and tool-result
  sentences, mostly — until it is hoisted into a marked constant.
- Rules 1-4 (tool names, note types, workflows) are not run over the marked class: its templates
  name job argument fields in `snake_case`, which rule 2 would read as unknown tools.
