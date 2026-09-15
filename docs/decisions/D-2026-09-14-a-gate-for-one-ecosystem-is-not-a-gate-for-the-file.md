# D-2026-09-14-a-gate-for-one-ecosystem-is-not-a-gate-for-the-file — the seven vulnerabilities are PyPI, at versions this lockfile does not contain, and the actions ecosystem is an accepted risk

## Status

Accepted.

## Context

`make deps-audit` reported **no known vulnerabilities** over both the production closure (212
packages) and the full one (248) while a push to this repository returned *"GitHub found 7
vulnerabilities on the default branch (1 high, 6 moderate)"*. `--no-dev` was not the gap — both
arms were run. `.github/dependabot.yml` declares updaters for **two** ecosystems, `uv` and
`github-actions`, and its own header asserted the pipeline "already *detects* a vulnerable
closure", twelve lines above the second one, which nothing in `make ci` reads.

So there were two candidate explanations and no evidence: either the gate was too narrow and the
seven were GitHub Actions advisories, or the gate was right and GitHub was reporting something
`pip-audit` cannot see. **Measured, 2026-09-14**, with the Dependabot alerts API returning 403 to
this session's token and the dependency graph read instead:

- `GET /repos/8fqycwdt8v-oss/Chemclaw3/dependency-graph/sbom` — 447 packages: 439 PyPI, 7 GitHub
  Actions, 1 the repository itself.
- Every one of the four actions this repository uses (`actions/checkout`, `actions/upload-artifact`,
  `astral-sh/setup-uv`, `azure/setup-helm`) queried against OSV's `GitHub Actions` ecosystem:
  **zero advisories, at any version.** The query was sanity-checked against
  `tj-actions/changed-files`, which returns two.
- The whole PyPI half queried at the versions **the graph holds**, which is not the same set as
  the versions `uv export` produces: 14 advisory hits, deduplicating to **7 GHSAs** —
  `cryptography` 49.0.0 (GHSA-g6cj-pr64-35w5, **HIGH**) and `pypdf` 6.14.2 (six **MODERATE**).
  One high, six moderate.
- Neither version is in `uv.lock`: `cryptography` is locked at **50.0.0**, which is the version
  that advisory names as the fix, and `pypdf` at **6.16.2**.

**Why they persist is the finding.** GitHub's dependency graph for this repository is a *union over
the branch's history* rather than a snapshot of its current manifests: 23 packages appear at two
versions each, and `agent-framework-anthropic`/`-core`/`-openai` are still listed although M13
deleted them from `pyproject.toml` outright. `make deps-audit` audits the lockfile as it stands.
The two instruments answer different questions and neither is wrong.

## Decision

**Correct the claim; do not widen the gate to `github-actions`, and say why in the file.**

Widening would have closed nothing — that ecosystem holds none of the seven, and holds nothing at
all today. But an updater for an ecosystem nothing audits is still a control a reader assumes
exists, which is the defect the old header had. So:

1. `.github/dependabot.yml`'s header says **Python** where it used to say "a closure", carries the
   measurement above, and carries an explicit `ACCEPTED RISK` paragraph naming `github-actions` as
   audited by nothing that runs here. Pinning by commit
   (`test_every_action_is_pinned_to_a_commit_not_a_tag`) bounds *what runs* and says nothing about
   whether it is vulnerable.
2. `tests/test_deploy_chart.py::test_every_declared_ecosystem_is_audited_or_accepted` holds the
   choice rather than the coverage: every `package-ecosystem` declared is either mapped to a target
   `make ci` actually runs, or named in the file as an accepted risk. A third ecosystem added
   without either fails, and so does dropping `deps-audit` from `make ci` while the claim stands.
3. An OSV-backed audit of the actions closure is a `BACKLOG.md` row with the data source named. It
   is not built here because building it now would be building a gate against a set with nothing in
   it, which is the `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` shape.

**Not decided here:** whether to ask GitHub to re-scan so the seven alerts close. That is an
operation on somebody else's index, not a change to this tree, and no test here could hold it.

## Consequences

The push warning will keep saying 7 until GitHub's graph drops the historical versions. Anybody
comparing it against a green `make deps-audit` now has the file that explains why the two disagree,
with both numbers and the method.

## What keeps it true

- `tests/test_deploy_chart.py::test_every_declared_ecosystem_is_audited_or_accepted` — mutations:
  rewording the `ACCEPTED RISK` line fails it; removing `deps-audit` from `make ci` fails it;
  adding a third `package-ecosystem` with neither fails it.
- `tests/test_deploy_chart.py::test_every_action_is_pinned_to_a_commit_not_a_tag` — the bound that
  is real for that ecosystem.
- `tests/test_deploy_chart.py::test_the_dependency_audit_gates_every_branch_push_and_the_local_gate`
  — the half that is a gate, unchanged.
