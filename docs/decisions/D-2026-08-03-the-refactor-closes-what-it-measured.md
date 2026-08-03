# D-2026-08-03-the-refactor-closes-what-it-measured — closing the grand refactor on re-measured numbers

**Status:** accepted · **Date:** 2026-08-03 · **Closes:** the R0–R5 programme of
`docs/planning/refactor-hardening-plan.md` (planned 2026-08-02, ADR-per-phase along the way:
D-2026-08-02-shipped-is-not-reachable, D-2026-08-02-the-seam-does-not-move, and the phase commits
on `claude/codebase-review-refactor-3lbnjs`)

## Context

The refactor ran as ~24 work packages across six phases, implemented by parallel agents against a
baseline of commit `39f9135`. Its own history is the reason this closing record is measurement-heavy:
during execution, three separate packages made a false gate claim (a syft pin to a tag that 404'd,
five over-length lines reported clean, a plan premise about `metrics_bridge` that would have caused
a regression if implemented), and six statements in the plan document itself were disproven by the
code they described. Prose in this repository is evidence about what its author believed, never
about what the code does — so the close is a re-verification pass (R5.3), not a summary.

## What changed structurally (verified against the tree, not the commit messages)

- **The two hot files are decomposed.** `api/app.py` (was 1946 lines at `39f9135`) is a composition
  root over `routes/` ×8 + `state.py`/`deps.py`/`schemas.py`/`middleware.py`; `api/runner.py` is
  split into `runner.py`/`runner_trace.py`/`runner_usage.py`/`runner_answer.py`. The largest file
  in `src/` is now `connectors/calc/server/tools.py` at 808 lines (pre-refactor: `core/config.py`
  at 2157). The app decomposition changed **zero** test files; the runner split re-pointed imports
  in four test files with no assertion changed (its commit message says exactly this — the "zero
  test changes" claim some retellings carry is true of one decomposition, not both).
- **`core/config.py` is a package** (D-2026-08-02-the-seam-does-not-move), import seam unchanged,
  310 `Settings` fields, `.env.example` parity CI-enforced (5 parity tests green).
- **The kernel rule is real and derived.** Import-graph diff of `src/` between `39f9135` and
  `c032e63` (same AST walk as `tests/test_layering.py`: module- vs function-scope, TYPE_CHECKING
  excluded), edges as (source, target, scope):
  - **Removed (8):** module-scope `core→api` (`worker_http.py:49` — the actual layering break),
    `kg→agent`, `connectors→api`, `api→cli`, `durable→cli`; lazy `core→api` (`metrics_bridge`),
    `core→agent` (`logging`'s ambient-identity getters), `templates→agent`
    (`templates/registry.py`'s lazy tool-registry import).
  - **Added (3):** lazy `core→connectors` (`core/logging.py:193-194`, the redaction filter
    resolving connector bearer-token env names — the Sec-6 fix, new in R0), lazy `templates→core`
    (the same registry import, now pointing at the moved primitive), module-scope `cli→science`
    (the new `safety-validate` entrypoint, declared with its reason in the layering policy).
  - `chemclaw.core` now has **zero module-scope sibling edges and exactly one lazy edge**
    (`core→connectors`), declared in `tests/test_layering.py::_ALLOWED_LAZY_EDGES` and stated in
    `core/README.md`. That closes the BACKLOG row asking for the accept-or-invert decision: it is
    accepted permanently. The inversion (registry pushes token names into a core-owned inventory)
    would trade one declared, tested edge for a startup-ordering contract, which is a worse deal.
- **Guards became tests**: the derived layering allow-list, the route-auth coverage walk, the
  entrypoint-default parity test, the `_BAD_DATA_TYPES` completeness walk (now covering
  `ProfileError` and `AuthorizationError` — registered by exact name, deliberately not reparented),
  and the MCP tool-exception sanitizer pinned over the real streamable-HTTP transport.

## The closing numbers (all re-measured on 2026-08-03, quiet box)

| Measure | Plan baseline | Close |
|---|---|---|
| `make test` | 2789 passed, 104 skipped | **2852 passed, 127 skipped, 0 failed, 311 s** |
| `make lint` / `make type` | green | green (534 files, `mypy --strict` clean) |
| Docstring coverage (module+class+def AST nodes, `src/`) | 97.5 % | **98.3 %** (2247/2285) |
| ADR count | "165+" | **208** (165 frozen `D-NNN` + 43 date-slug) |
| `Settings` fields | — | **310**, `.env.example` parity enforced |
| Largest `src/` file | 2157 (`core/config.py`) | **808** (`connectors/calc/server/tools.py`) |
| `core` sibling edges | 1 module-scope + 2 lazy | **0 module-scope + 1 declared lazy** |

Validators run individually at close: `kg-validate`, `eval-strict`, `eln-validate`,
`skill-validate`, `connector-validate`, `datasource-validate`, `template-validate`,
`prose-validate`, `safety-validate` — all green. **Not runnable here:** `helm-validate` (no `helm`
/ `kubeconform` binaries in this environment), so `make ci` as a whole cannot pass locally and is
not claimed; CI remains the only witness for the chart render, per the plan's own "the offline gate
is not the whole gate" rule. `audit-verify` needs a populated Postgres audit trail and is likewise
CI/deployment territory.

The MCP sanitization probe was re-run at close (a tool raising
`RuntimeError("could not connect to database: postgresql://user:PASSWORD@host/db")` behind
`connector_app` over real streamable HTTP): the caller receives
`Error executing tool blow_up: an internal error occurred` — no DSN — and a `ChemclawError`'s
deliberate wording still passes through unchanged.

## What was deliberately not done, and why

- **`metrics_bridge` stays.** The plan said delete it; its `try` wraps `update(METRICS)`, not the
  import, and `core/metrics.py` raises `KeyError` on an undeclared counter *by design* — deleting
  the swallow would let a metric typo fail the operation being counted at 11 call sites across six
  packages. Only the lazy import went.
- **`core/metrics.py`'s label-series cap is not a `BoundedLru`.** Refuse-new is cardinality
  protection; evict-oldest is churn. Four maps consolidated, not five.
- **`_visible_proposal` keeps its own 404 gate.** Reviewers may see any proposal; sessions and
  approval holds have no analogous allowance. Two gates unified, not three.
- **No package merges/splits, no `agent/` restructure, no re-export `__init__`s** — per the plan's
  verdict, unchanged at close.
- The plan document's six disproven statements are corrected in place (marked "corrected in
  execution"), each carrying the measurement from the shipped code's own docstring rather than a
  re-derivation.

## What is still open that a reader of the plan might believe closed

`Conn-F4`'s spike premise, `Test-2`'s subprocess counts and `Ops-F1` are corrected in the plan
itself. Beyond that: the live edges (`helm-validate` locally, real Entra/Temporal/cluster) were
never in scope and remain in `docs/planning/BACKLOG.md`; and one new row was filed at close — the
suite's two slowest pKa tests fail when the box is loaded (312 s → 1330 s, two failures whose text
was not captured), recorded with the cause explicitly unconfirmed.
