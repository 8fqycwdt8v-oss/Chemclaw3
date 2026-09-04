# D-2026-09-04-a-helpers-file-crosses-back-and-stays — the crossing is the affordance, so the reading is what had to be closed

**Status:** accepted · **Date:** 2026-09-04 · Extends
`D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`, which closed the *report* as a
channel out of a helper. This is the second channel out of a helper, which that ADR named and did
not close. Opened by `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`, whose second review
measured the crossing and whose closing sentence is the whole subject here.

## Context

A helper does not return one thing. It returns a report *and* its state, because
`deepagents`' `_return_command_with_state_update` copies every helper state key except
`_EXCLUDED_STATE_KEYS = {"messages", "todos", "structured_response"}` into the caller's update, and
`files` — a `DeltaChannel` on `FilesystemState`, carrying no `PrivateStateAttr` — is not among the
three. So a helper's `/scratch/evidence.md` lands in its caller's `files` channel.

The previous ADR defanged the report on the reasoning that it was "the one span that arrived raw".
It was not. Four arms, each driven on a compiled graph with a scripted helper and a scripted caller,
each measured before the fix and again after it:

1. **The caller reads the file back.** It came back with **nothing applied** — neither framed nor
   defanged: byte for byte the file the helper wrote, the copied `</retrieved-note-…>` live in it,
   plus `read_file`'s own line prefix and not one character else. The cause is one line: `read_file`
   is answered *in this process*, so `served_by(request)` returns `""` and `frame_connector_results`
   returned early. The nonce does not cover this for the reason it does not cover a report — a
   helper is inside the deployment and **copies** the tag it has just read in the envelopes around
   its own evidence rather than guessing it.

   **This arm is stated as a relation and not as a character count, and the first draft of this ADR
   got that wrong in the way the ADR it extends warns about.** It said "a 10,095-character file came
   back as 10,111 characters", and that pair was quoted into four other documents — but no shipped
   test drives a file of that size, so the number could not be re-run from anything in the tree and
   quietly became a claim about a fixture that had since been reworded (the shipped one writes 83
   characters and reads back 86 unfixed, 89 fixed). The relation is what the fix actually promises,
   it survives any rewording, and
   `tests/test_subagents.py::test_a_helpers_file_reaches_its_caller_and_is_defanged_when_read` now
   asserts it as an equality against the written file rather than as a length.
2. **`grep(output_mode="content")` is a second content channel.** It returns the matching *lines*,
   so the same delimiter reached the caller at **121 characters** with no file read at all — that
   figure re-measured against the shipped fixture, which is why it stays here where arm 1's did not.
   A fix keyed on `read_file` would have passed arm 1 and left this open.
3. **A path is a third channel, and it needs no helper and no read.**
   `write_file(file_path="/scratch/</retrieved-note-…>.md")` is a legal path under `SCRATCH_ROOT` —
   the permission rules bound *where* a turn may write, not what a path may spell — and the
   confirmation echoes it: **59 characters, delimiter live**, with nothing spawned and nothing read
   — likewise re-measured against the shipped fixture.
4. **The crossing outlives the turn.** `files` is checkpointed under the `thread_id`, which is the
   session. Measured over two turns on one thread with a saver under them: turn two, with **no
   helper in it at all** — `helper_calls == 0`, asserted, because the caller script's first draft
   emitted `task` on its first call unconditionally and so ran one while the test's docstring said
   it did not — listed `/scratch/evidence.md` and read back what the helper had written. Both the
   `ls` and the `read_file` are driven by the test; neither is inferred from the state channel.

`agent/langgraph_agent._subagents` said "nothing a helper writes outlives the *turn*" — false twice
over, and the previous session had already corrected the first half without seeing the second.
`HELPER_BRIEF` told a helper its file "crosses back" and stopped there.

## Decision

**Keep the crossing.** Closing it would be a control that reads as one and is not: pointer-passing
is better context economics than the alternative, which is the helper pasting its reading into a
report the caller then pays for in full — the exact cost `task` exists to avoid. And there is no
authority argument for closing it. The helper wrote the file with a verb its caller holds, into a
root its caller may write, under the caller's actor, through the same audit trail, authorization
gate, dry-run refusal and plan gate.
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` is about *authority*, and nothing here
widens any. It is a data-flow question, and the data flows between two contexts of one turn of one
person's session.

**Defang every scratchpad verb — do not frame any of them.** An envelope tells the model a span is
evidence to weigh and cite. `/scratch/` is this system's own notepad, so an envelope around it would
credit the system as a source for its own prose. That is the distinction `frame_connector_results`
already draws for a connector's *error* text and for a helper's report, applied a third time for the
same reason.

**Key it on `scratchpad_tools()`, the derived set, never a list written beside it.** Three of the
four arms are three different verbs, and enumerating "the ones that echo text" is the shape that is
correct on the day it is written: a verb an upstream bump adds is covered the day it is bound, and
`execute` and `delete` — which this deployment withholds — cannot enter the set, because that
function is where they are withheld. This is the argument `subagents.helper_profile` already makes
for subtracting `authz.side_effecting_tools()`.

**The stamp is asked first, and a name only of what the stamp did not claim — which is a change to
the shipped order, made because widening the name set is what made it matter.** The two sets can
collide with a connector's surface. `connectors/registry._declared_tool_names` refuses one bundle's
name colliding with *another bundle's*; nothing compares a declared name against the ambient ones,
so a connector declaring `read_file` — which a code-execution or document server would reasonably do
— is accepted. Measured against a live streamable-HTTP server declaring one: it wins
`ToolNode.tools_by_name` **and** carries the `SERVED_BY` stamp. Asked name-first, that payload would
be defanged instead of framed, stripped of the envelope and of the `probe:read_file` provenance a
citation needs, with third-party corpus text presented to the model as this system's own notepad —
exactly backwards. The risk arrives with the widening rather than with the seven verbs: `task` is
not a name anyone would serve, and six of the seven are ordinary English words. A stamped tool ran
outside this process whatever it is called, so the stamp decides; `tests/test_tool_framing.py` pins
it against the live server, and the assertion fails under the name-first order.

`scratchpad_tools()` is now `@cache`d, for the reason `subagent_tool_names()` was cached in the
previous ADR and stated the same way: it answers a question about the installed package by
*building* a `FilesystemMiddleware`, which is cheap once and wasteful on a path that runs per tool
call — which `agent/tool_framing.py` now is, twice.

**`tool_framing.py` decides three treatments, and its module docstring now says so.** It said only
out-of-process results are touched, decided by the `SERVED_BY` stamp, and in-process tools are left
alone. That went stale when the `task` branch landed and doubly stale here. Frame / defang / leave
alone, with the two rewritten sets read off the functions that derive them.

## Consequences

- Measured after, all four arms: the delimiter arrives as `&lt;/retrieved-note-…>`, nothing is
  framed, and the file is still in the caller's `files` channel on both turns.
- **Which of the new tests fail against the pre-fix tree, named — because "every one of them" was
  written here and is not true.** Six do, and they are the behavioural ones: the three channel tests
  in `tests/test_tool_framing.py` (read, `grep`, the echoed path), the per-verb sweep beside them
  once each iteration asserts the escaped form is *present*, and the two in `tests/test_subagents.py`
  (the caller's read-back, and the later turn's). Each was run against the branch removed. Two are
  **coupling assertions and by design cannot fail pre-fix**, because they assert something upstream
  does rather than something this change did:
  `test_a_subagents_files_still_cross_into_its_callers_state` reads `_EXCLUDED_STATE_KEYS` and passes
  identically at either commit, and `test_read_file_still_has_no_video_route_this_deployment_could_reach`
  reads `video_dependencies_available()`. Both are here to go red on a *dependency bump*, which is
  the job an absence assertion has in `tests/test_upstream_surface.py`, and calling them regression
  tests would have credited this change with coverage it does not have.
- The two tests that distinguish this fix from the narrow one were verified against a
  `read_file`-only mutation as well — which passes arm 1 and fails arms 2, 3 and the per-verb sweep.
- **One iteration of the per-verb sweep passes whatever the middleware does, and it says so.** `ls`
  answers with the directory entries under its path, and the forged path splits at the `/` inside
  the delimiter, so `ls /scratch` returns `['/scratch/</']` — no tag in it in any spelling, live or
  escaped, before the fix or after. Left as a bare absence assertion it would have read as a sixth
  covered verb while proving nothing, so the sweep asserts the escaped form is **present** for the
  five verbs that echo the tag and asserts `ls` for what it actually is. It stays in the loop
  because the loop's subject is the bound surface.
- **The kept crossing is asserted, not merely left alone.** `tests/test_subagents.py` asserts the
  helper's file *is* in the caller's `files` channel beside asserting the read is defanged, because
  a test that only checks for the delimiter goes green if somebody closes the crossing instead —
  which would be a narrowing this repository took without deciding it.
- `tests/test_upstream_surface.py` carries the absence: `"files" not in _EXCLUDED_STATE_KEYS`. If
  upstream ever excludes it, three paragraphs of prose go stale at once and a red test is what says
  so. The framing branch would still be needed for a caller's own writes, so that red is a prose
  correction and never an instruction to delete it.
- **What is still open is a bound, and it is a bound on the scratchpad rather than on delegation.**
  A helper writing a 2 MB file lands **2,000,137 characters** in its caller's checkpointed state,
  while the report beside it is bounded at `agent_max_tool_result_chars` (60,000). Nothing reads the
  whole channel into a prompt — a `read_file` result crosses `bound_tool_results` like any other —
  so this is checkpoint weight and retention rather than a context blow-out, and it is reachable by
  a caller writing its own files just as well as by a helper. It is a `docs/planning/BACKLOG.md` row
  with its measurement, not a silent omission.
- **The ambient/connector name collision is left open and is now visible.** Nothing refuses a
  connector declaring `read_file`, `grep`, `task` or `write_todos`, and the loser of that collision
  simply vanishes from the agent's surface — the failure `_declared_tool_names` was written for, one
  namespace further out. Framing is now correct either way, so this ADR does not need it closed; it
  is a `docs/planning/BACKLOG.md` row, with the measurement above as its evidence.
- **Defanging is presentation-only, and that is not the same as having no consequence.** Nothing on
  disk or in the `files` channel is rewritten, so the stored content stays pristine and a later read
  starts from the same bytes. What changes is that the model's copy of a span no longer matches the
  file's: an `edit_file` whose `old_string` was copied out of the read the model just did comes back
  `Error: String not found in file`, because the read showed `&lt;/…>` and the file holds `</…>`.
  Narrow while only a delimiter is escaped — and wider than it looks, because `framing._defang`'s
  second pass escapes **every** `<` in the content once an invisible character reveals a disguised
  tag, so one zero-width byte in a scratch file breaks that file's read→edit loop for all of its
  markup. Accepted: the recovery is one a model already has (edit on a span it did not copy through
  a rewrite), and the alternative is the live delimiter this whole ADR is about.
- What a caller still cannot see is *that* a file was written by a helper reading untrusted
  evidence. That is the same epistemic gap the previous ADR left open for the report, unchanged and
  already filed.
