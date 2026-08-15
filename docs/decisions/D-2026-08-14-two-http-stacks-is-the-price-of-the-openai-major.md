# D-2026-08-14-two-http-stacks-is-the-price-of-the-openai-major — The dependency bump, what the majors cost, and the one cap that stays

**Status:** accepted · **Date:** 2026-08-14

## Context

A full-codebase sweep checked every load-bearing framework's current release against what this
repository pins. Fifteen packages had moved within their existing specifiers; three had crossed a
major boundary. The bump is otherwise routine, and only the three majors need a decision recorded.

Read against this tree, they are not one question but three:

- **`mcp` 2.0.0** renames `FastMCP` to `MCPServer` and rebuilds the low-level server around a shared
  dispatcher. `connectors/server.py` patches `FastMCP._tool_manager.call_tool` **twice**, and one of
  those patches is the per-call caller re-binding — a security property.
- **`openai` 3.x** and **`starlette` 1.6** are the same change wearing two hats. openai 3.0 makes
  **httpx2** its default client and stops installing `httpx`; `starlette.testclient` already emits
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2
  instead` on the versions installed before this bump. httpx2 (2.10.0) is Pydantic's stewardship of
  httpx under a new name — its own README says it aims "to honour the original project's design" —
  so it is a rename with maintenance behind it rather than a redesign.
- The rest are patch and minor releases inside specifiers this repository already declares.

One resolution fact shaped the decision and is worth recording, because it is not visible from
`pyproject.toml`: **openai 3 was unreachable until `langchain-openai` moved too.** 1.4.2 requires
`openai<3.0.0`; 1.5.1 requires `openai<4.0.0`. An `--upgrade-package openai` alone resolves to
2.54.0 and reports success, which reads as "the major is not available yet" and is not what is
happening.

## Decision

**1. Take the safe tier.** `langchain` 1.3.15, `langchain-core` 1.5.5, `langgraph` 1.2.11,
`langchain-anthropic` 1.5.6, `mcp` 1.29.0, `sse-starlette` 3.4.8, `temporalio` 1.31.0,
`pydantic-settings` 2.15.0, `rdkit` 2026.3.5, `ruff` 0.16.3, `uvicorn` 0.52.3, `pypdf` 6.16.1 and
the dev pins. `mcp` 1.29.0 is inside the existing `<2` cap, which is the cap working rather than an
exception to it.

**2. Take `openai` 3.1.0 (with `langchain-openai` 1.5.1) and `starlette` 1.6.0 with `fastapi`
0.141.1, and accept two HTTP stacks in the image.** `httpx2` is
declared explicitly in `pyproject.toml` rather than left transitive, because two of this
repository's own concerns now depend on which stack a call uses. `httpx` stays: `mcp` 1.x and the
`langchain-*` packages require it, so it is in the closure no matter what this repository imports,
and the four first-party modules that `import httpx` keep doing so. Migrating them would be a
rewrite of every transport in the tree to buy a smaller image, at a moment when the thing that
would make the migration *complete* — `mcp` 2.x — is deliberately not being taken.

That is the cost, stated plainly: the image carries `httpx` 0.28.1, `httpcore` 1.0.9, `httpx2`
2.10.0, `httpcore2` 2.10.0, `httpx2-jsfetch` and `truststore` — measured after the bump, not
predicted — and a reader of a traceback has to notice which client raised. It is recorded here so
it is a known price rather than something discovered in an image scan (the same discipline the
`langchain-google-genai` closure note in `pyproject.toml` applies to its own 22 MB).

**3. `mcp` 2.0.0 is not taken, and the `<2` cap stays.** Two stacked patches on
`FastMCP._tool_manager.call_tool` are what make a connector re-bind the caller per call; a release
that renames the class those patches attach to lands a gate on a surface that no longer exists, and
the failure mode of a patch that silently stops applying is an unauthenticated or
mis-attributed call rather than an import error. `tests/test_connector_transport.py` and
`tests/test_connector_identity.py` are the ratchets that would catch it, and they are exactly why
the migration deserves its own change with its own verification rather than a line in a lockfile.
`docs/planning/DEFERRED.md` carries the row and names the cap as its trigger.

**4. ruff 0.16 does not reformat this repository's documents.** It formats fenced code blocks inside
Markdown now, which would rewrite snippets in `docs/archive/`, a merged planning document and two
session findings. Those are records — a merged decision is never edited, and a session finding is
what somebody observed — so `[tool.ruff.format] exclude = ["**/*.md"]` keeps the formatter on `.py`
where it belongs. The alternative is a formatter editing history to satisfy a style rule.

## What was verified

Against a real Postgres 16 + pgvector 0.8 (built in the session container, which has no Docker), so
the Postgres-backed tests ran rather than skipped:

- `make lint type test` green.
- Every declaration validator: `kg-validate`, `skill-validate`, `connector-validate`,
  `datasource-validate`, `template-validate`, `safety-validate`, `eln-validate`.
- `make eval-strict`: 4 gated metrics failed, all 4 by design, **0 regressions**.
- `make deps-audit` on the bumped lock: no known vulnerabilities.

## Consequences

- `uv sync --locked` is now what CI runs, so a `pyproject.toml` edit that was never locked fails the
  gate instead of being silently re-resolved. That gate and this bump landed together deliberately:
  the first thing it protects is the lockfile this ADR just moved.
- `langchain` 1.3.15 exposes two things this repository does not adopt here and that are worth
  naming so a later reader does not assume they were missed: `state_schema` on `wrap_tool_call`, and
  `trace_policy` on `AgentMiddleware`. Eleven `@wrap_tool_call` middlewares would be the surface for
  the first; neither is a fix for a problem this tree currently has, and adopting an API because it
  is new is how a chain acquires behaviour nobody asked for.
- The `httpx`-versus-`httpx2` split is a live question for exactly one future change — the `mcp` 2.x
  migration, which is where the last first-party reason to keep `httpx` goes away. Whoever takes
  that row should close this one with it.
