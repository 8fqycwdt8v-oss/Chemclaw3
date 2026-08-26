# D-2026-08-26-a-labels-block-says-what-a-source-carries-not-whether-to-label-it — the labelling drain reads every source, and keys its answers on the row

## Status

Accepted. Corrects two defects in the implementation merged by `#213`
(`D-2026-08-25-a-label-is-derived-not-recorded`). Neither is a change of decision — that ADR
already says what the code should have done.

## Context

`D-2026-08-25-a-label-is-derived-not-recorded` states that a source's `labels:` block is read for
**exactly two things**: the coverage report, and the subset check on `override`. The shipped code
read it for two more, and each was a defect.

### The drain was never scheduled for an ELN corpus

Two gates asked "does a source declare a `labels:` block?" and treated the answer as permission to
label:

* `durable/schedules.py` created the Schedule only `if label_policies():`.
* `ingest/labels/enrich.py` narrowed `stale()` to `sources=sorted(policies) or None`.

Of the ten sources in this tree exactly one declares a block — `pistachio` — and it ships disabled.
So on a stock deployment the Schedule was never created, and with Pistachio enabled the four ELN
sources were still never drained. Measured, with one ELN row and one Pistachio row in the index:

```
pass 1: labelled=1 unlabelled=0 has_more=False
pass 2: labelled=0 unlabelled=0 has_more=False
  eln-json   labelled_at_version=None
  pistachio  labelled_at_version='v1'
```

`has_more=False` is the damaging half: the drain reported *nothing left to do* while an entire
corpus sat unlabelled. Downstream this is invisible rather than loud — `current_version()` returns
the Pistachio version, `select()` filters on it, and every ELN row is quietly excluded from every
precedent answer. The coverage verdict says the corpus is unlabelled, correctly, and never stops
saying it.

### Two sources sharing a reaction id got each other's labels

`reaction_labels` keys a row on `(source, reaction_id)` deliberately — `ingest_reaction`'s own
docstring says why: *two ELNs may legitimately use one entry id.* But the drain sent the bare
`reaction_id` to the labelling server and keyed the answers by it, and `stale()` is
`ORDER BY source, reaction_id` across all sources, so one batch can hold both. The second answer
overwrote the first. Measured, against a fake that answers every id correctly:

```
report: labelled=2 unlabelled=0 has_more=False
  eln-a  record=CCO>>CCOC              mapped=MAP<c1ccccc1Br>>c1ccccc1N>  correct=False
  eln-b  record=c1ccccc1Br>>c1ccccc1N  mapped=MAP<c1ccccc1Br>>c1ccccc1N>  correct=True
```

An esterification stored with an amination's atom map and named reaction, reported as cleanly
labelled. `merge._species` then applies the wrong reaction's species list **positionally**, so a
reagent is recorded as a ligand — which is what a frequency table counts. Latent only because the
first defect stopped the drain running at all; fixing that one alone would have turned this on.

## Decision

**A `labels:` block says what a source carries. It is never read as permission to label.**

* `enrich.label_stale` calls `index.stale(version, limit)` unfiltered and looks the policy up per
  row, falling back to `_DERIVE_EVERYTHING`. That fallback is the ordinary case, not an edge one.
* `schedules.py` plans the drain wherever there is a corpus to label —
  `active_ingest_source_names() or corpus_sources()`. The original concern survives: a deployment
  with neither still gets no Schedule, so nothing asks the labelling server for its version hourly
  in order to label nothing.

**What goes on the wire is a correlation token, not the reaction's name.** The server has no stake
in our identity — it echoes whatever id it is handed — so `_batch` tags each row with a token that
is unique within the batch, and places the answers back onto rows by `(source, reaction_id)`. An
answer carrying a token the batch did not send is dropped with a warning rather than raised on: the
server is versioned separately, and one unplaceable answer is not a reason to lose the rest.

`LabelIndex.stale`'s `sources` parameter **stays**, with no production caller, and this is the one
deliberate exception here. It is not a guard — nothing is safer for its existence — it is how a test
scopes a row set in a Postgres schema every test file shares, which is what lets the `LIMIT` and
ordering contract be asserted against a real database at all. Removing it would have cost that
coverage to satisfy a rule about dead code aimed at something else. Its docstring now says plainly
that the drain must not use it, and a test pins that the drain reads every source.

## Consequences

Three tests that fail on `0367836` and pass here: the drain reads a source declaring no block, two
sources sharing an id keep their own chemistry, and the Schedule is planned for an ELN-only
deployment. `test_plan_covers_all_periodic_jobs` now expects two always-on Schedules rather than
one — the ELN sync and the labelling drain travel together, because an ingest source writes entries
and every entry it writes needs labelling.

Any index labelled before this change is unaffected in storage but may hold mismatched labels if two
enabled sources shared an entry id. Bumping `CHEMCLAW_RXNLABEL_SERVER_URL`'s server version, or any
of the three components of the version string, re-stales the whole index and re-derives it — which
is what the versioned design is for, and needs no migration.

Separately, `#212` (`D-2026-08-25-an-eln-transcription-is-data-not-a-claim`) landed while `#213` was
in flight and falsified prose in four of its modules, including two of the five reasons
`ingest/labels/corpus.py` gave for a corpus having no ingest half — `propose_note` and
`_merged_note_bodies` are both gone. The docstring now gives three reasons and records that the two
that went were the *review* reasons, so a reader does not reconstruct a case that no longer exists.
