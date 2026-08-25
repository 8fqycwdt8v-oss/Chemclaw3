# Task: benchmark ChemClaw3 against the agentic field (2026-08-25)

Asked for: deep web research on chemistry/pharma agents and on general agentic systems; an
intense benchmark of ChemClaw against both, to find blind spots, improvement opportunities, and
things newer systems now do better/cheaper. **GxP and regulatory framing is explicitly out of
scope** — which matters, because the previous external review
(`docs/archive/REVIEW-2026-08-13-…`) was organised around it.

- [x] Read the tree, the architecture map, the backlog and the deferred register
- [x] Measure the static per-turn context cost (instructions + tool schemas + skills listing)
- [x] Measure eval coverage: probe corpus vs the agent-callable tool surface
- [x] Run `make eval` and record the gated numbers
- [x] Inventory the capability surface across both repos (this one + `Chemclaw3-mcp`)
- [x] Research: chemistry/pharma agents 2026 (ChemCrow lineage, El Agente, Robin/Kosmos,
      OpenClaw-skills, ChemAmp, ether0, retro/ADMET/spectra state of the art)
- [x] Research: general agentic systems 2026 (context engineering, tool search / programmatic tool
      calling, memory, self-evolving skills, MAST, HAL, AstaBench, Temporal×LangGraph, security)
- [x] Attempt a live measured run — **blocked**: the environment's `API-KEY` is rejected 401 by
      `api.anthropic.com`, and `make live-up` is blocked by the open backlog row. Recorded as a
      limitation rather than worked around.
- [x] Write `docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`
- [x] File the actionable findings as `BACKLOG.md` rows with anchors
- [x] Publish the scorecard as an artifact for the reader

## Review

The benchmark is a *review*, not a code change: no `src/` file is touched, so `make lint type test`
is unaffected by it. What was run and what it said:

- `make eval` — 23 scored metrics over 14 cases, 4 gated failures (all four the case-set's own
  by-design failures), **0 regressions** against `data/evals/baseline.json`. This is where the
  report's eval numbers come from, not from reading the case files.
- `make prose-validate` — green. It is the validator this change can actually fail, since the
  report and the eleven new rows name tools, config keys, paths and ADR ids by hand.
- `make up` + `make db-migrate` — run first, so the eval and the import-time measurements ran
  against a real Postgres rather than the skip path `CLAUDE.md` warns about.
- `make test` with Postgres up: **4,251 passed, 3 skipped, 3 failed**, 21m23s. None of the three is
  this change (no Python moved) and none is a defect in the tree. Two are
  `tests/test_prompt_caching.py` guarding on `"API-KEY" in os.environ` while this environment's value
  is *present and rejected* — they ran instead of skipping and died on the same 401 that blocked the
  live measurement. The third, `test_reizman.py::test_bo_campaign_finds_high_yield`, hit the 180 s
  timeout under four concurrent pytest processes and **passes in 49 s in isolation**. The
  present-and-stale guard is filed as a row; the timeout is recorded beside it so nobody hunts a
  BoFire regression.

Failed approach, recorded so it is not retried: driving `build_langgraph_agent()` in-process against
the live API to measure real turn economics. The credential this environment carries is rejected;
`ANTHROPIC_BASE_URL` being set (the session relay) is not the cause — clearing it gives the same
401. Every number in the report is therefore static or from `make eval`, and the report says so.
