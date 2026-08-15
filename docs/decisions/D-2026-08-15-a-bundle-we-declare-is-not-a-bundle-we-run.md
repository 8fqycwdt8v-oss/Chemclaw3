# D-2026-08-15-a-bundle-we-declare-is-not-a-bundle-we-run — `chem`'s capability moves to Chemclaw3-mcp, its manifest stays

**Status:** accepted · **Date:** 2026-08-15 · Applies `D-2026-08-09-a-connector-we-do-not-run` to a sibling repository, and is the first of four tranches moving scientific capability out of this tree.

## Context

`CLAUDE.md` now says where a capability belongs: infrastructure here, scientific capability in
[`Chemclaw3-mcp`](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp), split within science by *runtime*
rather than by subject. `chem` is the first to move — four pure, synchronous RDKit tools whose own
manifest already described them as "no store, no network, no durable state", which is exactly the
shape that repository is built for.

The obvious move was to delete `src/chemclaw/connectors/chem/` outright. That is wrong, and the
evidence says so plainly.

## Decision

**Delete the server, keep the declaration.**

Two profiles (`data/profiles/safety.yaml`, `evidence.yaml`), five eval probe files, two `SKILL.md`s
and the agent's own prose name these four tools *by string*.
`chemclaw_agent.available_tool_names` builds the set four validators check against by reading
manifests out of `connectors_dirs`, whose default is the shipped `src/chemclaw/connectors/`. Delete
the manifest and `make skill-validate`, `prose-validate`, `template-validate` and the profile check
all fail — on references to tools that *exist*. That is the D-117 defect, which this repository has
now hit twice, and it would have been hit a third time by a deletion that looked tidy.

So `connector.yaml` stays as the declaration and `server/` goes. `D-2026-08-09` already built the
shape: `connectors.chem.url` in the chart says the server is hosted outside this release, so no
Deployment and no Service are rendered and the given address is dialled instead of a computed one.
That ADR was written for a third-party model server; a sibling repository's server is the same
thing, and `chem` is the first shipped bundle to use it.

**The general rule this establishes, and the next three tranches follow it:** *capability moves,
judgment and declaration stay.* A `connector.yaml` is a declaration; a bundle's `skills/` tree is
layer-3 judgment about how to act on what a tool returns, and `skills_dirs()` reads it out of this
repository. Neither follows the code that computes.

## Three things found by doing it, each of which would have shipped a defect

**The credential.** The manifest said `auth: mode: none`, and the server on the other side enforces
a bearer on `/mcp` itself — so `none` does not mean "no auth needed", it means every call is
refused. `manifest.py`'s `_a_networked_endpoint_carries_a_credential` exists to catch exactly this,
and it would **not** have fired: it reads the *declared* url, which is loopback here, while the
deployment supplies a real address by override. The reason to set bearer is the server, not the
validator. Recorded because the near-miss is the interesting part — a guard that is correct and
positioned one step away from the thing it guards.

**The offload guard.** `tests/test_event_loop_offload.py` imported the chem tools directly to assert
that synchronous RDKit work runs off the event loop — a property measured under load, where
throughput was flat at ~1.18 turns/s from 10 users to 50, the signature of a serialization point.
The `asyncio.to_thread` hops survived the port. The *test* did not. Deleting those cases here would
have left the property preserved in code and unguarded in both repositories, which is the
shape-without-effect failure this tree keeps recording. The guard was written into
`Chemclaw3-mcp:servers/chem/tests/test_event_loop_offload.py` **first**, and verified to fail with
the hop removed, before the cases here were deleted.

**A hole in the prose gate, opened by its own new feature.** Cross-repository citations needed a
form, so `Chemclaw3-mcp:path/to/file.py` is now one. Probing it revealed that `_POINTER` never
matched a colon at all — so *any* `prefix:path.py` had always escaped the gate, long before this
change. Introducing a meaningful colon is when that stopped being theoretical: an unrecognised
prefix is now checked as the local path it is. The gate then failed on its own file, because the
illustrative example in the new comment had become a real broken pointer.

## Consequences

- **The tool list now exists in two repositories** and nothing structurally forces them to agree.
  The server's copy is authoritative — it is what actually answers; this one is what this repository
  validates against. `make connector-validate` against a running server is the check that catches a
  drift, and `Chemclaw3-mcp`'s own kit asserts manifest ↔ served-tools on its side. Stated here
  rather than discovered later.
- **Two things are the operator's**, because the chart cannot do them: adding the host to
  `networkPolicy.egressDestinations`, and providing `CHEMCLAW_CHEM_TOKEN`.
- `prose-validate` now **derives** connector credential names from the manifests rather than
  allow-listing them. Every externally-hosted server brings one, so a hand-maintained list would
  grow by one per migration and be wrong the first time somebody forgot. A typo'd variable still
  fails, which was checked rather than assumed.
- `core/chem.py` and `core/reagents.py` stay — 26 and 5 importers, and `core/chem` is also the D-011
  cache-key definition. The server carries a *copy*, whose divergence is pinned by a 23-pair
  canonicalization contract test whose expected values were produced by running this repository's
  own `require_canonical_smiles`.
- **Honest limit:** nothing here has yet been exercised against the running server. The manifest,
  the chart entry and the credential are declarations; `infra/live/e2e-full-stack/up.sh` with the
  external server up is what would prove a turn still resolves a compound over the wire, and it has
  not been run.
