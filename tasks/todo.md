# Wave 8 — the mutation backstop's own coverage, the prose surface, and the organic line

## Items

- [x] **Pair the mutation selection with the source paths it claims to cover** (`826ba28c`).
      Six test files added to `pytest_add_cli_args_test_selection`, each with the coverage lift
      measured rather than guessed.
- [x] **Copy the schema tree, and guard the list that says what is copied** (`c35ff891`).
      The widened selection would not start: `tests/test_publish_end_to_end.py` reaches
      `cli/sink_schema.ddl`, which globs `schema/result-store/*.sql` from the repository root, and
      `mutants/` had no `schema/`. The finding is that `also_copy` and the selection are coupled and
      nothing related them; the guard drives both sides (mutmut's own `Config` for the copied set,
      the working tree for the directories) with `mutants/` the single declared absence. Proven
      non-vacuous by removing `schema` again.
- [x] **Widen the model-facing prose guards to the surface** (`0e60f65c`). Three classes were
      outside — the durable jobs' assembled docstrings (all of the `results` bundle), every
      `SKILL.md`, and `_INSTRUCTION_BLOCKS`. Measured first: eight hits, all eight correct text.
      The pattern shape the row proposed is disqualified by the one true positive the file has,
      which is itself past tense. Two narrowings are correct by construction; the recall widening
      is free. Three exemptions remain, each a quote containing its match, with a guard against a
      stale one and a guard against a vacuous one. ADR + ledger + row deleted.
- [x] **Widen `_is_organic`, bump to std10** (`1051dc2e`). The C–H/C–C test called urea inorganic
      and left a bare guanidinium salt short of the neutralisation branch. Widened to a carbon
      holding two nitrogens; twenty species driven, every defended inorganic case holds. Exactly
      one behaviour-table row moves. ADR + ledger + row deleted.
- [ ] **Set the `no_tests` ceiling in `.github/workflows/mutants.yml` from the new run**, not from
      a guess, and say where the number came from. The previous run scored 1612/3640 killed with
      1009 `no_tests` (27.7%) against a floor of 72.0.
- [ ] Full serial suite, fresh-context subagent review, PR, merge on green CI.

## Measurements this wave rests on

- `also_copy` vs. the working tree: `mutants` and `tests` were the only root directories absent,
  and `tests/` is absent because *mutmut appends it* — so the guard reads the effective list
  through `Config` rather than restating upstream's defaults.
- Prose guards, widened universe: 8 hits / 8 false positives with the patterns as they stood.
  Recall widening (`PRs`, review queue, knowledge gate, awaits review): 0 new hits.
  `propose[sd]? (?:what|it|them|the)`: 0 true positives, 4 false positives -> dropped.
- `_is_organic`: urea, thiourea, guanidine, cyanamide, melamine all `organic == 0` before.
  After: cyanide, cyanate, thiocyanate, carbonate, bicarbonate, CO, CO2, CS2, phosgene, CF4 and
  azide all still inorganic.

## Review

Pending the mutation run and the full serial suite.
