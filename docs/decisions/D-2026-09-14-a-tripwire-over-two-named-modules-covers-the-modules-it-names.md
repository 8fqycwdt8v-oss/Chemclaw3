# D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names — the `calc` seam check read 13 of 26 call sites while claiming all of them

## Status

Accepted.

## Context

W30.5 asked what `tests/test_sibling_manifest_agreement.py` and `tests/siblings.py` **actually**
compare, run against the real `Chemclaw3-mcp` checkout rather than skipped. Run on 2026-09-14 with
the fleet at `/home/user/Chemclaw3-mcp`, resolved by `infra/live/siblings.sh` exactly as the live
lanes resolve it: **every sibling-gated test runs — 11 of them across five files, none skipped, all
green.** `tests/conftest.py::_report_sibling_skips` printed nothing, which is what it is for.

What they compare, measured rather than read off the docstrings:

- **Bundle manifests declared in both trees.** This repository declares 8 (`bo`, `calc`, `chem`,
  `molfp`, `results`, `rxnfp`, `rxnpredict`, `safety`); the fleet publishes 5 (`chem`, `props`,
  `pyexec`, `rxnpredict`, `safety`). The intersection is **3** — `chem`, `rxnpredict`, `safety` —
  and those three are compared on tool list (12/6/3), read-only partition (12/6/3) and
  `token_env`, as sets. All agree. Two fleet bundles (`props`, `pyexec`) have no declaration here
  and five of this tree's have none there, so **13 of the 16 declared bundles are compared by
  nobody**, which is correct: a bundle declared in one tree only cannot disagree with itself.
- **The fleet's `manifests-internal/`** (`calc`, `rxnlabel`) is deliberately outside all of it —
  `mount: backend` is a key this repository's `extra="forbid"` model refuses.
- **The `calc` seam**, which no manifest covers in either direction. The fleet records
  `servers/calc/tool-surface.json` — **20 tools** — and this repository hardcodes tool names and
  argument dicts against it. The seam names **10** of those 20; the other 10 are served and never
  called from here. `tests/calc_server_fake.py` reproduces all 20 exactly.

**And that last check covered half the seam.** `_CALLERS` was a two-item tuple —
`connectors/calc/compose.py` and `connectors/calc/remote.py`, 13 call sites — while the test's own
docstring said *"Every hardcoded `calc` call must name a tool, and only arguments, the server
declares"*. Scanned against the tree, **five** modules hold such calls. The two unread ones were
`connectors/calc/server/tools.py` (11 sites) and `connectors/bo/calculators.py` (2) — which is the
half of the seam carrying `predict_pka`, `predict_solubility`, `compute_xtb_energy`,
`compute_atomic_descriptors`, `compute_surface_potential`, `predict_logd`,
`predict_site_reactivity`, `predict_developability_profile` and `compute_electronic_properties`.

Every tool name and every argument key those sites put on the wire is one the fleet declares today,
so **nothing was broken**. That is the finding rather than a counter-argument to it: this is the
`D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` shape a fourth time —
the *method* could not drift and the *fixture* was a hand-kept list.

## Decision

`_callers()` derives the module list from the tree: every module in `src/` that imports one of the
dispatchers **from `chemclaw.connectors.calc.remote`**, plus that module itself, which defines them
rather than importing them. 13 sites became **26**.

The import — not the function name — is what scopes it, and that is deliberate.
`ingest/labels/labeller.py` defines its own `_call`, the same spelling as one of `_DISPATCHERS`,
against the **rxnlabel** server: a different surface, with no `tool-surface.json` to check against.
Matching on the name alone would have checked its three call sites against `calc`'s tools and
failed on a server it never talks to.

## Consequences

A module that starts calling the calc backend is covered from the commit that adds the import,
with nothing to remember. The 10 fleet `calc` tools nothing here calls stay uncovered in that
direction, and that is correct — a tool nobody calls cannot be called wrongly.

## What keeps it true

- `tests/test_sibling_manifest_agreement.py::test_the_calc_seam_calls_only_tools_the_fleet_records_serving`
  — now over 26 sites. Two mutations, both applied and verified: renaming
  `predict_solubility` to `predict_solubilty` in `connectors/bo/calculators.py` (a newly covered
  module) fails it; adding an undeclared `bogus` argument to a `predict_pka` call in
  `connectors/calc/server/tools.py` (the other newly covered module) fails it. Both passed before
  this change.
- `tests/test_sibling_manifest_agreement.py::test_the_fake_calc_server_serves_exactly_the_surface_the_fleet_records`
  and `::test_a_bundle_declared_in_both_trees_declares_the_same_surface` — unchanged, and both run
  rather than skip when a checkout is present.
- `tests/conftest.py::_report_sibling_skips` — says how many did not run, so "all green" and "all
  ran" stay different statements.
