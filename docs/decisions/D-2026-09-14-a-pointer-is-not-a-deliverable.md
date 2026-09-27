# D-2026-09-14-a-pointer-is-not-a-deliverable — the report that reached a chemist outside the building was a note id, openable only from inside it

## Status

Accepted.

## Context

`D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` gave the `report` kind a producer:
`DevelopmentReportWorkflow` now delivers, so a chemist who closed the tab while the fan-out ran
hears that their report is written. What it says is:

> `{n} section(s), recorded as {note_ref}.\nOpen it beside its citations in the knowledge graph.`

The one reader this message has is, by construction, the reader who is not looking at the knowledge
graph. That is the whole reason the delivery exists. So the message told them a note id and asked
them to go somewhere they had already left.

The same gap is one seam over. `D-2026-09-14-an-artefact-three-files-name-and-none-produces` built
the run sheet's CSV export, and that artefact reaches a chemist over HTTP with a `Content-Disposition`
naming it — because a plate goes into instrument software, a LIMS or a workbook, and each of those
reads a *file*. The delivery seam had no way to carry one: `Message` is five strings.

## Decision

`Message.attachments: list[Attachment]`, bounded at 8, with `filename`, `media_type` and `content`.

**`body` is the message and an attachment is the artefact.** That is the rule that decides what goes
where, and it is not a size rule — measured, a full 96-well run sheet is **6,089 characters**, so
nothing here is about volume. It is about form: a CSV pasted into a Markdown body is not a file
anybody can open, and a note id is not a document.

**The filename is bounded by a pattern, not by a docstring.** `FileDeliveryDriver` joins it onto a
directory, which is exactly `Message.kind`'s argument one field over — that field said "a bounded
vocabulary" in prose while being a bare `str`, and an absolute or `../`-bearing value was an
arbitrary file write with the pod's uid. The pattern admits no separator, no `..` and no leading
dot. Driven: five hostile filenames, all refused.

**The share prefixes each attachment with the message's own id.** The message file is
content-addressed precisely so a retried activity overwrites its own copy rather than leaving two;
an attachment named only by its filename would undo that for the half a chemist actually opens, and
two designs both exporting `run-sheet.csv` is the ordinary case. The attachments are written
*before* the message that names them, the same ordering rule as `kg/record.py` writing a note's
dependencies first: a chemist watching the share opens the `.md` the moment it appears, and a
message listing a file that is not there yet reads as a delivery that lost it.

**The idempotency key reads an attachment's identity, never its bytes.** Both directions were
driven. Keyed on bytes, a redraft of one report with a word changed becomes a *second* delivery,
which is the opposite of what the key is for. Ignoring attachments entirely gives a message and the
same message carrying the artefact one id, so a compliant receiver drops the copy that has the file.

**The encoding is base64 in both directions, and that is a wire decision rather than a style one.**
`OutboundMessage` crosses a Temporal activity boundary, so the encoding is part of a durable
payload. pydantic's default for `bytes` is a utf-8 *decode* — driven, it raises
`PydanticSerializationError` on the first byte outside it — so a seam that shipped text-only and
widened later would be changing the wire under open histories, which is the class of defect
`D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes` records on this same
branch. Paying 33% on the largest artefact this system produces is two kilobytes.

**`OutboundAttachment` is loose for the reason `OutboundMessage` is.** The filename pattern has to
fail *inside* the activity: a workflow constructing a rejected `Attachment` raises in workflow code,
where no best-effort wrapper can catch it, and the courtesy copy becomes the thing that fails the
job whose real result is already durable. It keeps `AttachmentBytes`, because that annotation is
about the wire and not about validation — a workflow passes real bytes and the validator returns
them unchanged.

**An attachment goes through the redaction a body does.** Typing a field `bytes` is not a reason to
put the half that leaves the cluster outside the guarantee `redacted()` exists to give; the two
artefacts this seam carries are text assembled from tool results, exactly as a body is. Bytes that
are not utf-8 pass through untouched rather than failing the delivery — **a stated limit**, since a
credential inside a future binary artefact is not scrubbed, and the first such producer is when that
becomes a decision.

**The producer is the report, and the filename is the note's id.** `note_ref` is the *writer's*
reference — a commit sha, or the unchanged tree — so a file named after it tells a chemist nothing
and could carry characters the pattern rejects, costing the whole message rather than the file.
`report_note` is pure rendering over a value the workflow already holds, so it is called once in
workflow code (no command emitted) and read for both halves. This adds a field to an activity's
argument and **not** a new `await`, so unlike the seam it extends it needs no `workflow.patched`.

## What is deliberately not built

**docx and xlsx.** Wave 7 scoped "a renderer registry and docx/xlsx" beside this, and the second
half stays unbuilt for a reason this repository already holds: `openpyxl` is in the tree only
*transitively*, through `drfp`, with no version pin and no declaration in `[project.dependencies]`,
and four modules plus `tests/test_datasource_isolation.py` exist to keep it out of the chat
process's import graph. A deliverable format is not a reason to undo that. The seam now carries
whatever a producer can render, so adding one is a producer's decision rather than a wire change —
which is the point of getting the encoding right first.

## Consequences

- The webhook payload gains one key. `test_the_webhook_sends_the_recipients_view_and_not_the_join_key`
  pins the projection as set equality and had to be updated deliberately, which is the guard working.
- A deployment with delivery on and a file channel now gets two files per report instead of one.
- `_write_atomically` takes `str | bytes`; the temp-file mode follows the argument.

## What keeps it true

- `tests/test_delivery.py::test_an_attachment_reaches_the_share_as_its_own_file`,
  `::test_two_messages_carrying_one_filename_do_not_overwrite_each_other`,
  `::test_an_attachment_filename_cannot_escape_the_outbox`,
  `::test_an_attachment_is_redacted_like_a_body`,
  `::test_a_binary_attachment_survives_the_redaction_rather_than_failing_the_delivery`,
  `::test_an_attachment_crosses_the_wire_as_base64_and_comes_back_whole`,
  `::test_the_same_message_with_and_without_a_file_are_not_the_same_delivery`.
- `tests/test_outbound_delivery.py::test_the_report_message_carries_the_draft_and_not_only_its_reference`,
  `::test_an_attachment_a_workflow_builds_is_checked_where_it_can_be_caught`.
