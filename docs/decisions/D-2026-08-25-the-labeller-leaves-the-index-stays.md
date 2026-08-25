# D-2026-08-25-the-labeller-leaves-the-index-stays — the models ship to the fleet, the corpus stays here

**Status:** accepted · **Date:** 2026-08-25 · The sibling of
`D-2026-08-16-the-physics-leaves-the-cache-stays`, applying the same split to a second capability.
Builds on `D-2026-08-25-a-label-is-derived-not-recorded`, which defines the index this fills.

## Context

Filling the derived phase of `reaction_labels` means running four things over a reaction SMILES:

| Job | Chosen | Licence | Why |
|---|---|---|---|
| Atom–atom mapping | **RXNMapper** (ALBERT) | MIT | 99.4% on 49k unbalanced USPTO reactions; the state of the art open tool |
| Reactant vs. agent | **RDKit `Contrib/RxnRoleAssignment`** ("What's What", JCIM 2016) | BSD | ~8 ms median, ships with RDKit, consumes the atom map |
| Agent → solvent/catalyst/**ligand**/base/additive | a curated dictionary + SMARTS classes, baked into the image | first-party | no open model does this; seeded from the fleet's own CC0 solvent and reagent tables |
| Named reaction, class, functional groups, scaffold | **Rxn-INSIGHT** | MIT | 10 classes, 527 curated SMIRKS names, 107 Ertl functional groups; >91% class / >95% name accuracy at 40–100 ms |

RXNMapper is a transformer. Adopting it in-process puts `torch` and `transformers` into every chat
pod, every worker and every connector image in this repository — for a job that runs in a
background drain and never in a turn.

## Decision

**The labelling models ship as `Chemclaw3-mcp:servers/rxnlabel`; the index, the drain and the
search stay here.** That is `D-2026-08-16`'s test applied unchanged: labelling one reaction is a
*primitive* whose identity is derivable from its inputs, so it moves; the corpus, its staleness and
what is asked of it are about *our* data, so they stay.

Three consequences follow the calc precedent exactly, and one is new.

**The version is asked for, never derived.** `calc`'s ADR records why: a locally-built version is
*well-formed* and matches nothing, and nothing raises. Here it is worse than a miss — the version is
half of what decides staleness, so a derived one would make every row look stale forever and the
drain would re-label the whole corpus on every pass while reporting healthy progress. What this
side folds in is the two versions the server cannot see: `STANDARDIZATION_VERSION`, because the
species SMILES we send were normalised by our rules, and `VOCABULARY_VERSION`, because the role
names we store are ours. `remote_key` folds `CALCULATION_EPOCH` in on this side for the same reason.

**The manifest is not mounted.** `core/config/calculators.py` states the rule for the calculation
server: mounting an internal primitive's manifest on `connectors_dirs` puts it in the agent's
prompt as a tool to choose between. The labeller is that same kind of thing — nothing in a
conversation should be picking `represent_reactions` off a tool list — so its address is
`rxnlabel_server_url` plus `rxnlabel_server_token_env`, read by one client module.

**The tool name is `name_reaction`, not `classify_reaction`.** `servers/rxnpredict` already serves
`classify_reaction` for a different purpose (a coarse SMARTS class gating its Borda consensus), and
two tools with one name across one fleet is a collision waiting for a partial port.

**The D-011 cache is *not* used, and this is the new part.** The obvious move is to route the
labeller through `science/calc/store.py::cached_compute`, since it is a keyed primitive like any
other. It would be wrong: the label *row* is the cache, keyed by the row rather than by the input,
and that is precisely what makes staleness a query. A second cache in front would answer from a
superseded labeller's result while the row said it was stale, or the reverse. Deduplication within
a batch by canonical reaction SMILES is free and is enough.

## The client is shared, because it is the second one

Four things in `connectors/calc/remote.py` were worked out against a live server and are invisible
when wrong:

* the *connect* bound must be short even when the *read* bound is fifteen minutes — measured
  `connect 900.0, write 900.0, pool 900.0`, so a deleted pod stalled a durable activity for a
  quarter of an hour per attempt while the heartbeat reported it healthy;
* the MCP session's read bound must trip *before* httpx's, because `mcp.client.streamable_http`
  catches its own read timeout at debug level and never reconnects — the answer is lost silently
  and the caller waits forever;
* a rejected credential arrives as an `httpx.HTTPStatusError` nested inside the `ExceptionGroup`
  that `streamablehttp_client`'s task group raises, so the tree has to be walked — and it must not
  be classified as an outage, or a durable job spends its whole retry budget being told the same
  thing;
* `isError=True` covers both "the tool refused you" and "the server fell over", and only the second
  is worth a retry.

A second client made that a duplication. It now lives in **`core/mcp_session.py`**, beside
`core/db.py` (the pool), `core/http.py` (the client factory) and `core/temporal_client.py` — the
kernel already owns each engine's single primitive on everyone's behalf, and an outbound MCP
session is that same kind of thing.

It is in `core/` and not in `connectors/` because the second caller is `ingest/labels/labeller.py`,
and `ingest -> connectors` is not an edge this tree has: the alternatives were a new layering edge
or a second copy of four separately-measured hazards. `tests/test_third_party_layering.py` gained
one row, and it notes the direction — `("chemclaw.core", "mcp")` is the *client* half, independent
of `("chemclaw.connectors", "mcp")`, which is the server half. The kernel serves nothing.

What stays at each call site is what genuinely differs: the two error classes and their wording,
because those are read by a chemist and name a specific service.

## The drain

`ReactionLabelWorkflow` on `background-jobs`, modelled on `DocumentShareSyncWorkflow`: a planning
activity that reads the live values once, a bounded batch per activity, `continue_as_new` so a
multi-million-row backlog drains over many runs. `version` and `max_iterations` are read in the
activity and carried on the state, never in workflow code — both decide a command count, and for
`version` a mid-drain read would shift the stale set under the loop (D-093).

**A batch failure retries reaction by reaction.** `stale()` is deterministic — same `ORDER BY`,
same `LIMIT`, the same first batch on every attempt — so one reaction the server chokes on would
fail this activity identically forever and stop labelling *the entire corpus*. That is not
hypothetical: `ingest/documents/sync.py::reembed_stale` was changed for exactly this after one
un-embeddable chunk stalled every share. The rule that follows: a reaction that genuinely cannot be
labelled is still **stamped**, so it leaves the stale set, and the pass reports it under
`unlabelled` — separately from `labelled`, because a run that stamps thousands and derives nothing
is a broken labeller and one total cannot say so.

## Consequences

* `torch` and `transformers` stay out of this repository. The labelling capability is reachable
  from a worker and from nowhere else, over a bearer-authenticated `/mcp`.
* `connectors/calc/remote.py` shrank by roughly 150 lines and kept every measured behaviour;
  `tests/test_calc_remote.py` now patches the transport in `core.mcp_session` and still asserts all
  of them, including that no version derivation survives in the calc package.
* `LabelServerError` (retryable) and `LabelToolError` (registered non-retryable in
  `durable/publish.py`) are the labelling half of the split `CalcServerError`/`CalcToolError` draw.
* The server itself is a separate change in `Chemclaw3-mcp`. Until it exists, the Schedule is
  planned only where a source declares a `labels:` block, so a deployment without one asks nothing
  of an address that answers nothing.
