# Phase 2 — Automated Baseline

Captured on the audit branch `claude/codebase-audit-hardening-69v233` at HEAD `651459e`,
before any modifications. This is the "before" snapshot to diff against at handover (Phase 11).

## Commands run (discovered in Phase 0)

| Gate | Command | Result |
|---|---|---|
| Lint | `uv run ruff check .` | **All checks passed** |
| Format | `uv run ruff format --check .` | **196 files already formatted** |
| Types | `uv run mypy chemclaw agents bo calc eln evals kg mcp_servers memory report scripts workflows workers tests` | **Success: no issues found in 186 source files** |
| Tests | `uv run pytest` | **356 passed, 25 skipped, 0 failed** (151 s) |
| CVE audit | `uv run --with pip-audit pip-audit` | **No known vulnerabilities found** |
| Secrets (history) | `git log -p --all \| grep -E <secret patterns>` | **No real secrets** — only config-key *names* |

The canonical gate is `make check` (= lint + type + test), mirrored by `.github/workflows/ci.yml`.

## Test suite detail

- **356 passed, 25 skipped.** Zero failures. Run twice-equivalent: the suite is deterministic
  (no flaky tests observed; the BoFire/BoTorch `OptimizationWarning`s are library-internal retry
  noise, not test failures).
- **The 25 skips are environment skips, not disabled tests:**
  - 13 skipped in `tests/temporal_env.py` — Temporal test server binary can't be downloaded in the
    offline sandbox. These run for real in CI (and locally with `make up`).
  - 12 skipped in `tests/pg.py` — Postgres not reachable in the sandbox. CI spins up
    `pgvector/pgvector:pg16` and runs them (see `ci.yml` services block).
  - **Interpretation:** the Postgres calculation-store and the Temporal workflow/durability paths
    are exercised in CI but *not* in this offline audit environment. Findings that touch those paths
    are reasoned from code, and any change to them must be validated in CI (see execution plan).

## Lint / type detail

- Ruff config is deliberately strict: `E,F,W,I,B,C4,UP,D` (incl. mandatory docstrings, google
  convention). It passes cleanly across all 196 files.
- mypy runs `--strict` and passes across 186 source files. Third-party libs with partial/no stubs
  (`rdkit`, `bofire`, `pandas`, `sklearn`, `frontmatter`, `networkx`, `drfp`) are `follow_imports=skip`
  — a documented, reasonable escape hatch, but it means type safety stops at those library seams.

## Dependency audit detail

- `pip-audit`: **no known CVEs** across the resolved dependency set (`uv.lock`).
- Dependencies are version-floor pinned (`>=`) in `pyproject.toml` with a fully-resolved `uv.lock`.
  No abandoned/unmaintained packages flagged.
- Notable heavy transitive stack: `torch`, `xgboost`, `botorch` (via `bofire[optimization]`).

## Secrets scan detail

- Full-history scan (`git log -p --all`) for AWS keys, private-key blocks, GitHub/Slack/OpenAI
  token shapes, and `api_key = "…"` literals: **no real secret values** found in any commit.
- The only matches are **references to secret *names*** (e.g. `llmApiKey: "CHEMCLAW_LLM_API_KEY"`
  in the Helm chart) — i.e. the env-var key a secret is read from, not a value. Correct pattern.
- Dev-default credentials exist and are expected: `POSTGRES_PASSWORD: chemclaw` in
  `infra/docker-compose.yml` and the default DSN `postgresql://chemclaw:chemclaw@localhost:5432/chemclaw`
  in `chemclaw/config.py`. These are dev-stack defaults, ENV-overridable. Flagged in
  `04-security.md` only insofar as the default must never survive into a shared/prod deployment.

## Dead-code / unused-export tooling

- No dead-code tool (`vulture`) is configured in the repo. Ruff catches unused *imports/locals*
  (F401/F841) and passes, but does not catch unused *cross-module exports* or unused *declared
  dependencies*. Those are covered manually in `06-duplication.md`.

## Signal vs. noise summary

**The automated gate is genuinely green — this is not a codebase that fails its own CI.** The
merge left the deterministic quality gates intact. That means the audit's real signal is **not**
in the automated layer; it is in the areas automation can't see: cross-branch design divergence
(`03-consistency.md`), security *design* gaps that pass type/lint (`04-security.md`), latent
correctness/resilience issues on paths not covered by the offline suite (`05-correctness.md`),
duplicate implementations from independent branches (`06-duplication.md`), and layering/boundary
drift (`07-architecture.md`).
