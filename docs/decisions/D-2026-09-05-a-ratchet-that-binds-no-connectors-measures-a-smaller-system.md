# D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system — the floor was 42,730; the prefix is 75,695

**Context.** `tests/test_context_floor.py` exists to bound the static prefix every model call pays.
Its `_bound_tools` reads the surface off the compiled graph's `ToolNode`, and its docstring makes
the strong claim that this is why it cannot drift: *"any future tool source — a middleware, a
connector, upstream — lands here the moment it is bound."*

It called `build_langgraph_agent` **without the `connectors=` argument that function accepts**. So
the claim was true of the *method* and false of the *fixture*: the ratchet measured 61 tools and
**42,730** tokens against its 43,500 ceiling, while a shipped turn binds 113. This is the same
failure `D-2026-09-04-wiring-an-endpoint-bundle-is-invisible-to-the-ratchet` recorded from the other
side — that ADR measured "42,730 before and after, zero delta" for a newly wired endpoint bundle and
correctly concluded the ratchet cannot see one. What neither noticed is that it could not see the
**in-repo** bundles either, for a different reason: nothing passed them.

**Measured.** Building the connector surface from the repository's own manifests — each in-repo
bundle's real `FastMCP` server over an in-memory session, narrowed by production's own
`connector_specs` — the `default` profile binds **92 tools / 66,157 tokens**. The half this tree
cannot see, measured against the sibling checkout, is **9,538 tokens over 21 tools**. **The real
shipped prefix is 75,695 tokens over 113 tools**, against a ratchet reporting 42,730.

**Decision.**

1. *The ratchet binds connectors.* Ceiling re-baselined 43,500 → **67,000** for the half this tree
   can build, with `SERVED_ELSEWHERE` naming the remainder and a test that fails when it drifts.
   Nothing was added; the measurement got honest, which is the same sentence the 2026-08-29
   re-baseline needed and for the same reason. Counterfactually verified: deleting `connectors=`
   again fails the new test by name.

2. *The compaction defaults are re-derived against the real prefix.* At 75,695,
   `effective_trigger(73_500)` returns **1** — clear every reclaimable tool result on every model
   call — and the thread got 24,305 of a 100,000 budget. Both `agent/context_budget.py` and
   `tests/test_compaction.py` asserted the shipped configuration was *not* floored; both were
   asserting it against the connector-less prefix. Trigger 73,500 → 108,000 and budget
   100,000 → 135,000, which restores the thread allowance 2026-09-04 believed it was leaving, and
   the test now measures the prefix with connectors bound.

   **Cost, because it is a real change**: the per-request bound rises 35%, at the bottleneck this
   review names as the binding one.

3. *A new finding recorded rather than fixed here.* Four `bo` endpoint tools are the four widest
   schemas in the whole prefix — **12,055 tokens for four inlined copies of one
   `OptimizationProblem` model.** Narrowing them is a `connectors/bo/` change and is queued.

**And the prefix is byte-stable, which is what makes server-side caching possible at all.** Measured
rather than argued: two turns with different actors, correlation ids and threads send byte-identical
prefixes — 321,856 chars, same SHA-256. Across *processes* they differ in exactly one thing, the
`retrieved-note-<nonce>` tag. So with `CHEMCLAW_FRAMING_ENVELOPE_SECRET` unset, a server-side prefix
cache is bounded to one entry per pod-process — six cold 75,700-token prefills at `maxReplicas: 6`,
re-paid every rollout. With it set, byte-identical everywhere. The chart already declares that
secret and already warns when it is unset, for an unrelated reason. Both halves are now asserted,
the cross-process one by a real two-subprocess test.

This matters because `agent/llm_provider.py` returns `[]` for any provider that is not Anthropic and
the chart ships `openai_compatible`, so ~88% of every request is the same bytes re-prefilled at full
price. The remedy is the endpoint's (`--enable-prefix-caching` or equivalent) and is a deployment
decision, not code — but shaping the request so that remedy can actually hit **is** code, and one
unset secret was silently defeating it.

**Deliberately not done: making the helper roster lazy.** `_subagents()` is 18.81 ms of a 42.48 ms
graph build, and the objection expected to be decisive turned out to be wrong — `get_subgraphs()` is
empty, so laziness would not change the event stream. It was still declined: it saves nothing on a
delegating turn, degrades `governed_roster`'s build-time check to a promise, and moves a build
failure into a tool call. Building the whole graph off the event loop takes all of it out of the
loop's way instead, without any of that — and **that** change was made, with its own honest number:
worst-case loop stall 52.2 ms → 16.6 ms, a 3.1x reduction rather than the elimination first claimed,
because a thread buys no parallelism when the GIL is held between switch intervals.
