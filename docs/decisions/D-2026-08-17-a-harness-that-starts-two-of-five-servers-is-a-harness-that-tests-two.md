# D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two — `/readyz` is a connector probe, not a dependency probe

**Status:** accepted
**Context:** the four-repo live lane (`infra/live/e2e-full-stack/`), run end to end for the second time

## Context

`infra/live/e2e-full-stack/up.sh` was written when `Chemclaw3-mcp` had two servers. The fleet grew
to five — `props`, `rxnpredict`, `chem`, `safety`, `calc` — and the harness kept starting two. The
README's process table kept listing two. Nothing failed in CI, because none of this runs in CI.

Running it is what found that, and it did not fail once. It failed three times, in three different
ways, each of which says something the next person needs:

1. **`chem` and `safety` are connectors Chemclaw3 dials.** Both bundles declare
   `127.0.0.1:8858/8859`, and under `CHEMCLAW_CONNECTORS_REQUIRED` an unreachable one is a hard
   startup failure of the front door, not a degraded connector. The front door did not start:
   `ConnectorsUnavailable: ... chem, safety`. This is the loud failure, and the easy one.

2. **A connector token has two halves, in two places.** The `start_*` function gives the *server*
   the value it verifies; a separate `export` gives the *front door* the value it sends. Adding
   only the first is quiet: `/healthz` is unauthenticated, so `/readyz` reported `chem=healthy,
   safety=healthy` while every `/mcp` call was rejected and the turn emitted `capability_degraded`
   with nothing anywhere naming a credential.

3. **The `calc` server is not a connector, and it still has to run.** Its manifest correctly stays
   off `CHEMCLAW_CONNECTORS_DIR` — it says so in a box — because
   `D-2026-08-16-the-physics-leaves-the-cache-stays` moved the *physics* to the fleet while
   Chemclaw3 kept the bundle, the cache and the fifteen tools. `connectors/calc/remote.py::calc_session`
   dials it on a cache miss. With it down, `/readyz` is **entirely green** — it probes connectors,
   and this is not one — and every calculator tool fails at call time with `CalcServerError`. This
   is how `predict_pka` failed on the first real turn.

## Decision

The harness starts all five servers and exports all five tokens. `chem`, `safety` and `calc` join
the fleet start, the restart dispatch and the README's table.

The general rule this leaves behind, which matters more than the five names:

> **`/readyz` is a connector probe, not a dependency probe.** A green `/readyz` says every declared
> *connector* answered its health route. It says nothing about a backend reached from inside a tool,
> and nothing about whether the caller holds the credential that backend verifies. Neither of those
> can be inferred from it, and both were inferred from it here.

## Consequences

- The two things `/readyz` cannot see are exactly the two that were broken for hours while it
  reported ready. A future dependency behind a tool — a second cache backend, a vector service —
  will have the same blind spot, and the harness is where it has to be covered, because
  `CHEMCLAW_CONNECTORS_REQUIRED` will not catch it.
- Ordering of `CHEMCLAW_CONNECTORS_DIR` is left as it was, with Chemclaw3's own bundles first, and
  that is now a documented choice rather than an accident: the in-tree `safety` bundle ships
  `skills/safety-screening/SKILL.md`, and the fleet's manifest declares no skills. Both name the
  same URLs, so the servers must run either way.
- The related failures found in the same run are recorded with their fixes in
  `tasks/live-test/full-stack-e2e-2026-08-17.md`, and the three that live in companion repos went
  out as their own pull requests.

## Alternatives considered

**Give `/readyz` a dependency probe for the calc server.** Rejected for now, and not comfortably.
It would have caught defect 3 directly. But `calc_server_url` is read inside a tool call, the front
door has no other reason to know about it, and a readiness route that dials every transitive
backend is a different contract from the one `/readyz` documents. The honest fix for a harness gap
is in the harness. If a second such backend appears, this should be revisited rather than
re-argued — the blind spot is real, it is only the cost of closing it that is in question.

**Put the fleet's `manifests/` first on the connector path**, as `Chemclaw3-mcp`'s own integration
guide suggests. Rejected here specifically: it wins the bundle-name collision for `chem` and
`safety` and thereby drops the `safety-screening` skill, which is architecture layer 3 and not
something a tool server can carry. The fleet-first ordering is right for validating the fleet; the
in-tree-first ordering is right for an end-to-end pass over the whole product.
