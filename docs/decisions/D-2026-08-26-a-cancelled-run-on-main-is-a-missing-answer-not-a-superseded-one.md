# D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one — the CI review of 2026-08-26

## Status

Accepted. Amends D-117 (which moved the gates to where GitHub Actions reads them) and the
concurrency decision recorded in `ci.yml`'s own comments. Neither is reversed: both were right about
what they changed and wrong only about a case they did not separate.

## Context

`.github/workflows/` holds two workflows and no deployment — `ci.yml` (`check` + `chart`) and
`image.yml`. The CD half is a `DEFERRED.md` row with a concrete trigger (a real cluster, its
registry and credentials), which is the correct place for it and is not revisited here.

The pipeline was in good shape before this review: `make ci` and `ci.yml` run an identical gate set,
`--locked` prevents a lockfile that exists nowhere else, `deps-audit` classifies its output instead
of trusting an exit code that means two different things, and every gate carries a comment naming
the failure it exists to catch. The findings below are corrections to a careful pipeline, and three
of the four largest are cases where **a comment stated a measured number that had since gone stale**
— the same failure mode this repository has now hit in prose, in a config knob and in an audit
column.

Everything below was measured against the last 30 runs of each workflow and the per-step timings
GitHub records, not inferred from reading the YAML.

## The findings

### 1. `main` was cancelling its own gate, and nothing re-ran it

The concurrency group is `head_ref || ref_name`. On a push to the default branch that evaluates to
`main`, so two merges landing inside one run's duration cancelled the earlier one — permanently.

Measured over the 30 `ci` runs to 2026-08-26, **three commits that are ancestors of `origin/main`
today have no completed run of that workflow at all**:

| commit | title | outcome |
|---|---|---|
| `548266233b` | Fix the publish seam: nothing reached the projectors (#223) | cancelled 9.4 min in |
| `9dfb02a5f6` | Fix six defects a review found in the ELN transcription chain | cancelled |
| `3937fe568d` | A `labels:` block says what a source carries, not whether to label it | cancelled |

The distinction the original key missed: on a topic branch a cancelled run is a **superseded
answer** — the newer push is computing the thing the older one was asked. On `main` the two runs are
about *different commits*, so the cancellation does not supersede an answer, it deletes one that was
never produced. The whole claim of a merge gate is that what is on the default branch passed it.

`cancel-in-progress: ${{ github.ref_name != 'main' }}` in both workflows. The branch case keeps its
cost saving unchanged.

### 2. The timing comments were stale by 3.6x, and the headroom claim was false

`ci.yml` said the gate runs in **4m38s** and that "30 is already 6x headroom and raising it would
only lengthen the next burn". Re-measured over the 14 successful runs in the last 30: **median 16.8
min, max 22.0 min**. Actual headroom on the worst observed run was **1.36x**, not 6x.

That matters beyond the wrong number. The comment argues *against* raising the bound by citing
headroom the job no longer had, and a spurious cancellation at that bound produces exactly the
symptom the same comment describes as fixed — a burnt job reporting no test name. `timeout-minutes`
is 45, which restores ~2x on the worst run actually observed. The inner `pytest-timeout` cap (180s)
is unchanged and remains the bound that names a hung test; this outer one only catches a hang that
escapes it.

### 3. 87% of the `check` job was one serial step, and the cheapest failures reported last

From the step timings on `d8c312a`: `make lint type cov` was **12m06s of a 13m56s job**. All eleven
validators plus `deps-audit` together were 1m50s. Measured locally, the split inside that step is
`make lint` **1s** and `make type` **68s** — so ~1 minute of it is lint and type and ~11 minutes is
the suite.

A misformatted file or a bad annotation therefore cost a full ~14-minute job to hear about, behind a
Postgres service container and a migration neither of them touches. They now run in a `static` job
in parallel, where the same failure lands in about ninety seconds. `static` deliberately does not
gate `check`: making the slow job wait on the fast one would add the fast one's duration to every
green run in order to save nothing.

`pytest-xdist` is the larger lever and is **not** taken here. The suite looks parallel-safe —
`TEST_SCHEMA` is already pid-suffixed so each worker would get its own Postgres schema for free, and
Temporal uses `start_time_skipping()` on ephemeral ports — but "looks safe" is not a measurement,
and the sandbox this was reviewed in is too slow for its number to mean anything about a runner.
It is a `BACKLOG.md` row with the hypothesis written down, not a change.

### 4. `image.yml` held `actions: write` and no step has ever needed it

No API call, no cache deletion; `upload-artifact` authenticates to the artifact service with its own
token. `git log -S` shows the line arrived with the file's first commit and was never argued for,
while every other grant in these workflows says why it exists. Write access to Actions in the one
workflow that builds the shipped artifact is the wrong thing to hold by accident. Removed.

### 5. A supply-chain gate whose reach was a function of commit frequency

`deps-audit` scans `uv.lock` for known CVEs, blocking, in both workflows — deliberately, and that
decision stands. But an advisory published against an *unchanged* dependency was invisible until
somebody happened to push. A weekly `schedule:` makes the audit a property of the dependency closure
rather than of activity. `workflow_dispatch:` is added alongside it because re-running the gate
against a ref otherwise costs an empty commit, which this repository's own rules forbid pushing.

### 6. The pipeline pinned its Python closure and floated everything else

`actions/checkout@v4` is a mutable reference: the tag is repointed on every v4 release, and the
action runs with the job's token in the workflow that builds the shipped image. The same tree pins
its Python dependencies with `--locked`, audits them against an advisory database and emits an SBOM.
`kubeconform` was fetched with no checksum and `syft`'s installer was piped straight into `sh` — the
latter also runs a *truncated* download, because a half-transferred script is a valid prefix.

All four actions are pinned to a commit with a trailing `# vX.Y.Z`; both binaries are verified with
`sha256sum -c` before they execute. The kubeconform digest was cross-checked against the release's
own `CHECKSUMS` file rather than taken from one download.

A pin with no updater trades a supply-chain risk for a staleness one, so `.github/dependabot.yml`
carries a `github-actions` entry — Dependabot rewrites the digest and the version comment together —
plus a grouped `uv` entry, grouped because every bump runs a ~17-minute job with a service container
and this tree pins no upper bounds.

### 7. An SBOM of an image nothing keeps

The SBOM step cost 1m34s on **every** pull request and retained a 90-day artifact for an image
thrown away when the job ended. Its own comment states its purpose as answering "what was in the
image that produced this audit record" — a question that can only be asked about an image something
ran. Gated on `main`. The build, the revision check and both dispatch smokes still run on every PR,
because those catch a *broken* image; the SBOM only describes a *kept* one.

### 8. The drift half of the science gate ran nowhere

`eval-strict` fails on a regression — a case that stopped passing. `eval-baseline-check` fails on a
**drift**: an aggregate quietly worse against `data/evals/baseline.json` with no single case
flipping. It existed for exactly that, no workflow ran it, and it costs ~1s. Now a step in `ci.yml`
and a prerequisite of `make ci`.

## The test that should have caught the parity drift and did not

`test_every_gate_make_ci_runs_is_a_step_ci_yml_runs` sliced the workflow in two on a literal
`  chart:` and named the halves `check_job` and `chart_job`. Adding a third job made the first
"half" silently contain two jobs — and the test still passed, because it unions the buckets and a
union does not care how the text was cut. A test that survives the change it should have noticed is
not evidence. It parses the jobs now.

Rewriting it added the **reverse** direction, and that immediately found something: a step in
`ci.yml` that `make ci` does not run breaks CLAUDE.md's contract ("a green `make` locally means a
green CI") from the other side. The one legitimate case is `db-migrate`, which is not a gate but the
database the gates run against; it is named in an explicit `setup` set, so a *second* non-gate step
has to be argued for rather than slipping in.

Three further tests pin what this ADR changed — the default-branch exemption, the action pinning,
and that a downloaded binary is checksummed before it runs. Each was verified to **fail** when its
fix is reverted, because a guard that cannot fail is the shape this repository has now removed twice
(`reject_widening`, `set_current_specialist`).

## Consequences

- A merge to `main` is never left ungated, at the cost of one non-cancelled run per rapid merge.
- Lint and type answer in ~90s instead of ~14 min; the slow job is unchanged in content.
- Every third-party action and downloaded binary in the pipeline is content-addressed.
- The dependency audit runs weekly whether or not anyone pushes.
- ~1m34s and a 90-day artifact come off every pull-request run.
- `make ci` and `ci.yml` now agree in *both* directions, enforced.

## What this deliberately does not do

- **No registry push or rollout.** Still a `DEFERRED.md` row; nothing here changes its trigger.
- **No `pytest-xdist`.** A `BACKLOG.md` row with the parallel-safety evidence, to be settled by one
  experiment on a real runner rather than by an argument in this file.
- **No image scan.** `BACKLOG.md` already carries it with the reason it came back off: its last run
  reported two packages the build's own filesystem listing says are absent, and a gate whose last
  word contradicts the artifact it scanned makes every future red build ambiguous.
