# D-2026-09-19-a-cap-on-the-contents-is-not-a-cap-on-the-channel — a path is charged for what it is long

**Status:** accepted · **Date:** 2026-09-19 · Revisits `D-2026-09-19-a-cap-on-each-file-is-not-a-cap-on-the-command`

## Status

Accepted. Revisits `D-2026-09-19-a-cap-on-each-file-is-not-a-cap-on-the-command`, which is merged
and stands: its decision — files past what the budget can represent are omitted, with one entry
naming the set rather than a marker each — is unchanged. What changes is the arithmetic under it.

## Context

`agent_subagent_files_max_chars` is documented as a bound on **the caller's `files` channel**,
because LangGraph writes that channel whole per superstep and per version. Four decisions have now
tightened it: `D-2026-09-12` bounded a helper's file at all, `D-2026-09-13` made the bound the
channel's rather than one `Command`'s, `D-2026-09-18-a-pre-batch-snapshot-…` divided it by the
superstep's concurrent producers, and `D-2026-09-19-a-cap-on-each-file-…` capped the count because
`bounded_content`'s notice is a floor.

Every one of those measured **`content`**. `_files_already_held` summed `len(data["content"])` and
never read a key; `rewritten_command_files` divided a budget that had never seen one; and
`tests/test_subagents.py`'s superstep sweep — the assertion that exists to hold the bound — summed
the same half.

A channel is a mapping. A path is charged for what it is long, and a path is not text this system
composed: it is the string a **model** passed to `write_file`.

## Measurement

Driven through the shipped `bound_tool_results` on `main` at 5a91c607, one superstep, a
200,000-character budget, counting keys and text as the checkpoint does:

| width | files/call | path padding | content | keys | channel |
| --- | --- | --- | --- | --- | --- |
| 1 | 5,000 | 0 | 191,517 | 83,370 | **274,887** |
| 1 | 5,000 | 200 | 193,517 | 972,170 | **1,165,687** |
| 1 | 5,000 | 1,000 | 201,517 | 4,527,370 | **4,728,887** |
| 8 | 600 | 1,000 | 274,224 | 4,519,392 | **4,793,616** |
| 1 | 50 | 200,000 | 100,000 | 10,000,840 | **10,100,840** |

The first row is the one worth reading: **37% over the budget with no adversary at all**, on
ordinary `/scratch/w0-4443.md` keys. Every row's `content` column is what the shipped sweep
asserted, and rows 1, 2 and 4 pass it.

Row 3 shows a second defect in the same place. `_dropped_notice` sampled **ten paths** — which
bounds the count of the sample and not its length — so ten thousand-character paths put the
*content* alone at 201,517, the notice being the unbounded thing its own docstring said it must not
be. Re-driven against the new test with 20,000-character paths, the ten-path form lands 380,943.

## Decision

**The budget is charged for keys as well as text, and the allocation moves to the loop that can see
a key.**

- `_files_already_held` sums `len(path) + len(content)`.
- `_files_budget(request)` replaces `_representable_files`: one number — the setting, less what the
  channel holds, divided by `batch_siblings` — handed to `rewritten_command_files`. The count cap it
  replaces was a *model* of when the budget runs out; the budget running out is available directly,
  so the derived-floor arithmetic is deleted rather than corrected.
- `rewritten_command_files` spends a **remainder**: each changed file's share is what is left
  divided by the files still to come, less its own key, and a file the remainder cannot pay for is
  dropped. A file that came in under its share hands what it did not spend to the ones after it,
  which is what makes the total exact rather than merely bounded.
- `_bounded_file` keeps the cut, the WARNING and
  `chemclaw_subagent_file_truncations_total`, and takes a character share. Its `held`/`sharing`/
  `concurrent` arithmetic was about the *request*, not about one file, and now lives in
  `_files_budget` — so there is one division rather than two.
- `_dropped_notice` cuts its sample to fit a budget the loop reserves off the top, derived from
  `_dropped_head`'s own length. The count and "reading one back will fail" never shrink, because a
  caller cannot act correctly without them; the sample of paths is the part that is nice to have,
  so the sample is the part that goes.

**What it costs.** Fewer files survive a large fan-out, because the channel is now paying for what
it always stored: 5,000 changed files keep 3,240 of them where the old arithmetic kept 4,444 and
charged 37% over. At a channel already **at** its budget nothing new is stored at all — the
previous behaviour left a 44-character marker per file, which is the linear growth the cap exists
to stop.

## Consequences

- The superstep sweep now measures keys and text, and sweeps a path-padding dimension. Driven, the
  pre-fix accounting reds it at `1 × 600 → 210,453`.
- `tests/test_subagents.py::test_a_dropped_set_is_named_while_there_is_room_to_name_it` holds both
  ends of the notice: the sample appears when there is room, and it is cut when there is not.
- `test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` now asserts that an
  exhausted channel **drops** the helper's file and says so, rather than storing a marker.
- Three tests that drove `_bounded_file`'s old parameters were retargeted at the behaviour rather
  than at the parameters — which is the defect this review wave keeps finding, one file over.

## What keeps it true

- `tests/test_subagents.py::test_the_file_share_bounds_the_superstep_at_every_width_this_deployment_allows`
- `tests/test_subagents.py::test_a_dropped_set_is_named_while_there_is_room_to_name_it`
- `tests/test_subagents.py::test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with`
- `tests/test_subagents.py::test_several_files_share_one_budget`
- `tests/test_subagents.py::test_an_exhausted_budget_still_cuts_when_more_than_one_file_crosses`
- `tests/test_subagents.py::test_the_file_cap_set_to_zero_is_off_rather_than_absolute`
- `tests/test_subagents.py::test_the_fan_out_divisor_counts_the_tools_that_write_files_not_the_whole_batch`
- `tests/test_upstream_surface.py::test_a_file_a_helper_hands_back_is_a_mapping_carrying_its_text_under_content`
