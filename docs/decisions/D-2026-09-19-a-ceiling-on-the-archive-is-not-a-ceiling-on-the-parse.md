# D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse — bounding a document parse in the unit that kills the pod

**Status:** accepted · **Date:** 2026-09-19

## Context

`D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` sized the front door
and the background worker against the parse forkserver, and wrote the parse itself into
`tests/test_deploy_chart.py` as a coefficient:

```python
#: So it is linear in the expanded size, which is what makes it a coefficient rather than a table:
#: 3.07 and 2.86 MiB per expanded MiB. The larger, rounded up.
PARSE_MIB_PER_EXPANDED_MIB = 3.1
```

All three samples behind that number were ASCII. CPython stores a `str` at the width of its widest
code point (PEP 393), and `ingest/documents/parse.py::_parse_xlsx` ends in one document-wide
`"\n\n".join(blocks)` — so one wide character anywhere multiplies the whole joined document. Em
dashes, superscript minus, `Å` and equilibrium arrows are routine in chemistry documents.

That is the report this work started from, and it is true. What measuring it found is that the
width is the *smallest* of three reasons the coefficient cannot hold, and that the defect is not a
number but the quantity it is a coefficient of.

## What was measured

Every figure below was driven on this tree, at this commit, with the shipped settings, in a **real
memory cgroup** — `memory.max_usage_in_bytes`, reset immediately before each parse, over a process
tree holding the parent, the forkserver and every parse child, which is what a container is. Not
`Pss`; see the last section.

### The coefficient is a function of two things it was not declared against

One **legal upload** — 1,089,493 bytes on the wire against `attachment_max_bytes` of 2,000,000, and
63.4 MiB expanded against `document_max_expanded_bytes` of 64 MiB — built four times, identical but
for one character, and a `.docx` of the same shape:

| Shape | expanded | chars | ASCII | one `°` (Latin-1) | one `—` (BMP) | one U+1F9EA (astral) |
| --- | --- | --- | --- | --- | --- | --- |
| workbook, `_parse_xlsx` | 63.4 MiB | 52.2 M | **236 MiB** | 287 | 337 | **500** |
| `.docx`, `_parse_docx` | 61.0 MiB | 57.8 M | **369 MiB** | 424 | 480 | **589** |

Per expanded MiB that is 3.73 → 7.90 for the workbook and 6.05 → 9.67 for the `.docx`, against a
declared 3.1. A sparser workbook at the same ceiling reads 1.81 ASCII, so **width alone spreads one
declared quantity across 1.81 to 9.67 MiB per expanded MiB — a factor of 5.3** — and the constant
sat near its floor. The markup-heavy shape two sections down takes the same range to 16.5, which is
a factor of 9.1 on a number written as one.

Two of those columns are worth separating. The astral column is the reported mechanism and it is
the largest single step. The **Latin-1 column is not zero**, and the report expected it to be:
`sys.getsizeof` says a `°` costs nothing, because Latin-1 and ASCII are both the 1-byte kind. The
pod pays anyway — measured +0.96 to +1.44 bytes per character — because a non-ASCII `str` has no
UTF-8 representation of its own to hand to `pickle`, so one is built and cached beside it. A
`sys.getsizeof` budget cannot see that; a cgroup can.

### Two shapes where the expanded size predicts nothing at all

**A shared string is stored once and read N times.** A workbook whose `sharedStrings.xml` holds one
1,000-character string, referenced from 200,000 cells, is 222,485 bytes on the wire and **5.9 MiB
expanded** — 9% of the ceiling — and yields **96,280,012 characters**, charging the pod **321 MiB**.
Nothing about it is crafted: every `file_size` in the central directory is true, and Excel opens it.

**`python-docx` builds an lxml DOM out of the markup, not out of the text.** A `.docx` of 235,000
formatted paragraphs each holding one character is 207,973 bytes on the wire, **50.9 MiB expanded**
— 79% of the ceiling — holds **470,000 characters**, and charges the pod **840 MiB**. That is
16.5 MiB per expanded MiB, and a character-count bound would not see it either.

### The invariant was already broken, and the gate was green

The shipped front door modelled faithfully — a cgroup limited to `resources.service.limits.memory`
(1Gi) carrying ballast for the 523 MiB idle pair that ADR measured:

| Upload | wire | expanded | concurrency | outcome |
| --- | --- | --- | --- | --- |
| shared-string workbook | 222,485 B | 5.9 MiB | 1 | peak 857 MiB, served |
| shared-string workbook | 222,485 B | 5.9 MiB | **2** | **exit 137 — the parent SIGKILLed** |
| dense workbook, one U+1F9EA | 1,089,493 B | 63.4 MiB | 1 | child OOM-killed (`oom_kill 1`), `ParseWorkerLost` |
| dense workbook, one U+1F9EA | 1,089,493 B | 63.4 MiB | **2** | **exit 137 — the parent SIGKILLed** |

`test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares` read 920 against 1024 and
passed throughout. Two legal uploads, inside every declared ceiling, took the pod — and with it
every other connected turn — while the test that exists to prevent exactly that was green.

## The decision

**1. The bound moves to what the parse may *allocate*, enforced by the kernel on the process that
does the allocating.** `ingest/documents/isolate.py::_bound_allocations` sets `RLIMIT_DATA` in the
parse child, before it reads a byte, to its own current anonymous footprint plus
`document_parse_memory_bytes`. `RLIMIT_DATA` rather than `RLIMIT_AS` because since Linux 4.7 it
covers the heap and private anonymous mappings — what a parse spends — and leaves out the
file-backed mappings the child inherited. The baseline is read from `/proc/self/status` rather than
assumed, because a `forkserver` child starts with the whole preload list already resident.

The case for a kernel bound over a declarative one is the measurement above: three independent
mechanisms, in three different libraries, each of which makes a declared ceiling wrong in a
different direction, and none of which a coefficient can carry. Width is a property of CPython's
string representation, shared strings are a property of OOXML, and the DOM is a property of
`python-docx` — a model of all three is a model that the next parser, or the next release of one of
these, invalidates in silence. A ceiling on the process needs a model of none of it and covers the
format added next year.

**2. `document_parse_memory_bytes` is derived downwards from the pod, not chosen.**
`resources.service` limits the front door to 1024 MiB and it holds 523 idle with its forkserver
warm; two concurrent parses therefore have 501 MiB, and one parse charges the pod up to 1.4× its own
budget, so 501 / (2 × 1.4) = 178.9 MiB. **160 MiB** ships — rounded down, so the front-door
inequality keeps 53 MiB of the 1024 rather than the 1 MiB rounding alone would leave.

**3. The chart's coefficient becomes a coefficient of the quantity it is declared against.**
`PARSE_MIB_PER_PARSE_BUDGET_MIB = 1.4` multiplies `document_parse_memory_bytes`, and the
relationship is now enforced rather than modelled. Measured over fourteen documents spanning both
zip formats, all four width classes, the shared-string shape, the markup-heavy shape and three
different budgets: 0.45–1.33× at concurrency 1, 0.65–1.32× at concurrency 2. At the shipped 160 MiB
and the shipped cap of 2, the worst of those is two 50 MiB plain-text documents together — 406.7 MiB
of pod, 1.27× each, against the 448 MiB the assertion allows them. Above 1.0 because the
parent unpickles a second copy of what the child sent; below 2.0 because the child's own transient
intermediates are inside its ceiling rather than beside it.

**4. The worker's count becomes its activity cap.** The inequality asserted `1` for the background
worker, justified by `ingest/documents/sync.py` awaiting each `_read_and_parse` in turn. That
bounds one *activity* where the pod runs `worker_max_concurrent_activities` — 8 — of them. The
number happens to be right today (one `document-sync` schedule, `ScheduleOverlapPolicy.SKIP`, a
workflow whose activities are sequential), but it is a three-hop argument across two modules that a
second share schedule or one manual run breaks. The pod fits its cap outright — 370 + 8 × 1.4 × 160
= 2162 MiB against a 4Gi limit — so the cap is what is asserted and the argument is not needed.

**5. `document_max_expanded_bytes` stays, and stops being load-bearing.** It bounds *work* rather
than memory now: a refusal read from the central directory costs no decompression, where the same
document refused by the allocation ceiling costs a minute of CPU first, and it is a better-worded
answer. This also settles the residual `_refuse_a_bomb`'s own docstring states — that a hand-crafted
archive can understate `file_size` and the check believes it. **It changes the answer, and in the
direction of retiring the question**: what a lying archive could move was a declared bound, and the
real bound is now one nothing written in a file can reach. The *honest* archives above are as
unbounded as a lying one anyway, which is the stronger form of the same point.

**6. `_parse_csv` renders row by row instead of materialising the reader.**
`rows = list(csv.reader(...))` holds every *cell* as its own object until the last row is read, and
a `str` costs ~50 bytes of header before its characters: six cells a row over 900,000 rows is 5.4 M
objects alive at once. Measured, a 50 MiB delimited export needed **more than 512 MiB** that way and
~150 MiB after. The output is byte-identical. Found by the budget refusing a file it should hold,
which is the bound doing its job before a chemist met it.

## What it costs

**A document whose extraction does not fit 160 MiB is refused**, where before it was parsed —
sometimes successfully, sometimes by killing the pod. Measured on this tree, what still parses and
what does not:

| Document | outcome |
| --- | --- |
| 50 MiB plain-text file (the shipped share binding's whole `max_file_bytes`) | parses |
| 10 MiB delimited export, 12.3 M characters | parses |
| 17.2 M-character workbook, ASCII | parses |
| 8.7 M-character workbook with one astral code point | parses |
| 24.5 M-character delimited export | refused |
| 52 M-character workbook | refused |
| 17.2 M-character workbook with one astral code point | refused |

For scale, `document_read_max_chars` — what one whole-document read may put in front of a model — is
200,000 characters, so the smallest thing refused here is 120× a document this system will ever show
anyone in one piece. On the upload path the refusal reaches the chemist by name and says what to do.
On the share path it lands in `skipped_unreadable`, which the sync already counts and reports, so a
corpus losing documents to this is visible rather than silent.

Two further costs, both stated because they are behaviour changes:

- **A refusal is not always well-worded.** `parse_document` names `MemoryError`, and
  `_parse_into` names the same ceiling when the *pickling* of an answer is what exhausts it — one
  function, `parse.too_large_to_read`, because two arms wording one event separately is how two
  wordings drift. But a C parser that reports its own allocation failure never raises
  `MemoryError`: measured, `lxml` answers "Unable to allocate output buffer", so a markup-heavy
  `.docx` gets the generic "could not be read". Both are refusals; only one says why.
- **The `_SLOW_CSV` fixture had to change shape.** A million four-cell rows no longer fit one parse,
  so the test that proves a parse past its deadline is killed now uses 100,000 wide rows — the same
  20 MB, a tenth of the objects. It parses in 0.56 s against a 0.2 s deadline where the old comment
  claimed a factor of ten. Ten is no longer available, and that is the budget working.

## What this does not do

It does not give the two components different budgets, and they have different room: the worker's
limit is 4 GiB and its share binding admits 50 MiB files, of which a delimited one needs more than
512 MiB to render. One setting, sized from the tighter pod, therefore refuses share documents the
worker could have afforded. That is a `docs/planning/BACKLOG.md` row, not a silent gap — the refusal
is counted and named.

It does not bound `agent/attachments.parse_attachment`, the in-process form, because the ceiling is
set by the child and that function has no child. Its two callers are `cli/backfill_corpus.py` — an
operator command where a runaway parse costs the operator their own wait — and the format tests,
which is the argument that docstring already makes for it being in-process at all. Every serving
path goes through `parse_attachment_isolated` or `sync._read_and_parse`, and both are bounded.

It does not re-derive `FRONT_DOOR_RESIDENT_MIB`, `WORKER_RESIDENT_MIB` or
`FORKSERVER_POD_COST_MIB`, which remain `Pss` figures. It does correct what `Pss` **is**, because
the prose resting on those constants was wrong about it in a way that matters:

> `Pss` rather than `VmRSS` throughout this budget because a cgroup is charged for unique physical
> pages once

A memory cgroup charges a page **in full** to whichever cgroup first touched it, once. `Pss`
divides a shared page by the number of processes mapping it *system-wide*, which is a different
quantity, and one that moves with what else is running on the node. So `Pss` **understates** a pod's
charge whenever a page it brought in is also mapped outside it.

Driven rather than argued, because that is the same mistake one level down. One unchanged Python
process with pypdf, python-docx, openpyxl and python-pptx imported:

| | `Pss` | `Rss` |
| --- | --- | --- |
| alone | 53,118 kB | 72,916 kB |
| six unrelated siblings mapping the same shared objects | **47,653 kB** | 72,928 kB |
| alone again, siblings killed | 53,130 kB | 72,928 kB |

10.3% of that process's `Pss` belonged to what else happened to be running, and came back when it
stopped; `Rss` moved 12 kB, 0.016%, across the same three samples. That is also the mechanism behind
the flake `D-2026-09-18-…` already recorded and could not explain — its first `Pss` ratchet read
above 95 MiB in a fresh virtualenv and 90.0–91.0 MiB in five later runs of the identical assertion.

Every use of those constants here is a floor argument ("the pod already holds at least
this"), which survives the correction; the quantity a later derivation should use is the cgroup's
own `memory.max_usage_in_bytes`, which is what every parse figure above is. `D-2026-09-18-…`'s
§"What was measured" is superseded on that point and on `PARSE_MIB_PER_EXPANDED_MIB`; the rest of it
stands.

## What keeps it true

- `tests/test_parse_isolation.py::test_a_legal_upload_cannot_spend_more_than_the_parse_budget_declares`
  — the shared-string workbook, legal on the wire and legal by the expansion ceiling, is refused by
  name. Driven red with the `RLIMIT_DATA` call removed and nothing else changed:
  `Failed: DID NOT RAISE DocumentParseError`.
- `tests/test_parse_isolation.py::test_one_wide_code_point_does_not_multiply_what_a_parse_may_spend`
  — the same workbook twice, one astral code point apart, refused with it and parsed without it, so
  the bound cannot have been bought by refusing everything. Driven red the same way:
  `Failed: DID NOT RAISE DocumentParseError`.
- `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`
  — both components, against the enforced ceiling and the concurrency each pod actually permits.
  Driven red by doubling `document_parse_memory_bytes`: `assert 1239.8 <= 1024`; and by raising
  `attachment_max_concurrent_parses` to 4: the same.
- `tests/test_deploy_chart.py::test_a_warm_parse_forkserver_still_costs_what_this_budget_was_derived_against`
  — unchanged, and still the live guard on the one constant that is a property of this tree.
- `tests/test_config.py::test_env_example_documents_every_field` — that the new setting is
  documented where a deployment looks for it. Driven red before `.env.example` was written:
  `assert not {'document_parse_memory_bytes'}`.
- `tests/test_document_formats.py` — that `_parse_csv`'s row-by-row rendering produces what the
  materialising form produced.
