# `examples/` — a runnable walkthrough

`research_demo.py` drives the system end to end with in-memory stores: no database, no Temporal, no
credentials. It is the fastest way to see how the pieces fit before running the real stack.

**One thing it does need, and it is not a credential.** The solubility model moved out of this
repository (`D-2026-08-16-the-physics-leaves-the-cache-stays`), so section 3 calls the `calc` server
from `Chemclaw3-mcp` at `CHEMCLAW_CALC_SERVER_URL` — a loopback service, no API key. Without it the
walkthrough refuses with one line naming the setting and exits non-zero, rather than inventing a
number or ending in a traceback; `tests/test_research_demo.py` drives the whole loop against a fake
server, so the example stays covered with nothing running.

**Deliberately not shipped in the wheel** — `tests/test_packaging.py` asserts that. An example is
allowed to reach across layers for the sake of a readable narrative, which is exactly why it must
not become an import path anything depends on.

It is type-checked and linted like first-party code (`make type` covers `src examples tests`), so it
cannot quietly rot into a snippet that no longer runs.
