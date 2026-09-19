# D-2026-09-19-a-refusal-that-blames-the-document-is-worse-than-one-that-says-nothing — three residuals of the parse budget

**Status:** accepted · **Date:** 2026-09-19 · Extends `D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`

## Context

`document_parse_memory_bytes` bounds what one parse may allocate, enforced by the kernel on the
child that allocates. The bound is right and stays. A fresh-context adversarial review of the
commit that introduced it found three residuals, and the first reaches a chemist.

## 1. lxml's allocation failure told a chemist their document was malformed

`too_large_to_read` is reached by two arms: `parse_document`'s own, and `_parse_into`'s
`except MemoryError` for the pickling of an answer. **A C parser that reports its own allocation
failure rather than letting CPython raise reaches neither** — which was stated as a residual in
`too_large_to_read`'s docstring, and understated there twice.

Driven on the shipped path: a Word report of 2,000 paragraphs × 200 styled runs — the shape Word
itself produces after tracked changes, mixed fonts or a round-trip through another tool —
**485,186 bytes on the wire, 2,979,999 characters of text**, legal by every declarative bound
above. Unbounded it parses in 19.4 s. Through `parse_document_isolated` it came back as:

```
could not read report.docx: unknown error (<string>, line 0)
```

That is not a refusal missing its reason. It is a refusal asserting a *different* one: the document
is broken at line 0. `too_large_to_read`'s own docstring says the reason it exists is to stop
"telling a chemist their perfectly good workbook 'could not be read'", and this wording is worse
than the one it replaced, not equal to it. Neither the chemist nor an operator can act on it — the
knob is not named, and the honest remedy (the relevant section, or the file split) is not offered.

**Decision.** `isolate._at_ceiling` renames any failure that happened with the budget spent, so all
three arms arrive at `too_large_to_read`. `VmData` rather than a flag set by the failing allocation,
because there is no flag to set — the failure happens inside libxml2 and surfaces as a value. The
two populations do not overlap and that is measured, not assumed: a parse stopped by the budget
fails with **0.1 MiB** of its allowance left; a truncated archive, a non-zip, an empty file and a
wrong extension all fail with the **whole 160 MiB** unspent. `_CEILING_HEADROOM_BYTES` is 8 MiB —
small rather than generous, because anything in between is a parser this repository has not seen
and the conservative reading of it is "not the budget".

**What this does not do.** The document is still refused. 2.98 M characters are inside every
downstream limit and the parse asks for ~840 MiB of lxml DOM, so refusing is the bound working;
the defect was the sentence, and the sentence is what changed.

## 2. A lower ambient hard limit made every document unreadable

`setrlimit` cannot raise a maximum. A process tree carrying any hard `RLIMIT_DATA` below
`VmData + document_parse_memory_bytes` — a systemd `LimitDATA=`, a container security profile, or
simply an operator raising this knob above what the platform allows — made `_bound_allocations`
raise `ValueError: not allowed to raise maximum limit`, which lands in `_parse_into`'s broad arm.
**Every upload and every share document of every format** then came back as "could not be read",
with the cause only in a log line. Driven at `base + 8 MiB`.

**Decision.** The inherited hard limit is read and wins: a lower ambient ceiling becomes the budget,
at WARNING, which is the outcome this knob is for. The hard limit is passed through unchanged
rather than lowered to the soft one — lowering it is irreversible and this child has no reason to
take that from itself. A `setrlimit` that fails anyway is logged and the parse runs unbounded, which
is the same posture as a missing `/proc/self/status`.

## 3. Two of the six published character thresholds were false, and all six were the wrong unit

The setting's comment published: *"a 17.2 M-character workbook parses. Refused: a 24.5 M-character
delimited export, a 52 M-character workbook, and a 17.2 M-character workbook carrying one astral
code point."* Re-driven through the shipped path on the shared-string fixture:

| fixture | measured |
| --- | --- |
| ASCII workbook, 50,145,012 chars | **parses** (0.8 s) |
| ASCII workbook, 60,177,012 chars | refused, named |
| one-astral workbook, 18,052,214 chars | **parses** (0.5 s) |
| one-astral workbook, 20,058,012 chars | refused, named |

So the "52 M-character workbook" and the "17.2 M-character workbook carrying one astral code point"
both parse. **And the unit is the defect rather than the digits.** A character count is not what
this bound reads: the same count parses or refuses depending on the fixture's shape and on how much
of the allowance the forkserver's own baseline residency has already spent — the astral crossing
measured 22 M on one box and 20 M on another with no code in between. That is the argument of the
comment's own third paragraph, happening inside the paragraph that was demonstrating it.

**Decision.** The comment states the three *shapes* that reach the ceiling and stops publishing
thresholds. `tests/test_parse_isolation.py` holds the behaviour on named fixtures, which is a basis
a different box does not move.

## Consequences

- A markup-heavy `.docx` now earns the same named refusal a shared-string workbook does.
- A platform-imposed `RLIMIT_DATA` degrades the budget instead of disabling the parser.
- Two tests drive red-then-green: without `_at_ceiling` the refusal reads
  `unknown error (<string>, line 0)`; without the clamp `_bound_allocations` raises `ValueError`.
- Left open, and stated rather than fixed, with a `docs/planning/BACKLOG.md` row each:
  **`.pptx`** goes through the same lxml layer and its behaviour under this ceiling is unverified;
  and the budget is what a parse may allocate **on top of the document**, because
  `_bound_allocations` reads its baseline after `raw` is unpickled into the child — driven,
  `VmData` 230.4 MiB before a 50 MiB document and 280.5 MiB after. The chart's coefficient
  multiplies only the budget, and `binding.max_file_bytes` has no upper bound, so a site binding at
  200 MiB moves the real per-parse charge and moves no inequality. That is this ADR's parent
  argument — a coefficient of the quantity it is declared against — failing on its second quantity.
  Not fixed here because `tests/test_deploy_chart.py` is being rewritten on another branch, and the
  row states the three candidate fixes with what each costs.

## What keeps it true

- `tests/test_parse_isolation.py::test_a_document_stopped_by_the_budget_says_so_even_when_a_c_parser_reported_it`
- `tests/test_parse_isolation.py::test_an_ambient_hard_limit_below_the_budget_is_a_smaller_budget_not_a_dead_parser`
- `tests/test_parse_isolation.py::test_a_legal_upload_cannot_spend_more_than_the_parse_budget_declares`
- `tests/test_parse_isolation.py::test_one_wide_code_point_does_not_multiply_what_a_parse_may_spend`
