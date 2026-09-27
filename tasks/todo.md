# A screen answers per item; a distribution refuses by name

Source: arXiv 2609.14840 (El Agente Potente, typed execution graphs for MLIP campaigns). Its one
pattern this repository does not already have: **every input to a high-throughput run ends as a
typed outcome** — a result, or a typed failure naming the input — so one bad structure never costs
the campaign and is never silently dropped. Everything else in the paper is either already here
(typed dispatch over `XtbJobSpec`, per-primitive caching, provenance) or already decided against
(MACE: licence; a coding mode reaching validated functions: `D-2026-08-25-a-sandbox-is-a-server-not-a-verb`).

## The defect, measured before planning

`connectors/calc/compose.py`: the four list-taking composites run their items with no per-item
boundary, so the **first** item that raises aborts the whole durable job.

| Composite | Job | Items | Today |
|---|---|---|---|
| `bond_dissociation_survey` | `survey_bond_strengths` | bonds (independent) | 1st failure aborts |
| `solvent_comparison` | `compare_solvents` | media (independent) | 1st failure aborts the `gather` |
| `species_solvent_comparison` | `rank_species_across_solvents` | media (independent) | same |
| `species_ranking` | `rank_species` | species (**not** independent) | 1st failure aborts, names only that one |

## Design decisions

1. **The per-item boundary catches `ValueError` and nothing wider.** That is exactly the
   repository's existing "bad data" contract: `ChemclawError` (a `ValueError`) is documented as
   "catch this at batch boundaries (reject-and-continue)"; `CalcToolError` (a server refusal),
   `CalculationDomainError`, `InvalidSmilesError` and pydantic's `ValidationError` are all
   `ValueError`s, and `ValueError` is on `durable/publish.py::_BAD_DATA_TYPES` — i.e. already
   declared deterministic for that input. **Outages must not become item failures**:
   `CalcServerError` / `CalcBusyError` are `SubsystemUnavailableError` (not `ValueError`) and must
   still propagate so Temporal retries the activity; `CancelledError` is a `BaseException` and
   propagates. A test drives each of those through the boundary.
2. **Independent items (bonds, media) → typed failure, the rest still answered.** New models in
   `science/calc/models.py`: `FailedMedium(solvent: str | None, reason: str)` (two callers) and
   `FailedBond(atoms, bond, fragments, reason)`. New field `failed: list[...] = []` on
   `SolventComparisonResult`, `SpeciesSolventComparison`, `BondDissociationSurvey` — defaulted, so
   an in-flight run's payload without it still decodes.
3. **A distribution is not independent items — it refuses, by name, after trying every species.**
   Populations normalise over the set, so ranking a subset is "confident about the wrong universe"
   (the composite's own docstring). Dropping the failed species is therefore wrong, and so is
   returning partial populations. `species_ranking` attempts *every* species (each success is
   cached, D-011, so the rerun without the offender pays nothing for them), then raises one
   `ValueError` naming each failed species and its reason. Today it names only the first.
4. **Everything failed → one `ValueError` naming every item.** No empty result, no fabricated
   ranking.
5. **Honest aggregates over the survivors**, each with a warning:
   - failed items are listed in `warnings` too (the publish projection turns warnings into
     `calculation_flag` rows, so a published record carries the gap with no projection change);
   - bond survey: `is_weakest` is the weakest *of the computed bonds*, and the warning says a
     failed bond may be weaker; `considered == len(bonds) + len(failed)`; `method` taken from a
     computed bond;
   - solvent screens: fewer than two media computed → no spread/"does not distinguish" claim;
     say there is nothing to compare instead. A lost gas-phase reference is named as such.
6. **Activity summaries carry the gap** (`connectors/calc/activities.py`): a completion push-back
   must not read "weakest of 5 bonds" when 2 were not computed.
7. **The job descriptions say it** (`connectors/calc/connector.yaml`, which is the prompt): the three
   screens report per-item failures under `failed`; `rank_species` refuses naming every failed form.
8. **One small helper, four callers**: `_attempt(awaitable) -> result | ValueError` in `compose.py`.
9. **Out of scope, argued**: `durable/orchestrator.fan_out` drops a failed child (D-030) — its report
   caller already reconciles the gap into a visible `retrieval_failed` marker and its memory caller
   counts it on `chemclaw_fan_out_children_dropped_total`; changing its return type is a separate
   decision. Template waves abort on a failed step by design (a step's output feeds the next).
10. **ADR**: `D-2026-09-27-a-screen-answers-per-item-a-distribution-refuses-by-name.md` — a choice
    between options (drop / partial populations / refuse) and the boundary class, not a defect fix.

## Items

- [ ] `science/calc/models.py`: `FailedMedium`, `FailedBond`, `failed` fields (defaulted).
- [ ] `compose.py`: `_attempt` helper; `bond_dissociation_survey` per bond; `solvent_comparison`
      and `species_solvent_comparison` per medium (inside `one()`, so `gather` still propagates
      outages); `species_ranking` try-all-then-refuse-by-name; the all-failed refusals; the
      fewer-than-two-media wording.
- [ ] `activities.py`: summaries for the three screens name the failed count.
- [ ] `connector.yaml`: four descriptions.
- [ ] Tests (`tests/test_calc_ensembles.py`, `tests/test_calc_compose.py`, `tests/test_calc_jobs.py`),
      driven through `FakeCalcServer.overrides` so a refusal arrives on the real wire path as
      `CalcToolError`:
  - [ ] survey: one bond refused → other bond answered, `failed` names it with the server's reason,
        `considered == bonds + failed`, warning says a failed bond may be weaker.
  - [ ] survey: every bond refused → `ValueError` naming each.
  - [ ] survey: an outage (`CalcServerError`) on one bond propagates, nothing is returned.
  - [ ] solvent screen: one medium refused → ranked over the rest, `failed == [FailedMedium]`.
  - [ ] solvent screen: only one medium left → no "does not distinguish" claim, says nothing to compare.
  - [ ] solvent screen: every medium refused → `ValueError`.
  - [ ] species screen: a species refused in one medium only → that medium in `failed`, others ranked.
  - [ ] ranking: one species refused → `ValueError` naming it; every other species was still relaxed,
        and ranking the set without it relaxes nothing new.
  - [ ] ranking: two species refused → both named in one error.
  - [ ] ranking: `CalcBusyError` propagates as itself (stays retryable).
  - [ ] jobs: a survey job with a refused bond has a summary naming the failure.
  - [ ] wire: a payload without `failed` still validates.
  - [ ] mutation check: remove the boundary → the per-item tests go red.
- [ ] ADR + ledger row.
- [ ] `make lint type`, targeted tests, then full serial `make test` with Postgres up; report skips.
- [ ] Fresh-context subagent review (correctness; contract/wire/publish; docs-vs-code), fix findings.
- [ ] PR, CI green, merge, delete branch.

## Review

(filled in at the end)
