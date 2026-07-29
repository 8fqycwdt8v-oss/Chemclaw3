# D-135 — A dataset may be vendored into the image at build time — the one amendment to D-089's scope

**Context.** D-089 fixed the scope: this system takes no external data sources, and
`tests/test_no_egress.py` enforces it because the prose form of the same constraint demonstrably
did not (TOOL-6 sat in `DEFERRED.md` as "blocked on choosing a source", which reads as an
invitation, and duly got built).

That decision is right about what it rules out: a *runtime* dependency on somebody else's service —
an address in first-party code, a network call on the retrieval path, an availability and licensing
question the deployment cannot answer. What it was never meant to rule out is knowing things. The
gap that leaves is concrete: `chemclaw/reagents.py` is a hand-maintained name→SMILES table and it
is the hard ceiling on `resolve_compound`, so a chemist naming an ordinary reagent gets nothing
back. Every fix for that is a dataset.

**Decision.** A dataset may arrive the way a dependency arrives: **installed into the container
image at build time**, pinned to a version, checksummed, licence-labelled, and reviewed once in a
pull request by a person who can read its licence. At runtime it is a file on local disk.
`sources/vendored/` attaches it through the existing manifest seam (D-120) with zero core edits.

The escalation is narrow, and three things keep it that way:

1. **No network path exists.** `tests/test_no_egress.py` is *extended, never relaxed*: a new test
   asserts `sources/vendored_dataset.py` imports no HTTP client, so it cannot acquire one by
   accident in a later edit either. The source is also named in the registry assertion rather than
   exempted from it.
2. **Provenance is required by the schema.** `name`, `version`, `licence`, `retrieved_from`,
   `description` and `sha256` are all mandatory. A corpus with no recorded licence is a legal
   question nobody can answer later; one with no checksum cannot be shown to be what the review
   approved. `retrieved_from` is documentation — nothing reads it as an address and nothing can
   fetch it.
3. **Retrieve-only.** Vendored data is reference material, not experiments. An ingest half would
   give unreviewed third-party records a write path into the knowledge graph behind the PR-gate's
   back, which is a much larger decision than reading a table.

A checksum mismatch refuses to load and names both hashes, because the tempting fix — editing the
manifest to agree with the bytes — defeats the mechanism entirely. A missing dataset yields no
evidence rather than raising: an optional corpus that is not installed must not break every query
in the process. Citations read `vendored:<dataset>:<row>` rather than posing as note ids: a
citation must resolve to something a reader can check, and for vendored data that is the row.

**What actually ships, stated plainly.** The mechanism, plus `common-reagents` v0.1.0 — a
first-party, hand-authored reagent/solvent/base/ligand table under the trivial names chemists write
(`DIPEA`, `Cs2CO3`, `mCPBA`, `T3P`). It carries no licensing question at all and is independently
useful. **No third-party dataset has been vendored.** Doing so is a build-pipeline step plus a
licence review and belongs to whoever adds one; `data/vendored/README.md` says how. Not enabled by
default — a deployment shipping no dataset is unaffected by the mechanism existing.

**Also in this decision: the embedding cache (STO-12).** The audit's finding on "tool result
caching" was largely that it is *not* a gap — every calculator already routes through `run_cached`,
and the RDKit chem tools are cheaper than the Postgres round trip a cache would add, so building a
caching subsystem there would have been ceremony. Saying so is the finding. The one genuine
repetition is `embed_texts`: every retrieval embeds its query, the same query recurs constantly,
and under a real provider each repeat is a network round trip on the interactive path paid by all
three graph-backed retrievers. It is now cached, bounded, keyed on **provider + model + dimension +
text** — the same lesson D-011 taught, since serving one model's vectors after a switch would
corrupt every similarity comparison silently.

**And the seed corpus (STO-10).** `knowledge/` held `.gitkeep`, so `make kg-validate` passed by
validating nothing and every retrieval, crosslink and conflict property was measured against
fixtures. It now holds 37 seed notes covering all ten note types and all fourteen relations, with
real instances of the awkward cases: a superseded pair with a closed `valid_to`, a declared
conflict, calculation crosslinks including an artifact reference. The original plan proposed
*promoting* `evals/retrieval_corpus/` into it; that was wrong and is recorded as such — that
directory's README states it is kept outside `knowledge_dir` precisely so the recall/precision
numbers stay independent of the live graph. The two are separate, and a test asserts they share no
ids.
