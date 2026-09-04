# D-2026-09-04-a-quote-is-evidence-about-a-person-not-about-a-turn — the ambient is the thread's user turns

**Status:** accepted · **Date:** 2026-09-04 · Extends `core/turn_text.py`'s founding argument
rather than replacing it: a quote is evidence about a person and cannot be supplied by the thing
being checked. What changes is *which* of the person's words count.

## Context

`require_quotes_are_verbatim` checked a `stated` quote against `get_current_user_text()` — the
message that started *this* turn. But `structure_experiment_request`'s own docstring says to call it
"first … while correcting it is still cheap", i.e. iteratively, across turns. Measured through
`run_turn` on a compiled graph, turns "24 wells, no DMF, by Friday please." → "what about the base?"
→ "ok go ahead", with `max_runs` quoting `'24 wells'`:

> these slots are marked `stated` but their quote is not in the message that started this turn:
> max_runs: '24 wells'.

So on the ordinary multi-turn path an honest `stated` was **unrepresentable**, and the remedy the
refusal prescribed — restate it, or drop to `inferred` — recorded a real chemist constraint as a
model inference. That is the mislabelling the check exists to prevent, running the other way.

## Decision

The ambient is `tuple[str, ...] | None`: the chemist's own messages in this thread, oldest first,
this turn's last.

**Separate haystacks, never joined.** A quote running off the end of one message into the start of
the next is words nobody wrote in that order.

**The transcript, not the checkpointer, and the reason is measurable.**
`_job_results_message` enters the graph via `turn_input` as a **user** message, so the checkpointer
carries framed tool output under the chemist's role — a model could quote a tool result back as
`stated`. `session_messages` has exactly one human-row producer (`_record_transcript`), and
`chemist_words` filters to decoded `HumanMessage` rows with string content. **The anti-spoofing
property is a claim about producers, so it is enforced at the read rather than by the check.**

**Bounded in two currencies** — `agent_stated_quote_turns` (20) and `agent_stated_quote_chars`
(20,000), both ENV-overridable. Either alone is unbounded in the other: the front door accepts
`service_max_message_chars` of 100,000 in a single message, so 20 turns is up to 2 MB held in a
contextvar per concurrent turn. The turn in flight is **always** kept whole, so widening can never
refuse what the narrow version accepted, and `turns=0` is exactly the old behaviour.

`_turn_ambient` stays synchronous: the read is awaited *before* the `with` block and passed in,
because an `await` inside it re-raises the disconnect cancellation on the spot (D-130) and leaks one
turn's ambient identity into the next turn on that worker.

**`get_messages`' no-`LIMIT` rule is not violated**, and the module now says so rather than leaving
it to be inferred: that rule is explicitly a *rendering* rule — a person reloading a conversation
must not see a transcript that silently omits its own beginning. `recent_user_texts` is a second,
differently-bounded read for a different question.

## Consequences

**Cost, measured against Postgres rather than argued.** The SQL filters to the chemist's rows so
tool-result JSONB is never detoasted: a 12,000-row session is a backwards index scan stopping after
240 rows, **0.20 ms**, 15 buffers. End to end including pooled acquisition, **12.4 ms p50**, flat
across 360 / 3,600 / 12,000 rows. The existing unbounded `get_messages` on the same session is
47.8 ms. No migration — the existing `(session_id, id)` index serves it.

**The failure direction is the strict one.** An unreachable store degrades to *no* earlier words,
refusing a truthful quote rather than admitting a false one, counted on
`chemclaw_degraded_total{subsystem=stated_quote_history}`. Absent still means refused, not waived.

**Both writers agree.** The front door reads the durable transcript; the CLI, which writes no
transcript, accumulates typed prompts in-process — excluding `/plan` and `/approve`, and recording
only after a turn answers, the same rule `_record_transcript` follows. The bound lives in
`core/turn_text`, so what counts as the chemist's words is identical on both. A check that behaved
differently on the admin CLI would be a check with a bypass.

**A trap closed that `mypy --strict` cannot see: `str` is a `Sequence[str]`.**
`set_current_user_texts("hello")` type-checked and bound one haystack per character. It now raises,
and that guard caught two live call sites on its first run.

**One mutant survived and was treated as a question rather than a pass.** The `turns <= 0` guard is
redundant with the providers' own, so removing it changed no assertion — but its real job is
skipping the round trip when the feature is off, and nothing asserted that. The test now counts
reads, and all 15 mutants bite.

**It widens a known gap without changing its rule.** `_quote_supports` still cannot tell whether a
figure a quote carries is about *this* slot, and there is now more of the chemist's own text for a
figure to coincidentally match. That strengthens the case for the count that row already asks for;
its `BACKLOG.md` row says so.
