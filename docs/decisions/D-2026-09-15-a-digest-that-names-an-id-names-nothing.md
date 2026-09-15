# D-2026-09-15-a-digest-that-names-an-id-names-nothing — what the sweep computed and what a reader was sent

**Status:** accepted · **Date:** 2026-09-15 · Beside
`D-2026-09-15-a-watch-that-nothing-evaluates-is-a-promise-a-deployment-cannot-keep`, which is the
other half of the same path.

## Context

`collect_digests` produces a `DigestItem` per subscription with four fields. Two of them reached
the person.

**`disputed` stopped at the API model.** Since `D-2026-08-27` the sweep has computed which of a
subscriber's new matches the corpus now *disagrees* with, and writes them into the mailbox payload.
Both outbound delivery channels render them, as "2 of 9 disagree with something already in the
graph". `api/routes/streams.Digest` declared no such field and `_digest` never read the key — and
`CHEMCLAW_DELIVERY_CHANNELS` is empty in every shipped deployment, so on the only path a UI reads,
the flag was computed on every run and reached nobody. `DigestItem`'s own docstring names that
asymmetry as the reason the field exists — *"a chemist who happens to ask is told, and a chemist
watching the subject is not"* — and it was still true, one layer further down.

**Nothing named a match.** Every surface — the text body, the mailbox payload, `GET /digests`, the
UI card — identified each match by its note **id**. So the one proactive thing this system does
told a chemist `playbook-aee3d30407cc` and left them to go and look it up. There was nothing else
to send: `Note` has no title field.

## Decision

**A digest carries a headline per match, and the route carries every field the job computed.**

`Note.headline()` returns the first non-empty body line, `#` marks stripped and `[[wikilinks]]`
flattened, cut on a word boundary. **Deliberately derived rather than a new `title` field**: a title
is a second place to say what a note is about, settable independently of the body and therefore able
to disagree with it, and every existing note would need one backfilled. The first body line already
*is* the headline in every note this corpus holds — a playbook opens with the rule as a heading, a
campaign with a one-sentence summary. Measured over the 39 shipped notes: **39 of 39** yield a
usable line, none empty.

`headlines` is a mapping keyed by id rather than a list parallel to `note_ids`, because the two must
not be able to come apart: a parallel list reordered or short by one relabels somebody's evidence.
The id stays beside the headline in every rendering — it is what a reader passes to
`GET /notes/{id}` and what the watermark works in.

## Consequences

- `Digest` gains `disputed` and `headlines`; `_digest` reads both behind `isinstance` guards, so a
  row written before they existed still reads and the route's deliberate leniency is preserved.
  That leniency is exactly why the dropped field was droppable in silence.
- The digest body reads `- Change the ligand before the temperature [playbook-abc]` instead of
  `- playbook-abc`.
- The sweep costs one string per match; it has already parsed every note by the time it builds the
  item.
- `Chemclaw3_ui` renders ids through `CitationChip`; using the headline there is a change in that
  repository, on its own pull request.

## What keeps it true

- `tests/test_digest.py::test_the_route_carries_the_dispute_flag_the_job_computed`
- `tests/test_digest.py::test_a_digest_names_what_it_found_and_not_only_its_id`
- `tests/test_digest.py::test_a_digest_is_read_by_its_owner_and_by_nobody_else`
