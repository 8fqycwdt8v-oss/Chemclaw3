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

---

# Two defects left by the tool-result surface (2026-08-09)

Both against `D-2026-08-09-a-preview-is-not-a-result`, which is now merged: the screens it made
*visible* were still narrowing their input, and the ref it added never reached the one route a
chemist uses on every reload. Two decisions, so two ADRs — the merged one is not edited.

## Defect 1 — a screen of `"CCO junk"` silently screened ethanol

ADR: `D-2026-08-09-a-valid-prefix-is-not-a-molecule`.

- [x] `core/chem.py::require_molecule` — the "RDKit read this string, all of it" gate factored out
      of `require_canonical_smiles`, which is where it already lived and where the screens were not
      looking. `require_standard_smiles` moves onto it too, so the three strict helpers cannot
      drift on what "parses" means (`tests/test_ids.py` pins that they agree).
- [x] `science/safety/screen.py::parse_molecule` — refuses what it cannot parse whole, translating
      `InvalidSmilesError` to `SafetyRulesError` so the package keeps one exception type and the
      refusal still reaches the model as a worded `ValueError` rather than an internal-error notice.
- [x] `parse_components` — shared by both screens, and the reason it exists is the message: a
      reaction refusal names *which* component ("component 2 of 3"), counted in the list as the
      caller wrote it rather than in the deduplicated mapping.
- [x] `science/safety/notes.py::_is_structure` — moved onto the same predicate. The PR-gate promises
      to ignore a code span that is not a structure; `` `CCO at 80 °C` `` passed a bare parse and
      would have failed the screen it was then handed, turning an ignored span into a gate failure.

**Measured before and after**, because the defect is invisible from the outside: `screen_structure`
of `"CCO CN=[N+]=[N-]"` — an organic azide sitting in the tail RDKit discards — returned
`flags=[]`, `screened=["CCO"]` and the verdict "No rule in the hazard table matched"; the
genotoxicity screen dropped a nitroarene from `"CCO O=[N+]([O-])c1ccccc1"` the same way. Both now
refuse. `tests/test_safety.py` pins the refusal *and* the absence of a clean result, since a test
that only expected the exception would pass against a version still returning `flags=[]`.

**The rest of `science/` was checked rather than assumed.** Every calculator reaches RDKit through
`require_canonical_smiles` at its cached-compute boundary, so `run_cached_solubility("CCO junk")`
already raised before `predict_solubility` saw it. The one live instance left is
`fingerprints/molfp/fingerprint.py::_parse` — measured, `ecfp_bitstring("CCO junk")` equals
`ecfp_bitstring("CCO")` — and it is a `BACKLOG.md` row rather than a silent omission: a wrong search
result, not a false clearance, and `_parse` also indexes ELN labels where refusing is a different
trade.

## Defect 2 — a reloaded conversation could not resolve past results

ADR: `D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one`.

- [x] `TranscriptToolCall.result_ref` — the same handle the live stream carries, resolved through
      the same route. Additive; nothing about `ToolResultEvent` or
      `GET /sessions/{id}/tool-results/{ref}` changes.
- [x] `tool_results.fetchable_refs` — the session's stored refs, read once per transcript, so an
      advertised ref means *fetchable* and not merely computable. Skipped when the store is off;
      an unreachable store degrades to an empty set rather than failing the reload.
- [x] `_transcript(stored, *, fetchable=…)` — stays a pure projection; the route does the one read.

**The pairing question was the real one, and content addressing dissolves it.** `tool_result_links`
carries session, tool, correlation id and a timestamp, and those four cannot separate two calls of
one tool in one turn — a join on them would be right most of the time, which is the worst thing a
hazard-screen link can be. The transcript instead hashes the result text it is already holding, and
that is the same string the producer hashed (MAF coerces a function result to `str` once, at the
content; the durable row is that content's JSON round trip). `tests/test_tool_results.py` drives
both real paths and asserts the two derivations agree, rather than asserting it in a comment.

**Retention gets a third state rather than a tombstone.** A swept blob leaves the transcript with a
result and an empty ref, which is distinguishable from `result is None` ("it ran and nobody knows
how it ended") and instructs a surface identically to "never stored". Separating "swept" from
"never stored" would mean a durable record per expired blob on the table that grows per tool call —
a record of a rendering, which is the thing this store deliberately is not.

---

# Review of the two ADRs above (2026-08-09) — three findings, all fixed here

Three blocking findings against the branch, plus three smaller ones. Both ADRs are unmerged, so
each decision is corrected in the ADR itself rather than superseded.

## 1 — the safety fix had weakened the hazard gate (BLOCKING)

- [x] `science/safety/notes.py::structures_in` — cuts a code span on every character a SMILES
      cannot contain (whitespace, control, anything outside printable ASCII) *before* asking the
      strict predicate, so a span like `` `CN=[N+]=[N-] (2 equiv)` `` is classified rather than
      dropped. `_is_structure` moving onto `parse_molecule` was half a change: `structures_in` uses
      it as a **filter**, so the span was not screened narrowly, it was not screened at all.
      Measured on the branch: `structures_in` `[]`, `hazard_problems` `[]`, against a high-severity
      `organic-azide` problem before. No heuristic is added and RDKit stays the arbiter — the cut
      is where the character set ends, which is also what `require_molecule` refuses. It errs
      towards screening: `` `80 °C` `` yields `C`, i.e. methane, which is the price of keeping
      `` `CN=[N+]=[N-]°` `` and `` `CN=[N+]=[N-]—the azide` `` screened.
- [x] `core/chem.py::require_molecule` — non-ASCII refused too, since RDKit skips a non-ASCII run
      at the *edges* of a string (`"°C"` is methane, `"CC°"` is ethane, `"C°C"` is an error). One
      gate, so the case is added once for every caller; the note gate makes the same statement as a
      separator rather than a refusal.
- [x] `connectors/bo/knowledge.py::_molecule_in` — the docstring's "the same arbiter … that
      `structures_in` applies" was false. Kept lenient and says why: its `True` is what puts a value
      in backticks, and backticks are what the gate reads, so strictness there would hide an
      annotated level from the screen entirely.
- [x] `tests/test_safety.py` — the annotated-span test that would have caught it, six span shapes
      parametrized, and the hard constraint as a **generated** property: no note loses a flag the
      lenient predicate would have raised, over Hypothesis-built spans, checked against a
      re-implementation of the old extraction. Verified to fail against the branch's version.

## 2 — a one-line crash on the reload route (BLOCKING)

- [x] `api/schemas.py::_transcript` — coerces a non-`str` `result` with `str()` before hashing it.
      Unreachable through this MAF (`Content.from_function_result` coerces), reachable from a row
      another version wrote — and it was an `AttributeError` inside `content_address`, i.e. a 500 on
      `GET /sessions/{id}/messages` that costs a chemist the whole conversation. `str()` and not
      `repr()`, because `runner_trace._result_text` coerces the producer's side that way and the ref
      only means anything if both hash the same bytes. Test pins both.

## 3 — the link row's labels were last-writer-wins (BLOCKING)

- [x] `api/tool_results.py::_UPSERT_LINK` — `tool` and `correlation_id` collapse to `''` when a
      second call disagrees, instead of being overwritten. The row is keyed
      `(session_id, content_hash)`, so two calls returning identical text are one row; every failed
      call in the system returns the same `"Error: Function failed."` (`include_detailed_errors` is
      off), so the relabelling was guaranteed, not hypothetical. Verified against a real Postgres:
      two calls → `('', '')` with the bytes intact, a third disagreeing write stays empty, and the
      same call twice keeps its labels. Rekeying on the call id was rejected in the ADR: the fetch
      route is `…/{ref}`, so several call rows would still match one read.
- [x] `StoredToolResult` docstring, migration 042's column comment and the ADR now say what the
      code does — empty means "the store will not name one call".

## Also fixed

- [x] `require_screenable_size` refuses an empty list: `screen_hazards([])` was a clean screen of
      nothing, which is the thesis of the whole ADR inverted.
- [x] `fetchable_refs`'s index comment named `tool_result_links_session_idx`, which is
      `(session_id, created_at DESC)` and cannot serve the query. Measured on 20,000 links over 200
      sessions: `Index Only Scan using tool_result_links_pkey`, `Heap Fetches: 0`.
- [x] The producer/transcript identity test fed the same literal to both sides through a hand-rolled
      double, so it only proved `content_address` is deterministic. It now builds one MAF turn from
      a `dict` through `Content.from_function_result`, drives `ToolCallTrace` with the real contents
      and `_transcript` with their `to_dict`/`from_dict` round trip — a MAF coercion change fails it.

**Verification.** `make lint type test` green. The Postgres-backed tool-result tests, which skip in
this sandbox, were run for real against a locally-initialized Postgres 16 (with the two pgvector
0.7 indexes of migrations 002/003 removed from a scratch copy, the sandbox's shipped pgvector being
0.6.0): 24 passed. Two failures on `main` are unrelated and unchanged —
`tests/test_properties_core.py::test_a_note_survives_the_write_read_round_trip` (a Hypothesis
lone-surrogate draw) and the 84% coverage floor, which `main` measures at 82.82% locally because the
Postgres and Temporal suites skip here.
