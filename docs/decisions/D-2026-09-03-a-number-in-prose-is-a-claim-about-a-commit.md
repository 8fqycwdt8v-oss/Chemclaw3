# D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit — a fresh-context review found the code sound and four of its own sentences stale

## Status

Accepted.

## Context

Four fresh-context reviews were run over the previous session's own output — the helper subagent, its
narrowing, and `agent/tool_result_shape.py`. The code held. What did not hold was the prose about it,
in four places, every one of them written by the session that had just measured the thing it was
describing:

- **The context floor.** `CLAUDE.md` and `tests/test_context_floor.py` both said the `default`
  profile measures **42,505** tokens with **995** of headroom. Measured at `HEAD`: **42,549**, so
  951. Nothing in this repository's subject had changed — the drift arrived in a merge that touched
  `agent/protocol_design_tools.py`, a tool-schema module. That paragraph is itself the *second*
  correction of those numbers, and its own text explains that the first went stale because "the
  basis shifted underneath the commit that was correcting the basis". It then did it again.
- **The helper's tool counts.** "the helper held its caller's **54** in-process tools … It now holds
  **24**" — two different bases in one subtraction. In-process the helper holds **18**; the compiled
  graph binds **24**, because `FilesystemMiddleware` supplies six file verbs `tool_names` does not
  reach. 54 − 36 = 18, and the sentence read as though it were 24.
- **`HELPER_BRIEF`.** "You hold read-only tools only", in the helper's *own system prompt*, while
  two of the tools bound to it are `write_file` and `edit_file`. The model was told a boundary that
  was not the one it had.

  **The first correction of this sentence was also wrong, and a second review caught it.** It read
  "what they write reaches nothing but your own scratch space, which goes away when you return".
  Measured: upstream's `_return_command_with_state_update` copies every helper state key except
  `messages`, `todos` and `structured_response` into the caller's update, and `files` is not among
  the three — the helper's `/scratch/evidence.md` arrives in the caller's `files` channel, 9,937
  characters of it. The inbound direction is the same: a helper is handed the caller's state minus
  those three keys. So the brief now says a file crosses back, because it does.
- **"57 characters".** The number is right and reproducible; the gloss was not. It is not "the
  `task` call and a 28-character report", it is the whole thread: a 17-character question, a `task`
  call with empty content, the 28-character report, a 12-character answer.

Two further findings were about what nothing checked. `rewritten_tool_messages` rewrites only a
*dict-shaped* `Command.update`, while its docstring said "whatever shape it arrived in" —
`Command.update` is typed `Any` and LangGraph's own `_update_as_tuples` accepts two further forms,
either of which would pass a helper's report through with **both** controls unapplied. And the
`task` tool's suppression of upstream's unguarded `general-purpose` subagent was asserted against a
phrase *copied out of* upstream's description, which is the assertion that goes quiet on a reword
rather than red.

## Decision

**Stop writing the context-floor figure in prose.** `CLAUDE.md` and the test comment keep the
historical measurement, anchored to the date it was taken, and state no current one. The live number
is what `tests/test_context_floor.py` measures and prints on failure; the figure worth reading is
the ceiling it ratchets against. This is not a new rule — this file already refuses to write the
`make` target count ("the one that was said 23 while the file held 28") and the skipped-test count
for exactly this reason. The floor is the same kind of number and was not treated as one.

**Correct the other three claims, and pin the one that had no test.** The isolation measurement is
now asserted as a *ratio* rather than as 57, because 57 is a property of the fixture's wording and
the mechanism is not.

**Assert the upstream assumptions instead of believing them.** `tests/test_upstream_surface.py`
gains the seventh coupling — that deepagents' `_build_task_tool` returns a dict-shaped `Command`
update — naming `agent/tool_result_shape.py` as what breaks otherwise. The suppression assertion now
imports `DEFAULT_GENERAL_PURPOSE_DESCRIPTION` rather than quoting it.

**No guard on `held`.** One was written and is not shipped. It raised on an empty set and on names
the caller does not hold, and a review found both halves wrong: the empty case is *reachable and
legitimate* — a profile naming only connector tools resolves no in-process tools, and the raise
killed the whole caller build with a message blaming the registry — while the second case cannot
fire, because `_capability_tools` intersects with `tool_names` before the call site derives `held`,
and the only profile that ships has `tool_names=None` so it no-ops entirely. That is the
`reject_widening` shape this repository deleted 254 lines over: an unreachable branch kept alive by
a test that calls it directly. The invariant stands on the construction — every operation in
`helper_profile` removes from `held` and none adds — which is where it was already stated.

## Consequences

The merged ADR `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread` carries the
"`task` call and a 28-character report" gloss and is **not edited**, per this repository's rule that
a merged ADR is never rewritten. The correction lives here, in `CLAUDE.md`, and in the test that now
pins the ratio — which is the durable half.

**One explanation everyone reached for is wrong, and the test that would have shipped saying so was
caught by running it.** The obvious reason an oversized *connector* result cannot reach the model as
an unbalanced envelope is that `bound_tool_results` runs inside `frame_connector_results`, so the
cut happens before the envelope is added. A test was written asserting exactly that, with a docstring
saying "swap the two entries and this is the test that notices". Swapping them, it passed.

**The conclusion drawn from that was then wrong in the other direction, and a second review caught
it too.** "It passes with the order swapped, therefore the order is not the reason" is the inference
two sufficient causes defeat. Measured across all four arms of order x truncation strategy, only
*swapped order with a head-only cut* fails: in the shipped order `bound_tool_results` is inner, so
it cuts the raw payload and the framer wraps it afterwards — the delimiters are added last and the
cut never sees them — while head-and-tail is what would save a swapped order. In the configuration
that ships, the ordering is the reason. The test is a ratchet against changing both.

Nothing about the running system changed except the two raises in `helper_profile`. Every other
change here is a sentence that was true when written and false when read, or an assumption that was
held and not asserted.
