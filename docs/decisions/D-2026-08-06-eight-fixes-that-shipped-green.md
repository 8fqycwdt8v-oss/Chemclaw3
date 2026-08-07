# D-2026-08-06-eight-fixes-that-shipped-green — Eight fixes that shipped green

**Status:** accepted · **Date:** 2026-08-06

## Context

An intensive review of this branch — eleven hardening packages, 131 files — found **nine** defects.
Eight were introduced by the branch itself and one was extended by it. Every one of them passed the
full suite, `mypy --strict` and `ruff` at the moment it was committed.

They are recorded together because what they share is not a subsystem. It is a failure mode:

| Finding | What made it invisible |
|---|---|
| `SecretStr` emptied the log-redaction inventory | the test asserted the field **names**, never a value |
| `_closing` released nothing | the test built a shape production never produces |
| the report citation forged wikilinks | the fix was applied to the sibling branch, not this one |
| the framing-secret warning was silent by default | the guard's condition was narrower than its subject |
| retention swept the wrong database | the sweep predated the split it was extended under |
| `state <> 'pending'` | the test asserted the literal rather than the states that exist |
| the workload-identity label | the guard counted **files**, not pod templates |
| `match` vs `fullmatch` | the two spellings agreed on every input anyone tried |

## Decision

### The credential leak, which is the one that matters

Six credentials became `SecretStr` (WP-3) so a forgotten `.get_secret_value()` renders as
`**********`. `SecretStr` is **not** a `str` subclass, so `_secret_values`' `isinstance(value, str)`
guard — written to skip a non-credential field — began skipping every converted credential.
`_secret_values()` returned an empty tuple and `redact_secrets` passed a live key through verbatim.

Measured, not inferred: with `llm_api_key = SecretStr("sk-live-…")`,
`redact_secrets("key sk-live-…")` returned the key unchanged; as a plain `str` it returned `***`.

**The mitigation is what hid it.** A masked `repr` means the key rarely reaches a log by accident,
so the paths that mattered — a provider echoing it in an auth error, git stderr persisted into
`note_proposals.reason` — were the ones nobody was watching. `_plain()` reads either shape now, and
the regression test feeds a **value** through per field rather than checking the inventory's keys.

### `_closing` was a no-op on every real turn

`agent.run(stream=True)` returns `ResponseStream.from_awaitable(...)` over a `.map(...)`-wrapped
inner stream (`agent_framework/_agents.py:1058`, `:1196`). So the outer object's `_iterator` is
another `ResponseStream` — which has no `aclose` — and the outer's own `cleanup_hooks` list is
empty. Reading one level found nothing to close and nothing to clean.

WP-9's test built a flat `ResponseStream(async_generator)`, whose `_iterator` *is* a raw generator.
It passed, and proved nothing about production. The helper now walks the nest to the generator at
the bottom and runs every stream's hooks on the way, with a cycle guard because
`ResponseStream.__aiter__` returns `self`.

### The forged link the sibling fix did not cover

WP-6 escaped `[[` in ELN note bodies and made the report citation link only when the reader's own
parser round-trips the id. The **else** branch — "not a note id, so write it as plain text" — still
interpolated a warehouse-derived key verbatim into the report body, where `Note.outgoing_links`
reads a `[[supersedes:…]]` sitting inside it as a real edge. A generated report could propose
retiring another team's result.

`safe_identifier` reduces it instead of escaping it, because that branch has already decided the
value is not a note id: a provenance label only has to be recognisable.

**That import moved the function.** `retrieval` may not import `agent` (`tests/test_layering.py`),
and the rule is right rather than in the way — the charset `safe_identifier` reduces to is exactly
the one that survives neither an instruction nor a wikilink, so it belongs in `kg.note`, the module
that defines what reads as a link. `agent.framing` re-exports it; every existing caller is unchanged.

### Three guards whose condition was narrower than their subject

- The **framing-secret** warning fired only under `session_store == "postgres"`, which is not the
  default. A *connector* frames in its own process by construction
  (`connectors/calc/server/tools.py:fetch_artifact`), so the per-process nonce mismatch is
  guaranteed on any deployment running one. It is unconditional now: any two processes suffice, and
  a deployment has at least two.
- The **workload-identity** guard searched each file for the label. `deployment-connectors.yaml`
  holds two pod templates; the label was in the server block, and `qm` — the bundle that talks to
  HPC — declares no endpoint, so the worker template is its only pod. The guard counts pod specs
  now.
- **`state <> 'pending'`** named a state that does not exist (`open|merged|rejected|failed`), so it
  was unconditionally true. Named positively now, and `failed` is excluded with `open`: its own
  docstring says it is *not a decision*, and it is kept so the proposal can be replayed.

### Two that were simply wrong

- The **retention sweep** opened `postgres_dsn` while every table it prunes follows
  `session_store_dsn`. Pre-existing for two tables; this branch added four more to it — and taught
  `migrate.py` to create the schema in *both* databases, which is what makes the wrong one silently
  succeed with `deleted: {..: 0}` instead of failing on a missing table.
- **`is_note_slug`** used `_SLUG.match` where `Note`'s validator uses `fullmatch`. A trailing
  newline passes one and not the other. Narrow — `/` is outside the charset, so no traversal
  survives — but unifying the two spellings is the whole reason the function was extracted.

### One declined, with the number

`default_write_tool_gates()` is rebuilt per tool call and walks the enabled manifests twice.
Measured: **5.3 µs per call, 0.53 ms across a 100-tool-call turn**, against a turn dominated by
model latency. A cache would need invalidation wired to `discovered.cache_clear()` and to the
thirteen tests that repoint `connectors_enabled`/`entra_expensive_actions` — real machinery for half
a millisecond, and it would take away a property worth keeping: a gate that reflects config as it is
now, with no second place for the two to disagree. The measurement is in the docstring so it is not
re-litigated.

## Consequences

- Credentials are redacted from logs again, asserted by value and per field.
- A disconnected client releases the model's HTTP response deterministically, on the shape
  `agent.run` actually returns.
- A report cannot carry an edge a retriever's data invented.
- `safe_identifier` lives in `kg.note`; `agent.framing` re-exports it.
- Every pod template carries the workload-identity label, and the guard can see a per-template gap.
- `tests/test_review_findings_2026_08_06.py` pins all eight, each mutation-proven against the
  pre-fix code.

## What this says about the eleven packages

Every package in this branch was mutation-proven, and mutation proved the wrong thing four times
here: it kills a test that exercises the *right shape*, and says nothing about a test that exercises
the wrong one. Three of the eight were tests passing on a shape production never produces (the flat
stream), a value the code never holds (`SecretStr` vs the name list), or a literal that does not
exist (`pending`).

The discipline that would have caught them is not more mutation. It is **asserting against the
production shape**: feed a real credential through the real redactor, close the object `agent.run`
really returns, name states from the enum rather than from memory. Recorded in `tasks/lessons.md`.

## Alternatives rejected

- **Making `_secret_values` call `str()` on anything.** Would stringify a non-credential type in the
  inventory into the match set; an unexpected type there is a mistake to notice, not to coerce.
- **Escaping `[[` in the report citation** instead of reducing the label. A third copy of an escape
  that already exists in `ingest/eln/note.py`, and visible noise in a provenance line.
- **Leaving `safe_identifier` in `agent.framing` and adding a `kg`-local copy.** Two definitions of
  one charset, which is how they come to disagree.
- **Keeping the framing warning conditional on durable sessions.** It would stay silent on the
  shipped default, which is the configuration the finding is about.
- **Caching `default_write_tool_gates()`.** Measured at 0.53 ms per turn; see above.
