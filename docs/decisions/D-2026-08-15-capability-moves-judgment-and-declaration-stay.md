# D-2026-08-15-capability-moves-judgment-and-declaration-stay — a bundle can be declared here and served elsewhere, and `chem` is the first

**Status:** accepted · **Date:** 2026-08-15 · Applies `D-2026-08-09-a-connector-we-do-not-run` to the first shipped bundle, and sets the rule the rest of the migration follows.

## Context

Scientific capability is moving to [`Chemclaw3-mcp`](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp);
this repository keeps infrastructure. `chem` — four pure RDKit tools — went first because it is the
cleanest: no store, no network, no durable state.

The obvious execution was to delete `src/chemclaw/connectors/chem/` entirely. That is wrong, and the
evidence is countable: two profiles, five eval probe files, two `SKILL.md`s and the agent's own
prose name these four tools **by string**, and `chemclaw_agent.available_tool_names` builds the set
four validators check against by reading manifests out of `connectors_dirs`. Delete the manifest and
`make skill-validate`, `prose-validate`, `template-validate` and the profile check all fail — on
references to tools that *exist*. That is the D-117 defect, which this repository has now hit twice.

## Decision

**Capability moves; judgment and declaration stay.** One sentence, and it decides every remaining
tranche:

- **Capability** is the server: `server/tools.py`, the engines it calls, the data baked into its
  image. It moves.
- **Declaration** is `connector.yaml` — the tool names, their read-only/state-changing
  classification, the address. It stays, because it is what this repository's validators, profiles
  and gates resolve against, and because `D-2026-08-09` made hosting a deployment fact rather than a
  capability fact.
- **Judgment** is `skills/`. It stays: a skill is layer 3, how a chemist should act on a result, and
  `skills_dirs()` reads bundle skills trees out of this tree. `chem` has none; `safety` does, and
  this is the rule that keeps it here when its engine leaves.

**The chart shape is `server: true` *and* `url:`**, which is not the obvious pairing and cost a live
defect to learn. `server:` mirrors whether the *manifest* declares an `endpoint:` — `chem`'s still
does — while `url:` says somebody else hosts it. The templates gate on
`if and $cfg.server (not $cfg.url)`, so the url suppresses the Deployment and the Service; and
`chemclaw.connectorUrls` visits only `enabled && server` entries, so `server: false` would make the
address **ignored** and leave the front door dialling the manifest's loopback default in production,
with nothing failing. `test_a_connector_url_is_only_declared_beside_a_server` documents itself as
"vacuous over the shipped values by design … so the first entry that sets `url` is checked against
the one shape it works in". That entry is this one, and it caught it.

**A cross-repo citation is `Chemclaw3-mcp:servers/chem/tools.py`.** The prose gate can only resolve
what is in this checkout, and a citation to a sibling repository is not a broken one. The prefix is
required rather than inferred: a reader following it knows which checkout to open.

## What this was verified against

The PR shipped saying plainly that nothing had run against the real server. That is now closed, with
`make run-chem` on 8858 and the shipped manifest:

| check | result |
|---|---|
| manifest's four tool names vs what the server advertises | exact match |
| `resolve_compound("toluene")` | `Cc1ccccc1` |
| `green_metrics(product 10 g, inputs 50 + 20 g)` | E-factor 6.0 |
| `/mcp` with no bearer | **401** |
| `/mcp` with a wrong bearer | **401** |
| `/mcp` with the right bearer | past auth |

The last three matter more than the first three. `auth: mode: none` against a server that enforces a
bearer means every call is *refused*, not that none is needed — and the guard for that
(`manifest._a_networked_endpoint_carries_a_credential`) reads the **declared** url, which is loopback
here, so it would never have fired on this file. The reason to set bearer was the server, not the
validator, and 401-vs-past-auth is what turns that from a belief into a measurement.

## The accepted cost, stated rather than discovered

**The tool list now exists in two repositories** and nothing structurally forces them to agree. The
server's copy is authoritative, because it is what answers; this one is what this repository
validates against. `make connector-validate` against a running server is the check that catches a
drift, and the exact-match row above is that check having been run once. A drift between runs is
possible and is the price of the seam.

## Four defects this found, and the one thing they have in common

Three latent, one live, plus one older than the tranche:

1. **`auth: mode: none`** — see above.
2. **The event-loop offload guard.** `tests/test_event_loop_offload.py` imported these tools to
   assert synchronous RDKit work runs off the loop — a property measured under load at ~1.18 turns/s
   flat from 10 users to 50. The `to_thread` hops survived the port; the test did not, so the
   property would have been preserved in code and unguarded in both repositories. Written into the
   other repo and verified to fail without the hop *before* the cases here were deleted.
3. **`_POINTER` never matched a colon**, so any `prefix:path.py` had always escaped the prose gate.
   Found while adding the cross-repo form, and closed; the gate then failed on its own file, because
   the example in the new comment had become a real broken pointer.
4. **`server: false` beside a `url`** — the live one.
5. **`server_tools_module` raised for a bundle with no server package**, which its own docstring says
   returns `None`. Older than this tranche: it had always done so for `qm`, and callers avoided it
   because, as that docstring says, "both callers skip an endpoint-less bundle before asking" — a
   caller-side workaround for a function-side defect, holding until a caller had reason to ask. It
   had no test at all; its three documented outcomes are pinned now.

**Each was true only by circumstance, and the migration removed the circumstance.** A bundle always
had a `server/`; a `url` was never set; a colon never appeared in a backticked path. That is the
thing to expect in tranches 3 and 4 rather than the specific bugs, because both delete a `server/`
directory the same way.

## Consequences

- A local run can disagree with CI on exactly this change, and did. Deleting `server/` leaves its
  `__pycache__`, so the directory survives as a PEP 420 namespace package, the import gets one level
  further, and the error names the module rather than the package. Clear stale `__pycache__` before
  trusting a local pass on any tranche that removes a package.
- `deploy/helm/chemclaw/values.yaml` no longer ends "No shipped bundle sets it: every one below is
  ours." `chem` sets it, and the sentence now says so.
- The operator gains two obligations the chart cannot discharge: the host in
  `networkPolicy.egressDestinations`, and `CHEMCLAW_CHEM_TOKEN`.
