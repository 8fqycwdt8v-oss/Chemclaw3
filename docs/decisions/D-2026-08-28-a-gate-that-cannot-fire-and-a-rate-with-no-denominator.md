# D-2026-08-28-a-gate-that-cannot-fire-and-a-rate-with-no-denominator — the weekly mutation job creates the label it files under, and checks which modules the rate is over

**Status:** accepted

Supersedes points 4 and 6 of `D-2026-08-27-a-survivor-is-not-a-failing-build` in their operative
detail only. That ADR's decision stands: survivors are published rather than gated, and the gate is
the kill rate against a recorded floor. What it stated in the present tense and could not do is
what this one corrects.

## Context

Two of that job's controls were written down and could not act, and neither is visible by reading
the workflow.

**The notification cannot file anything.** Point 6 says a failure "opens (or comments on) an issue
labelled `mutation-testing`". That label does not exist in `8fqycwdt8v-oss/Chemclaw3` — confirmed
against the API, with `bug` and `dependencies` as passing controls — and nothing in the tree or the
repository settings creates it. `gh issue create` resolves label names to node ids *before* it
issues the createIssue mutation, so the step exits 1 with `could not add label:
'mutation-testing' not found` and no issue is filed at all; reproduced verbatim against gh 2.63.2
and 2.82.1, in both cases with zero createIssue mutations reaching the server, and passing in both
generations the moment the label exists. The dedup read above it cannot warn either: `gh issue list
--label` routes through search, which answers empty with exit 0 for a label that does not exist, so
control always falls through to the failing create. The trigger is also broader than a kill-rate
breach — `if: ${{ failure() }}` fires on a `uv sync` failure or a Postgres service that never came
up — so the first thing this job was ever going to produce is a red row in the tab that point 6 was
written to stop relying on.

**The rate has no denominator anybody checks.** The gate divides `killed` by `total` and never asks
*which* mutants are in `total`. mutmut 3.7.0's `walk_all_files` falls through to `walk(path)` for a
`[tool.mutmut] source_paths` entry that is neither a file nor a directory, which yields nothing —
no warning, no error — and `_load_config` never stats the paths; `export_cicd_stats` then skips any
file with no `.meta`. So a module moved without `pyproject.toml` following it is scored on six
modules instead of seven, and `also_copy = ["src", ...]` puts the moved file into the mutant tree
unmutated, so no test fails either. Reproduced on a two-module stand-in of this configuration: after
moving one module and leaving the config stale, `mutmut run` exited 0, printed nothing naming the
missing path, halved `total`, and the gate script passed at 100%.

The rate does not merely fail to catch that — it moves the wrong way. Per-module counts from run 1,
scaled to that run's 825: dropping `authz` leaves 75.7%, `audit_store` 74.4%, `budget` 72.4%,
`runner_trace` 75.7%, `note` 74.5%, `pr_gate` 73.8%, `store` 78.2%. All seven are above the 72.0
floor and five are above the 74.9% baseline, because the aggregate loses a below-average module.
No floor on the rate can see this, and a floor on the *count* would move every time a module
legitimately grows — which is the reason point 4 chose a rate in the first place.

## Decision

1. **The workflow creates the label it files under**, with `gh label create mutation-testing
   --force` immediately before the list/create pair. `--force` rather than `2>/dev/null || true`:
   the create is idempotent, so there is no failure worth swallowing, and swallowing one is how
   this step would go quiet again. Repository settings are not in this checkout, so a workflow
   carrying its own label is the only form of this a reviewer can check.

2. **The gate asserts coverage as well as rate.** It reads `[tool.mutmut] source_paths` back from
   `pyproject.toml` and fails, naming them, when any declared path left no `mutants/<path>.meta` —
   the artifact `export-cicd-stats` itself keys on. Coverage rather than a `MUTANT_TOTAL_FLOOR`,
   because a count floor is the thing point 4 rejected and it would still pass a run that dropped a
   small module while a large one grew.

3. **Both are driven by a test rather than read.** `tests/test_mutation_workflow.py` runs the
   notification step under `bash -e` against a `gh` stand-in carrying this repository's real label
   set and refusing an unknown label exactly as the real one does, and runs the gate step's Python
   against a synthetic `mutants/` tree with one module's results missing. Three of its four cases
   were red before this change; the fourth is the control that shows the gate is not simply
   refusing everything.

## Consequences

- Point 6 of the superseded ADR reads as true from today; until now it described a step that had
  never fired. Nothing about the survivor policy, the floor, the Postgres service or the
  seven-module scope changes here.
- The label's colour and description are the workflow's, not a repository setting, so renaming it
  is one edit in one file.
- A run that dies part-way now fails the gate for a second reason — its later modules have no
  `.meta`. That is the intended reading: a partial run's rate is not this job's number.
