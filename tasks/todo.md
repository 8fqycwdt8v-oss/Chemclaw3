# Giving a tool result somewhere to live

Prompted by: a companion study of the frontend (`Chemclaw3_ui/docs/chemistry-aware-frontend.md`,
§7 item 1) finding that the only thing about a tool call that reaches the browser is
`ToolResultEvent.preview` — 200 raw characters, explicitly not JSON. So a `ScreenResult`'s cited
hazard flags, a `ChargeTable`'s rows and a solvent ranking all arrive as prose the model wrote about
them, and no frontend change can fix it. The full text already existed untruncated at emit time and
was discarded.

## Plan

- [x] `infra/sql/042_tool_result_store.sql` — content-addressed `tool_result_blobs` +
      `tool_result_links`, modelled on `019_artifact_store.sql` (same two-table shape, same
      `SET STORAGE EXTERNAL`, same load-bearing `ON DELETE CASCADE`).
- [x] `api/tool_results.py` — the store, the `ResultSink` seam, and the failure policy (`""`, never
      an exception: storing must not fail a turn), swallowed through `degraded()` so a run of lost
      writes is one counter and not only a log flood.
- [x] `ToolResultEvent.result_ref` + `stream_max_result_bytes` — over the cap a result is refused
      whole, never trimmed, and the refusal is logged (`_capped_numbers`' rule, one field over).
- [x] `runner_trace.feed` becomes a coroutine taking an injected sink, so the blob lands before the
      event naming it is yielded; the runner builds the sink where the session and correlation id
      exist. `tests.fakes.fed` keeps the forty synchronous call sites synchronous.
- [x] `routes/results.py` — `GET /sessions/{id}/tool-results/{ref}`, gated by `resolve_session`.
- [x] `routes/notes.py` — `GET /notes/{id}`, the same `NoteView` `expand_note` returns.
- [x] Retention — `tool_result_blobs` joins `_PRUNABLE` by `created_at`; links cascade, so no orphan
      pass. Window defaults to 0 like the others (see Review).
- [x] Canonical SMILES echo — `ScreenResult.screened` / `AlertResult.screened`.
- [x] Declarations that are test-enforced: `routes/README.md` table, `infra/sql/README.md` rows and
      the foreign-key count, `grants/app_privileges.sql`, `.env.example`, the session-scope
      inventory in `tests/test_service.py`, the `_PRUNABLE` set in `tests/test_retention.py`.
- [x] `tests/test_tool_results.py` — store round trip, dedup, cross-session miss, a write that fails
      costing the turn nothing, the producer's ref, the oversize refusal + its log line, the cap
      being bytes rather than characters, and both routes.
- [x] ADR `D-2026-08-09-a-preview-is-not-a-result` + ledger row.

## Review

**Why a ref and not the payload.** `data: dict` on the event for a whitelist of small results is
simpler and re-opens exactly the question the 200-character truncation closed: the stream fans out
to every consumer, and a size whitelist is a promise a connector author can break without noticing.
A ref costs one round trip and only for what somebody chose to look at.

**Why the route hangs off a session.** A ref is the SHA-256 of a result's own text — unguessable,
not secret. Under `/sessions/{id}` it resolves through `resolve_session`, the ownership gate that
already exists, and the store's read joins the link row for the same session on top of that. The
bare `/tool-results/{ref}` alternative needed an auth story invented for it, and the story it would
have got is "the ref is a bearer token".

**Why `feed` became async.** The alternative kept it synchronous by buffering `(ref, tool, text)`
for the runner to flush afterwards. That preserves forty test call sites and introduces a two-call
protocol whose second half can be forgotten — and forgetting it ships refs pointing at nothing. The
coroutine cannot be got wrong. Its cost is one shared test helper and one honest sentence in the
module docstring, since "pure function of the provider objects" is no longer the whole truth.

**The retention default is uniform, and that was a correction.** It shipped as 30 days first, on the
argument that this table holds no record and therefore no policy to defer. Measured against the
configuration that actually ships, the argument buys nothing: `retention_enabled` is off by default,
so 30 and 0 delete equally much, and they differ only for a deployment that turned retention on
without stating this window — which is the case `test_retention_is_off_until_a_policy_is_stated`
exists to refuse. One rule for every window beats a default that changes nothing, and the cost
(the highest-volume table in the set is unbounded until an operator says otherwise) is written in
`infra/sql/README.md`'s Disposal column rather than implied away. Recorded in `tasks/lessons.md`.

**The canonical echo is deliberately partial.** It landed on the two safety results because they are
uncached, already parse the molecule, and carried *no* structure at all — a clean screen serialized
to `{"flags": [], "verdict": …}`, naming nothing it had looked at, which for a result that must
never read as a clearance is the wrong thing to be vague about. It did **not** land on the
calculators: `PkaResult.smiles` is already canonical, and making `SolubilityResult.smiles` canonical
would change the shape of rows already in `calculation_results` — a `CALCULATION_EPOCH` bump, i.e.
cache-wide invalidation, for a cosmetic echo. Stated in the ADR rather than left to be rediscovered.

**Not done, deliberately.** Nothing writes a `result_ref` into the persisted transcript
(`TranscriptMessage.tool_calls` still carries a 400-char result), so a reloaded conversation cannot
resolve the results of past turns — only the live stream can. That is a second, separable change
against `api/schemas.py` and it is not in this one.

## Verification

`make ci`, on the branch, in the offline sandbox:

| step | result |
| --- | --- |
| `lint` (ruff check + format) | pass |
| `type` (`mypy --strict` over `src`, `examples`, `tests`) | pass, 613 files |
| `cov` | 3903 passed, 175 skipped, **1 failed** — see below |
| kg · eln · skill · connector · datasource · template · prose · safety validate | pass |
| `eval-strict` | pass — 4 gated metrics fail *by design*, 0 regressions |
| `helm-validate` | not run: `helm` is not installed here (`docs/guides/runbook.md`) |
| `deps-audit` | no known vulnerabilities |

**The one failure is pre-existing and unrelated.**
`test_properties_core.py::test_a_note_survives_the_write_read_round_trip` is a Hypothesis property
test that drew a lone surrogate (`'\ud800'`) as a note body; `render_note` → `Path.write_text` then
raises `UnicodeEncodeError`. Checked out `main` and ran the same test: it fails identically, on the
example Hypothesis had already saved. Two earlier full runs (one on this branch, one on `main`)
passed it, because the draw is random — which is what it looks like when a property test finally
finds its case. Left alone: it is neither this change's defect nor this change's area.

**The coverage floor fails at 82.82%, and `main` measures 82.82% too.** Ran `pytest --cov` on `main`
in the same sandbox before concluding anything: 19,698 statements / 3,020 missing / 82.82%. The
branch: 19,787 / 3,033 / 82.82%. So this change is coverage-neutral to two decimal places, and the
1.2-point gap to the 84.0% floor is the sandbox rather than the diff — 139 Postgres tests and 23
Temporal tests skip here for want of a server, including the three that cover this store's SQL.

**The store was measured against a real database anyway**, since a skip is not evidence. Ran a
PostgreSQL 16 cluster locally, applied `042_tool_result_store.sql`, and drove `store_tool_result` /
`load_tool_result` directly: byte-exact round trip; the same text stored three times from two
sessions producing one blob row and two link rows with the link's `correlation_id` refreshed;
a ref from another session reading as a miss; a multi-byte payload round-tripping with
`byte_size` in bytes; the retention statement `retention.py` builds deleting the blob and the link
cascading away with it; and a write against an unreachable DSN answering `""` with one `degraded`
line rather than raising. The suite's own Postgres tests still could not be run — the shipped
`pgvector` in this image is 0.6.0 and migration 012 needs `bit_jaccard_ops` (pgvector ≥ 0.7) — so
they remain unproven *here* and will run in CI.
