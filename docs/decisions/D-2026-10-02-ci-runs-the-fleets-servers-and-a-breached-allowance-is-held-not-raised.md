# D-2026-10-02-ci-runs-the-fleets-servers-and-a-breached-allowance-is-held-not-raised — how CI sees the half of the prefix this repository does not author, and what it does about a breach

**Status:** accepted · **Date:** 2026-10-02 · Closes the `BACKLOG.md` row *"`SERVED_ELSEWHERE_ALLOWANCE`
is breached and CI cannot see it"* (added by #515) · Files Chemclaw3-mcp#152.

## Context

`PREFIX_BOUND` is the ratchet ceiling plus `SERVED_ELSEWHERE_ALLOWANCE`, the bound for the three
bundles this repository declares and `Chemclaw3-mcp` serves (`chem`, `rxnpredict`, `safety`). Both
compaction defaults and `agent_context_prefix_basis` derive from it. The test that checks the
allowance runs the fleet's servers in the fleet's own interpreter, so it skipped wherever the
sibling had no `.venv`, which included CI. CI checked the sibling out but never built it; the
`check` job's comment called building it "the decision nobody has taken" and priced it as
"RDKit, torch and a T5 checkpoint's dependencies".

Measured 2026-10-02 against the fleet's `main` (d25e165): **12,020** against the 11,000 allowance
(`chem` 7,604 / 13, `rxnpredict` 2,784 / 6, `safety` 1,632 / 3). `chem` accounts for all of it:
5,577 over 12 tools when the allowance was set on 2026-09-05, then a new 939-token tool and +1,088
of description across six existing ones.

## Options for making CI see it

1. **Read the fleet's published `servers/*/tool-surface.json`, which needs no venv.** Ruled out
   by reading the files. They record each tool's argument names, types, defaults and
   required-ness and carry **no descriptions**, and descriptions are most of a schema (2,000-3,300
   characters a `chem` tool against 140-350 of argument schema). A bound computed from them would
   undercount by most of the quantity it bounds.
2. **Import the fleet's sources into this repository's interpreter** (`PYTHONPATH` over
   `packages/mcp_server_kit/src` and `servers/<name>/src`). Ruled out by running it: the import
   fails on `prometheus_client`, a dependency of the fleet's kit that this environment does not
   carry. Adding it here to measure another repository is the closure
   `D-2026-08-09-a-connector-we-do-not-run` keeps out, and the measurement would then run a
   different `mcp` from the one the fleet locks.
3. **Build the fleet's own locked environment in CI.** Measured cold in a fresh container:
   `uv sync --frozen` takes 24-44 s and 708 MB over 94 packages, RDKit and SciPy and **no torch**,
   because the model backends are optional extras. The price that kept this out was never what a
   default sync installs.

**Chosen: 3.** The `check` job builds `.sibling/Chemclaw3-mcp`'s environment, and sets
`CHEMCLAW_SIBLINGS_REQUIRED=1` so that a skip carrying `tests/siblings.SIBLING_SKIP` fails
(`tests/conftest.py::pytest_runtest_makereport`). In that job the checkout and the environment
are provisioned on purpose, so a skip can only mean a precondition broke. That is the state the
allowance sat in, breached, while every run was green. Locally the sibling stays optional and the
epilogue still counts what a run skipped. Run against a built sibling copy, the cross-repository
files go from skipping to running: every schema-measuring test, the `calc` key derivation and the
e2e lane doubles. The only failures were the allowance, which is the point, and a `jq`-dependent
Jenkins test that fails in the local gate image only (CI's runner carries `jq`).

## Options for the breach

1. **Raise the allowance to the measurement plus headroom**, which moves `PREFIX_BOUND`, the clear
   trigger and `agent_context_prefix_basis` together. **Measured, and it fails.**
   `agent_context_token_budget` is pinned from above by the smallest window this stack targets
   (`tests/test_compaction.SMALLEST_TARGET_WINDOW`, with the margin under it asserted exactly), so
   a larger bound comes out of the thread. The warm arm of
   `test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` needs the calibrated
   thread above one maximal tool batch (15,000 estimated tokens). At 11,000 it is **15,471**, and
   it falls one token per token of allowance: **14,451 at the bare 12,020**, 13,071 at 13,400 (the
   ~11% headroom the allowance was set with). No raise that covers the measurement survives.
2. **Raise the budget as well.** This reverses the recorded choice at
   `test_a_maximal_request_at_the_shipped_budget_fits_the_smallest_window_it_targets`. The budget
   stays where the window put it, and every raise spends headroom the provider decides. It is
   declined here for the same reason.
3. **Narrow `chem` in the fleet.** That is where the growth is, and it is above the band the
   fleet's other single-purpose servers occupy (585 a tool against 489-565). Filed as
   Chemclaw3-mcp#152 with the per-tool table.

**Chosen: 3**, and meanwhile the breach is held at its measured size rather than hidden or
absorbed. `tests/test_context_floor.SERVED_ELSEWHERE_KNOWN_BREACH = 12_020` makes the bound red if
the three bundles grow past it, and red again once they are back inside 11,000, so the tolerance
is deleted as soon as it is no longer needed. Until then a chart deployment's prefix exceeds
`agent_context_prefix_basis` by roughly 1,000 tokens. That excess is paid in spend rather than
thread, which is the mechanism
`D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread` built for exactly
this, and it logs `context.prefix_over_basis` once per process.

## Consequences

- Every `check` run spends roughly half a minute building the fleet's environment and now runs the
  fleet-dependent tests. CI reads the fleet's `main`, so a fleet merge that grows one of the three
  bundles reds this repository's CI on its next run. That is the property the allowance needed,
  and it is the same arrangement the manifest-agreement tests already have.
- `FLEET_PUBLISHED_ALLOWANCE` is checked in CI for the first time too.

**Revisit when:** Chemclaw3-mcp#152 closes. If the three bundles measure inside 11,000 the
breach constant is deleted, and the test says so. If the fleet declines to narrow `chem`, the
choice is between the budget's warm floor (`_unreclaimable_batch_tokens`, i.e.
`agent_max_tool_result_chars`) and the window margin, and that needs a decision of its own.
