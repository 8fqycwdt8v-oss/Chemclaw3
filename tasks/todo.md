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

(to be written at the end)
