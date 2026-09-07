# D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend — the eight in-memory stores stay in `src/`, and three of the reasons for moving them were measured false

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold (the predicate: a thing no *configuration* can
reach is dead; a thing a *deployment* selects is not), D-2026-09-07-a-driver-with-no-caller-is-not-a-capability
(which decided every other item on its list and deliberately left this one open),
D-2026-08-08-a-test-that-survives-the-mutation-it-names,
D-2026-08-26-the-driver-s-signature-is-the-schema (`module:callable`, which is why one of the nine
was misfiled) · **Supersedes** nothing.

## Context

`D-2026-09-07-a-driver-with-no-caller-is-not-a-capability` ran six AST passes over `src/` and
deleted or relocated five unreachable paths. It stopped short of one finding and queued it: nine
`InMemory*` classes, 772 lines by its count, whose `default_*()` returns the Postgres implementation
and which therefore fail its own predicate. The queued row proposed relocation to `tests/` rather
than deletion, because these are **differential oracles** — `test_store.py` compares the Postgres
answer against the in-memory one, `D-2026-08-08-a-test-that-survives-the-mutation-it-names` uses
`InMemoryStore` as its mutation target, and `retrieval/vectors/memory.py`'s own docstring calls
itself "the definition of what the adapters are expected to agree with".

The row gave three benefits for relocating: out of the rootless image, out of `mypy --strict`'s
`src/` pass, out of the coverage denominator. Working the row means measuring them, and two of the
three do not survive it. Neither does the row's premise.

## What was measured

**The premise is false for one of the nine.** `vector_store_provider` is not a closed `Literal`; it
takes a `module:callable` resolved through `core.connect.resolve_driver`
(`D-2026-08-26-the-driver-s-signature-is-the-schema`), and `InMemoryVectorStore` takes no
constructor arguments and implements `VectorStore` — the whole of what that seam asks. Driven:

```
CHEMCLAW_VECTOR_STORE_PROVIDER=chemclaw.retrieval.vectors.memory:InMemoryVectorStore
CHEMCLAW_VECTOR_STORE_URL=memory://none
  provider: chemclaw.retrieval.vectors.memory:InMemoryVectorStore
  resolved: chemclaw.retrieval.vectors.memory.InMemoryVectorStore
  note index: ExternalVectorNoteIndex
```

So it is a deployment backend by this repository's own predicate, and moving it to `tests/` would
have removed a module a documented configuration can name. The row was derived by reading
`retrieval/vectors/registry.SHIPPED` — which holds `qdrant` and `databricks` — rather than by trying
the seam, which is the identical mistake the row itself warns about one paragraph later for
`InMemoryCampaignStore`. That warning was also short: there are **four** in-memory backends
`session_store="memory"` selects (`InMemoryCampaignStore`, `InMemoryHistoryProvider`,
`InMemoryPlanApprovalStore`, `InMemoryDesignStore`), so the carve-out named a quarter of the set.
Eight classes are genuinely unreachable, not nine.

**Two further sentences of the row do not hold.** "The return annotation is the concrete class, so
no configuration branch is even expressible" is false for two of them: `default_document_index() ->
DocumentIndex` and `default_note_index() -> NoteIndex` both annotate the Protocol and both already
*carry* a configuration branch (`pgvector` versus an external store). And the class bodies are 817
lines, not 772.

**Benefit 1, the image, is real and small.** `deploy/Containerfile` copies `src`, `data`, `skills`,
`knowledge`, `infra` and `schema`, and no tests — so these bytes do ship: **40,025 bytes** across
the eight class bodies, in an image whose closure carries BoFire, BoTorch and torch.

**Benefit 2, `mypy --strict`, is nil.** `make type` is `uv run mypy src examples tests`. Relocating
takes them out of the `src` argument and puts them under the `tests` one; nothing stops being
type-checked, and an oracle that stopped being type-checked would be a worse oracle.

**Benefit 3, the coverage denominator, points the other way.** Measured over a thirteen-file subset
of the suite — a lower bound, since these classes are reached from ~150 test files — the eight
oracles plus the vector reference are **94.3 % covered** (280 of 297 statements), against a tree
whose last measured total was 85.30 % and whose floor is 84. Removing near-fully-covered statements
from the denominator *lowers* the reported percentage. It is a small cost, not a benefit.

**And the relocation is not small.** ~600 references across ~150 test files, against Postgres
modules whose correctness comments cite the reference by name as the answer they are written to
reproduce (`ingest/documents/index.py:890`, `retrieval/vector_index.py:324/372/406/543`,
`science/calc/postgres_store.py:55`). Those comments are read *beside* the class they name.

## Decision

**1. The eight oracles stay in `src/`, beside the Protocol and the Postgres implementation each
defines the contract for.** The predicate `D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold`
states is about a *hold* — a control or a capability that nothing can open, where the harm is a
claim that something exists. A reference implementation claims nothing about what the deployment
can do; it is the executable form of the Protocol's docstrings, and the SQL one is written to agree
with it. 40,025 shipped bytes is the price, stated rather than hidden, and it buys the property
that the two implementations are read together.

**2. `InMemoryVectorStore` is a deployment backend and is filed as one.** Not because a site runs
it, but because the seam accepts it and this repository's predicate is about what a configuration
can reach, not about what somebody has reached for.

**3. Eight docstrings stop offering a deployment mode that does not exist.** Seven said "for tests
and single-run use" and `InMemoryStructureStore` said "for tests and for a deployment with no
database" — a mode `default_store()`, `default_note_index()` and every other selector refuse to
return. A reader checking whether this system can run without Postgres would have found eight
classes agreeing that it can. That is the same shape as a comment asserting a control exists, which
is what wave 8 found one file over in `infra/sql/grants/app_privileges.sql`.

**4. What replaces the row is `tests/test_reference_stores.py`, and it is derived rather than
listed.** The row's closing demand was that "a third sweep re-finding nine undecided classes" must
not happen. An ADR cannot stop that, because a sweep need not read one. Three assertions can:

- `_in_memory_classes()` walks `src/` by AST and finds **every** `InMemory*` class there is, and
  the partition into `SELECTABLE` and `ORACLES` must be total. A tenth added next year lands in one
  table or the other, and its author has to say which. A hand-written list would be a list of what
  the tree looked like the week it was written — the failure `framing._INVISIBLE` was rewritten to
  avoid one layer down.
- No shipped module may *name* an oracle outside the module defining it. That is the property the
  whole decision rests on, and the moment it breaks, a deployment backend has arrived with no
  configuration to select it.
- The vector-store seam is **driven**, so the next sweep counting `SHIPPED` and concluding "dead"
  meets a red test instead of a paragraph.
- The absence of the deployment claim is asserted per oracle, which this repository is otherwise
  sparing about — for the reason `tests/test_tool_authz.py` asserts the absence of a withdrawn
  promise. The claim is the defect, so the absence of the claim is the fix, and nothing else holds
  it.

**What is not decided here:** whether a Postgres-free single-user mode is worth building. It is not
being built, no `DEFERRED.md` row is opened for it, and nothing in the tree implies it exists any
more. If somebody wants one, it is a new decision with a selector, a `default_*()` branch and a
`SELECTABLE` entry — which is what the four `session_store="memory"` backends already look like.

## Consequences

- The row is deleted from `docs/planning/BACKLOG.md` §4.
- `tests/test_reference_stores.py` fails on: a new undeclared `InMemory*` class; a shipped module
  reaching an oracle; an oracle's docstring re-offering a deployment mode; the vector seam ceasing
  to resolve the reference.
- Eight docstrings gained a paragraph naming this ADR, which is what a reader finds first.
- The image keeps 40,025 bytes of Python no process executes. That is the accepted cost, and the
  first assertion above is what keeps it from growing without somebody deciding to.
