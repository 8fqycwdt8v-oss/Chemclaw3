# D-2026-09-19-a-coefficient-measured-at-one-cap-is-a-claim-about-that-cap — the parse budget's other three residuals

**Status:** accepted · **Date:** 2026-09-19 · Extends `D-2026-09-19-a-refusal-that-blames-the-document-is-worse-than-one-that-says-nothing`

## Context

That ADR closed three residuals of the parse memory bound and left three open, each with a
`BACKLOG.md` row rather than a guess. This closes all three. Two needed code; one needed only to be
driven, and driving it is what turned it from a worry into a fact.

## 1. The coefficient has two terms and only one was declared

`tests/test_deploy_chart.py::PARSE_MIB_PER_PARSE_BUDGET_MIB` multiplies
`document_parse_memory_bytes`, and that budget is what a parse may allocate **beyond** the document
it was handed: `_bound_allocations` reads its baseline after `raw` is unpickled into the child.
Driven — `VmData` **230.4 MiB** before a 50 MiB document and **280.5 MiB** after, the whole 50 MiB
of it.

So a pod's real per-parse charge is a function of the largest document a binding will hand it, and
`binding.max_file_bytes` is set per `datasource.yaml` with `ge=1024` and **no upper bound**. A site
binding at 200 MiB moved the real charge to ~360 MiB while
`test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares` did not move at all. The
coefficient's own comment named "the shipped cap" in its measurement and nothing held it there.

**Decision: refuse at load, rather than charge both terms in the chart.** The coefficient is a
*measurement* at a basis — 406.7 MiB of pod for two concurrent 50 MiB plain-text documents — so a
site that wants larger documents needs it re-measured, not re-arithmetic'd. `max_file_bytes` gains
`le=PARSE_COEFFICIENT_BASIS_BYTES`, and a test asserts the bound and the basis are the same number,
so raising the cap is legitimate and visibly costs a re-measurement.

### The alternative was built, measured and reverted

Folding the document *into* the budget — baseline `VmData − len(raw)`, so the ceiling bounds
`document + work` — is the version where the coefficient has one term again, and it is wrong for
this parser set. Driven at the shipped 160 MiB budget it refuses a **40 MiB** plain-text file,
where the share binding's own `max_file_bytes` permits 50, because a text parse holds the bytes,
the decoded `str` and the pickle at once.

**And the measurement that made it look viable was mine and was invalid.** A probe that lowered
`document_parse_memory_bytes` in the *parent* and reported every shipped document still parsing
measured nothing at all: a `forkserver` child is forked from a server started earlier, so a
parent-side settings change never reaches it. Driven afterwards — a 20 MiB document parsed
cleanly under a parent-side 7 MiB budget. Recorded because the conclusion it produced
("50 MiB parses with 107 MiB to spare") was stated before it was checked against the shipped path,
and it is the same defect this line of work keeps finding: a number measured in conditions nobody
wrote down.

## 2. A refusal was pickled under the ceiling that caused it

Python does not route an exception raised inside one `except` clause to a later one, so a
`MemoryError` while pickling the refusal onto the pipe escaped `_parse_into` entirely and the
caller got an EOF it could only report as "stopped without answering" — the one failure in that
module whose cause is knowable, arriving nameless. The refusal is ~250 characters, so this is
unlikely rather than impossible, and *unlikely* is an argument rather than a measurement.

`_release_allocations` removes the question instead of estimating it. By the time a handler runs
the parse is over and the child is about to exit, so the budget's job is done: the soft limit goes
back to the hard one before any failure arm sends, and the reply gets the room the parse was
denied. The success path is unchanged and still pickles **inside** the ceiling, deliberately — that
copy is real memory spent on this document, which is what its own arm has always said. The hard
limit is not touched, because lowering it is irreversible for the process.

## 3. `.pptx` was already covered, and now something says so

The row asked whether a markup-heavy deck earns the named refusal, the wrong one, or none. Driven:
a **1,552,596-byte** deck of 600 slides × 600 styled runs holds **2,821,690 characters**, parses
unbounded in 4.4 s, and is refused through the shipped path in 0.5 s **with the memory refusal**.
`_at_ceiling` covers it, because it asks about the process rather than about the format. A smaller
deck (200 × 400, 620,490 characters) parses, so the bound is not simply refusing decks.

No code changed for this one. What changed is that it is now asserted, in both directions, beside
the `.docx` pair it mirrors.

## Consequences

- Raising a share's `max_file_bytes` past 50 MiB is now a startup refusal naming the coefficient,
  where it used to be a silent invalidation of the pod's memory inequality.
- Every failure arm in `_parse_into` releases before it replies; the success arm does not.
- Three `BACKLOG.md` rows are deleted, two of them opened by this ADR's parent.

## What keeps it true

- `tests/test_deploy_chart.py::test_the_parse_coefficient_still_describes_the_largest_document_a_binding_may_declare`
- `tests/test_parse_isolation.py::test_a_refusal_is_not_bounded_by_the_ceiling_that_caused_it`
- `tests/test_parse_isolation.py::test_a_deck_stopped_by_the_budget_earns_the_same_named_refusal_a_document_does`
- `tests/test_parse_isolation.py::test_a_document_stopped_by_the_budget_says_so_even_when_a_c_parser_reported_it`
- `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`
