# D-2026-09-04-wiring-an-endpoint-bundle-is-invisible-to-the-ratchet — rxnpredict, enabled by default

**Status:** accepted · **Date:** 2026-09-04 · Third bundle of the
`D-2026-08-09-a-connector-we-do-not-run` shape, after `chem` and `safety`.

## Context

`Chemclaw3-mcp`'s `servers/rxnpredict` has been built and serving for weeks, and
`grep -rn rxnpredict src/` in this repository found **one docstring mention and nothing else**: no
bundle, no `connectors:` entry in the chart, no token obligation. So `skills/protocol-generation`
routed a chemist's "how would people run this" question entirely through precedent — which answers
from what *this* corpus holds and is silent on a transformation nobody here has run.

That is the seam working as designed being easy to miss, and worth stating as the cost of the
design: **zero core edits also means zero core changes to remind anybody.**

## Decision

`rxnpredict` is wired as a declaration-only bundle, **enabled by default**.

The surface was read off the **running server** rather than off the backlog row: six tools, every
one `read_only`, and no state-changing surface at all — upstream's `clear_prediction_cache` is
deliberately not served. Port 8857 and `CHEMCLAW_RXNPREDICT_TOKEN` confirmed against the fleet's own
`MODULES.md`, which is its only port registry.

The enablement default is taken rather than re-argued: six read-only predictors, no store, no code
execution, and no state-changing tool on the server, so the worst an unapproved plan does with them
is read a guess. A deployment that does not host the server names a narrower
`CHEMCLAW_CONNECTORS_ENABLED`, or leaves the bearer unset and every call is refused at the server.
This is the opposite posture to `pyexec`, and the difference is the capability rather than the
mechanism.

## The row was wrong about the context floor, and that is the finding to carry

The row said this "raises the context floor — `tests/test_context_floor.py` ratchets it".

Measured: **42,730 before and 42,730 after — zero delta.** The ratchet reads bound tools off the
compiled graph's `ToolNode`, and an **endpoint** connector's tools are fetched from a live MCP
session that never opens offline. Verified directly: `resolve_compound` and `screen_hazards` —
`chem` and `safety`, the same declaration-only shape — are absent from the 61 tools it counts.

The cost is real and simply unratchetable here: **2,526 tokens per turn** across the six, largest
`predict_forward_reaction` at 667, all under `MAX_SINGLE_TOOL_TOKENS`. It is observable in a
deployment only as `chemclaw_connector_tool_schema_tokens{connector="rxnpredict"}`.

**So: wiring an endpoint bundle is invisible to the ratchet and is not free.** `CLAUDE.md` already
says the ratchet cannot see this half; this ADR is the worked instance.

## Six tools nothing tells the model to use would have been cosmetic

`skills/protocol-generation` now names them, with the rule that makes them safe: reach for them
**after** the record has been asked and found empty, never before — a predictor consulted first
answers confidently about a coupling this site has run forty times, replacing forty real runs with
a guess — and the result is `predicted`, never `precedent`, which is the distinction the skill's own
basis rule already turns on. The per-model spread is the part worth reading: arms that disagree say
the transformation is outside what the models saw.

## Consequences

**Two guards, because the row warned about `pyexec` and a comment is not a gate.** Every
externally-hosted connector's `url` port must have a matching `egressPorts` entry, and the template
must actually emit it — asserted in both directions, because the egress rule restricts by port
independently of the peer list, so a destination with no matching port still drops. Both verified by
mutation.

**A guard that had never run contained a finding.** `helm` installs fine in this sandbox, so the
chart skips are a *default* rather than a limit — with it on PATH the chart suite is 122 passed, 0
skipped and `helm template | kubeconform -strict` gives 29 valid / 1 skipped (the OpenShift Route,
as documented). Running it turned `test_a_release_that_enables_no_connector_does_not_render`'s
all-disabled arm red: its parametrisation typed out the seven bundles that existed the day it was
written, so an eighth turned "all disabled" into "all but one disabled" and the release correctly
rendered while the arm reported a broken guard. Now derived from `values.yaml`. A second instance
of the same shape was found in the same branch and is recorded in
`D-2026-09-04-a-compare-and-set-on-the-document-is-silent-about-the-decision`.

**Two gaps left open deliberately.** `infra/live/processes.sh::start_fleet_bundles` iterates
`chem safety` only, so `make live-up` does not start this server and probes `pr-01`…`pr-06` would
meet an unreachable connector — starting a torch-backed predictor in the live lane is a decision
about checkpoints rather than a wiring omission. And nothing here can verify the declared→served
direction for an endpoint bundle; that is `Chemclaw3-mcp`'s `assert_manifest_matches`, against the
running server, which is the only place it can be caught.
