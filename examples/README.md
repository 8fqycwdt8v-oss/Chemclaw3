# `examples/` — a runnable walkthrough

`research_demo.py` drives the system end to end with in-memory stores: no database, no Temporal, no
credentials. It is the fastest way to see how the pieces fit before running the real stack.

**Deliberately not shipped in the wheel** — `tests/test_packaging.py` asserts that. An example is
allowed to reach across layers for the sake of a readable narrative, which is exactly why it must
not become an import path anything depends on.

It is type-checked and linted like first-party code (`make type` covers `src examples tests`), so it
cannot quietly rot into a snippet that no longer runs.
