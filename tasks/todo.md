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

(Filled in at the end of the sweep.)
