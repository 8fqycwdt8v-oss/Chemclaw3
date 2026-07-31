# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phases 0–5b of the plan are **implemented and CHECKMATE-reviewed**: toolchain + config,
the MAF+Temporal spine, fast calculators (xTB/pKa/solubility) with the Postgres
calculation cache, BoFire BO campaigns, the knowledge graph + PR-gate, the eval/metric
layer, ECFP4/DRFP fingerprint search, ELN ingestion, the memory layers, and the report
harness.

The **foundation build F0–F7** (the real target stack: OpenShift + HPC/Nextflow + an internal
OpenAI-compatible LLM, Entra identity system-wide) is **implemented for everything verifiable
offline**, each phase ADR'd (D-039…D-050) and green under `make lint type test`:

- **F0** LLM provider seam (generic credential, not Entra) · **F1** MAF harness (plan/execute) ·
  **F2** FastAPI+SSE front door · **F3** durable Postgres sessions + job→session push-back.
- **F4** Entra identity/RBAC: front-door OIDC, one authorization gate, `require_actor` reject-if-absent
  core rule, workload identity federation, OBO (dormant), Temporal-mTLS + HPC identity bridges.
- **F5** real Nextflow (Seqera/Tower) launcher behind the QM activities (mock kept for CI).
- **F6** OpenShift delivery: one rootless image, Helm chart, CI, three-secret model, Temporal self-hosted.
- **F7** the generic `DataSource` seam (`chemclaw.ingest.sources`) — ELN re-hosted unchanged; a new source is one
  `ingest/sources/<name>/datasource.yaml` folder plus its name in `CHEMCLAW_DATA_SOURCES`, with **zero**
  core edits (D-120). First live connector (deferred): a custom Snowflake ELN source.

**Live edges remain open** (need a real Entra tenant / Temporal broker / OpenShift cluster): real token
validation, federation/OBO exchanges, live cluster durability + `helm`/`kubeconform` render. See
`docs/planning/BACKLOG.md` for the exact list.

**On the design documents below: they are historical, not current.** `docs/reference/architektur.md` is
pre-implementation design and contains **zero** references to connectors — the seam that now carries
every tool, job and skill (D-118) — so it describes a system that no longer exists in its details
while remaining right about the four layers. Read it for intent; read `docs/decisions/`, the package
READMEs and `docs/guides/runbook.md` for what is true today.

- `docs/reference/architektur.md` — the four-layer architecture (§6 = the real OpenShift/Nextflow/internal-LLM
  deployment; §7/§8 = Entra durchgängig).
- `docs/planning/implementation-plan.md` — the original build order; `docs/planning/implementation-tickets.md` — the
  F0–F9 ticket backlog with per-phase status.

## Related repositories

This repo is the backend/orchestration core. Two companion repos complete the system and are
developed separately:

- [`8fqycwdt8v-oss/Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui) — the ChemClaw3
  frontend.
- [`8fqycwdt8v-oss/Chemclaw3_mock`](https://github.com/8fqycwdt8v-oss/Chemclaw3_mock) — a mock
  server that stands in for external MCP tools and data sources, plus a mock HOC, so the system
  can be live-tested end-to-end without real integrations.

If a task requires changing or fixing code that lives in `Chemclaw3_ui` or `Chemclaw3_mock`
(not this repo), add that repo to the session (`add_repo`) and open a PR directly against it —
do not proxy the change through this repo, and do not just describe the fix here and stop.
Each repo gets its own branch/commit/PR, scoped to that repo's own conventions. Only pause to
ask first if the required change is destructive, ambiguous, or outside what was asked.

## Architecture (the one thing to internalize)

`ARCHITECTURE.md` maps every directory to its layer and explains the two name pairs that look
like duplicates and are not (`science/calc/` vs `connectors/calc/`; `skills/` vs
`connectors/*/skills/`). Adding a top-level directory or a subpackage means **adding a row there
and giving the directory a `README.md`** — `tests/test_repo_map.py` fails otherwise (D-155).

Three rules the tree is arranged around, each enforced by a test rather than asked for:

- **`src/` is all the code.** Everything beside it is data, configuration or documents.
- **Capability code lives in a connector bundle or in `science/`, nowhere else.** The engine is
  pure computation; the bundle is its durable-job and MCP wrapper. They are a pair, not a
  duplication, and merging them would put Temporal imports inside the physics.
- **`data/` holds every corpus the code reads at runtime** — except `knowledge/` and `skills/`,
  which stay at the root because they are architecture layers 4 and 3, not configuration.

Four layers, each with a single responsibility. **Never merge their concerns.**

1. **MAF** (Microsoft Agent Framework) — conversation orchestration + short reasoning steps.
2. **Temporal** — durable execution of long/expensive jobs. Early focus is fast local compute
   (xTB/GFN2, ML predictors) + BoFire BO; **HPC/DFT is deferred** (D-010). Two task queues:
   `hpc-jobs` (few, heavy workers) and `background-jobs` (light workers: sync, re-index, reports).
   Every result is persisted once via the calculation store — never recomputed (D-011).
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data + relations).

Durability lives **only** in Temporal, never in MAF. Skills hold judgment; MCP servers hold
capability (deterministic tools). Anything agent-generated enters the graph via a **PR-gate**
(human validates before merge) — this is the GxP "AI proposes, human signs off" line, reused
everywhere (job results, reports, distilled playbooks). See `docs/reference/architektur.md` §4, §9, §12.

## Commands

The toolchain is scaffolded and `make help` (the default goal) lists all 23 targets. Use them rather than raw
invocations — CI runs exactly these, so a green `make` locally means a green CI.

- **The gate**: `make lint` (ruff lint + format) · `make type` (`mypy --strict`, every first-party
  package) · `make test` (pytest) · `make check` runs all three · `make cov` adds the coverage floor.
- **The validators**, each guarding a declaration against the live surface: `kg-validate`,
  `skill-validate`, `connector-validate`, `template-validate`, `prose-validate`, `eln-validate`,
  `helm-validate`, `audit-verify`. (`datasource-validate` joins them with D-120.)
- **Running things**: `make up` (docker-compose: Temporal + Postgres/pgvector) · `make connectors`
  (every enabled connector in one dev process) · `make chat` · `make db-migrate`.
- Single test: `pytest path/to/test_file.py::test_name` or `pytest -k "name substring"`.

A step is done only when its acceptance check passes **and** `make lint type test` is green.

## Workflow (how to work a task)

**Plan first.** For any non-trivial task (3+ steps or an architectural decision), enter plan
mode before touching code; simple, obvious fixes skip this. Write the plan to `tasks/todo.md`
as checkable items, write detailed specs upfront to kill ambiguity, and check in before
implementing. Mark items done as you go and give a one-line summary at each step. Plan
verification too, not just building. If something goes sideways, **stop and re-plan** — never
keep pushing a failing approach. Close the loop with a short review section in `tasks/todo.md`.

**Verify before done.** Never mark a task complete without proving it works: run the tests,
check the logs, demonstrate correctness. Where it clarifies things, diff behavior between the
base and your change. The bar is "would a staff engineer approve this?" — if not, it is not done.

**Fix bugs autonomously.** Given a bug report, failing CI, or an error/log, just fix it:
find the root cause and resolve it without asking for hand-holding or step-by-step direction.

**Ship automatically.** Once a task is fully done and verified (tests pass, `make lint type
test` green where applicable), do not stop at a pushed branch and wait for a go-ahead: open the
PR, merge it directly to `main` yourself, and delete the branch once the PR is closed. This
applies here and in the companion repos (`Chemclaw3_ui`, `Chemclaw3_mock`) — each repo's change
gets its own PR, auto-merged the same way. Skip the auto-merge only if CI is red, the change is
destructive/ambiguous, or the user asked to review before merge for this task.

## Code quality (non-negotiable)

- **Perfection over speed**: when unsure, ask — do not guess.
- **Demand elegance (balanced)**: for non-trivial changes, pause and ask "is there a more
  elegant way?" and challenge your own work before presenting it. If a fix feels hacky,
  redo it as the elegant solution knowing everything you now know. Skip this for simple,
  obvious fixes — don't over-engineer.
- **Root cause, not band-aid**: no temporary patches; fix the underlying cause. Keep changes
  minimal and focused — touch only what the task needs, and don't introduce new bugs.
- **KISS**: simplest working solution; no over-engineering. No abstraction without a second
  real caller (Rule of Three); an abstraction with one caller gets inlined.
- **DRY**: no duplicate logic — extract shared code. The PR-gate and the retriever interface
  are single reusable pieces, not copy-paste.
- **No boilerplate**: only code that is actually used. Delete dead params, empty interfaces,
  and "for later" stubs on sight.
- **Docstrings on every module/function**: state the *purpose* and the *why*, not just the what.
  Every public function is fully type-annotated.
- **Small, single-responsibility, clearly named functions.**
- **After every change**: run existing tests, add tests where they prove behavior (not mocks).
- **Config, never magic numbers**: every URL, path, threshold, timeout, model name comes from
  the one `pydantic-settings` config, ENV-overridable.

Run the plan's **Quality-Gate ("Checkmate")** checklist (G1–G7, see `docs/planning/implementation-plan.md`)
after each cluster of steps before moving on.

## Persistent knowledge (read at session start, update at session end)

- `docs/planning/BACKLOG.md` — prioritized open action items.
- `docs/planning/DEFERRED.md` — consciously postponed items **with the reason they are not now**.
- `docs/decisions/` — architecture decisions with rationale, one file per ADR (`D-NNN-<slug>.md`).
  Never edit a merged ADR; a decision that has changed gets a new ADR that supersedes it.
- `docs/decisions/README.md` — the `D-NNN` allocation ledger, one row per number. **Every session that
  writes an ADR must reserve its number here** (see below).
- `tasks/lessons.md` — self-improvement log. Review it at session start; after **any**
  correction from the user, add the pattern here and write a rule for yourself that prevents
  the same mistake. Iterate ruthlessly until the mistake rate drops.

Keep these current; they are the memory across sessions. For recurring patterns, prefer a
`.claude/skills/<name>/SKILL.md` over bloating this file.

### Allocating an ADR number

ADR numbers collided three times, each costing a renumber during a merge. The cause was structural,
not carelessness: several branches ran concurrently, all appending to the end of one `DECISIONS.md`,
each picking "highest I can see, plus one" — against its *own* branch, which cannot see the others.
So they picked the same number **and** conflicted on the same line, inside ninety lines of prose
where it was easy to miss.

D-147 removed the shared append point: one file per ADR. Two branches adding different ADRs now
touch disjoint files, and two branches claiming the same number collide on a **filename** — an
add/add conflict git reports loudly. The procedure below is what remains.

**1. Enumerate against `origin/main`, never against your branch.** Your branch's highest number is
stale the moment another branch merges.

```sh
git fetch origin main
# the highest number currently allocated (the ledger and the files must agree):
git show origin/main:docs/decisions/README.md | grep -oE '^\| \[?D-[0-9]+' | grep -oE 'D-[0-9]+' | sort -V | tail -1
git ls-tree --name-only origin/main docs/decisions/ | grep -oE 'D-[0-9]+' | sort -V | tail -1
```

Your number is that highest one **+ 1**. Locally, `ls docs/decisions/` is the whole record.

**2. Reserve it in your first commit, not your last.** Add the row to `docs/decisions/README.md` as
soon as you know you will write an ADR — before the ADR file exists. A number you have not yet
pushed is a number another session will take.

Mark such a row `| D-NNN | RESERVED — what it will be about |` and swap the marker for the real
title *and a link to the file* in the commit that adds the ADR. `tests/test_decision_log.py` exempts
`RESERVED` rows from "the ledger and the files name the same ADRs" while still counting them as
taken. Without that marker the two rules contradicted each other and the test won: `1f1f233`
reserved six numbers as instructed here, and `8f6a319` deleted five of them to get CI green.

**3. When it collides anyway, the branch merging *second* renumbers.** This is a rule, not a
judgement call, so two sessions never both wait or both move. Whoever is merging (you, if you hit
the conflict) takes the *new* free number — `git mv` the file to its new name, fix the `#` heading
inside it, move the ledger row, and fix every reference:

```sh
git grep -n 'D-0*<old>'   # docs/decisions/, its README, docs/planning/, code comments
```

Never drop, reorder or edit an already-merged ADR to resolve this; only your own file moves.

**4. Do not renumber a merged ADR to close a gap.** A gap is harmless; a moved number breaks every
citation to it. `D-008` was written after `D-009` for this reason — the numbers stay put.

If collisions somehow continue, the remaining fix is to abandon the global sequence for
date-plus-slug ids (`D-2026-07-27-harness-streaming`), which cannot collide at all. That costs every
existing citation, so it is a deliberate convention change — raise it, don't drift into it.

## Token / context management

- **Compact policy** — when context is compacted (`/compact`), the summary MUST preserve:
  open TODOs (from `docs/planning/BACKLOG.md`), API/interface changes **with their rationale**, the list of
  changed files, and a one-line summary of any failed approach (so it is not retried).
- After finishing a self-contained step, actively suggest/use `/compact` (or `/clear`).
- Keep replies as short as possible; no explanations without added value.
- Use **subagents** liberally to keep the main context clean: offload research, exploration,
  and parallel analysis so failed attempts never accumulate in the main window (subagents
  have their own context and tools). One focused task per subagent. For hard problems, throw
  more compute at them by fanning out across several subagents.

## Governance

Treat this file like code: version it, review changes in a PR, and re-test it in a fresh
session before merge. Do not duplicate anything already in `README.md` or a package manifest.
