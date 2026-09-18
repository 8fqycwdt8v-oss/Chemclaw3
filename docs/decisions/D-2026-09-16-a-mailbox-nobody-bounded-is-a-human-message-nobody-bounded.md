# D-2026-09-16-a-mailbox-nobody-bounded-is-a-human-message-nobody-bounded — the job push-back block is cut before it is framed

## Status

Accepted. Closes a gap in the producer enumeration `agent/compaction.py`'s third-reducer paragraph
and `tests/test_compaction.py::test_no_shipped_producer_of_a_human_message_reaches_the_offload_threshold`
carry between them — whose reasoning stands and whose list was short by one. That enumeration
shipped in `ed7be871` with no decision record of its own, which is why this one cites the module and
the test rather than an id: a citation to a record that does not exist resolves for nobody, and
writing one was this document's own first draft.

## Context

`agent/compaction.py` documents a third reducer above its own two: `deepagents.FilesystemMiddleware`
offloads an oversized `HumanMessage` to a file and hands the model a pointer plus a head-and-tail
preview. That preview is **not** defanged, and the module argues at length that this is harmless:

> It grants nothing new, because it is a strict substring of a `HumanMessage` that sat in the
> model's context verbatim one call earlier — a chemist's own message is not framed as untrusted
> data.

It then enumerates the producers and asserts, in `test_no_shipped_producer_of_a_human_message_
reaches_the_offload_threshold`, that each sits below 200,000 characters: the front door at 100,000,
template steps at 60,000, and `cli/chat.py` deliberately excluded as an operator's own paste.

**`_with_pushed_job_results` is a fourth producer and it is in every Postgres-backed deployment.** It
takes the front door's message and *appends* the job push-back mailbox to it. Nothing bounds that
block: `claim_unconsumed` takes no limit, and `ConnectorJobResult.summary` declares `min_length=1`
and no maximum. Measured with one unbounded summary beside a maximum-length chemist message:
**235,377 characters**, past the threshold.

Two things make it worse than the case the argument covers, and the test's own docstring names the
first as the condition under which the argument stops holding:

1. **The block is not the chemist's words.** It is workflow output, and it is wrapped in
   `frame_untrusted` precisely because it is untrusted. "A strict substring of what the model
   already read verbatim" is true of the chemist's half and false of this one.
2. **The preview cuts by lines**, five from the head and five from the tail. The opening delimiter
   sits after the chemist's message, so with a one-line question it survives at line 4 — and with a
   **five-line** question, an entirely ordinary shape, it falls in the truncated middle while the
   closing delimiter survives. Driven:

   ```
   opening delimiter survives: False
   closing delimiter present : True
   ```

   The model is handed four thousand characters of job output with no opening frame, terminated by a
   stray `</retrieved-note-…>`. (The unconditional form of this claim is wrong, and stating the
   condition is the point: with a short question the frame is intact.)

## Decision

The push-back summary is cut to `agent_max_tool_result_chars` by `bounded_content` — the same
function that bounds one tool result, with the same notice naming itself as system text — **before**
it is framed. Cutting inside the frame rather than after it means the delimiters cannot be what a cut
removes. A cut is logged with the count of waiting events, because a mailbox large enough to be cut
is itself worth knowing about.

Measured after: 235,377 → **160,385** characters, one opening delimiter, one closing delimiter, the
chemist's words still leading.

`test_no_shipped_producer_of_a_human_message_reaches_the_offload_threshold` now asserts the
producers as a **sum** rather than one at a time, because one of them appends to another and
comparing each half separately passed while the total did not.

## Consequences

- A chemist with a very large mailbox sees the newest outcomes and a notice saying what was cut,
  with `get_durable_job_status` named as the way to the rest. Previously they would have seen a file
  pointer and a preview.
- The bound is the tool-result ceiling rather than a new setting. It is the right order of magnitude
  and it keeps the sum below the threshold with 40,000 characters to spare; a new knob would be a
  fourth number nobody reconciles.
- Neither `claim_unconsumed` nor `ConnectorJobResult.summary` gains a limit here. Bounding what is
  *rendered* fixes the reachable harm; bounding what is *claimed* would silently drop rows the
  claim has already consumed, which is a worse failure and a separate decision.

## What keeps it true

- `tests/test_compaction.py::test_the_job_push_back_block_is_bounded_before_it_is_framed` drives
  the real function over a 600-event mailbox and a maximum-length message, and asserts the length,
  both delimiters, and that the chemist's words still lead.
- `tests/test_compaction.py::test_no_shipped_producer_of_a_human_message_reaches_the_offload_threshold`
  holds the arithmetic as a sum, reading the threshold off the installed distribution.
