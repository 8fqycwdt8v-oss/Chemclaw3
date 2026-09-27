# D-2026-09-27-a-literature-index-waits-for-a-corpus-and-a-licence — `deep-research` stays on internal records

**Status:** accepted · **Date:** 2026-09-27 · Closes the `BACKLOG.md` row *"`deep-research` has no
index behind it"* (issue #486). **Owner-delegated decision**: the owner asked for this to be decided
without them ("decide on your own", 2026-09-27), and it was decided by the assistant working the row.

## Context

`agent/research_tools.py::gather_evidence` sweeps this deployment's own records — the knowledge
graph, the ELN transcribed into it, and whatever retrieve sources the deployment enables under
`CHEMCLAW_DATA_SOURCES` (a mounted share, `src/chemclaw/ingest/sources/pistachio/datasource.yaml`'s
patent-reaction corpus where a site has loaded its own licensed copy). Nothing in that sweep is
journal literature. `skills/deep-research/SKILL.md` nevertheless read as a general research
capability, and its description promised to compose "every data source", which a model can turn
into "I searched the literature" when it did nothing of the kind.

The fleet has a design for the missing piece: `Chemclaw3-mcp/MODULES.md` lists `litsearch` as
**proposed** — `search_literature`, `fetch_abstract`, `resolve_doi`, `citation_graph` over "a local
index built from Europe PMC / OpenAlex / Crossref bulk dumps at build time", which "gives Chemclaw3's
existing `deep-research` skill a real index". The backlog row cited ChemRAG's measured gain from a
chemistry corpus as the reason to want it.

## Options

1. **Build the index now** — a `litsearch` server in the fleet, a `connector.yaml` here, a skill
   pointer. Under the no-egress posture (`CLAUDE.md`: every server "answers from data baked into its
   image and makes no outbound call at request time") the index has to be a build-time snapshot, and
   the snapshot is the whole cost: OpenAlex's works dump and Europe PMC's open-access subset are each
   a multi-gigabyte corpus, refreshed only by rebuilding the image, and the three
   sources carry different licences per subset — CC0 metadata, CC BY / CC BY-NC full text, and
   abstracts whose reuse terms differ by publisher — each of which needs a review before it is baked
   into an image a site deploys.
2. **Reach a live literature API** — declined by the no-egress posture itself, not by this record;
   re-opening that is a different and larger decision.
3. **Decline the index for now, and make the surface honest about its absence.**

## Decision

**Option 3. Building a literature index is declined for now**, because what it costs — a
multi-gigabyte build-time corpus, its refresh story, and a per-subset licence review — is out of
proportion to a row whose evidence for the gain is one external benchmark, while the cost of the
absence can be removed today by not pretending the index exists.

What ships instead is the honest half:

- `skills/deep-research/SKILL.md` says, before the loop, that there is **no literature index**:
  `gather_evidence` searches this deployment's own records; "nothing on file" is not "no precedent
  exists"; a precedent the model recalls from training is labelled as recollection, with no note id,
  reference or DOI invented for it; a patent-reaction hit is precedent from elsewhere and named as
  such. Its description says "this deployment's own records and tools (there is no literature
  index)" instead of "every data source".
- `gather_evidence`'s docstring — the tool description the model reads — says the same in two lines:
  internal means this deployment's own records, and an empty sweep says nothing about what is
  published.

Revisit when: a deployment mounts a licensed literature export as a retrieve source (a
`datasource.yaml` under `src/chemclaw/ingest/sources/` whose corpus is journal text, the shape
`pistachio` already has for patents), or `litsearch` in `Chemclaw3-mcp/MODULES.md` leaves `proposed`
with a named corpus and a licence review. The second is executable:
`tests/test_literature_index_decline.py` reads that row and fails when its status changes, naming
this record.

## Consequences

- `deep-research` keeps working as it did; what changes is what it may claim. Answers get less
  impressive where the model used to imply a literature search, and more accurate.
- When the trigger fires, the work is the one the fleet already sketched: a server there, a
  `connector.yaml` and a skill pointer here, and this ADR superseded.
- Nothing moves to `docs/planning/DEFERRED.md`. A decline is recorded by its ADR and its trigger,
  as `D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model` is; a register row
  beside it would be the second copy `D-154` removed.
