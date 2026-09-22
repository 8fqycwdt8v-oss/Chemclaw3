# Wave 8 — the denominator nobody kept

Wave 7 merged as #436. Its audit left 30 live rows; this wave starts with the one wave 7's own
finding pointed at: the mutation gate scores 44.3% against a floor of 72.0, and the reason is that
`total` counts 1009 mutants (27.7%) that no selected test reaches.

## R1 — pair the selection with the source paths it claims to cover

- [x] Measured the pairing. Of 15 `source_paths`, only **5** had their obvious `tests/test_<stem>.py`
      in `pytest_add_cli_args_test_selection`, and three of those files *existed and were simply not
      listed*. Per-source-path coverage from the 17-file selection:

      | module | covered |
      |---|---|
      | `agent/authz.py` | 99% |
      | `agent/audit_store.py`, `agent/spend_cap.py`, `kg/record.py` | 95-96% |
      | `science/calc/store.py`, `kg/note.py` | 90-92% |
      | `api/runner_trace.py`, `kg/git_writer.py`, `api/budget.py` | 77-86% |
      | `core/chem.py`, `core/quantities.py` | 69% |
      | `core/logging.py` | 62% |
      | `core/fulltext.py` | **44%** |
      | `publish/outbox.py` | **31%** |
      | `templates/resolve.py` | **19%** |

      A mutant on a line no selected test executes is `no_tests` by construction, and `total` counts
      it. So the 44.3% was arithmetic, not a regression.
- [x] Six files added, each with its measured lift recorded on its own line in `pyproject.toml`
      rather than as a bare filename: `test_fulltext.py` (44 -> **100%**), `test_templates.py`
      (19 -> 87%), `test_quantities.py` (69 -> 92%), `test_logging.py` (62 -> 83%),
      `test_compound_identity.py` (69 -> 84%), `test_publish_end_to_end.py` (31 -> 64%).
- [x] Re-measured the widened selection: 23 files, **726 tests in 2:14**, and the weakest module is
      now `publish/outbox.py` at 64% against `templates/resolve.py`'s 19% before.
- [ ] A fresh mutation run, to see what the gate actually scores now.

## R2 — the guard the row asked for is the wrong shape, and measuring said so

The row's closing line asks for "a derived guard that fails when a declared source path has no test
file in the selection". I built the static version first and **it passes on the broken state**.

- [x] Driven over the 15 source paths: a rule matching a module's name or dotted path inside any
      selected file finds a hit for **every one of them**, including `templates/resolve.py` — which
      had 8 such "hits" while being covered at 19%. The word `resolve` appears in test prose
      everywhere. A naming rule cannot tell a file that exercises a module from one that mentions it.
- [x] So the check belongs where the evidence already is: the mutation run's own
      `mutants/mutmut-cicd-stats.json` reports `no_tests`, and `.github/workflows/mutants.yml`
      already gates on `killed / total`. A ceiling on the `no_tests` **share** turns "the selection
      drifted from the declaration" into a red build instead of a silent drag on the kill rate.
- [x] That is also what the workflow's own reasoning asks for. Its comment records the standing
      state as "**34 `no_tests`** and 2 timeouts both times" — measured when `source_paths` held
      seven modules, against 1009 at fifteen — and argues for "a rate, not a count, because a count
      breaks the first time one of these modules legitimately grows".
- [ ] Set the ceiling from the new run rather than from a guess, and say where the number came from.

## Verification

- [ ] `make lint type`, the full serial suite, a fresh-context review, PR, merge on green CI.
