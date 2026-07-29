# D-037 — Tooling gaps: coverage, unified mypy scope, worker tests, preflight, skill-validate

**Context.** The review found tooling gaps that let regressions slip past the local gate.

- **Coverage.** No coverage measurement existed. Added `pytest-cov` (dev dep), a `make cov`
  target (kept out of the default `make test` so it stays fast/dependency-light), and
  `[tool.coverage]` config over the first-party packages. No hard `--cov-fail-under` yet — it
  can't be calibrated offline; set it from the first CI baseline (BACKLOG P2), then ratchet.
- **Pre-commit vs CI mypy drift.** The pre-commit mypy hook checked a narrower package set than
  the Makefile/CI, so a type regression in `eln/evals/mcp_servers/memory/report/scripts` passed
  pre-commit and failed CI. The hook now invokes `make type` — one source of truth.
- **Worker entrypoints.** `workers/*` had no direct tests. `test_workers` asserts both mains
  import cleanly, register non-empty duplicate-free workflow/activity sets, and cover their
  responsibilities (QM on hpc; ELN sync + cursor activities on background) — a wiring-drift guard.
- **API-key preflight.** `_default_chat_client` now fails at agent build with a clear
  "set ANTHROPIC_API_KEY" message instead of an opaque 401 on the first model call (injected
  clients skip it).
- **`make skill-validate`.** `scripts/validate_skills.py` validates every SKILL.md's frontmatter
  (name/description present, `name` matches its directory) and gates in CI, mirroring
  `kg-validate`/`eln-validate`, so a broken skill fails the build rather than vanishing from the
  agent's skill surface.

**Result.** New tests: `test_workers`, `test_validate_skills`, and an `_default_chat_client`
preflight case in `test_agent`. `make lint type` green; `make test` green offline. CI gains a
`make skill-validate` step.
