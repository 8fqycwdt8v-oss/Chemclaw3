# The delegation experiment — the run half

`evals/delegation.py` is the comparator and it has never seen a run. This builds the runner.
Requirements: `docs/planning/BACKLOG.md` row "The delegation experiment: build the runner, then run
it against a gateway"; `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`.

## Plan

- [ ] 1. Arm profiles — symmetric, so the contrast is one variable
      (`D-2026-09-14-tools-were-never-the-variable`). `data/evals/profiles/no-helper.yaml`,
      `delegating.yaml`, `handing-off.yaml`: one shared instrument body, one act paragraph each.
      A test pins the body byte-identical across the three.
- [ ] 2. `src/chemclaw/evals/delegation_run.py` — the arm table, the per-repeat drive, the two
      observations (`delegated` off `audit_events`, `billed_tokens` off `turn_costs`), `ArmRun`
      assembly, the report.
- [ ] 3. `cli/live_probes.py --suite delegation`, `make live-delegation`.
- [ ] 4. `cli/delegation_behaviours.py` — the mock catalogue that can emit a `task` call, plus
      `mock_llm --catalogue`.
- [ ] 5. Drive it against the mock on loopback. All four compliance buckets.
- [ ] 6. Tests + mutation discipline (>=2 other mutations watched failing, >=1 a reword).
- [ ] 7. BACKLOG row rewritten to what is left.

## Verification

- `make lint`, `make type`, the tests added plus `tests/test_delegation*.py`,
  `tests/test_live_storm.py`, `tests/test_mock_llm_contract.py`, `tests/test_turn_cost.py`,
  `tests/test_repo_map.py`.
- The mock run: a `runs.json` holding real `ArmRun`s and a `summary.md` naming every bucket.
- **No number the mock produced may be reported as evidence about delegation.**

## Review

(filled at the end)
