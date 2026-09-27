# D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared — sizing the front door and the worker against the parse forkserver

**Status:** accepted · **Date:** 2026-09-18

## Context

`D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica` moved every document parse out of a
worker thread and into a killable child process, forked from a `multiprocessing` **forkserver**
that `ingest/documents/isolate.py` starts on first use. That was the right fix for a liveness
failure — a parse that does not terminate can now be killed and its slot returned — and it put a
second Python process inside two pods that had been sized without one.

`docs/planning/BACKLOG.md` carried the consequence as an open row, measured on 2026-09-13: RSS
111,140 kB in the front door and 111,188 kB in the forkserver, "~109 MB the front door's pod was not
sized for", against `resources.service`'s `requests: 512Mi / limits: 1Gi`. The row asked for three
things — a decision between raising the request, keeping the forkserver cold, or both; a measurement
of the background worker, which `ingest/documents/sync.py` now starts one in too; and the laziness
claim checked rather than assumed.

## What was measured

Everything below was driven on this tree, at this commit, with the shipped settings.

**The unit is `Pss`, not `VmRSS`, and that is the first correction.** A cgroup is charged once for a
unique physical page. The parent, the forkserver and every parse child map the same libpython and
the same shared objects, so summing `VmRSS` across them counts the interpreter three times. The
row's 109 MiB is the forkserver's `VmRSS` (111,592 kB here, stable to 0.2% across five different
parents and two virtualenvs). What the *pod* is actually charged for warming it is the delta in the sum of `Pss`:

| Parent | Pod delta on warming the forkserver | Forkserver `Pss` | Forkserver `VmRSS` |
| --- | --- | --- | --- |
| Front door (`create_app()`) | 83.5 MiB | 90.4 MiB | 108.9 MiB |
| Background worker | 79.1 MiB | 90.7 MiB | 108.9 MiB |
| pytest | 76.1 MiB | 90.4 MiB | 109.0 MiB |

So the process really is a second, full, non-copy-on-write copy of pypdf, python-docx, openpyxl and
python-pptx — `forkserver` starts its server by fork **and exec**, exactly as the row says — but
18.6 MiB of what `VmRSS` attributes to it is a shared object the parent already has mapped, and the
pod's measured delta is 23–30% below it. **The row overstated the pod's charge by about 30%, in the
direction that would have bought memory nobody needed.** The forkserver's own `Pss` is a rigorous
upper bound on that delta (a parent's `Pss` can only fall when a second process starts sharing its
file-backed pages), which is what makes it the constant worth holding.

**The resident sets the forkserver is added to.** The front door was measured as the real serving
object — `uvicorn chemclaw.api.app:create_app --factory` against the dev Postgres, lifespan run,
`/healthz` served — at 445,204 kB `VmRSS` / 442,270 kB `Pss` = **431.9 MiB**. The background worker,
with every activity module imported, at 284,880 kB `Pss` = **278.2 MiB**. The MCP face, which shares
`resources.service`, at 414,099 kB = 404.4 MiB.

**One parse in flight.** A real document of each type the parser handles — a 12-page PDF, a
200-paragraph DOCX, a 400-row XLSX, a 30-slide PPTX and a 5,000-row CSV — was driven through the
shipped path in each of the three parents. What that establishes is that the forkserver's resident
set is the same after each of them (111,476–111,508 kB across all fifteen parses) and that the
child's peak on documents of that size is 93.7 MiB: the parsers are copy-on-write from the
forkserver, so what a child adds is the extracted text.

**What the coefficient is measured from is therefore the size, not the format.** The shape that
reaches the caps is a workbook, because OOXML is a zip and the expansion ceiling is what binds.
Measured as the pod's peak `Pss` over its warm-idle baseline, sampled at 3 ms across the whole
parse:

| Shape | Expanded size | Concurrency | Pod peak over idle |
| --- | --- | --- | --- |
| Largest legal upload (1.69 MB on the wire, 12.6 M characters) | 29.7 MB | 2 (the shipped `attachment_max_concurrent_parses`) | 169,532 kB — 82.8 MiB each |
| The same, bare parent | 29.7 MB | 2 | 178,054 kB — 86.9 MiB each |
| Share-crawl shape (3.38 MB on disk, 25.4 M characters) | 59.9 MB | 1 | 166,966 kB — 163.1 MiB |

It is linear in the *expanded* size — 3.07 and 2.86 MiB of pod memory per expanded MiB, for 2.01×
the expansion — which is what makes it a coefficient rather than a table of document sizes.

## The finding

**From the first upload onwards, the front door sat over its memory request while idle.** 431.9 MiB
resident plus 90.4 MiB of warm forkserver is **523 MiB against a 512Mi request** — and that is with
no turn running, no connector session open and no parse in flight; the forkserver does not go away
when the upload that started it finishes. A pod whose steady
state exceeds its request is scheduled onto a node that does not have the memory it uses, and is
first in line when that node comes under pressure. Nothing anywhere said so, because the chart was
sized when a parse ran on a worker thread inside that same process.

**The limit was never the thing that was wrong.** Two concurrent parses at
`document_max_expanded_bytes` cost 3.1 × 64 = 198.4 MiB each, so the worst legal pair is
431.9 + 90.4 + 396.8 = **920 MiB of the 1024 MiB the limit allows**. The argument already written
into `document_max_expanded_bytes` — that 64 MB "leaves two concurrent parses well inside 1 GiB" —
holds, at 90% of the limit rather than comfortably; this is the first time it has been checked with
the front door's own resident set in the sum.

**The background worker needs nothing.** 278.2 + 90.7 = 369 MiB idle against a 1Gi request, and one
parse at the expansion ceiling — one, because `ingest/documents/sync.py` awaits each
`_read_and_parse` in turn, so a crawl never has two children alive however large the batch — brings
it to 567 MiB against a 4Gi limit. Its sizing is fine and this records the basis rather than
changing the number.

**Laziness holds, and it is not an answer for the front door.** Driven on all five shipped
entrypoints, no forkserver exists after import: `service`, `background-worker`, `mcp-face`, the CLI
and a connector process all report `_forkserver_pid is None`. (The *parsers* are a different matter —
`pypdf` is in `sys.modules` in all but the connector process, because `agent/attachments.py` imports
`isolate`, which imports `parse`. That is the parent's copy, and it is why the forkserver's is a
second one.) But the front door **is** the upload path: the first upload warms it and nothing ever
cools it, so "keep it cold" describes what every component that does not parse already gets for
free, and describes nothing about the two that do.

## The decision

1. **`resources.service.requests.memory` goes 512Mi → 640Mi.** The limit stays 1Gi. 640 covers the
   523 MiB idle pair with room for one parse at the size a real legal upload reaches (88 MiB), and
   leaves burst above the request to the limit, which is what a request and a limit are for.
2. **Nothing else changes.** The worker's sizing is confirmed rather than moved, and the forkserver
   is not warmed at startup: it stays lazy, which is what keeps the MCP face, the CLI, the connector
   processes and the migration jobs out of this budget entirely.
3. **The size is derived and asserted, not typed.** `tests/test_deploy_chart.py` carries each
   measured constant beside its measurement, and two inequalities over them and over the settings
   that bound the work — an idle one and a peak one, applied to both components — so raising
   `attachment_max_concurrent_parses` or
   `document_max_expanded_bytes`, or lowering either declaration, fails in the gate rather than in an
   eviction or an OOMKill.
4. **The constant that is a property of this tree is re-measured live.** `FORKSERVER_POD_COST_MIB`
   is whatever `isolate._PRELOAD` drags in, and a number transcribed from a five-day-old measurement
   is exactly what this row turned out to be. The closure is taken off a running forkserver instead,
   in `VmRSS` — the budget's unit is `Pss` and a ratchet's cannot be, for the reason the last section
   gives.

## What it costs

`service.replicas` is 2 and `service.autoscaling.maxReplicas` is 6, so the reservation goes from
1–3 GiB to 1.25–3.75 GiB across the front-door fleet: **+128Mi per replica**, +256Mi at the floor and
+768Mi at full scale. That is memory the pods were already using; what changes is that the node is
told about it.

`mcpFace` shares `resources.service` and never parses — no upload route, forkserver never started —
so it pays 128Mi for memory it does not use. It ships `enabled: false` with one replica, and its own
404.4 MiB resident set against a 512Mi request was tight on its own account, so the key is not split.
A second `resources.mcpFace` key would be a declaration whose only correct value is within rounding
of this one.

## What this does not do

It does not measure what a *turn* costs the front door's memory. Every figure here is a resident set
with no agent graph compiled, no connector session open and no turn in flight, against an admission
cap of 12 concurrent turns. So the request is now derived against the pod's floor plus its parse
path, and the turn path is bounded by nothing — a `docs/planning/BACKLOG.md` row of its own.

It also does not touch the expansion ceiling. 920 MiB of a 1024 MiB limit is inside it and is not
comfortable, and the honest lever if that becomes a problem is `document_max_expanded_bytes` rather
than a larger pod; the new assertion is what will say so.

## What keeps it true

- `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`
  — the request and the limit of both components, against the measured constants and the settings
  that bound the work. Driven red on the shipped 512Mi: `assert 523 <= 512`.
- `tests/test_deploy_chart.py::test_a_warm_parse_forkserver_still_costs_what_this_budget_was_derived_against`
  — the closure, measured off a running forkserver. Driven red by adding a single module to
  `isolate._PRELOAD`: 411.8 MiB against a ceiling of 112.

  **It guards `VmRSS`, and the first draft guarded `Pss` and flaked.** `Pss` is the right unit for
  the budget and the wrong unit for a ratchet: a page's share depends on how many other processes
  happen to map it. Observed — the first run inside a freshly created virtualenv read above 95 MiB
  and failed, and five later runs of the identical assertion read 90.0–91.0 MiB and passed. `VmRSS`
  belongs to the process alone: 108.9–109.1 MiB across five parents and two virtualenvs, a 0.2%
  spread, moving with the same closure. A gate that reds
  for a scheduling artefact teaches everybody to re-run it, which is
  `D-2026-09-13-a-stable-failure-set-is-not-two-green-runs`'s argument applied to a unit rather than
  to a worker count.
- `tests/test_parse_isolation.py::test_the_parse_does_not_run_in_this_process` — that a parse is
  still a separate process at all, which is the premise the whole budget rests on. Nothing asserts
  that the context is specifically a `forkserver`; the live measurement above is what would notice,
  since a `spawn` context would cost a fresh interpreter per parse and a `fork` one would cost
  almost nothing.
