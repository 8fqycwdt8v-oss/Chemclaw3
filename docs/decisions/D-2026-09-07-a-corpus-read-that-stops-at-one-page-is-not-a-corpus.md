# D-2026-09-07-a-corpus-read-that-stops-at-one-page-is-not-a-corpus — the memory read pages, and says so when it cannot

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
`D-2026-08-26-the-driver-s-signature-is-the-schema` (the warehouse adapter and its `fetch_limit`),
`D-2026-08-08-a-partial-answer-must-say-so`, D-019 (the memory layers add no infrastructure) ·
**Corrects** the `BACKLOG.md` row that recorded this as a *cost*.

## Context

`durable/memory_jobs.read_corpus` is the corpus all three memory miners reason over. It called
`fetch_new_entries(datetime.min)` once per source and took what came back. The register carried
that as a scaling defect: a full table scan per activity per scheduled run.

## What was measured

The opposite failure, and a worse one. Driven against the warehouse adapter over a 12-row corpus at
`fetch_limit: 5`, `read_corpus` returned **5 of 12** reactions with `complete=True`. The adapter
pages: it returns `min(limit, fetch_limit)` rows and reports itself truncated, and nothing here
asked. At the shipped `eln-databricks` binding default that is the oldest **500** rows of the ELN,
distilled into campaign, playbook and optimization notes and recorded as what this deployment
knows, with no warning, no counter and a `complete` flag saying the read was whole.

The row's own premise was therefore false for the only shipped source that pages: there was no full
table scan to be worried about, because the read stopped after one page.

## Decision

`read_corpus` loops per source: fetch, map what is new, advance the floor to the newest
`entry_window` seen, and stop when a page offers nothing new. Entry ids are deduplicated per source,
which is what absorbs the inclusive-`since` boundary row every fetch replays. A drop directory
reads its whole directory in one call and reports no truncation, so it makes exactly one pass —
unchanged behaviour and unchanged cost for both shipped file sources.

**A source still reporting rows waiting when the loop ends makes the read incomplete.** That is the
warehouse adapter's un-crossable watermark block (`_MAX_TIE_PAGES`), which reports itself truncated
forever; `CorpusRead.complete` already existed for the unmappable-entry case and the miners already
take `corpus_complete`, so the fact reaches them by the path built for it rather than by a new one.

**The cost this makes real is accepted and stated.** A scheduled memory run now reads each source
whole, three times, once per miner activity — which is the scan the register was worried about,
arriving because the read became correct. A complete read that costs what it costs is the right way
round; the cheap read was wrong. The `BACKLOG.md` row stays open for that cost, corrected to say
what is now true, and its two candidate answers are unchanged: a fetch-by-id on the adapter
protocol (every source pays) or a derived store of mapped `OrdReaction`s.

## What holds it

`tests/test_memory_jobs.py::test_the_memory_corpus_is_the_whole_source_and_not_its_first_page`,
watched failing against the unfixed source at **5 of 12** reactions with `complete=True`. Its second
arm drives a source that reports more waiting and cannot page: the loop ends after the page that
offers nothing new — two fetches, not a spin — and the read is returned incomplete.
