# Off-the-shelf adoption — the 2026-09-16 dependency audit, reconciled against what shipped

**This is a closed plan, reconciled on 2026-09-16 against the tree rather than against itself.**
Every row below carries a verdict and the anchor that proves it. Nothing here is a claim about work
still to come: what is still open left this file for `docs/planning/BACKLOG.md`, which is the
register a next branch does not overwrite.

Four verdicts, and they are not interchangeable:

| Marker | Means |
| --- | --- |
| `- [x]` **landed** | shipped as the row described it |
| `- [x]` **landed, modified** | shipped, and the clause after the marker says what differs from the row |
| `- [ ]` **DECLINED** | measured and *not* taken; the measurement or the ADR that settled it is named |
| `- [ ]` **not done** | neither taken nor settled |

A checked box is a claim about a commit, so `grep -c '^- \[x\]' tasks/todo.md` and
`grep -c '^- \[ \]' tasks/todo.md` are what count them. No number here does.

Scope decided by the owner on 2026-09-16: everything except the RDKit Postgres cartridge, including
the three changes that invalidate a deployed calculation cache and both `Chemclaw3_ui` policy
reversals. One PR per repository — `Chemclaw3-mcp` #79 and `Chemclaw3_ui` #86 are merged to their
`main`; this repository's branch is not, and each sibling has a follow-up in flight.

## The rule this plan followed

A finding was only worth implementing if the *argument* behind the hand-written code had actually
expired. Where a docstring argued correctly for what it does, the item was declined — and a decline
is only a decline if a later session can find it, which is why the reasons are in
`docs/planning/BACKLOG.md`'s upstream-capability register and not in this file.

---

## A. Verified defects (this repository)

- [x] **A1 — `core/metrics.py` rendered `inf`/`nan` into a Prometheus scrape.** **Landed, modified:**
      one `_sample()` at `core/metrics.py:61` used by every numeric emission, `le` deliberately left
      on `:g` because it is a label. The `:g` spelling turned out to be wrong a second way nothing
      would have reported — six significant digits, so a counter past 1,234,567 rendered
      `1.23457e+06`, accepted and wrong.
      `D-2026-09-16-six-significant-digits-is-not-the-number-that-was-counted`.
- [x] **A2 — `operations/activity.py` guarded SQL string surgery with a bare `assert`.**
      **Landed, modified:** `if …: raise` at `operations/activity.py:381,390`, and the sibling's
      no-`assert`-in-serving-code rule adopted here as an argued allowlist rather than as the
      one-line fix. `D-2026-09-16-an-assert-is-a-control-with-an-off-switch-in-this-repository-too`.
- [x] **A3 — `agent/audit_store.py` buffered without a write-side bound.** **Landed, modified:**
      `agent_audit_buffer_max_events` (`core/config/agent.py:730`) sheds oldest and counts on
      `chemclaw_audit_events_shed_total`. A reviewer then found the bound was half the real ceiling
      — `_flush_all` swaps the list out and the in-flight batch was uncounted, measured peak 20 at a
      bound of 10 — and that it logged one WARNING per audit event; both fixed.
      `D-2026-09-16-a-buffer-bounded-against-a-dead-database-is-not-bounded-against-a-slow-one`.
- [x] **A4 — `deliver/driver.py` opened an `AsyncClient` per delivered message.**
      **Landed, modified — half of it was declined on measurement.** The shared TLS context is taken
      (`deliver/driver.py:380`, `core.http.default_ssl_context`, the 390x certifi parse). The shared
      **connection pool** is *not*: it needs per-event-loop caching, which is the shape `core/db.py`
      already carries a measured bug and a `_forget_pools_of_ended_loops` sweep for. The comment at
      `deliver/driver.py:376` is the record.

## B. Already in the closure (this repository)

- [x] **B1 — `httpx-sse` replaces three disagreeing SSE decoders.** **Landed, modified:** one
      `evals/live.decoded_events` for all three call sites, but driving `httpx_sse`'s **private**
      `SSEDecoder`/`SSELineDecoder` rather than `aiter_sse` — the adopted reader dropped the final
      event of a stream that ends without a blank line, which is exactly what `cli/live_storm.py`
      exists to generate, and raised inside the iterator on a wrong content type where one caller
      has no handler. 19-case matrix, 15/19 before and 19/19 after; the coupling is pinned in
      `tests/test_upstream_surface.py`.
- [x] **B2 — `pathspec` replaces `fnmatch` tried three ways.** **Landed:** `ingest/documents/crawl.py`
      and `binding.py:exclude_spec`, with the directory prune taken and proven by recording
      `os.scandir`. The compatibility check over the shipped patterns ran first — 13 cases, no
      divergence — and the one gitignore semantic that differs is stated in the docstring.
- [x] **B3 — `charset-normalizer` replaces a hardcoded UTF-8 decode.** **Landed, modified:**
      `ingest/documents/parse.py:29`, ordered `utf-8-sig` strict → detection → `errors="replace"`,
      so every file that decodes correctly today is byte-identical. `_CHUNK_TEXT_VERSION` is
      deliberately **not** bumped, so an already-indexed non-UTF-8 document keeps its mojibake until
      its fingerprint moves.
- [x] **B4 — `networkx.utils.UnionFind` replaces a hand-typed union-find.** **Landed as written:**
      `agent/message_pairing.py:51,306`. (It is B9, in `memory/similarity.py`, that uses
      `scipy.sparse.csgraph.connected_components` — not this row.) Identity measured over 30,000
      random row/candidate sets.
- [x] **B5 — `bisect.bisect_right(…, key=)` replaces a hand-written binary search.**
      **Landed, modified:** over `range(1, len(text) + 1)`, not `range(len(text) + 1)` — `text[-0:]`
      is the whole string, so the obvious form's keys are not sorted. `agent/model_calls.py:323`.
      Identity over 20,000 random strings at budgets 0–40, zero mismatches.
- [x] **B6 — `PyJWKClient.fetch_data` overridden onto `httpx`.** **Landed:** `_HttpxJwkClient` at
      `api/auth.py:77`; the `os.environ` `no_proxy` surgery, the `threading.Lock` and two exception
      workarounds are gone, the 401-vs-503 split re-mapped deliberately, and the unpromised shape
      pinned in `tests/test_upstream_surface.py`. It had a consequence nobody predicted — see the
      netguard paragraph in the Review.
- [x] **B7 — `psycopg` `executemany` + `class_row`.** **Landed, modified:** `executemany` is an
      **optional** `BatchingCursor` member with a row-at-a-time fallback, because a site brings its
      own driver (`D-2026-08-26-the-driver-s-signature-is-the-schema`). A reviewer then found the
      shipped `_PostgresCursor` had no such method, so the whole change was inert in production;
      `publish/drivers/postgres.py:93` is the fix, measured 1,500 round trips / 5.6 s → 9 / 0.41 s.
      `class_row` at `operations/evidence_pack.py:334`.
- [x] **B8 — RDKit's `rdSubstructLibrary` replaces a scan that re-parses the corpus per query.**
      **Landed, modified:** `science/fingerprints/molfp/substructure_index.py`, holder
      `CachedMolHolder` over `ToBinary()` rather than `CachedTrustedSmilesMolHolder` (that one needs
      `MolToSmiles` per record, which this tree records as an uncatchable SIGSEGV past ~16k atoms).
      `GetMatches` defaults `useChirality=True` where `HasSubstructMatch` defaults it `False` — taken
      by default that turned 514 matches into 0. Reviewer fix: the build got its own budget and a
      missing index is skipped rather than fatal, after it failed 3 of 3 on a 19,996-record corpus
      the loop answered in 2.01 s. `D-2026-09-16-an-index-is-an-optimisation-not-a-precondition`.
- [x] **B9 — numpy replaces an O(n²) Python Tanimoto loop.** **Landed, modified:** **sparse**
      `csr @ csr.T` into `scipy.sparse.csgraph.connected_components` (`memory/similarity.py:11,125`),
      not the dense `X @ X.T` the row proposed — dense is *slower than the Python loop* below a few
      hundred fingerprints (BLAS sync costs a flat ~75 ms) and its float64 matrix is 800 MB at the
      deferral's own trigger. Measured 4–5x, not the ~100x the row guessed, because the parse had
      already been hoisted. Clusters byte-identical at five thresholds.

## C. Needing a decision

- [x] **C1 — `pint` replaces the hand-built unit registry.** **Landed, modified:** built from a
      restricted definition list (`pint.UnitRegistry(None)` + `define()`), and resolution goes
      through `UnitRegistry.get_name`, **not** `ureg.Unit` — `Unit("m/g")` builds metre-per-gram even
      on a restricted registry, which would have lost the no-derived-unit-algebra invariant the row
      set out to keep. The rewrite found a fourth live instance of the family: `parse_unit('cM')`
      resolved to a **centimetre** on `main`, inside the allowlist written to excuse the family.
      Differential: 22,500 ordered pairs, 0 divergences; the refusal half bit-identical.
      It is **larger**, not smaller, and the growth is argument rather than logic. The commit that
      landed it measured 434 → 609 lines; `wc -l src/chemclaw/core/units.py` already answers
      differently, because a later commit in this same wave moved three constants onto
      `scipy.constants` — which is why the figure is named as a measurement of a commit and not
      restated as the module's size.
- [x] **C2 — `tiktoken` replaces chars/4.** **Landed, modified:** the **request prefix** is counted
      exactly (`agent/context_budget.py`, encoding configured, merge table baked at
      `TIKTOKEN_CACHE_DIR`, `tiktoken` declared rather than transitive). The per-turn **thread**
      count is now deliberately *refused* rather than staged next: 24.2 ms against 0.03 ms, at least
      three times per model call and two of the three on the loop that serves every SSE stream. The
      audit's premise was half wrong and measuring is what found it — chars/4 is 0.05% low on JSON
      schemas and 18% *high* on the English prompt, so the whole correction was the prose. The
      calibration stays, and not only as a fallback.
- [ ] **C3 — delete the second lexical ranker.** **DECLINED on measurement.** The duplication is real
      and the removable leg is the *opposite* of the obvious one — at a matched slot budget the
      Postgres `ts_rank` leg dominates and the graph leg contributes zero gold notes it misses — but
      `note_reindex_effective` schedules the reindex only when `lexical` or `vector` is in
      `CHEMCLAW_DATA_SOURCES`, and the shipped default is `graph,eln-json`, so the survivor would
      read an index nothing maintains. The narrower removal is its own regression: dropping
      `_relevance` moves graph-alone mean gold rank 4.72 → 5.67 and loses a gold note from the
      shipped arm, 39 → 38, which `retrieval_recall` gates on. No retrieval code changed; three
      docstrings stopped claiming what their own measurements contradict. **The decision this left
      open is now a `BACKLOG.md` row** — see the Review.
- [ ] **C4 — ruff `TID253` as a second belt on the third-party layering rule.** **DECLINED here,
      adopted in `Chemclaw3-mcp`.** `D-2026-09-16-a-flat-ban-cannot-express-a-matrix`: the sibling
      bans a flat list of network roots everywhere, which the rule expresses exactly; this tree
      declares a `(package, stack)` matrix with 51 edges, which `per-file-ignores` can only restate
      in another syntax — the "two declarations with nothing reconciling them" `D-2026-09-13` already
      refused for `S101` — and `TID253` sees one of the three import scopes the policy distinguishes.
      What *was* missing was root coverage, and that is closed: an unmapped root is skipped by the
      walk, which is how `httpx_sse`, `pint`, `tiktoken`, `pathspec`, `charset_normalizer` and
      `scipy` were invisible to it.
- [x] **C5 — reconcile `_STRUCTURAL_SECRETS` against a maintained ruleset.** **Landed as written:**
      the inventory was imported, the engine kept. `detect-secrets` stays out of the runtime for the
      four reasons the row gave. `core/logging.py:902` gained `ASIA`/`ABIA`/`ACCA`, a PEM private-key
      block (there was **no** rule at all), `dapi…`, `glpat-`, `xapp-` and `sk-admin-`; every added
      pattern timed against adversarial input at 10/40/80/160 kB; and staleness is now visible — a
      dated reconciliation with a 180-day bound, a per-shape sample list that fails by name, and a
      register of what was declined. A reviewer then found the PEM rule let the **encrypted** form
      through whole — see the Review.
- [x] **C6 — an async plugin, so tests stop hand-rolling `asyncio.run`.** **Landed, modified:**
      `anyio`'s bundled plugin with `anyio_mode = "auto"` and a session `anyio_backend`, adding zero
      distributions. The brief was wrong in a way worth recording: it asserted anyio had no auto
      mode and required a marker per file. The conversion is **partial by design** — strict-shape
      sites only, leaving a test with two `asyncio.run` calls, a `_run` that takes arguments or
      returns a value, and anything inside a `with` block alone. Collection 9,495 before and after.
      A full serial run then left four red; three were the conversion's and one was A1's metrics
      change, and all four are fixed and argued.

## D. Duplication with no library answer

- [x] **D1 — one Markdown table helper.** **Landed:** `core/markdown.py`; `grep -rn 'render_table('
      src/chemclaw` is what counts the call sites. Deliberately not `tabulate`. The escaping turned
      out to be a live correctness defect rather than a style inconsistency — seventeen of twenty
      sites escaped nothing, and a connector tool result containing `|` produced four cells under a
      three-column header.
- [x] **D2 — the electronvolt constant has one definition.** **Landed:**
      `publish/properties.py:107` derives it from `core.units`; the four bare `4.184` literals went
      with it. Verified equal to 0.0 before changing anything, so no published value moved. The
      constants themselves later moved to `scipy.constants` — see the Review.
- [x] **D3 — ionisable-site perception as an RDKit SMARTS table.** **Landed:**
      `science/calc/logd.py:50,70`, 262 → 227 lines, `maxMatches` bound to the atom count in place of
      RDKit's silent 1,000 default. Equivalence measured over **742** molecules against the old
      implementation: 0 disagreements, same counts and same refusals. That bit-identity is what stops
      this and the sibling's pKa predictor drifting.

## E. `Chemclaw3-mcp` — PR #79, merged

- [x] **E1 — four physical constants from `scipy.constants`.** **Landed:**
      `servers/calc/src/chemclaw_mcp_calc/engine/xtb_engine.py:24`, `xtb_props.py:35`, with the
      `_HAMILTONIAN_REVISION` bump **shared with E2** — one cache invalidation, not two. scipy ships
      CODATA 2022 where the comment claimed 2018.
- [x] **E2 — `geomeTRIC` replaces the hand-written preconditioned optimizer.** **Landed:**
      `engine/xtb_opt.py:48-54`; `engine/anc.py` is deleted. Same revision bump as E1.
- [ ] **E3 — `rxn-insight` replaces the second reaction classifier.** **Not done — queued rather
      than declined:** it is a row in that repository's own `docs/BACKLOG.md`, carrying the
      label-vocabulary mapping it needs against vendored `trust_priors.json` and the reason the
      SMARTS path has to stay as the no-extra fallback. The consensus-ranking move the Verification
      section below promised to state did not happen, because the change did not.
- [x] **E4 — `molmass` replaces a hand-transcribed periodic table.** **Landed:**
      `servers/thermalsafety/.../engine/oxygen_balance.py:39`; the deliberate refusals stay *in front
      of* the library, which is what keeps `Ca(NO3)2` and hydrates refused by name.
- [x] **E5 — one reconciliation test for molecular mass.** **Landed:**
      `tests/test_fleet.py::test_the_three_answers_to_molecular_mass_agree` — four independent
      sources written as three comparisons against the vendored `mw`, tolerance 0.05 g/mol, which is
      the one `props`' own check already used.
- [x] **E6 — the ionisable-site SMARTS table, shared shape with D3.** **Landed:**
      `servers/calc/.../engine/pka.py:111,143,153`. Transcription only; `dimorphite-dl` stays out.
- [x] **E7 — `TID251/253` beside `no_egress`, and pydantic for the two loaders.** **Landed, both
      halves:** `pyproject.toml:109` selects `TID253` with a test reconciling it against the static
      scan, and `mcp_server_kit/datasets.py` and `testing.py` validate through `BaseModel` with
      `extra="forbid"`.

## F. `Chemclaw3_ui` — PR #86, merged

- [x] **F1 — Web Locks replaces hand-built cross-tab leader election.** **Landed:**
      `src/state/jobStreamLeader.ts`; crash takeover 3.0–4.3 s → under 250 ms, a throttled leader is
      no longer deposed, two leaders are now impossible. `freeze`/`resume` handled beside
      `pagehide`/`pageshow`, and the wedged-thread trade is a named test rather than a comment.
- [x] **F2 — `eventsource-parser` in `scripts/smoke.mjs`.** **Landed:** `smoke.mjs:15`. The
      hand-rolled parser was dropping the multi-line `data:` frame — 3 events where 4 arrived.
- [x] **F3 — `culori` replaces hand-transcribed OKLab matrices.** **Landed, modified:** three gamut
      treatments measured and CSS Color 4 mapping adopted rather than reproducing the old linear-RGB
      clamp; 2 of 46 pairs move, by at most +0.22, no threshold touched. It also found 10 of 23
      tokens sit outside sRGB.
- [x] **F4 — `immer` in `ProtocolEditor`, `comlink` for the RDKit worker.** **Landed** as F4a and
      F4b; `isDirty` and `clone` both kept, the second with a measurement, and the
      rejection-vs-`null` mapping is under two tests.
- [x] **F5 — policy reversal: `valibot` in `shared/events.ts`.** **Landed:** every event's type is
      now `v.InferOutput` of its decoder, so a field cannot exist in the interface and be absent from
      the decoder; two compiler-API walks over the file are deleted with the drift they existed to
      catch. `src/env.ts` is deliberately unchanged and says why. Recorded in `docs/dependencies.md`.
- [x] **F6 — policy reversal: `@tanstack/react-query`.** **Landed:** ten `cancelled`-flag triads, an
      in-flight map and a TTL cache replaced, each of the three must-survive behaviours under a
      control that can fail. It turned up that a provider-less client never calls
      `queryClient.mount()`, which had made `refetchOnWindowFocus` inert everywhere.
- [x] **F7 — a bundle-size budget.** **Landed, modified:** the budget is on the **first load**, not
      the entry chunk, and it lives inside `scripts/check-bundle.mjs` rather than a new script — the
      entry chunk moved 4 bytes while the first load moved 13 kB gzipped, so an entry-chunk budget
      would have reported this wave as nothing. Set above the measurement with the headroom stated.

---

## Verification

- **Each repository's own gate.** `Chemclaw3-mcp` #79 and `Chemclaw3_ui` #86 merged green, and
  each has an uncommitted follow-up in its checkout. **Here, no `make lint type test` run is
  recorded after the reviewer commits, and that is what this branch owes before merge.** What is
  recorded: `make lint` and `make type` green over the full targets at the conversion fix, a full
  serial suite run during the anyio conversion whose four failures were diagnosed and fixed, and a
  red `tests/test_docstring_paths.py` that a reviewer found on an unrelated file — which is the
  evidence that the per-agent verification target was narrower than the gate. `make type` is
  `mypy src examples tests`; every agent in this wave verified with `mypy --strict src/chemclaw`,
  and two errors lived in exactly that gap.
- **Postgres-backed tests must actually run.** `sudo -n dockerd && make up && make db-migrate`
  first; a green local line over a skipped durable layer is not evidence about it
  (`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`). How many the run skipped is what
  `tests/conftest.py`'s terminal epilogue prints — no number belongs in this sentence, and the one
  that stood here was a transcription of CLAUDE.md's own example of the mistake.
- **Behaviour-preserving items were diffed against the base rather than asserted**: B8 (same hit
  set), B9 (byte-identical clusters at five thresholds), C1 (22,500 pairs, 0 divergences, refusals
  bit-identical), D3 (742 molecules, 0 disagreements), C6 (9,495 tests collected before and after).
- **Number-moving items state what moved**: E1/E2 share one `_HAMILTONIAN_REVISION` bump. E3 is not
  in this wave, so there is no consensus-ranking move to state.
- **Register hygiene.** Neither `DEFERRED.md` row was deleted, and both were edited to say why:
  the substructure row is **retitled** to name the Postgres `pattern_bits` GIN screen, which is the
  half an in-process index cannot do, and the sub-quadratic clustering row is **corrected** to say
  the constant was taken and the exponent was not. `ls docs/decisions/D-2026-09-16-*` lists the ADRs
  this wave wrote, each with its ledger row in `docs/decisions/README.md`.

## Review

**What was adopted.** Every library in section B was already resolved in `uv.lock`, and that framing
was itself corrected in the register: *resolved* is not *in the image*. `deploy/Containerfile`
installs `uv sync --frozen --no-dev`, and `pathspec` arrived only through `mypy`, a dev-group tool —
so it, like `pint` and `tiktoken`, is a new install in every shipped image. `uv export --frozen
--no-dev` is what answers that question per package.

**What was declined, and why it matters that it was declined rather than skipped.** C3 and C4 are
measured refusals with the measurement written down where a re-proposal will hit it — the
upstream-capability register in `docs/planning/BACKLOG.md` now carries the adoptions, both declines,
and every library the audit rejected, one line each. Where the audit recorded no
measurement, the register says so rather than inventing one: "declined, measurement not recorded" is
a question a later session can settle in an afternoon, and a fabricated number is one it cannot.
A4's connection pool and C2's per-turn thread count are the same shape one level down — half a row
declined inside a row that landed.

**What the wave's own reviews found.** Eight fresh-context reviewers were run over the implemented
work, told to measure rather than read, and they found real defects in it:

- **The `inf`/`nan` fix carried the same total-outage bug it was written to remove.** `_sample` began
  `if isinstance(value, int): return str(value)` under a comment asserting bool renders 0/1. `bool`
  *is* an `int`, so a gauge bound to a flag would have emitted `True` and lost the whole exposition.
  The branch was also dead — instrumented, `render` hands it `float` and nothing else.
- **The adopted SSE reader lost data the hand-written ones kept.** `aiter_sse` drops the last event
  of a stream cut off without a trailing blank line; the chaos harness that generates truncated
  streams could not see the last answer of the runs it cuts.
- **Making the JWKS fetch immune emptied a boot refusal's charge sheet.** B6 was right, and its row
  had to leave `_env_reading_destinations` — but for `entra_required=true` with `otel_enabled=false`
  that row was the *only* destination charged, so behind a loopback sidecar the knowledge-graph note
  push and its credential had no observer at all. The refusal grew a second arm rather than
  resurrecting a false one.
- **The new PEM redaction rule did not redact an encrypted private key.** The lookahead required an
  unbroken base64 run within 8 whitespace characters of the header and `Proc-Type:`/`DEK-Info:` sit
  in between, so `openssl genrsa -aes256` output went through verbatim; at ~12 kB, 85 body lines
  survived past the `***`. One mistake three times: every bound was a guess about the *unencrypted*
  64-column shape.
- **The substructure index answered by failing.** Charged against the *match* timeout with nothing
  cached on abandon, it failed 3 of 3 on a corpus the loop it replaced answered in 2.01 s — reachable
  by following this tree's own advice to raise the scan cap.
- **The batching seam was inert in production**, because the one driver this repository ships had no
  `executemany`; and the audit's own scope had a hole — `scipy.constants` was approved and never
  taken, so `core/units.py` was still transcribing the calorie, the hartree and the electronvolt.
- **The tiktoken air-gap guard disagreed with the function it transcribes**, in the unsafe
  direction: an empty `TIKTOKEN_CACHE_DIR` read as "baked" while tiktoken went to the network.

Two of those are the same lesson as A1's: a control written against a defect can carry the defect.
Three more are `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` arriving inside the commits
that wrote the prose — a `scipy` layering edge whose reason named a module path that does not exist,
an alert-count sentence that said "three" over nineteen rules, and a fleet-token figure that moved on
a sibling's merge between the measurement and the commit recording it.

**What is still open.** The full serial suite has not been re-run since the reviewer commits, and
this branch is not merged while the two siblings' are. One genuinely open decision was moved out of
this file: **the meaning of the `graph` retrieval source** — C3 proved the in-process ranker is the
weaker of the two and *unremovable* only because `note_reindex_effective` leaves the better leg's
index unmaintained under the shipped `CHEMCLAW_DATA_SOURCES` default. That is a `data_sources` and
manifest decision, not a retriever edit, and it is now a row in `docs/planning/BACKLOG.md` §2 beside
the `hybrid` retrieval row it bears on. E3 is queued in `Chemclaw3-mcp`'s own `docs/BACKLOG.md`. One
hole is written down and deliberately not closed: no pytest rule catches a forgotten `await` inside
an async test — the obvious `filterwarnings` line does not work, because the warning is raised by
the garbage collector after the test has returned.
