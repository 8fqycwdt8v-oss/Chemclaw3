# D-2026-08-06-a-pair-rule-is-a-cross-product — A pair rule is a cross-product, and the list is the caller's

**Status:** accepted · **Date:** 2026-08-06

## Context

From the whole-codebase security sweep. The connector-boilerplate lane reported the `safety` bundle
as "the one bundle that neither threads nor bounds its CPU work". It is worse than the phrasing
suggests, and it applies to two tools rather than one.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### 1. The screens amplify, and the request cap cannot see it

Both screens in `science/safety/` check their pair rules as a **cross-product**: every component
matching one side of a rule against every component matching the other
(`[(a, b) for a in left for b in right if a != b]`). So the *result* grows with the square of the
input while the request stays small.

Measured on `screen_reaction`:

| request | flags | event loop blocked |
|---|---|---|
| 8.2 KiB | 103,040 | 0.77 s |
| 13.0 KiB | 251,000 | 2.48 s |

`connector_max_request_bytes` (1 MiB) is no bound on this, because the amplification is in the
**response**: 13 KiB of SMILES is three orders of magnitude below the cap. At the cap the input
would be ~78,000 components.

`screen_genotoxic_alerts` has the identical shape and was not reported — 640 components produced
102,400 alerts in 933 ms. **The defect was generalised before it was fixed**, so the bound is one
function (`require_screenable_size`) that both screens call, rather than a fix in the one place
that happened to be looked at.

Bound: `safety_max_components`, default 64 — far above any real reaction, where the largest shipped
ELN entry has well under a dozen species. At the limit the worst case is **1,088 flags in 26.3 ms**.

**Refused, never truncated.** A screen that silently dropped components would report "no rule
matched" for chemistry it never looked at, and every tool description in this package says in bold
that an empty result means no rule matched and *never* that something is safe. Truncating would
make the tool lie in exactly the way its own words forbid.

### 2. Even bounded, the work does not belong on the event loop

SMARTS matching is CPU-bound C++ that holds the GIL, and a connector server answers every connected
chat turn on one loop. `connectors/chem/server/tools.py` already records this reasoning and uses
`asyncio.to_thread`; `safety` was the last bundle not to follow it, and it is the bundle where a
caller controls the size of the work.

Both screens are now awaited off the loop. `ich_impurity_limit` is a dictionary lookup over two
small tables and is left alone — offloading it would cost more than it saves.

The bound and the offload are independent and both are needed: the bound stops one caller
manufacturing seconds of work, the offload stops even legitimate work from stalling every other
turn the process is serving.

## Consequences

- New setting `safety_max_components` (default 64, `gt=0`), in the `safety` section mixin and in
  `.env.example` per the CI-enforced parity check.
- A component list over the limit raises `SafetyRulesError`, which derives from `ChemclawError` and
  so from `ValueError` — the family `connectors/server.py::_sanitize_tool_errors` passes to the
  caller unchanged. The message says what the limit is and why, so the model can correct the call
  in the same turn rather than retrying it.
- Worst case at the limit: 1,088 flags / 26.3 ms, off the event loop. Previously unbounded.
- All three controls are mutation-proven: removing the bound fails both refusal tests, removing the
  `to_thread` fails the loop test.

## Alternatives rejected

- **Truncating the component list.** Makes the tool report "nothing matched" about chemistry it did
  not screen. That is the single failure mode this package's tool descriptions are written to
  prevent.
- **Bounding only `screen_hazards`,** which is what was reported. The genotoxicity screen has the
  same cross-product and was measured with the same blowup; fixing one would have moved the defect
  rather than closed it.
- **Relying on `connector_max_request_bytes`.** Measured not to bound this at all — the defect is
  response amplification from a tiny request.
- **Capping the number of *flags* instead of the input.** Bounds the payload but not the work: the
  cross-product is already computed by the time you could truncate its result.
- **A quadratic-free matching algorithm.** There is no defect in the cross-product itself — pair
  rules genuinely relate every left component to every right one, and a real reaction has under a
  dozen species. The input size is the thing that was never anybody's to choose.
