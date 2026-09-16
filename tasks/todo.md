# Off-the-shelf adoption — implementing the 2026-09-16 dependency audit

Fourteen read-only audits across `Chemclaw3`, `Chemclaw3-mcp` and `Chemclaw3_ui` asked one question of
every module: **is this hand-written code a library already does better?** Mostly the answer is no and
the tree already argues it. This plan implements the residue — the places where the argument is stale,
absent, or contradicted by a sibling repository.

Scope decided by the owner on 2026-09-16: **everything except the RDKit Postgres cartridge** (it needs
a database image this session cannot provision), **including** the three changes that invalidate a
deployed calculation cache, **including** both `Chemclaw3_ui` policy reversals. One PR per repository,
auto-merged.

## The rule this plan follows

A finding is only worth implementing if the *argument* behind the hand-written code has actually
expired. Where a docstring argues correctly for what it does, the item is not here — it is in the
report's "checked and declined" list, and re-proposing it later is the failure the register at
`docs/planning/BACKLOG.md:1013` exists to prevent. Three items below reverse a merged decision; each
gets an ADR that says so and states what changed, because **a decision that has changed gets a new
ADR, never an edit** (CLAUDE.md).

---

## A. Verified defects (this repository)

These are bugs, not adoptions. Each was reproduced at the top level before it was written down.

- [ ] **A1 — `core/metrics.py:1638,1653` renders `inf`/`nan` into a Prometheus scrape.**
      `f"{float('inf'):g}"` is `inf`; the text format requires `+Inf`. One bound gauge with a zero
      denominator poisons the whole exposition body. The histogram path at `:1673` already emits
      `le="+Inf"` correctly, so this is an inconsistency inside one renderer, not a convention.
      Fix: one `_sample(value)` formatter used by every numeric emission. Test: a gauge source
      returning each of `inf`, `-inf`, `nan` and a finite float, asserted against the rendered body.
- [ ] **A2 — `operations/activity.py:381` guards SQL string surgery with a bare `assert`.**
      `python -O` deletes it and the failure is then silent: `_TOOL_USAGE_ONE` becomes identical to
      `_TOOL_USAGE`, a third parameter is still appended, and psycopg raises a bind-count error at
      query time rather than at import. One of only four asserts in `src/`. Fix: `if ...: raise`.
      Also adopt the sibling repo's rule — see F3.
- [ ] **A3 — `agent/audit_store.py` buffers without a write-side bound.**
      The docstring argues correctly that a *failed* batch must not requeue without bound; a merely
      *slow* database is the case it does not cover, at ~90 rows a turn in a pod the chart limits to
      1 GiB. Fix: bound the buffer, drop oldest, and count the drop on a declared counter — an audit
      row lost silently is worse than one lost loudly.
- [ ] **A4 — `deliver/driver.py:364` opens an `AsyncClient` per delivered message.**
      A fresh TCP+TLS handshake per message to the same host. `registry.build` is uncached *on
      purpose* (a driver may hold a credential that must not outlive a rotation), so the pool cannot
      live on the driver: it goes on a module-level transport keyed by nothing, with `trust_env=False`
      preserved for the measured reason in the comment above it.

## B. Already in the closure (this repository)

Every library here already resolves in `uv.lock`. Adoption is a declaration line, not a new download.

- [ ] **B1 — `httpx-sse` (in lock via `mcp`) replaces three disagreeing SSE decoders.**
      `cli/live_storm.py:166` slices `[6:]`, `cli/live_benchmark.py:257` slices `[5:].strip()`,
      `evals/live.py:252` does it a third way. All three discard the `event:` name that
      `api/events.sse_frame` deliberately sets and re-read `type` out of the body. Latent rather than
      broken today — `sse_starlette` emits `model_dump_json()`, which carries no raw newline — so the
      cost being paid is three spellings drifting, not a live fault.
- [ ] **B2 — `pathspec` (in lock via `mypy`) replaces `fnmatch` tried three ways.**
      Measured: `fnmatch("Projects/Archive", "**/Archive/**")` is `False` in *all three* forms
      `crawl.py:80` attempts, so an excluded **directory** never matches and `descend` walks the whole
      archive subtree, excluding it one file at a time. Take the directory prune as well as the match.
      Needs a compatibility test over the shipped `sharedrive` patterns first: gitwildmatch drops the
      basename fallback, so a deployment-authored `Foo/Bar` would stop matching a file named `Bar`.
- [ ] **B3 — `charset-normalizer` (in lock via `requests`) replaces a hardcoded UTF-8 decode.**
      `parse.py:95,107` decode a decade-old Windows/CIFS share with `errors="replace"`. Measured:
      cp1252 `60 °C` becomes mojibake that is parsed, chunked, embedded and citable with nothing
      counted; a BOM breaks the first CSV header cell; UTF-16 is filed `skipped_unreadable` with a
      misleading reason. The same parser serves chemist uploads via `agent/attachments.py`.
      Strict UTF-8 (and `utf-8-sig`) first, so every currently-correct file stays byte-identical.
- [ ] **B4 — `networkx.utils.UnionFind` replaces a hand-typed union-find.**
      `agent/message_pairing.py:292`. `networkx` is a declared dependency, already imported in this
      package, and explicitly permitted by `tests/test_third_party_layering.py`. `.to_sets()` is the
      `members` dict built by hand at `:311`.
- [ ] **B5 — `bisect.bisect_right(..., key=)` replaces a hand-written binary search.**
      `agent/model_calls.py:304`. Worth taking beyond the line count: the hand-rolled version shipped
      with a `text[-0:]` off-by-one that returned 100,024 characters at a budget of 0–2 — the exact
      failure the function exists to prevent.
- [ ] **B6 — `PyJWKClient.fetch_data` overridden onto `httpx`.**
      `api/auth.py:103` mutates **process-global** `os.environ` (`no_proxy`) on every client build
      because PyJWT fetches with `urllib`, and carries a `threading.Lock` solely because that
      read-modify-write races on the validation pool — five concurrent writers left one of five hosts
      in `no_proxy`. `httpx` is a declared dependency. `fetch_data` is an unpromised shape, so this
      owes an assertion in `tests/test_upstream_surface.py`, which is what that file is for.
- [ ] **B7 — `psycopg` `executemany` + `class_row`, both already used elsewhere in this tree.**
      `publish/drivers/sql.py:298` opens a cursor and executes once *per row*, three loops deep, on an
      autocommit connection — order 10³ round trips per drain pass. And `operations/evidence_pack.py:295`
      unpacks **nine adjacent `str` columns positionally**, so reordering the SELECT produces a
      plausible evidence pack that passes `mypy --strict`. `dict_row` is already in use at
      `publish/drivers/postgres.py:23`, so there is no in-house policy against the row factory.
      The `WarehouseCursor` Protocol needs an optional `executemany` with a row-at-a-time fallback,
      because a batch shares a failure and today's per-row `except` names the offending table.
- [ ] **B8 — RDKit's `rdSubstructLibrary` replaces a scan that re-parses the corpus per query.**
      `science/fingerprints/molfp/search.py:114,240` calls `MolFromSmiles` on every stored SMILES on
      every query; the parse, not the isomorphism, is a large share of the 343 ms/molecule the
      docstring quotes. Zero mentions in the tree. `DEFERRED.md:63` defers this exact problem and
      frames the fix as a Postgres GIN back-port; the in-memory library is the cheaper half and does
      not close that row. Results identical — same `HasSubstructMatch`, same screen soundness — but
      `CachedTrustedSmilesMolHolder` skips sanitisation, so rows that land in `unreadable` today must
      keep landing there, and `maxResults` truncation must re-plumb into `scan_truncated`.
- [ ] **B9 — numpy replaces an O(n²) Python Tanimoto loop.**
      `memory/similarity.py:36`. `unpackbits` → `X @ X.T` → threshold → `connected_components`.
      Same asymptotics, ~100× constant, exact at the threshold. `DEFERRED.md:62` defers the
      *asymptotic* fix and is orthogonal: this does not close it.

## C. Needing a decision — taken, with the decision recorded

Each of these reverses or reopens something. Each gets an ADR.

- [ ] **C1 — `pint` replaces the hand-built unit registry.** `core/units.py:119-268`.
      The module's own history is three instances of one bug: a prefix rung existing on one ladder and
      not the other (`nM` missing so nanomolar folded to nanometre; `pM` added without its length twin
      so **picometre resolved to picomolar**; `µm` aliased to micromolar so a particle size was
      accepted as a concentration). A registry that *derives* prefixes cannot have that class of bug,
      and pint is case-sensitive by default, which is the other half. No ADR anywhere — 648 grepped.
      Built from a **restricted definitions file**, not the default registry: the docstring's
      "0.5 furlongs" refusal is right and must survive. `log_solubility` and `acidity` become declared
      pseudo-dimensions so nothing converts into them; `basis` (`area%` vs `% w/w` vs `mol%`) has no
      pint concept and stays first-party; `uncertainty` stays first-party. Use `ureg.Unit(symbol)`,
      never `parse_expression`, which would re-open the derived-unit algebra the docstring refuses.
- [ ] **C2 — `tiktoken` replaces chars/4, and the calibration class it needs.**
      `agent/context_budget.py:140` is ~110 lines of EWMA, seed-bias correction, sanity band and clamp
      that exist *only* because the counter is approximate. Three constraints: the gateway's model is
      deliberately unknowable (`D-2026-09-04`), so the encoding is configured rather than derived;
      tiktoken **downloads** its merge table on first use, which the air-gap forbids, so the encoding
      is baked into the image and `TIKTOKEN_CACHE_DIR` is set in the chart; and encoding a 100k-token
      thread per model call costs real CPU, so the exact count is memoised on the same key the
      estimate already uses. Land the **prefix half first** (`estimate_tool_schemas:435` is already
      memoised per bound surface, one encode per process) and keep the calibration as the fallback for
      a gateway whose encoding is unknown, rather than deleting it outright.
- [ ] **C3 — delete the second lexical ranker.** `retrieval/retrievers.py:229` + `agent/graph_tools.py:220`.
      `_scan_notes` stalls the event loop a measured 151 ms per call and 836 ms at eight concurrent on
      a 10k-note corpus, and its stated reason — "there is no database to push the scan into" — is
      false: `retrieval/vector_index.py` maintains a GIN-indexed `tsvector` over those same notes and
      `LexicalRetriever` already queries it. In `hybrid` mode both rankers run over one corpus.
      **Not** a BM25 library: `rank_bm25` rebuilds its index per construction and would pay the same
      cost. The risk is precise — `term_coverage` is *substring* and `term_frequencies` is *token*,
      and `D-2026-08-05-three-searches-that-disagreed-about-one-note` is about exactly this — so the
      gold-set numbers in `BACKLOG.md:210` get re-measured before and after, not assumed.
- [ ] **C4 — ruff `TID253` as a second belt on the third-party layering rule.**
      It bans a module at *module scope only* while permitting a function-scope import, which is the
      distinction `tests/test_third_party_layering.py` hand-builds. Ruff is already installed and
      already run by `make lint`. **A second belt, not a replacement** —
      `D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for` refused exactly this
      trade for `S101` on the grounds that two declarations of one rule with nothing reconciling them
      is worse than one, so the test stays and gains an assertion that the ruff config agrees with it.
- [ ] **C5 — reconcile `_STRUCTURAL_SECRETS` against a maintained ruleset.**
      `core/logging.py:889` is eleven vendor-prefix regexes that go stale in silence. Import the
      *pattern inventory*, keep the engine: `detect-secrets` is scan-shaped, returns spans rather than
      redactions, has no equivalent of the `(?P<keep>…)` group that keeps a redacted line saying which
      credential failed, carries no ReDoS bounds of its own, and **declares `requests`**, which is on
      the fleet's forbidden-import list. So this is a periodic reconciliation with a test that fails
      when the inventory drifts, not a swap.
- [ ] **C6 — `pytest` gains an async plugin and 660 tests stop hand-rolling `asyncio.run`.**
      Counted: **2,236** `asyncio.run(` sites across 135 files defining `async def _run`. The largest
      entirely unargued pattern in the tree — stated only in two test docstrings, nothing in 648 ADRs.
      `anyio` is already in the lock (via httpx/starlette) and ships a pytest plugin, so this adds
      zero distributions. Drive it on the **Postgres-backed files first**:
      `D-2026-09-13-a-loop-that-abandons-its-pool-can-fail-to-end` is precisely the teardown hazard a
      plugin-managed loop has to reproduce. Staged last, because a 260-file diff that goes wrong
      buries every other change in this PR.

## D. Duplication with no library answer

- [ ] **D1 — one Markdown table helper.** ~21 emitters across `cli/`, `evals/`, `memory/`,
      `protocols/`, `ingest/`, each with its own escaping and empty-cell convention. **Not `tabulate`**:
      `memory/comparison.py` is right that its three "honesty rules" are the part worth having in one
      place, and a library that renders cells uniformly pushes them back out to 21 call sites.
- [ ] **D2 — the electronvolt constant has one definition.** `publish/properties.py:102` writes
      `23.060547830619026` while `core/units.py:213` holds `ELECTRONVOLT_TO_KJ`. Verified equal today
      (`96.48533212331 / 4.184` is *exactly* that literal, difference 0.0) — latent drift, not a live
      bug, and `core/units.py:200` already names this file as owing the import.
- [ ] **D3 — ionisable-site perception is three rule sets in two repos.** Here: `science/calc/logd.py:82`.
      Express the rules as an RDKit SMARTS table rather than `GetBonds()` loops, transcribed to
      reproduce today's partition exactly, and pinned by the existing fixtures. Paired with F4/F5.

---

## E. `Chemclaw3-mcp` (own PR)

- [ ] **E1 — four physical constants take their value from `scipy.constants`**, already a dependency of
      that server. The comment claims "CODATA 2018, to full double precision" and is false for the
      first: `1.8897261246` against an exact `1.8897261246257702`, 1.4e-11 relative.
      **Invalidates the cache deliberately** — `calc_version` does not cover these literals, so
      `_HAMILTONIAN_REVISION` is bumped in the same commit or stored rows are served for a physics the
      code no longer reproduces.
- [ ] **E2 — `geomeTRIC` replaces the hand-written preconditioned optimizer** (`engine/xtb_opt.py:236`
      + `engine/anc.py`, ~330 LOC driving L-BFGS-B with both its own stopping tests disabled).
      `anc.py`'s docstring already states the gap in the library's favour. **Every stored
      `Structure.structure_id` changes**, and that id is the `input_hash` of every `xtb.*` key in
      Chemclaw3's cache and calibration ledger. Same revision bump as E1, one invalidation not two.
- [ ] **E3 — `rxn-insight` replaces the second reaction classifier**, already an optional dependency of
      that exact server and already constructed in `engine/predictors/conditions/rxn_insight.py`.
      The label vocabulary is a wire contract against vendored `trust_priors.json`, so it needs a
      mapping, and the SMARTS path stays as the no-extra fallback. Consensus ranking moves.
- [ ] **E4 — `molmass` replaces a hand-transcribed periodic table** in `servers/thermalsafety`.
      Zero-dependency, which is the only candidate respecting that server's stated closure rule.
      The deliberate refusals stay *in front of* the library: molmass parses `Ca(NO3)2` and hydrates,
      which `parse_formula` refuses by name because guessing is wrong by a factor of two.
- [ ] **E5 — one reconciliation test for molecular mass.** Four sources in one fleet;
      `tests/test_fleet.py` already reconciles densities across servers and nothing does the same for
      mass. Cheaper than any of the above and independent of all of them.
- [ ] **E6 — the ionisable-site SMARTS table**, shared shape with D3 above (`servers/calc/engine/pka.py`,
      `servers/chem/engine/species.py`). Transcription only — `dimorphite-dl` would desynchronise the
      three and invalidate a fitted calibration.
- [ ] **E7 — `no_egress`'s `network_imports` gains ruff `TID251/253` beside it** (second belt, same
      argument as C4), and `datasets.load_dataset` + `testing.load_manifest` validate through pydantic
      rather than defensive dict-walking — `extra="forbid"` catches the typo'd key that today parses
      clean and then reports the *correct* key as missing.

## F. `Chemclaw3_ui` (own PR)

- [ ] **F1 — Web Locks replaces hand-built cross-tab leader election.** `src/state/jobStreamLeader.ts:313`,
      ~150 lines of claim/heartbeat/lease/resign plus a two-leaders watchdog. Kernel-held leadership
      means a crashed tab releases immediately, which collapses the lease apparatus and the
      "two leaders is expected, not prevented" invariant. Zero bytes. The every-tab-leads fallback stays.
- [ ] **F2 — `eventsource-parser` in `scripts/smoke.mjs:324`.** Already a *production* dependency, and
      `src/lib/sse.ts:6` already documents why writing this twice is wrong.
- [ ] **F3 — `culori` replaces hand-transcribed OKLab matrices** in `scripts/check-contrast.mjs`.
      Real caveat: line 36 clamps out-of-gamut **linear** RGB before luminance, which culori will not
      reproduce, so every pair is re-measured and the deltas explained rather than a green run accepted.
- [ ] **F4 — `immer` replaces nested spread-chain updaters** in `ProtocolEditor.tsx:278`, and
      **`comlink`** replaces the hand-rolled worker RPC in `src/chem/rdkit.client.ts`. The parts that
      earn their keep stay: `REPLY_BUDGET_MS`, the retire-and-rerun-in-process fallback, and the
      three-ways-the-worker-is-absent handling. `check-bundle.mjs` asserts the worker chunk spelling,
      so Comlink's `new Worker(new URL(...))` must survive it verbatim.
- [ ] **F5 — policy reversal: `valibot` in `shared/events.ts`.** The file header says keep it
      dependency-free (three bundlers import it) and is also a **nine-incident changelog of this seam
      failing** — six events and three fields shipped upstream and were *deleted in transit*, because
      `normalizeEvent` rebuilds field by field. A schema makes the type derived, so a field cannot
      exist in the interface and be absent from the decoder. ADR reverses the rule explicitly and
      addresses `src/env.ts:8`, which declines a schema library for a dozen string checks — sound
      there, not transferable to a 17-member union.
- [ ] **F6 — policy reversal: `@tanstack/react-query`.** Replaces an `inFlight` map, a TTL cache with
      manual invalidation, and a `let cancelled = false` + two-`useState` triad repeated at nine
      components. Three behaviours must survive deliberately: `orEmpty` folding 404 to `[]` *with a log
      line*, `listPendingPlans` deliberately not swallowing errors, and `refetchOnWindowFocus` **off**
      for `/plans/pending`, which `client.ts:176` quotes as the most expensive call in the app.
- [ ] **F7 — a bundle-size budget, because F5 and F6 add bytes nothing currently watches.**
      `check-bundle.mjs` asserts bundle *shape* and says nothing about size. Adding weight to an entry
      chunk the repo actively polices, without a budget, is how the next reviewer inherits a number
      nobody measured.

---

## Verification

- Each repository's own gate: `make lint type test` here and in `Chemclaw3-mcp`; `npm run ci` in
  `Chemclaw3_ui`. **The Postgres-backed tests must actually run** — a green local line over 216 skips
  is not evidence about the durable layer (`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).
  `sudo -n dockerd && make up && make db-migrate` first, and report what the run skipped.
- Behaviour-preserving items are diffed against the base rather than asserted: B8 (same hit set),
  B9 (same clusters), C1 (same conversions and the same refusals), C3 (the gold-set numbers).
- Number-moving items state what moved and why: E1/E2 (the revision bump), E3 (consensus ranking).
- One ADR per decision, `D-2026-09-16-<slug>.md`, with its row in `docs/decisions/README.md`.
- `docs/planning/BACKLOG.md` and `DEFERRED.md` rows deleted in the commit that closes them — B8 and B9
  do **not** close `DEFERRED.md:62,63`, and saying so is part of the change.

## Review

_(filled in at the end)_
