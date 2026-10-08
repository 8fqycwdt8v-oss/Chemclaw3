# Platform architecture programme (2026-10-07)

Source: the read-only architecture review of 2026-10-07 across Chemclaw3, Chemclaw3-mcp and
Chemclaw3_ui. This file is the plan for implementing **every** finding of that review, in waves.
The previous task (deep documentation pass, 2026-10-04) shipped and its record is in git history.

## Decisions taken (2026-10-07, by the owner)

- **Knowledge graph: Postgres only, no git.** Not a mirror, not an export target: git leaves the
  knowledge path entirely. History is `kg_note_revisions`; human edits go through the API.
- **Shared coordination: Postgres only.** No Redis.
- **Agent builder: stay on off-the-shelf `deepagents`** to profit from upstream updates. The
  self-build is the thing to challenge, not the default: every workaround is either replaced by a
  public deepagents/LangChain seam, contributed upstream, or argued as the one remaining gap. The
  standard is that deepagents covers everything a self-built builder would.
- **Tenancy: `tenant_id` + Postgres row-level security** in one deployment.

**Decided 2026-10-08, on the W1 open points:**
- **The test gate goes parallel** (`D-2026-10-08-the-test-gate-runs-in-parallel`, superseding
  `D-2026-09-13`). A failure seen only in parallel is a concurrency defect to root-cause, never a
  test to quarantine. Lands in W3, whose subject is exactly that class of defect.
- **Model-facing text is changed, behind an evaluation**
  (`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`). Tool docstrings, schema
  descriptions and prompt blocks get the same diet as the prose, one batch at a time, and a batch
  ships only if the eval says it is not worse. Lands in W2, because the text is part of each
  connector's contract.
- **The git-safety hook is changed explicitly**, on the owner's authorisation. Lands in W2.

Scope: all three repos (`Chemclaw3_mock` only where a contract it serves changes). Each repo's change
is its own branch and PR, as the repo's rules require.

## How to read this plan

- **Waves** are sequenced by dependency and by risk: the cheap, low-risk ones first, and the ones
  that change data or the turn path last. **Tracks** inside a wave can run in parallel sessions,
  because each track owns a disjoint set of files.
- Every wave has **entry criteria**, **work items** (checkable, each item = one PR unless stated),
  **exit criteria that are measurements**, and a **rollback** story.
- An item marked **ADR** is a genuine choice between options. Under the repo's own rule
  (`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision`) nothing else gets one.
- Large swaps (knowledge store, agent builder, calc RPC) ship **behind a setting** with the old
  path intact, are flipped in a deployment, and the old path is deleted one wave later. No
  big-bang cutovers.
- "Measure, don't argue" applies to every exit criterion. W0 sets up the baselines the later waves
  are judged against.

## Overview

| Wave | Theme | Repos | Risk | Rough size | Depends on |
| --- | --- | --- | --- | --- | --- |
| W0 | Baselines, guard rails, programme setup | all | none | S | — |
| W1 | Docs and process diet | all | low | M | W0 |
| W2 | One owner per contract (manifests, events, API types); model-facing text behind an eval; git-safety hook | all | low–med | M | W0 |
| W3 | Horizontal scale foundations (no per-process truth); parallel test gate | core | med | M | W0 |
| W4 | Knowledge graph in Postgres | core (+ui read paths) | high | L | W3 (shared locks), W1 |
| W5 | Agent core: deepagents via public seams, one mechanism per concern | core | high | L | W0 baselines, W2 (event schema) |
| W6 | Backend RPC, fleet Helm, release unit, CI | mcp, core, ui | med | M | W2 |
| W7 | Missing capabilities: semantic retrieval, tenancy, backup, lineage, BFF auth, config profiles | all | med–high | L | W3, W4, W6 |
| W8 | Decomposition, dead-code sweep, final measurement | all | low | M | W1–W7 |

Parallelism: W1, W2 and W3 can start together after W0. W4 and W5 can run in parallel; they touch
disjoint packages (`kg/`, `retrieval/`, `memory/` versus `agent/`, `api/`). W6 can run beside W4
and W5 once W2 lands.

---

## W0 — Baselines, guard rails, programme setup

**Goal.** Make every later claim measurable, and stop the overhead growing while it is being cut.

Entry: none.

- [x] **W0.1 Baseline script** (`src/chemclaw/cli/bench_baseline.py`, or extend the existing
      `live_*` harnesses rather than adding a fourth). It records, as JSON committed under
      `data/evals/baselines/architecture-2026-10.json`:
  - cold and warm import time of `chemclaw.api.app` (measured: 16.6 s cold, 6.5 s warm);
  - first-turn and steady-state graph build (measured: 0.56 s, then 0.08–0.12 s);
  - time-to-first-token and total turn latency on the mock LLM for 3 canned turns;
  - root middleware count and tool count (`len(tool_call_middleware(...))`);
  - KG write latency per note and notes/s under 4 concurrent writers; KG RSS per 1k notes;
  - `make test` serial wall time and the number of Postgres-backed skips;
  - LOC and prose ratio per package (the AST/tokenize script the review used, saved to
    `scripts/` so it can be re-run);
  - Helm render line count; number of settings fields; number of `CHEMCLAW_*` names.
- [x] **W0.2 Same baseline for the fleet** (`Chemclaw3-mcp/scripts/bench_baseline.py`): per-server
      cold start, `/mcp` handshake + `list_tools` latency, `calc` `calculation_key` round trip.
- [x] **W0.3 Freeze the overhead while it is cut.** Agree, in this file, that until W1 closes:
  no new ADR without an Options section; no new meta-test of prose; no docstring longer than
  ~10 lines in new code. (A rule here rather than a test: W1 decides which tests survive.)
- [x] **W0.4 Tracking.** One GitHub issue per wave per repo, linked here, so a session claims a
      wave atomically (the repo's own claim rule, `D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit`).
- [x] **W0.5 Programme ADR** `D-2026-10-xx-the-architecture-programme`: the choices this plan takes
      (KG system of record, agent builder, backend RPC, tenancy model), each as Options + Decision +
      `Revisit when:`. This ADR **supersedes** the ones it overturns, listed by id. Written once, not
      per wave.

Exit: the baseline JSON exists for both repos and the issues exist.
Rollback: n/a.

---

## W1 — Docs and process diet

**Goal.** Cut the cost every session pays before it writes a line: ~2.9k lines of mandatory
reading, 59% prose in `src/`, 761 ADRs, 1.9k lines of lessons. Nothing here changes runtime
behaviour, which is what makes it the right first wave.

Entry: W0.3 agreed.

### Track A — CLAUDE.md and session reading (all repos)
- [x] **W1.1** Rewrite `Chemclaw3/CLAUDE.md` to ≤150 lines of **current rules only**: layers (once),
      where things go, commands, workflow, quality bar, persistent-knowledge files. Delete every
      "used to / this sentence / audited" passage, the duplicated layer section, and the reference
      to the archived G1–G7 checklist. **Resolve the contradiction** between "when unsure, ask" and
      "fix autonomously / merge yourself": state when to ask (destructive, ambiguous, outside scope)
      and that everything else is autonomous.
- [x] **W1.2** Same for `Chemclaw3-mcp/CLAUDE.md` (671 lines → ≤150). The egress, auth, health and
      manifest rules stay **as rules**; their history moves to the ADRs that already hold it.
- [x] **W1.3** `Chemclaw3_ui`: check its CLAUDE.md/README for the same pattern and trim.
- [x] **W1.4** `ARCHITECTURE.md`: one table row per directory, ≤2 sentences each. The paragraphs
      inside cells move to the package READMEs (which already exist).

### Track B — Decision record
- [x] **W1.5** Write `docs/decisions/CURRENT.md`: one page of the decisions in force, grouped by
      area, each one line plus its ADR id. This is what CLAUDE.md links to.
- [x] **W1.6** Mark superseded ADRs. Today 72 mention supersession and 6 say "superseded by". Add a
      `Superseded-by:` header line (a one-time mechanical exception to "never edit a merged ADR",
      stated in W0.5's ADR). `test_decision_log.py` is reduced to: unique ids, filename matches
      heading, ledger row exists, superseded-by target exists.
- [x] **W1.7** Add an ADR template with a mandatory `## Options` section and `Revisit when:` for
      declines, and a check that new ADRs (after a cursor) carry both.
- [x] **W1.8** Mark the ~60% of ADRs that are defect reports `Kind: defect-record` in the ledger
      (no file edits), so CURRENT.md and readers can skip them. No new defect ADRs from here on.

### Track C — Source prose (core, then mcp)
- [x] **W1.9** Write the docstring standard in CLAUDE.md: what, why, invariants, ≤~10 lines; no
      history, no "measured on date X", no correction of earlier prose; at most one ADR id per
      module docstring, and none in inline comments.
- [x] **W1.10** Mechanical pass, **one package per PR**, largest-prose first: `agent/` (2.06 prose to
      code), `core/config/` (92% prose), `api/`, `durable/`, `kg/`, `science/`, `connectors/`, the
      rest. The history goes into the PR description and commit message; the ADR stays the record.
      Target ≤30% prose per package. Each PR is behaviour-neutral: `make lint type test` green and
      the W0 benchmark unchanged.
- [x] **W1.11** Same pass over `Chemclaw3-mcp` (54.5% prose) and `test_fleet.py`'s docstrings.
- [x] **W1.12** Tests: trim docstrings over ~10 lines (27% of test lines are docstrings).

### Track D — Planning and memory files
- [x] **W1.13** `tasks/lessons.md` → ≤30 rules, no incident narratives. The rule broken 20 times (the
      destructive git command) becomes a **PreToolUse hook** in `.claude/settings.json` that blocks it
      (`update-config` skill). Delete `test_lessons_stay_a_digest.py` once the file is short.
- [x] **W1.14** Move `tasks/audit-2026-08-16/` (49k lines), the dated `review-*`, `story-audit-*`,
      `live-test*`, `paperclip-*` and concept files to `docs/archive/tasks/` (or delete; git keeps
      them). `tasks/` keeps `README.md`, `todo.md`, `lessons.md`.
- [x] **W1.15** Merge `docs/planning/DEFERRED.md` into `BACKLOG.md` as a `Deferred` section, **one
      line per item** (what / why not now / trigger). Collapse the 496-line BACKLOG to ≤2 lines per row.
      Keep one register test (`test_backlog_register.py`), delete `test_deferred_register.py`.

### Track E — Meta-tests
- [x] **W1.16** Delete the tests that police prose: `test_claude_md_figures`, `test_dead_vocabulary`,
      `test_docstring_symbols`, `test_docstring_paths`, `test_declines_carry_a_trigger` (replaced by
      W1.7's template check), `test_literature_index_decline`, `test_deferred_register`,
      `test_lessons_stay_a_digest`. In mcp: the 14 CLAUDE.md-prose tests in `test_fleet.py`.
- [x] **W1.17** **Keep** and simplify: `test_layering`, `test_third_party_layering`,
      `test_upstream_surface` (until W5 shrinks it), `test_sibling_manifest_agreement` (until W2
      replaces it), `test_repo_map` (directories and READMEs only, no prose), `test_context_floor`,
      `test_decision_log` (reduced per W1.6).
- [x] **W1.18** Split mcp `tests/test_fleet.py` (5.2k lines) by concern: layout, deploy shape,
      egress, auth, manifests.

### Track F — Make and CI
- [x] **W1.19** Collapse the 79 Make targets: keep the gate (`lint type test check cov`), `ci`,
      the validators, `up/down/chat/connectors/db-migrate`, and move the live/bench/storm harnesses
      behind one `make live-<x>` family. `make help` grouped by section.
- [x] **W1.20** Moved to **W3.10–W3.12** (owner decision 2026-10-08: the gate goes parallel). W1
      delivered the private database for the retention test and the per-worker pool cap.

Exit (measured against W0):
- CLAUDE.md ≤150 lines in each repo.
- `src/` prose ratio ≤30% (from 59%).
- `tasks/` holds 3 files.
- Markdown outside `docs/decisions` and `docs/archive` down ≥60%.
- Serial suite time unchanged or faster; gate time ≤10 min.
- Zero behaviour change: W0 turn benchmark within noise.

Rollback: every PR is docs/tests only; revert per PR.

---

## W2 — One owner per contract

**Goal.** End copy-and-test between repos. Each contract has exactly one owner, a version, and
generated consumers. Today: 8 `connector.yaml` files are duplicated and already 29–126 lines apart,
the agreement test skips without a sibling checkout, and the UI hand-mirrors 3.4k lines of types.

Entry: W0.

### Track A — Connector contract (mcp owns, core consumes)
- [ ] **W2.1 ADR** (part of W0.5): the fleet is the single owner of every manifest it serves; the
      artifact is a versioned package. The options are a Python wheel `chemclaw-contracts`, an OCI
      artifact beside the image, or a git submodule. **Recommendation:** a wheel built from
      `Chemclaw3-mcp/manifests/`, because both consumers are Python and a wheel pins by version in
      `uv.lock`.
- [ ] **W2.2** Add `contract_version: <semver>` to the `connector.yaml` schema (core's manifest model
      and mcp's). Bump the minor version on an additive tool change and the major on a
      removal/rename/argument change. Each server's `/healthz` reports it.
- [ ] **W2.3** `Chemclaw3-mcp/packages/chemclaw_contracts/`: ships `manifests/<name>/connector.yaml`
      plus, for backend servers, the pydantic request/response models (used in W6). Published by the
      fleet CI on tag.
- [ ] **W2.4** Core: delete the 8 manifest-only bundle copies (`chem kinetics props rxnpredict safety
      suitability thermalsafety unitops`) and resolve them from the installed contracts package.
      **Keep** each bundle's `skills/` directory in core (judgment stays layer 3); the bundle dir then
      holds only `skills/` plus a one-line pointer.
- [ ] **W2.5** Session open compares the manifest's `contract_version` with the server's
      `/healthz`. A major mismatch refuses the connector by name (`capability_degraded`), a minor one
      warns.
- [ ] **W2.6** Make the name collision on `CHEMCLAW_CONNECTORS_DIR` a startup **error**, not
      first-wins.
- [ ] **W2.7** Replace `test_sibling_manifest_agreement.py` (core) and `test_consumer_agreement.py`
      (mcp) with a **required** CI job in mcp that installs the built contracts wheel into core's
      test env and runs core's validators (`connector-validate`, `skill-validate`). It never skips.

### Track B — API and event contract (core owns, UI consumes)
- [ ] **W2.8** Core: model the SSE event union as pydantic models (`api/events.py` is already the
      source) and publish it in OpenAPI via a schema-only endpoint or `components.schemas`. Add the
      `/openapi.json` export to `make` and commit the generated `schema/api/openapi.json`, so a diff
      shows up in review.
- [ ] **W2.9** UI: generate the TS types (`openapi-typescript`) and the valibot schemas from that file
      in `shared/generated/`. Replace the hand-written `shared/events.ts` and the request/response
      types in `src/api/client.ts`. Keep `client.ts` as the thin fetch layer only.
- [ ] **W2.10** UI CI: regenerate and fail on diff against core's pinned `openapi.json` (pinned by core
      version, not `main`). Retire `tests/backendContract.test.ts` (which parses a Python file) and
      `scripts/check-openapi.mjs`'s live-service mode.
- [ ] **W2.11** Close UI ISSUES.md #14 (the event-union drift: `capability_degraded`, `tool_failed`,
      `job_failed`) by construction.

### Track C — Calc wire (prepares W6)
- [ ] **W2.12** Move `servers/calc/tool-surface.json` and the argument dicts hard-coded in core's
      `connectors/calc/compose.py` and `remote.py` into typed models in `chemclaw_contracts`. Core
      imports them, and the fake in `tests/calc_server_fake.py` is built from the same models.

### Track D — Model-facing text, behind an evaluation (owner decision 2026-10-08)

What the model reads is a prompt, so W1 left it untouched: agent-tool docstrings, pydantic/
TypedDict/Enum descriptions that reach a tool or response schema, the fleet's `@server.tool`
descriptions and output schemas, `ModelProse` constants, the system prompt's instruction blocks
and `SKILL.md` bodies. Much of it still carries history. It gets the same diet, but **a batch ships
only when the evaluation says it is not worse** (`D-2026-10-08-model-facing-text-changes-ship-
behind-an-evaluation`).

- [ ] **W2.13 Inventory and measurement.** Add a `make model-text` dump: every model-facing string
      in both repos, with its owner (file:symbol), its token count, and whether it is in every
      request's prefix (tool schemas and prompt blocks) or loaded on demand (skills, results). Commit
      it as `schema/model-text/inventory.json`. This also gives `tests/test_context_floor.py`
      measured inputs rather than a hand-kept ceiling.
- [ ] **W2.14 Evaluation protocol, fixed before any edit.**
      - **Offline, every batch, in CI:** `make eval-strict` and `make eval-baseline-check` (scripted
        model, deterministic), plus `test_prose_contract` (the text names only tools that exist).
      - **Live, every batch:** `make live-ab` with the current text as the control arm and the
        rewrite as the candidate. Use the same probe corpus, a real gateway, and at least 3 runs per
        arm. The noise floor is the control arm's own run-to-run spread, measured first, as
        `D-2026-09-27` did.
      - **Metrics:** tool-selection accuracy, argument validity (first-call schema errors),
        refusal correctness (the probes that must refuse), task success as graded by the probe
        rubric, tokens per turn, and turn cost.
      - **Ship rule:** no metric worse than the control by more than its noise floor, and the
        prefix shrinks. Otherwise the batch is reverted, or reworded and re-run.
      - **Entry condition:** a gateway credential (`CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL`,
        `CHEMCLAW_LLM_API_KEY`) in the environment running the live arm. Without it, no batch ships.
- [ ] **W2.15 Rewrite in batches.** One batch = one tool family: one fleet server, or one core
      `*_tools.py` module, plus its schema classes. Same standard as the W1 docstrings: units, what
      the tool does and is **not**, what it refuses, and when to use it rather than a neighbour. No
      history. A fleet batch bumps that connector's `contract_version` minor (W2.2), because
      descriptions are part of the contract. Prompt blocks and `SKILL.md` bodies are the last batch,
      each block on its own.
- [ ] **W2.16 Ratchet.** After each shipped batch, lower `CEILINGS`/`PREFIX_BOUND`
      (`tests/test_context_floor.py`) to the measured prefix, so the saving goes to the thread
      budget rather than disappearing. The derived defaults in `core/config/agent.py` follow
      automatically.
- [ ] **W2.17 Record the results.** For each batch, the eval table (control and candidate, mean and
      spread per metric) goes in the PR description, and a one-line summary goes in this file.

### Track E — Guard rails (owner decision 2026-10-08)
- [ ] **W2.18 Git-safety hook** (`.claude/hooks/block_destructive_git.py`; the owner authorised
      the change explicitly):
      - Block `git checkout -f`/`--force`, `git switch --discard-changes`/`-f`, and destructive
        verbs wrapped in `bash -c`/`sh -c` (recurse into the quoted string).
      - Stop over-blocking: allow `git stash pop`/`apply`, resolve paths against `git -C <dir>`,
        and do not split heredoc bodies.
      - Add `tests/test_destructive_git_hook.py`, which drives the script through subprocess with
        JSON payloads: one case per blocked verb and per allowed look-alike.

Exit:
- `grep -r "connector.yaml" Chemclaw3/src/chemclaw/connectors` finds only the bundles core serves
  (`bo calc molfp rxnfp results`).
- The UI has zero hand-written backend types.
- A deliberate breaking change in either repo fails the other's CI on the PR, never later.
- Every model-facing text batch that shipped has an eval table showing it is not worse than the
  control on any metric, and the per-request prefix is smaller than at W0 (73,450-token ceiling).
- The hook's test file covers every blocked verb and every allowed look-alike.

Rollback: a text batch is one PR per family, reverted on its own. W2.4 is a revert of one PR. The contracts package stays (additive).

---

## W3 — Horizontal scale foundations

**Goal.** No correctness or limit depends on a single process or pod. Today the rate limiter, the
concurrent-turn cap, calc single-flight, `Session.state`, several fire-and-forget writes and the
background worker are all per-process or singleton.

Entry: W0. Can run in parallel with W2. W3.10 starts after W3.2–W3.7, because those change the
shared state the parallel failures come from.

- [ ] **W3.1 ADR** (in W0.5): the shared coordination substrate is **Postgres only (decided)** —
      advisory locks, `SKIP LOCKED`, a small counters table; no Redis. It is already required, already pooled and already backed up, and the rates involved
      (turns/s, not requests/ms) are well within it. Revisit when the limiter needs more than
      ~1k decisions/s.
- [ ] **W3.2 Shared rate limiter and concurrency cap.** Replace the in-memory `BoundedLru` limiter and
      the per-process turn cap with Postgres-backed token buckets (one row per actor, updated in a
      single `UPDATE … RETURNING`) and a `turn_leases` table (lease with TTL and heartbeat). Then
      `maxReplicas` no longer multiplies the limit.
- [ ] **W3.3 Cross-process calc single-flight.** `calculation_results` gets a `pending` state:
      claim with `INSERT … ON CONFLICT DO NOTHING` and a lease, and losers wait on
      `LISTEN/NOTIFY` (or poll with backoff) for the winner's row. Keep the in-process future as the
      fast path. Close the `DEFERRED.md` row. Test: 2 processes × 8 concurrent misses → 1 compute.
- [ ] **W3.4 No fire-and-forget writes.** The `_PENDING` task sets in `api/budget.py`,
      `agent/turn_cost.py` and `agent/plan_gate.py`, and `_PENDING_SETTLES` in `api/runner.py`: write
      synchronously in the turn's final step, or through a transactional outbox (one `outbox` table
      drained by the background worker). Test: kill -9 mid-turn, then assert the cost and transcript
      rows exist.
- [ ] **W3.5 Scale the background worker.** Remove `workers.background.replicas: 1`. Each job that
      really must be single-instance (retention, schedule bootstrap) takes a Postgres advisory lock
      or becomes a Temporal Schedule (Temporal already guarantees one run). Make the reindex
      idempotent and partitioned. Close the BACKLOG row. Set the PDB to `minAvailable: 1` with 2
      replicas.
- [ ] **W3.6 Turns survive their pod.** A turn's execution state is already in the checkpointer. Add
      a `turn_leases` heartbeat (W3.2). On lease expiry another replica marks the turn
      `interrupted-resumable`, and the next client attach resumes from the last checkpoint instead of
      ending it. Then simplify `turn_relay.py` / `detach.py` / `turn_remotes.py` / `session_queue.py`
      (1.5k LOC): with leases and `LISTEN/NOTIFY` for the event fan-out, the polling relay can go.
- [ ] **W3.7 `Session.state` out of process memory**: into the checkpointer state, or deleted if it
      only caches what the checkpointer holds.
- [ ] **W3.8 Pool budget.** Give every process one pool per role (app, checkpointer) instead of per
      (event loop, dsn, options, size), and an alert on
      `sum(chemclaw_pg_pool_max_size) > max_connections * 0.8`. Document PgBouncer (transaction mode)
      as the deployment default for more than 3 replicas, with the checkpointer's autocommit pool on
      session mode.
- [ ] **W3.9 Multi-replica test lane.** A `make live-replicas` (or a kind lane) that runs 3 service
      replicas plus 2 background workers and asserts: limits hold globally, a killed pod's turn
      resumes, and one calc miss computes once.
- [ ] **W3.10 Parallel gate: collect the failure set.** Run `make cov PYTEST_WORKERS=4` 10 times
      on an otherwise idle runner (CI's runner class), after the W3.2–W3.7 changes have landed.
      Record every test that fails in any run, with its failure. Each one is a concurrency defect:
      shared state between xdist workers, timing assumptions, or a shared database horizon (the
      retention VACUUM case is the worked example). Among them is
      `test_context_budget::test_a_burst_of_cold_prefix_measurements_leaves_the_loop_schedulable`.
- [ ] **W3.11 Root-cause each one.** Isolate the state per worker: schema, database, Temporal
      namespace and task queue, temp directories, ports. Replace wall-clock sleeps with events, or
      fix the product code when the defect is real. **Never skip, retry-decorate or quarantine.** A
      test that is in principle about global state (one Postgres server's settings) gets an
      `xdist_group` so it runs on one worker, and the test file argues why.
- [ ] **W3.12 Flip the gate.** Make `PYTEST_WORKERS ?= 4` the default for `test` and `cov`, and run
      CI the same way. Keep a nightly serial `make cov` as a cross-check, which reports a failure that
      only shows up serially. Write `D-2026-10-08-the-test-gate-runs-in-parallel`'s measurement into
      its PR. Update CLAUDE.md's command line and `CURRENT.md`.

Exit:
- `grep` finds no module-level mutable dict, set or LRU that holds a limit or correctness state,
  except caches that can be recomputed.
- The W3.9 lane passes.
- The background worker runs with 2 replicas.
- 10 consecutive parallel `make cov` runs are green, and the gate's wall time is ≤10 min (it was
  18:13 serial).

Rollback: each item is behind a setting (`*_backend: memory|postgres`) for one release; the default
flips once the lane is green.

---

## W4 — Knowledge graph in Postgres

**Goal.** Postgres becomes the knowledge graph's **only** store; git leaves the knowledge path
entirely (decided). This removes the cluster-wide git lock (~300 ms/note, ≈3 notes/s ceiling), the per-pod
NetworkX copy (~5 kB/note/pod), the per-pod clone plus `reset --hard` sidecar, and the
`note_index` second copy.

Entry: W3.1 (shared substrate), W0 KG baselines. W1.10's `kg/` pass done (smaller files to move).

- [ ] **W4.1 ADR** (in W0.5): Postgres is the only store for notes (decided; options weighed were
      git + batching, Postgres + git mirror, Postgres only, a graph database). Supersedes the
      git half of the layer-4 description; "Markdown with frontmatter" survives as the note **format**
      (body + JSONB frontmatter), not as files.
- [ ] **W4.2 Schema** (`infra/sql/122_kg_notes.sql`):
  - `kg_notes` (id, type, frontmatter JSONB, body text, created_by, valid_from, valid_to,
    revision, content_sha, created_at);
  - `kg_note_revisions` (append-only history, which replaces git log as the audit trail);
  - `kg_edges` (src, rel, dst, valid_from, valid_to);
  - indexes for type, rel, dst and the bi-temporal queries.

  `note_index` (embeddings) gets a foreign key to `kg_notes` instead of being rebuilt from git.
- [ ] **W4.3 Store interface.** `kg/store.py` with `NoteStore` (`put`, `get`, `related`,
      `neighborhood`, `current_successor`, `search`) implemented by `PostgresNoteStore`, plus a
      `GitNoteStore` adapter over today's `graph.py`/`git_writer.py` for the transition. Every reader
      in the grep list (`agent/graph_tools`, `protocol_tools`, `retrieval/retrievers`,
      `vector_index`, `durable/digest`, `hypothesis_tournament`, `memory/*`, `evals/retrieval`,
      `cli/validate_kg`, …) moves to the interface first, with no behaviour change.
- [ ] **W4.4 `kg/record.py` stays the one write path** and keeps its order (dependencies → subject
      → retirements), now in **one transaction**. That gives the order a stronger guarantee than git
      gave it.
- [ ] **W4.5 Graph queries in SQL.** `related` and `current_successor` are indexed lookups;
      `neighborhood(hops)` is a recursive CTE with a hop limit. Keep NetworkX only for the analytics
      that need a whole-graph algorithm (`kg/analytics.py`, `hypotheses/`), loaded on demand in the
      background worker and never per request.
- [ ] **W4.6 Backfill and dual-write.** A `cli/kg_import` command loads the git corpus (42 notes
      today) into Postgres. Under setting `kg_store=dual`, writes go to both, reads come from git,
      and a nightly `kg-validate --compare` diffs the two.
- [ ] **W4.7 Flip reads** (`kg_store=postgres`). No mirror: git is not written after the flip.
- [ ] **W4.8 Human edits through the API.** `PUT /knowledge/notes/{id}` (privileged role for
      others' notes) writes a new revision through `kg/record.py`; a `cli/kg_export` dumps the
      corpus as Markdown on demand for offline review, and `cli/kg_import` is the one-time and
      disaster-recovery loader. The shipped seed corpus in `knowledge/` becomes import fixtures.
- [ ] **W4.9 Delete** `kg/git_writer.py`, `BatchingNoteWriter`, the per-pod clone, `deploy/knowledge-sync.sh`, the init container and sidecar,
      the advisory lock held across the push, the stat-fingerprint cache in `graph.py`,
      `knowledge_sync_age_seconds` and its alert. Update the chart values (`knowledge.*`).
- [ ] **W4.10 Validators.** `kg-validate` runs against the Postgres store (citation existence in one
      query). The pure checks in `kg/validate.py` stay pure.
- [ ] **W4.11 UI**: confirm that the read paths (note view, provenance links) go through the API
      only, and adjust if anything reads a git URL.

Exit (measured against W0):
- Write throughput ≥50 notes/s with 4 concurrent writers.
- Per-pod RSS independent of corpus size (±10 MB at 20k synthetic notes).
- No git process on the request path.
- `kg-validate --compare` is clean for 7 days before W4.9.

Rollback: until W4.9, flip `kg_store` back to `git`; dual-write keeps git current. After W4.9 the
rollback is a Postgres restore (W7.7), which is why W7.7's drill is pulled forward to run before W4.9.

---

## W5 — Agent core: deepagents via public seams, one mechanism per concern

**Goal.** Use `create_deep_agent` through its public seams instead of fighting it (32 middlewares,
64 pinned private upstream shapes), then merge the parallel mechanisms that successive redesigns left (spend 3.1k LOC, context 4.5k,
persistence 5.5k, authz 2.5k, skills 3.2k, plan 2.1k).

Entry: W0 baselines (turn latency, middleware count). W1.10 `agent/` prose pass done. W2.8 event
schema (so the UI cannot drift during the refactor).

### Track A — The builder: stay on deepagents, and make it carry everything (decided)

The decision is to **stay on off-the-shelf `deepagents`**. The self-built builder is challenged,
not adopted: for each workaround the question is "which public deepagents/LangChain seam does this,
and if none, can upstream take it?" The bar: deepagents must cover everything a self-built builder
could.

- [ ] **W5.1 Gap inventory.** For every workaround in `agent/` (and every entry in
      `tests/test_upstream_surface.py`), record against the **current** deepagents/LangChain
      release: (a) now covered by a public API → migrate; (b) coverable by a documented extension
      point (custom `BackendProtocol`, `SubAgent`/`CompiledSubAgent`, `AgentMiddleware`,
      `context_schema`) → migrate; (c) a real upstream gap → open an upstream issue/PR and keep a
      minimal shim with the upstream link. Known items:
  - filesystem `permissions=` reached via private `_permissions=` → a permission-enforcing
    `BackendProtocol` (as `skill_backend.py` already is) or upstream public parameter;
  - helpers built as bare `SubAgent` dicts with only `spec["middleware"]` (no audit/authz/plan
    gate) → pass `CompiledSubAgent`s compiled by `build_langgraph_agent`, so a helper always carries
    the chain;
  - `disabled_summarizer`, `ReloadingSkillsState` redeclaration, `.name` splicing, the
    `ModelCallLimitMiddleware` incompatibility, `task` returning `Command` → each classified (a)/(b)/(c).
- [ ] **W5.2 Upgrade deepagents/langchain/langgraph to latest** and migrate every (a)/(b) item. The
      upgrade is the first deliverable because each later release should then be a lockfile bump.
- [ ] **W5.3 Upstream the (c) items** (issues/PRs against `langchain-ai/deepagents`), each shim
      carrying its upstream link and a test that turns red when upstream fixes it (the pattern
      `test_upstream_surface.py` already uses for absences). Target: `test_upstream_surface.py`
      ≤500 lines; root middlewares ≤15 by removing first-party ones that duplicate upstream
      behaviour, not by replacing upstream ones.
- [ ] **W5.4 Build once per process, bind per turn.** Compile the deep agent once per (profile,
      bundle-set) and inject the turn's connector sessions through `runtime.context` (LangGraph
      `context_schema`) instead of closing over them. If deepagents cannot take per-turn tools that
      way, that is a (c) item for W5.3, and the per-turn build stays meanwhile.
- [ ] **W5.5 Import time.** Lazy-import heavy stacks (bofire/torch, rdkit) out of the API's import
      path. Target: cold import of `chemclaw.api.app` ≤5 s (from 16.6 s).

### Track B — One mechanism per concern
- [ ] **W5.6 Spend.** One `spend/` module holding:
  - one ledger: the `TurnTotal` channel inside the turn, persisted at turn end (W3.4);
  - one policy object with per-request, per-turn and per-actor/day limits;
  - one middleware.

  Merge `spend_cap`, `loop_cap`, `turn_cost`, `turn_cost_store`, `turn_usage`, `runner_usage` and
  `api/budget` plus `budget_store` (9 files → ≤3). `context_budget.py` stays a context concern
  (below), not a spend one.
- [ ] **W5.7 Context.** One `context/` module and one middleware with ordered strategies: cap a tool
      result at ingest (`tool_result_size`), clear old tool results, window the conversation, condense
      as the last resort. Merge `compaction`, `condense`, `tool_result_size`, `context_budget`; keep
      `tool_result_shape` as the shared result-rewriter. 4.5k → ≤2k LOC. The budget **derivation**
      stops reading test constants: `PREFIX_BOUND` becomes a measured value written to
      `data/` by a `make measure-prefix` target, and the test asserts the file is current.
- [ ] **W5.8 Persistence.** The checkpointer is the **only** conversation store. `session_messages`
      becomes a read projection rebuilt from checkpoints (or a SQL view if the shape allows).
      Migrate the remaining MAF-shaped rows once (`cli/migrate_transcripts`), then delete
      `message_migration.py` and the MAF compatibility in `session.py`. Merge `session_*` modules
      (members, fork, events, queue, store) behind one `sessions/` package.
- [ ] **W5.9 Authorization.** One `authz` decision function `decide(actor, tool, args, plan,
      dry_run) -> Allow | Refuse(reason)` and one middleware, replacing the four gates
      (`refuse_undeclared_writes`, `refuse_writes_on_dry_run`, `enforce_plan_approval`,
      `enforce_tool_authz`). The audit row records the single decision. `api/auth.py` keeps
      authentication only. Test: one table-driven test over the decision matrix.
- [ ] **W5.10 Skills.** One `skills/` package: a single `SkillStore` with three tiers (repo, org,
      mine) behind one interface, one access predicate, one manifest. 11 files → ≤4.
- [ ] **W5.11 Plan.** One `plan/` package (state, scope, approval store, gate). 6 files → ≤3.
- [ ] **W5.12 Rename leftovers.** `chemclaw_agent.py` → `profiles/surface.py` (it now only answers
      "what tools and instructions does a profile have"). Remove the "ported from MAF" ordering
      comments. `session.py` stops imitating MAF's `AgentSession`.
- [ ] **W5.13 Split `api/runner.py`** (3k LOC, `run_turn` ≈570 lines) into stages: `open_surface`,
      `stream`, `resume_on_jobs`, `verify`, `settle`. Each stage is a function with its own test,
      and `run_turn` is the 30-line pipeline.
- [ ] **W5.14 Delegation re-check.** After the upgrade, re-run `make live-delegation`
      (`D-2026-09-27`). If it still does not pay, ship `agent_helper_roster` **off** by default (its
      surface stays available).

Exit (against W0):
- Root middlewares ≤15.
- `agent/` LOC −35% (prose excluded).
- `test_upstream_surface.py` ≤500 lines.
- TTFT and turn latency equal or better.
- Cold import ≤5 s.
- No test removed without its behaviour being covered by a new one.

Rollback: the deepagents upgrade is a lockfile revert; each Track B item is an internal refactor
behind unchanged public tools and events (the W2.8 schema pins the events).

---

## W6 — Backend RPC, fleet delivery, release unit, CI

**Goal.** Use plain RPC where MCP buys nothing, deploy the fleet the way core deploys, and give the
three repos one release unit.

Entry: W2 (contracts package holds the calc and rxnlabel models).

### Track A — Backend RPC (mcp + core)
- [ ] **W6.1 ADR** (in W0.5): backends (`mount: backend`: `calc`, `rxnlabel`) speak typed HTTP/JSON,
      not MCP. Options: HTTP/JSON (pydantic), gRPC, keep MCP. Recommendation: HTTP/JSON. It needs
      no new toolchain, and `connector_app` already owns auth, headers, metrics and egress.
- [ ] **W6.2 mcp:** `mcp_server_kit` gains `rpc_routes(models)`: `POST /rpc/<op>` with bearer auth,
      `X-Chemclaw-*` read per request (no session-context trick), the same metrics, body cap and
      error sanitising. `calc` and `rxnlabel` serve both MCP and RPC for one release.
- [ ] **W6.3 core:** `connectors/calc/remote.py` and the rxnlabel client move to a pooled `httpx`
      client against `/rpc/*` behind `calc_transport=rpc|mcp`. Delete the per-call MCP session, the
      `cancel_on_timeout` monkeypatch, and the `open_session` path for backends. Measure the
      `calculation_key` round trip against W0.2.
- [ ] **W6.4** Next release: remove MCP from `calc` and `rxnlabel` and drop
      `manifests-internal/` (a backend no longer needs a manifest, because the contracts package
      describes it).
- [ ] **W6.5 Agent-facing MCP session cost.** Reuse a connector's MCP session across the turns of
      one **session** where identity allows (an identity-scoped pool keyed by actor and connector,
      with a TTL), instead of `initialize + list_tools` per connector per turn. Cache `list_tools` per
      `contract_version`. Measure TTFT with 8 bound bundles.

### Track B — Fleet delivery
- [ ] **W6.6** `Chemclaw3-mcp/deploy/helm/chemclaw-fleet`: one library chart (Deployment, Service,
      HPA, PDB, NetworkPolicy, ServiceMonitor, optional KEDA) and a `values.yaml` table of
      ~15 lines per server. It replaces 83 files and 4.2k lines. The rationale comments live once,
      in the templates.
- [ ] **W6.7** Move `tests/test_deploy_shape.py` to run against `helm template` output (the same
      assertions: egress deny + selector, liveness ≠ readiness, ports, ingress peers).
- [ ] **W6.8** Core chart depends on the fleet chart as a subchart. `connectors.<name>.url`, port and
      token secret are derived from one value per server, so they are declared once.
- [ ] **W6.9** UI: replace `deploy/openshift/*.yaml` with a small chart (or a subchart of the
      umbrella).
- [ ] **W6.10 Umbrella release.** A `chemclaw-platform` chart (or a `releases/<env>.yaml` of image
      digests plus chart versions) pins core, fleet and UI together. Semver applies to the API
      (`openapi.json`), the event schema and `chemclaw-contracts`.

### Track C — CI
- [ ] **W6.11** One CI system for gating (GitHub Actions) and one for delivery (Jenkins:
      build/publish/deploy only), stated in each repo. Remove the opt-in `RUN_GATE` duplication, and
      make Jenkins' `Preflight` read the GitHub check status for the commit it builds.
- [ ] **W6.12** Pin the shared Jenkins library (core's `build_and_push`) by tag in the fleet and UI.
      Pin the UI's core checkout by release tag, never `main`.
- [ ] **W6.13 Release gate.** The four-repo e2e (`infra/live/e2e-full-stack`) runs against the
      **pinned digests** of a release file, from published images (a compose profile), with no
      sibling checkouts.

Exit:
- `calc` cache-hit round trip ≤10 ms in-cluster (W0.2 baseline for comparison).
- Fleet YAML ≤600 lines total.
- One file says what is deployed in an environment.
- The e2e runs from images alone.

Rollback: `calc_transport=mcp` until W6.4; the chart migration is per environment.

---

## W7 — Missing capabilities

**Goal.** Close the gaps the review found, now that the foundations (W3, W4, W6) exist.

Entry: W3, W4 (lineage and tenancy touch KG tables), W6 (chart for backup jobs).

### Track A — Retrieval quality
- [ ] **W7.1** Make a semantic embedding provider the shipped default (`openai_compatible` through
      the gateway, or a local model baked into an image for the air-gapped case). `hash` becomes an
      explicit dev/test choice, and the chart **refuses to render** a release with `hash` unless
      `allowNonSemanticEmbeddings: true`.
- [ ] **W7.2** Make the vector width configurable: a migration that creates the column from a setting
      (`embedding_dimensions`), and a re-embed job (Temporal, resumable, batched) for a model change.
      Record the model and dimensions per row so mixed corpora are detected.
- [ ] **W7.3** Retrieval eval in CI: the existing `evals/retrieval` harness on the committed corpus
      with a recall@k floor, run with the real provider in the live lane.

### Track B — Multi-tenancy
- [ ] **W7.4 ADR** (in W0.5): the tenancy model is `tenant_id` + Postgres RLS (decided; options
      weighed were deployment-per-tenant, RLS, schema-per-tenant). Revisit if a tenant needs separate
      encryption keys.
- [ ] **W7.5** `tenant_id` on every table (migration with a default tenant), RLS policies,
      the tenant set per connection from the authenticated principal (`SET app.tenant`), the KG
      (`kg_notes`) and the vector stores included, and a cross-tenant leak test over every API route.
- [ ] **W7.6** A startup guard that two releases do not share a database without
      tenancy (the gap CLAUDE.md says "no chart guard can check" — a startup check of a `deployment_id`
      row in the database can).

### Track C — Operability
- [ ] **W7.7 Backup and PITR.** Document and template it: a CloudNativePG `Cluster` (or the site's
      operator) with WAL archiving and scheduled base backups, and a **restore drill** Make target that
      restores to a scratch namespace and runs `kg-validate` plus row counts. Include Temporal's own DB
      and the git mirror.
- [ ] **W7.8 Lineage.** One `lineage` table, or a `provenance` JSONB on each record, joining
      KG note ↔ `calculation_results.key` ↔ published sink record id ↔ turn/session id. Written by
      `kg/record.py`, `cached_compute` and `publish/`; one API route answers "where did this number
      come from".
- [ ] **W7.9 Config profiles.** Group the 540 settings into documented profiles (`dev`, `pilot`,
      `production`, `airgapped`) selected by one `CHEMCLAW_PROFILE`, so a deployment overrides tens
      of values, not hundreds. Delete settings that no deployment has ever changed (grep chart values,
      `.env.example` and the docs). Target ≤200 fields. Split Helm `values.yaml` (2.2k lines) the
      same way.
- [ ] **W7.10 Retry policy.** An explicit `RetryPolicy` per activity class (calc, I/O, publish) in
      one module, instead of SDK defaults at 68 of 76 activities.

### Track D — UI auth
- [ ] **W7.11 Token handler in the BFF.** The `server/` BFF does the auth-code + PKCE exchange, holds
      tokens server-side, and gives the browser an `HttpOnly; Secure; SameSite=Strict` session cookie.
      Requests to the backend get the bearer injected by the BFF. This closes UI ISSUES.md #8
      (tokens in the browser; silent refresh relies on third-party cookies). Needs a small session
      store (Postgres or the BFF's own). MSAL stays only for the login redirect, or goes.
- [ ] **W7.12** Close the remaining UI issues that are architectural: #12 (a job's ending dies with
      the tab; use server-side job subscription, which the W3.6 fan-out provides) and #22
      (shared-session turn queueing; use W3.2 leases).

Exit:
- The retrieval eval passes on the semantic provider.
- A tenancy decision is implemented and leak-tested.
- A restore drill has run green.
- One query answers lineage.
- No token in browser storage.

Rollback: per item. W7.5 ships with a single default tenant, so it is inert until a second tenant
exists.

---

## W8 — Decomposition, dead-code sweep, final measurement

**Goal.** Clean up what the earlier waves leave, and prove the programme paid.

- [ ] **W8.1 Large files.** Split anything still over ~800 LOC (code, not prose) along
      responsibilities: `connectors/calc/compose.py` (3.4k), `durable/hypothesis_tournament.py`
      (2.5k), `publish/project.py` (2.3k), `durable/retention.py` (2.1k), `core/metrics.py` (2k,
      split per subsystem with one registry), `core/logging.py` (1.8k; redaction becomes its own
      module).
- [ ] **W8.2 CLI.** Move the live/storm/bench harnesses (`cli/live_storm.py` etc., much of `cli/`'s
      15k LOC) out of the product package into `tools/` or `infra/live/`, outside the image.
- [ ] **W8.3 Vestiges.** Delete the remaining prose references to removed systems (PR-gate 57,
      MAF 79, HPC 13, `reject_widening`, challenge panel, GxP) where they describe absence. Keep
      `publish/project.py::_dft` only if `calculation_results` still holds `dft` rows (query
      production; if none, delete).
- [ ] **W8.4 Dead code.** `vulture` plus coverage over the suite and the live lane. Delete what
      neither reaches, one package per PR.
- [ ] **W8.5 Final measurement.** Re-run W0.1/W0.2, commit
      `data/evals/baselines/architecture-<date>.json` and a short review section below: every exit
      criterion with before and after.
- [ ] **W8.6** Update `docs/decisions/CURRENT.md`, the three CLAUDE.md files and `ARCHITECTURE.md`
      to the end state. Close the programme issues.

Exit: the review table below is filled in, with every row measured.

---

## Cross-cutting rules for every wave

- **One repo, one PR, one concern.** No PR mixes a refactor with a behaviour change.
- **Gate:** `make lint type test` green **with Docker/Postgres/Temporal up**, and the skip count
  reported. In mcp, `make check`; in the UI, lint, vitest and Playwright.
- **Behaviour-neutral refactors prove it**: the W0 turn benchmark within noise, and the event
  stream for the 3 canned turns byte-identical (modulo ids and timestamps).
- **Feature flags have an owner and a removal item**, in the next wave at the latest.
- **Data migrations are forward-only and two-phase** (expand, migrate, contract over two releases),
  per the repo's existing no-down-path rule.
- **No new prose debt**: a docstring states what and why; the history goes in the commit.

## Risks

| Risk | Wave | Mitigation |
| --- | --- | --- |
| Knowledge-store cutover loses or diverges notes | W4 | Dual-write, nightly compare, 7 clean days before deleting git reads, git mirror kept forever. |
| A deepagents upgrade breaks a security property | W5 | Upgrade gated on the existing authz, subagent, skill and handoff tests; helpers passed as `CompiledSubAgent`s built by `build_langgraph_agent` always carry audit and authz, which is stricter than today. |
| The prose cut removes knowledge someone needed | W1 | History moves to commit messages and ADRs, never just deleted. `CURRENT.md` is reviewed by a human. |
| Contract versioning blocks releases | W2/W6 | Minor mismatch warns, only major refuses. The umbrella release pins all three. |
| Tenancy retrofit misses a table | W7 | Migration test that every table except an allowlist has `tenant_id` and an RLS policy. Leak test over all routes. |
| Parallel sessions collide | all | One issue per wave track as the claim. Tracks own disjoint files. |

## W0 + W1 review (2026-10-07)

Shipped as one PR per repo: Chemclaw3_ui#153 and Chemclaw3-mcp#161 (both merged), and this
repository's PR. Tracking issue #569.

**How it was kept behaviour-neutral.** All prose edits went through a tool that refuses any file whose
AST, with docstrings stripped, would change. Two classes of docstring are prompts or schemas rather
than prose, and both were restored to `main`'s text: agent-tool docstrings, and pydantic, TypedDict
and Enum class docstrings. The fleet reviewer caught this class. Every tool schema is byte-identical
to `main`: 75 here (agent tools and in-repo MCP servers) and 81 in the fleet.

Fresh-context reviewers checked every repository. In total, 4 here, 3 in the fleet and 1 in the UI
reported no code change, and their findings were fixed.

| Metric (this repo) | Before | After | Target | Met? |
| --- | --- | --- | --- | --- |
| CLAUDE.md lines | 532 | 149 | ≤150 | yes |
| `src/` prose share | 55.6% | 37.2% | ≤30% | no — tool/schema docstrings restored, 1–2 line docstrings kept |
| `src/` lines | 201,462 | 146,858 | — | −27% |
| Test lines | 253,130 | 217,070 | — | −14% |
| Markdown outside `docs/decisions` + `docs/archive` | 83,415 | 22,748 | −60% | yes (−73%) |
| `tasks/` files | 1,640 | 3 | 3 | yes |
| `lessons.md` lines | 1,924 | 77 (30 rules) | ≤30 rules | yes |
| Gate wall time | 18:13 serial | serial (unchanged) | ≤10 min | no — W1.20 open |

The Fleet's prose share went from 47.0% to 32.1%; CLAUDE.md there went from 671 to 150 lines. In the UI, Markdown went
from 6,008 to 934 lines and the comment share from 39.3% to 23.6%.

**Left open, on purpose. All three were decided by the owner on 2026-10-08 and are now planned:**
the parallel gate is W3.10–W3.12, the model-facing text is W2.13–W2.17, and the hook is W2.18.
- **W1.20.** The parallel-only flake in `test_context_budget` did not reproduce under load, so it
  was not fixed. The retention one is isolated in a private database. The gate stays serial, as
  `D-2026-09-13` says, until a parallel run is shown stable.
- **The `.claude/hooks/block_destructive_git.py` hook.** It misses `git checkout -f`,
  `git switch --discard-changes` and `bash -c` wrapping, and it over-blocks `git stash pop`. The
  permission classifier refused an agent's edit to it as self-modification, so a person makes that
  change.
- **Schema-class docstrings that still carry history.** Some pydantic model and tool docstrings
  still contain history. They are prompts, so shortening them is a behaviour change, to be made
  with an eval run in a later wave.

## Review (filled in at W8.5)

| Metric | Baseline (W0) | Target | Result |
| --- | --- | --- | --- |
| CLAUDE.md lines (core / mcp) | 532 / 671 | ≤150 / ≤150 | |
| `src/` prose ratio | 59% | ≤30% | |
| Gate wall time | 18:13 | ≤10 min | |
| Cold import `chemclaw.api.app` | 16.6 s | ≤5 s | |
| Per-turn graph build (steady) | 0.08–0.12 s | ≈0 | |
| Root middlewares | 32 | ≤15 | |
| KG write throughput | ≈3 notes/s | ≥50 notes/s | |
| KG RSS per pod at 20k notes | ≈100 MB | ≈0 (corpus-independent) | |
| Duplicated manifests | 8 | 0 | |
| Hand-written UI API types (lines) | ≈3.4k | 0 | |
| Fleet k8s YAML lines | 4.2k | ≤600 | |
| `calc` cache-hit round trip | W0.2 | ≤10 ms | |
| Settings fields | 540 | ≤200 | |
| Background worker replicas | 1 (pinned) | ≥2 | |
| Per-process limits/correctness state | 6+ | 0 | |
