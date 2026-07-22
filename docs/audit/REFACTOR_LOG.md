# Refactor Log (Phase 10)

One line per executed backlog item: finding ID · what changed · commit · test evidence.
Full suite (`make check`, 356 tests) is re-run at each wave boundary; per-item changes are
verified with `ruff` + `mypy --strict` + the targeted test file(s) for the area.

| ID | Change | Commit | Test evidence |
|----|--------|--------|---------------|
| A1 · INV-1 | Declare `httpx>=0.28` as a direct dependency in `pyproject.toml`; relock/sync | _pending_ | ruff ✓, `uv lock`/`uv sync` resolved (170 pkgs); no behavior change |
