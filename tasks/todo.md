# Codebase improvement sweep (2026-08-14)

A full sweep of `src/` plus a documentation check against every load-bearing framework. Findings
were verified by running them, not by reading alone; one reported finding was checked and rejected
(see the review section). Plan: `/root/.claude/plans/glimmering-swinging-lynx.md`.

## Batch A — defects

- [x] `GET /profiles` returned `[]` in every deployment (`load_profiles` reports only what it newly
      registered; the lifespan had already registered everything). Now reads the registry. The test
      that missed it asserted only `isinstance(names, list)`.
- [x] Connector bearer auth failed **open** when the bundle ships a manifest this process did not
      discover; the fail-closed sentinel also latched for the process lifetime.
- [x] Checkpointer/pool published before `setup()` / `open()` completed — measured: 3 of 4
      concurrent first turns got an unmigrated saver.
- [x] `_tls_http_client` built a fresh `httpx.AsyncClient` per turn and never closed it.
- [x] `fan_out` and the template step started child workflows with no `execution_timeout`.
- [x] `make db-grants` exited 0 having applied nothing.
- [x] BO seeding could loop forever on a constrained domain.
- [x] A suspected conflict could outrank a declared one at severity 1.0; the `detail` prose printed
      a confidence belonging to no note.
- [x] `RequestLimiter(per_minute=0)` divided by zero after the burst drained.

## Batch B — measured latency and cost

- [ ] `declared_tools` re-reads 28 `SKILL.md` per graph build, on the event loop (2.6 ms/turn).
- [ ] `gather_section` fans out serially inside a budgeted activity.
- [ ] `describe_schedules` serial Temporal RPCs, no timeout, default retry.
- [ ] Redundant SCF in `xtb_opt` and a second calculator + SCF in `xtb_hessian`.
- [ ] `turn_evidence` computed twice per challenged turn.
- [ ] `tanimoto` re-parses both bitstrings per pair (O(n²) parses).
- [ ] `_labelled(_skill_dirs())` computed twice per build; document-index citation resolved per
      scored chunk before truncation.

## Batch C — dead code and dead configuration

- [ ] `calibration_conformal_*`: two settings with no reader (+ `.env.example`), and the function
      they were written for has no caller.
- [ ] Four `memory/jobs.py` async wrappers with no production caller, plus the parity test.
- [ ] `_AU_TO_DEBYE` dead constant.
- [ ] `skills_dirs`/`profiles_dirs` and `resolve_params_model`/`resolve_precondition` duplications.

## Batch D — tooling and dependencies

- [ ] Ruff: adopt `RUF100` (72 dead `# noqa`), `ASYNC`, `G`, `DTZ`, `PIE`, `RET`, `ERA`; fix the
      real hits.
- [ ] `--strict-markers` and a first-party `filterwarnings`.
- [ ] CI: `uv sync --locked`; collapse the duplicate push/PR run via the concurrency key.
- [ ] Dependency bump incl. the majors (openai 3 + httpx2, starlette 1.6), with an ADR. `mcp` 2.0
      stays out — it renames `FastMCP`, which `connectors/server.py` patches.

## Batch E — prose that contradicts the code

- [ ] The announcer-ordering bullets, `loop_cap`'s `>`/`>=` comment, the "softest" imaginary mode,
      `harness._report_id`, `compaction`'s cited sync caller, the "three tests" count, and the
      BACKLOG/DEFERRED rows that no longer verify.

## Review

**What the sweep was worth.** Five things were broken in a deployment and none of them had a failing
test: `GET /profiles` answered `[]` on every request in every environment, a connector served its
`/mcp` surface unauthenticated whenever its manifest was not discovered, the checkpointer handed
concurrent first turns a saver whose migrations had not run, the production LLM path leaked a
connection pool per turn, and two child-workflow starts had no wall-clock bound. The common shape is
that each was *invisible*: an empty list is a valid list, an open door answers 200, an unmigrated
saver is a live object, a leaked pool is collected eventually, and a hung child looks like a slow
one.

**Two claims were rejected on verification, one of them mine.**

- A sweep reported `tests/test_prompt_caching.py`'s `"API-KEY" not in os.environ` skip as permanently
  dead, on the grounds that `API-KEY` is not a settable shell identifier. It is real: CLAUDE.md
  documents it as the credential name in this repository's remote environments,
  `infra/live/e2e-full-stack/up.sh` maps it with `printenv 'API-KEY'`, and it was set in the session
  that checked. No change.
- My own first version of the Hessian finding said the path ran "a second SCF". Counted, the SCF
  total is unchanged at 6N+1 — what was duplicated is the *calculator* (2 Hamiltonian assemblies to
  1). The docstring says so explicitly, because "a second SCF" is the more interesting claim and is
  not the true one.

**Two tests were written that passed against the unfixed code, and had to be rewritten.** The
checkpointer race needed `setup()` widened before a second caller could be observed inside it; at
real speed the first task wins and both earlier versions measured nothing. A test that cannot fail
is an execution trace — the repository's own mutation-testing note makes the same point, and it cost
two attempts to relearn it here.

**One fix was re-planned mid-batch.** Failing closed whenever a connector's manifest was undiscovered
broke six transport tests, which is how it surfaced that an app no bundle backs is a *supported*
construction here rather than a misconfiguration. The distinction that resolves it — does a
`connector.yaml` for this name ship beside the module? — also has a stated limit: a bundle an
operator ships outside the package cannot be spoken for, and the docstring says so rather than
implying coverage it does not have.

**What the environment gave.** The sandbox has no Docker, so the Postgres-backed tests would have
skipped as they do offline. Building Postgres 16 + pgvector 0.8 in the container took one detour
(the packaged pgvector is 0.6 and lacks `bit_jaccard_ops`) and turned 22 skips into 4,407 tests
actually run — which is what made the checkpointer, retention and store findings verifiable at all.

**Left open, deliberately.** `mcp` 2.0.0 (a migration with a security gate to re-verify, now a
`DEFERRED.md` row with the `<2` cap as its trigger); wiring `conformal_uncertainty` to a predictor
(a capability decision, its dead settings deleted); and `langchain` 1.3.15's `state_schema` /
`trace_policy`, named in the ADR so a later reader knows they were seen and not adopted.
