# Fresh full-family code review, hardening and refactoring (2026-08-26)

All prior review results are discarded. This is a from-scratch audit of all three repos
(`Chemclaw3`, `Chemclaw3-mcp`, `Chemclaw3_ui`) at `origin/main`, run as a wide subagent fan-out,
with findings verified by execution before any fix lands.

## Ground rules for this pass

- **A finding is a claim about the code, and a claim is checked by running it** (CLAUDE.md
  "measure it, don't argue it"). A reviewer's prose is evidence about the reviewer, not the code.
  Every accepted finding needs either a failing probe, a reproduced trace, or a line-exact reading
  a second agent confirmed.
- **The baseline must be genuinely green first**, with Postgres up — a local run that skips ~157
  Postgres tests and prints green is not a baseline.
- **No fix without a test that fails before it.** Behavioural fixes get behavioural tests, not mocks.
- Refactors are only merged when they *delete* coupling or duplication; "tidier" alone is not a
  reason to touch code (KISS, Rule of Three).
- Each themed cluster of fixes is its own PR per repo, merged before the next starts, so the branch
  never carries two unrelated arguments.

## Phase 0 — baseline (done)

- [x] `uv sync` in Chemclaw3 and Chemclaw3-mcp; `npm ci` in Chemclaw3_ui
- [x] `dockerd`, `make up`, `make db-migrate` — Postgres/pgvector + Temporal running
- [x] Baseline recorded, including what did **not** run:
  - **Chemclaw3**: `ruff check` + `ruff format --check` green (677 files); `mypy --strict` green
    (677 files). The full `pytest` did not finish locally — the box was carrying ~24 concurrent
    review agents and load average sat above 25, and one run died with a pytest `INTERNALERROR`
    under that load rather than a test failure. **CI is the authoritative gate for this repo's
    suite in this pass**, and every fix is verified against its own suites locally before push.
  - **Chemclaw3-mcp**: `ruff` green (201 files), `mypy --strict` green (71 files). `pytest` was
    killed by its own timeout at ~11% under the same load (exit 143) — not a failure, and not a
    pass either. Recorded as unrun rather than green.
  - **Chemclaw3_ui**: `tsc -b` green, `eslint` green, `vitest` **424 passed / 36 files / 0 skipped**.
    Playwright could not run — no Chromium binary in this environment — so the e2e tier is unrun.

## Phase 1 — review fan-out (fresh, no prior results consulted)

Each agent reviews one area with a single question: *what is wrong here, and how would I prove it?*
Output is a structured finding list (file:line, claim, failure scenario, confidence). No fixes.

### Chemclaw3 (backend core)
- [ ] A1 `agent/` — LangGraph graph build, the 7 middlewares, checkpointer, compaction, plan gate, skills backend
- [ ] A2 `api/` — front door, SSE contract, OIDC/authz gate, token budget, session push-back
- [ ] A3 `core/` — config, db pools, audit trail, roles/entitlements, note proposals / PR-gate
- [ ] A4 `durable/` — Temporal workflows/activities, timeouts, retention, worker wiring
- [ ] A5 `science/` — calc cache + `cached_compute`, calibration ledger, bo, fingerprints
- [ ] A6 `connectors/` — bundle loading, `HttpEndpoint`, MCP client/session, tool classification
- [ ] A7 `ingest/` — sources seam, ELN warehouse engine, documents share, ORD records
- [ ] A8 `publish/` + `kg/` — result sinks, projectors, graph indexer, PR gate
- [ ] A9 `retrieval/` + `memory/` + `evals/` + `templates/`
- [ ] A10 `cli/`

### Cross-cutting (Chemclaw3, all packages)
- [ ] X1 Security: authn/authz gaps, secret handling, injection (SQL/prompt/command), SSRF, path traversal, deserialization
- [ ] X2 Concurrency: asyncio misuse, blocking calls in the loop, pool/session lifetimes, races, cancellation
- [ ] X3 Resource safety: fds, connections, subprocesses, temp dirs, unbounded growth, retries/backoff
- [ ] X4 Dead code, duplication, single-caller abstractions, "for later" stubs
- [ ] X5 Config discipline: magic numbers, hardcoded URLs/paths/timeouts/model names outside settings
- [ ] X6 Test quality: mock-only tests, untested critical paths, tests that cannot fail
- [ ] X7 Doc/claim audit: every present-tense claim in CLAUDE.md, package READMEs and merged ADRs that
      the code does **not** back (the `audit_events.agent` failure mode)

### Chemclaw3-mcp
- [ ] M1 `servers/calc` — the heaviest server; process isolation, keys, timeouts
- [ ] M2 `servers/chem` + `servers/rxnlabel`
- [ ] M3 `servers/rxnpredict` + `servers/props`
- [ ] M4 `servers/safety` + `servers/pyexec` — **pyexec is a sandbox; treat as adversarial**
- [ ] M5 `packages/mcp_server_kit` + fleet invariants (egress guard, manifests, identity headers)

### Chemclaw3_ui
- [ ] U1 `src/components/` — rendering, state leaks, accessibility, error surfaces
- [ ] U2 `src/state/` + `src/api/` — SSE client, reconnect, cancellation, error propagation
- [ ] U3 `src/chem/` — structure editor/paste path
- [ ] U4 `server/` + `shared/` + auth — token handling, XSS, CSP, proxying
- [ ] U5 tests + e2e quality

## Phase 2 — triage and verification

- [ ] Merge all findings into one register; de-duplicate across agents
- [ ] Kill anything unreproducible: a finding that cannot be demonstrated is not a finding
- [ ] Rank: (a) security/correctness defects, (b) resource/concurrency, (c) coupling and duplication,
      (d) false claims in docs
- [ ] For each survivor: write the failing probe *first*

## Phase 3 — fix waves (one PR per theme per repo, merged before the next)

- [ ] W1 security + correctness defects
- [ ] W2 concurrency + resource safety
- [ ] W3 refactor: delete duplication, dead code, single-caller abstractions
- [ ] W4 doc/claim reconciliation + ADRs for anything that changed a decision

## Phase 4 — close out

- [ ] `make lint type test` green with Postgres up, in both Python repos; ui suite green
- [ ] ADRs written for every decision taken here (`D-YYYY-MM-DD-<slug>.md` + ledger row)
- [ ] `docs/planning/BACKLOG.md` / `DEFERRED.md` rows added or deleted as the pass decided
- [ ] `tasks/lessons.md` updated
- [ ] Review section written below

## Review

**Merged**: `Chemclaw3#245`, `Chemclaw3-mcp#25`, `Chemclaw3_ui#28`. 27 review sweeps, 10 fix teams.

### What the pass was worth

The defects that mattered were not the ones a linter finds. Nearly all of them share one shape,
and it is worth stating because it predicts where to look next time: **a design argued correctly in
prose, resting on a premise the code contradicts.**

- `_check_classification`'s docstring is *right* that refusing to load is the only option that
  cannot be wrong quietly — and a partition of nothing is trivially satisfied, so the manifest that
  declared the least got the widest surface, unclassified.
- `D-2026-08-25` removed the ELN PR-gate because "`record_from_ord_reaction` infers nothing", which
  was true of that function and false of the adapter beside it, so 80 °C for 12 h was stored as
  0 °C for 0.5 h.
- The `Warehouse` protocol omitted `close()` on the written premise that halves live for the
  process; they were rebuilt per tool call, 100 for 100.
- The retrievers' own `except` clauses made `fanout`'s `failed` channel unreachable, so an outage
  read as "no prior art".

Mutation testing put a number on it: 22 of 31 killed, and **all eight survivors were defences
argued at length in a docstring and asserted nowhere**. That is the single most useful sentence
this pass produced, and it is a search key, not a slogan.

### What was checked and found sound

Reported because a review that only lists failures cannot be calibrated. The authorization spine
held under adversarial probing (22 of 22 routes, a full JWT bypass battery, the redaction filter
against 15 real `LogRecord`s, the prompt-injection envelope against seven forgery spellings). The
RRHO/Boltzmann arithmetic re-derived to CODATA at 1e-13. The magic-number rule genuinely holds
across 355 settings. `mcp_server_kit`'s four documented traps all held against a running server.
The `props` corpus survived four independent cross-checks. D-011 re-measured exactly as documented.

### Three findings the fix teams refuted or redirected, by measuring

- The condenser was reported unmetered; it is not — a model built inside a tool body inherits the
  graph's callbacks (3 x 55 spent, 165 metered). Only the verifier's judge was.
- `pyexec`'s per-call isolation was reported broken; it is not — the *claim* was too absolute, so
  the README now says what the boundary does not cover instead of a control being invented.
- `CALCULATION_EPOCH` was expected to need a bump; it does not — Chemclaw3 folds its own epoch over
  the server's `params_hash`, so the two compose and a bump would invalidate unrelated rows.

### What I got wrong

One validation change (refusing an empty `tools` list) broke tests in **three separate rounds**,
two of them found by someone else — a reviewer working a different lens, then CI. The file that
broke last was in my first grep's output; I read the hit, saw a variable passed through, and did
not ask what it defaulted to. The rule is in `tasks/lessons.md`: when a change makes a
previously-legal value illegal, search for what *produces* that value, not what names the type —
and run the whole suite, because four of the five files that broke had nothing to do with
connectors. I also pushed once with `ruff` red, having chained the gate into the same command as
the commit so it ran too late to stop anything.

### Left open, deliberately

- `FingerprintReactionRetriever`'s `except FingerprintError: return []` — that type means a bad
  caller anchor as well as a corrupt index, and separating them is wider than this pass.
- An X-H rotor still cannot be *scanned*: the fix needs a dihedral in `AddHs` numbering and a
  Chemclaw3-side change, so it spans two repositories.
- `connector-validate` still cannot check a remote server's declared surface against what it
  serves, and nothing bounds a connector's *response* while its request is capped (64 MB arrived
  intact). Both are named in `D-2026-08-26-an-empty-allow-list-is-not-an-allow-list` so neither
  reads as covered.
- The unfixed remainder of the finding register (durable/, core/, cli/, X4's 769 deletable LOC,
  X5's chart seam) is real and unactioned — it was scoped out, not disproved.

---

# (Concurrent pass, kept rather than overwritten)

The section below is another session's working notes, which landed on `main` while this
review was running. Both are kept: a scratch file is not a reason to delete someone's
record of what they were doing.

# Atom-addressable reactivity — implementation

Concept: `tasks/reactivity-labels-concept.md` · ADR: `docs/decisions/D-2026-08-26-an-atom-index-is-not-a-name.md`

## ADR
- [x] `D-2026-08-26-an-atom-index-is-not-a-name.md` + ledger row

## Tier 0 — structural site labels (Chemclaw3-mcp / servers/chem)
- [x] `engine/sites.py`: `Site` + `describe_atom_sites`, one entry per symmetry class
- [x] content-addressed `site_id` on the `torsion_handle` construction
- [x] `describe_sites` tool, declared `read_only`
- [x] 27 tests: symmetry classes, ring relationships, C-H folding, handle stability

## Tier 1 — free descriptor panel (Chemclaw3-mcp / servers/calc)
- [x] read the ion energies `compute_fukui` was discarding
- [x] global panel (IP/EA/mu/eta/S/omega) + local (dual, s±, omega_k) + free valence
- [x] `test_the_panel_costs_no_extra_single_point` pins the SCF count at three

## Tier 2 — xtb-binary descriptors (Chemclaw3-mcp / servers/calc)
- [x] `engine/xtb_atomic.py` + `compute_atomic_descriptors`
- [x] property-table and ESP-grid parsers, written against a captured 6.6.1 run
- [x] refuses by name when the binary is absent; the *key* still derives (CREST's convention)

## Composition (Chemclaw3)
- [x] mirrored reader models, `CALCULATION_EPOCH` -> "2" in both repos
- [x] `compute_atomic_descriptors` on the `calc` bundle; `describe_sites` on `chem`
- [x] publish projector + property vocabulary carry the panel
- [x] `skills/reactivity-descriptors` rewritten: start with `describe_sites`, scope the
      question, aggregate by class, report only differences that exceed the class spread
- [x] probe `an-34` for the new tool

## Gate
- [x] Chemclaw3 `make check`: **4799 passed, 3 skipped** — with Docker/Postgres up, so the
      ~157 Postgres-backed tests really ran
- [x] Chemclaw3-mcp `make check`: **1188 passed, 5 skipped** — the 5 are the binary-only Tier 2
      tests, which run and pass with `xtb` installed (verified separately, 18 passed)

## Review

**What the concept got right.** The diagnosis held: the failure was presentation, not physics.
Phenol's *para* carbon is still rank 6 of 13 in the raw ranking, and scoping plus class
aggregation is what makes it reportable.

**What building it changed.**

1. **Tier 2 was verifiable after all.** `apt` carries xtb 6.6.1. Installing it replaced guesswork
   with captured output, and caught two things prose would have got wrong: the polarisability
   table is on *stdout*, not in `xtbout.json`, and an `--esp` run aborts (SIGABRT) after writing
   the grid and before the JSON — so a surface calculation cannot also carry the atomic multipoles.
2. **It exposed a live defect unrelated to this work.** `xtb_engine` defaults to `"auto"`, so with
   a binary present `compute_xtb_energy`, `compute_electronic_properties` and
   `predict_site_reactivity` stamped `+xtb+xtb-6.6.1` onto results computed entirely by tblite —
   none of the three has a binary code path. Fixed by making the backend a property of the
   **task** (`_FIXED_BACKEND`), not of the caller.
3. **`GetDefaultValence` is the wrong RDKit call for a free valence.** A sulfone's sulfur came out
   at −2.94. `GetValenceList` is the right one, and an element with more than one normal valence
   now gets `None`.
4. **Rounding a derivation separately from its inputs** made `f_zero` disagree with its own
   definition in the fourth decimal.
5. **A ring fusion is not a substituent.** Naphthalene was being labelled "bearing the CH
   substituent"; fused rings and two-heteroatom rings now refuse the classical *ortho/meta/para*
   names rather than misapplying them.
6. **A key derives without a binary; only computing refuses.** Got this backwards first;
   `test_deriving_a_key_runs_no_scf` caught it.

**Post-merge review (8 findings, all real, all fixed).** Tests were green and saw none of them.
The two that mattered most: `describe_sites` numbered atoms from the caller's spelling while the
calculators canonicalise, so the documented index join mis-attributed every per-atom number; and the
ESP surface was a flag kept out of the cache key, so `surface=True` was served the panel-only row and
returned nothing having run nothing. Also: no projector for `xtb.atomic` (results silently dropped),
`free_valence` still meaningless on charged atoms, colliding labels on fused rings and azines, a
`nitro_nitrogen` SMARTS matching a form RDKit never builds, a new cached payload outside the digest
guard, and the skill instructing the model to report a `resolved` field no tool returns. Kept rather
than fixed: resonance-equivalent atoms do not merge, because topological symmetry cannot see
resonance — the label-uniqueness rule is what makes that safe.

**Left open, deliberately.**

- **The cross-molecule claim for local electrophilicity is unsettled.** omega is 3.24 eV for
  phenol, 3.52 for *N,N*-dimethylacrylamide, 3.74 for pyridine — plausible ordering on a
  demonstrably wrong absolute scale. It ships as a ranking quantity for the calibration ledger to
  settle, not as an established one.
- **The xtb binary Hessian path produces no dipole derivatives.** Pre-existing — proven by
  stashing this change and re-running with the binary installed — so a deployment that adds xtb
  loses IR dipole derivatives and fails two `test_engine.py` assertions. Not this change's to fix;
  it needs its own decision about whether the binary Hessian route is supported at all.
- **No `profile_reactivity` composite tool.** The join, the class aggregation and the noise-floor
  rule live in the skill rather than in a cross-connector tool: the pieces are all free and
  `read_only`, and a composite spanning two connectors has no precedent here. If a second caller
  appears, that is the trigger to extract it.
