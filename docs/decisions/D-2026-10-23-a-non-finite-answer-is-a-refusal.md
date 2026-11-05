# D-2026-10-23-a-non-finite-answer-is-a-refusal — A non-finite answer is a refusal

**Status:** accepted · **Date:** 2026-10-23 · **Owner decision** (2026-10-24, option 1) · **Spans:** `Chemclaw3-mcp`
(`packages/mcp_server_kit/src/mcp_server_kit/finite.py`) · **Closes, with its pin:** the
`BACKLOG.md` row *"A NaN or infinity is refused in a request but not in an answer over `/rpc`"*

## Context

The fleet refuses a NaN or an infinity in every tool *argument*, on both channels
(`mcp_server_kit/finite.py`). Nothing governs one in an *answer*. The row said `/rpc` returns such a
float as `null` while MCP's text carries it; re-measured against `Chemclaw3-mcp` `main` with a
probe served by the real `connector_app(rpc=...)`, the behaviour is different and worse:

| tool returns | MCP text content | `/rpc` |
|---|---|---|
| a pydantic model with `nan`/`inf` (every `calc` and `rxnlabel` result) | `null` | **HTTP 500**, plain `Internal Server Error` (Starlette's `JSONResponse` renders with `allow_nan=False` and raises) |
| a `dict` with `nan`/`inf` | a bare `NaN` / `Infinity` literal | **HTTP 500**, as above |

So one computation reaches the caller three ways. Over `/rpc`, `RpcSession._unstructured` reads the
bare 500 as `McpServerFault` — "the server stopped", which callers retry, though the answer is
deterministic. Over MCP, the backend answer is decoded and handed to `connectors/calc/remote.py::
cached_remote` as a `dict` without validation against its contract model, so a `null` in a field the
contract types `float` is **persisted in the D-011 cache, which never recomputes**.

## Options

1. **Refuse in the tool, fleet-wide** — the kit wraps every served tool's function and refuses an
   answer carrying a non-finite float as a caller-safe `ValueError` naming its path
   (`overflow computed answer['scaled'], which is not a finite number`). Both channels then carry one
   answer: `/rpc` 400 `refused` → `McpRequestRefused`; MCP a tool error. Nothing is cached, nothing is
   retried. Costs: a tool that means "undefined" must say so with `None` in an optional field, not a
   NaN; one wrapper per tool call (a tree walk over the answer).
2. **Declare null on both channels** — `/rpc` renders with pydantic's JSON mode (NaN → `null`) as MCP
   already does for models; the contract fields become `float | None`. Costs: a null that means "the
   arithmetic failed" is indistinguishable from "not computed", it is cached permanently, and every
   consumer must handle it; a dict-returning tool still emits a bare `NaN` on MCP unless also changed.
3. **Status quo** — 500 over `/rpc` (retried as a fault), `null` over MCP (cached). Two answers for one
   computation, one of them a permanent wrong record.

## Decision

Decided by the owner 2026-10-24: **option 1**. A NaN is not a result in any field a chemist reads (a log S, a pKa, an
energy), the cache's never-recompute rule makes any accepted placeholder permanent, and refusing is
the only option that gives both channels the same, non-retryable, non-cached answer. It mirrors the
argument rule already in force, at the same seam. Measured on the fleet suite with the wrapper
installed: no shipped tool's test relied on returning a non-finite value.

## Consequences

`Chemclaw3-mcp` installs `refuse_non_finite_answers` beside `refuse_non_finite_arguments` in
`connector_app` (merged there as Chemclaw3-mcp#179, with its tests:
`packages/mcp_server_kit/tests/test_finite.py::test_a_non_finite_answer_is_refused_by_path`,
`packages/mcp_server_kit/tests/test_rpc.py::test_a_non_finite_answer_is_a_refusal_not_a_fault`). This
repository's client is unchanged; the fleet pins the refusal itself, and the BACKLOG row narrows to
pinning it from here in `tests/test_backend_rpc_fleet.py`.

Revisit when: a fleet tool has a field where a non-finite value is a meaningful answer (an
asymptote a chemist should read as such), which would need that field declared and exempted.
