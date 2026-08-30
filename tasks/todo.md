# An unparseable tool call is an ordinary tool failure

Four fresh reviewers over the merged `4a657ad` + `4859748` + `7f52df3`. One built and measured an
alternative design; the user chose it. This replaces the mechanism rather than patching it again.

## Why replace rather than patch

The shipped design retries the model from inside `wrap_model_call`, discards the reply, and hand-
rolls a report to three audiences. Three rounds of review found seven defects in that machinery and
each round's fix introduced the next round's defect. The alternative — promote the unparseable call
onto `tool_calls` behind a sentinel and let the *tool chain* refuse it — is ~20 lines and gets for
free everything the hand-rolled path had to build and kept getting wrong:

| | shipped | promoted |
| --- | --- | --- |
| audit row | none (argued as principled) | the ordinary row |
| span | none | the ordinary span |
| `call_id` on the event | dropped; forced a `graph_stream` guard that broke pair-exhaustiveness | the model's own id |
| authz / dry-run / repeat guard | never run | all run |
| a valid call beside a broken one | discarded with the reply | runs |
| the retry | outside the loop cap; needed a hand-rolled "never a loop" bound | an ordinary graph iteration |
| reporting | `_announce_unrun` + `_reportable` + 5 prose constants + a setting | `announce_tool_failures`, already there |

## Code

- [x] **N1** `_UNPARSED_ARGUMENTS` sentinel key, defined once, in the module both halves import.
- [x] **N2** `PromoteInvalidToolCalls` (`wrap_model_call`) replaces `RepairInvalidToolCalls`: count,
      log, move each `invalid_tool_call` onto `tool_calls` carrying the raw document under the
      sentinel, clear `invalid_tool_calls`. No retry, no discarded reply, no prose to carry.
- [x] **N3** `refuse_unparsed_arguments` (`wrap_tool_call`), **innermost** of the governance chain:
      raises `UnparsedArguments(ChemclawError)` before the body runs. Innermost so audit, authz,
      dry-run and the announcer all see the call; before the body because **11 of 54 in-process
      tools take no required argument**, so a promoted call with an unexpected key would otherwise
      *execute* one.
- [x] **N4** `UnparsedArguments` is not in `refusal_reason`'s table, so it is a fault
      (`reason=None`), not a gate refusal.
- [x] **N5** Delete what the mechanism obsoletes: `_announce_unrun`, `_reportable`, `_retry_request`,
      `_report_repair`, `_carrying_prose`, `_because`, `valid_tool_calls`, `_prose_of`, and the five
      prose constants.
- [x] **N6** Delete `agent_max_reported_lost_calls` and its `.env.example` row — the ceiling it
      bounded no longer exists; `agent_max_parallel_tool_calls` and `tool_result_size` now apply.
- [x] **N7** Revert the `graph_stream` `if signal.call_id:` guard. It was added for a producer that
      no longer exists, and it broke suppression for a tool-chain refusal whose call carried an
      empty id — measured: `tool_failed` *and* `tool_result`, with the refusal sentence joining the
      grounding corpus `score_answer` reads.

## Defects that outlive the redesign

- [x] **D1** The tool **name** is unescaped where the parse error is escaped — same `%s`, same
      WARNING line. Measured: a newline in the name forges an `actor=admin … result=granted` audit
      line under the default `log_json=false`.
- [x] **D2** `_bounded_reason`'s tail slice cuts `repr` escapes in half (`\n` → the letter `n`).
- [x] **D3** `_empty_answer_event` says "N tool call(s) ran"; `called_tools` is documented as calls
      *announced*, so a dry run reads as 3 ran **and** 3 refused — six intents where there were three.
- [x] **D4** Its docstring says the remedy "follows from what dominates"; the code is
      failure-precedence.
- [x] **D5** `evals/live.py` still splits only `plan_gate` off `tools_failed`, so four of the five
      gates are scored as failures — the defect `7f52df3` fixed, one layer over.

## Prose

- [x] **P1** The ledger's Phoenix correction, which my own merge resolver silently dropped (it keyed
      rows by id and let `main`'s copy win every collision).
- [x] **P2** A supersession row for this subject in the ledger's map — four ADRs, no row, so a
      reader landing on the first gets the reverted rule with no pointer onward.
- [x] **P3** The unreproducible **841 kB** figure, in four first-party files plus the ADR and the
      ledger. Measured at the relevant commit: 623 kB. Remove it with the machinery it described.
- [x] **P4** `"0 failures / 3 held"` — the UI renders `"3 refusals"`. I quoted a string I never read.
- [x] **P5** `_bounded_text`'s docstring still describes the `error` field it no longer serves.
- [x] **P6** The `RepairInvalidToolCalls` closing paragraph is self-referentially false.
- [x] **P7** "falls back to the literal `arguments did not parse`" — that literal exists nowhere.
- [x] **P8** `_TurnLedger.tool_refusals` carries no comment; the line quoted is on `TurnCost`.

## Tests

- [x] **T1** The promoted call runs the whole chain: audit row, span, `tool_failed` with a **real**
      `call_id`, and the tool body never entered — proven on a no-required-argument tool.
- [x] **T2** A valid call beside a broken one **runs**.
- [x] **T3** The model corrects in its own loop, counted by the loop cap.
- [x] **T4** `test_the_parse_error_keeps_its_reason…` is vacuous — its 79-char fixture fits inside
      both the head and the tail window (measured: head-bounding first loses the reason at 93).
- [x] **T5** The bounded/escaped tool **name** (no test today: `_bounded_text` unbounded survives).
- [x] **T6** `chemclaw_turn_empty_answers_total` behaviourally (today only a name-presence check).
- [x] **T7** `.strip()` on the answer text (a whitespace-only answer skips the guard).

## Verification

- [x] `make lint type test` with Postgres and Temporal up: **6260 passed, 15 skipped, 0 failed**
      (helm not installed ×9, three truncated-history migration checks, two prompt-caching tests
      whose credential has no balance, one retention case a live checkpointer makes unproducible).
      Nothing in this change's subject is among the skips. An earlier run found two failures, both
      mine and both fixed: `test_profile_attenuates_but_audit_and_authz_always_attach` (the chain
      has one more entry) and `test_every_chemclaw_error_subclass_is_listed_non_retryable`
      (`UnparsedArguments`). That earlier run overlapped edits to the tree it was testing and is
      therefore not evidence about this one; the figures above are from a run started after the
      last source change.
- [x] Live lane: storm C+F **11/11**, including the check that was permanently red, and the F6 turn
      hand-driven — `tool_call` + `tool_failed` with the model's own id, no `tool_result`, an audit
      row under `error`, `turn_costs` at `tool_calls=1 tool_failures=1 tool_refusals=0`, and the
      counter at 2. Recorded in `tasks/live-test/storm-cf-2026-08-30.md`.
- [x] ADR superseding all four + ledger row + supersession map + `lessons.md`.

## Review

**Shipped.** `PromoteInvalidToolCalls` + `refuse_unparsed_arguments` replace `RepairInvalidToolCalls`
and the ~180 lines around it. Every plan item is done, offline and on the live stack.

**What the mutation runs proved** (from a committed baseline, one file at a time, and *targeted at
the test written for each defect* rather than at the suite):

| mutation | caught by |
| --- | --- |
| `_bounded_reason` slices the quoted form | `test_the_bounded_reason_never_cuts_an_escape_sequence_in_half` |
| the tool name unescaped in the WARNING | `test_the_tool_name_is_escaped_in_the_warning_that_carries_it` |
| the parse error bounded from the head | `test_the_parse_error_keeps_its_reason_where_head_bounding_would_lose_it` |
| the promotion carries `{}` | `test_a_promotion_cannot_execute_a_tool_that_needs_no_arguments` |
| the model's call id dropped | `test_a_promoted_call_crosses_the_whole_governance_chain` |
| valid siblings discarded | `test_a_valid_call_beside_a_broken_one_still_runs` |
| `invalid_tool_calls` not cleared | `test_the_broken_call_moves_onto_the_field_the_tool_node_iterates` |

**One mutation survived the test that claimed to catch it**, and that is the finding worth keeping:
dropping the call id survived `test_an_unparseable_tool_call_reaches_the_stream_as_a_real_tool_failed_event`,
whose comment I had written asserting the opposite. The announcer and the refusal both read
`request.tool_call["id"]`, so they pair with each other whatever it is. The comment is corrected in
place and the real proof is at the producer, against the signal itself.

**The live lane ran.** `CHEMCLAW_MCP_REPO=/home/user/Chemclaw3-mcp` plus the mock-model triple
(`CHEMCLAW_LLM_PROVIDER=openai_compatible`, `CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1`,
`CHEMCLAW_LLM_MODEL=mock`) is what `infra/live/processes.sh up` needs here; without the model
variables it starts the durable half only and skips the front door, which the storm requires.

**What is still not evidence this session has:** `make live-probes`. The environment's `API-KEY` is
present and the provider refuses it — *"Your credit balance is too low"* — which is also why two
`test_prompt_caching` cases skipped. Nothing in this change depends on a real model: the behaviour
under test is a malformed emission, which is exactly what a real model will not produce on request
and what `cli/mock_llm.py` exists to ask for by name.
