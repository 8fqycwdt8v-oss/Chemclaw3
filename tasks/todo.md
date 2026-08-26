# ELN records become queryable data, not knowledge-graph notes (D-2026-08-25)

## Task
Remove the PR-gate from ELN ingestion. Keep the ELN queryable ("similar reaction", "same
product") with full content. Nothing extracts knowledge automatically without a user asking.

## Done
- [x] Measure the gate before changing it (202 ms/note serialized git; 425 µs/note corpus scan;
      zero LLM calls in ingest; refs *not* the bottleneck — disconfirmed)
- [x] Migration `052_reaction_records.sql` + grants + `infra/sql/README.md` inventory row
- [x] `ingest/eln/records.py` — Protocol + InMemory + Postgres, shaped like `fingerprints/store.py`
- [x] `ingest/eln/note.py` → `record.py`; `note_from_ord_reaction` → `record_from_ord_reaction`
- [x] `ingest.py` drops `propose_note`; fingerprint indexing untouched
- [x] `sync.py` drops `_merged_note_bodies` (the O(corpus) scan) and `awaiting_merge`
- [x] `dangling_links` external-id namespace + `cli/validate_kg.py` citation-existence check
- [x] `expand_note` falls back to the store (graph still wins — `reaction-` is a prefix, not a
      reservation); D-018's dangling-citation failure class removed
- [x] Retriever filter resolves against the store
- [x] No Schedule opens a PR: 3 memory schedules removed, observation promotion split out
- [x] Layering: removed both new edges by injecting one-method Protocols, not by declaring them
- [x] Slug validation kept (`require_note_slug` extracted, not copied) — caught by a test, not review
- [x] `tests/test_reaction_records.py` — the 4 claims the change would be wrong without
- [x] ADR + ledger + CLAUDE.md + ARCHITECTURE.md + DEFERRED row

## Review
The elegant version was not the first one. Two things forced it:

1. **The layering test.** Putting the store in `ingest` inverted `ingest → kg` and
   `ingest → retrieval`. The file's own rule ("move the code rather than excuse the edge") gave
   the answer: each consumer declares a one-method Protocol, the caller injects the store, and the
   edge disappears instead of being allowlisted. `FingerprintReactionRetriever`'s `records` is
   required rather than defaulted for the same reason.
2. **A dropped guard.** `ReactionRecord` initially had no slug validation, because a Postgres PK
   does not need one. An existing test failed and was right: the id still becomes the
   `reaction-<id>` citation a campaign note carries into git.

One hazard I introduced and then removed: routing on the `reaction-` prefix *before* the graph made
any human-authored note under that name silently unreachable. Graph first, store second.

## Merge with main (main moved mid-flight)
- [x] Base was `bed7d69`, whose own CI run **failed**; `50cb06f` on main fixed the two mypy errors
      that had kept main red since 2026-08-22. Merged main in rather than waiting.
- [x] Main added `ProcessConditions` frontmatter, read by `condense.py` and `protocol_tools.py`.
      Both sides changed the same mapping, so it is carried rather than picked: the record gains a
      `conditions` JSONB column, and `condense_protocols` gains a record fallback — without it
      every reaction reference would read as `missing`, silently breaking a feature main had just
      shipped.
- [x] Verified the exact CI command: `mypy src examples tests` → clean.

## Not done, deliberately
The chemist's actual insight is still not captured — see the `DEFERRED.md` row. This change makes
the ELN queryable; it does not make it teach.

---

# Review: fixing what the review of the GFN work found (2026-08-26)

#211 merged green — `make lint type cov`, seven validators, `eval-strict`, the Helm render, the
image build — and an adversarial review of the merged diff then found the feature did not work.

## What was actually broken

- [x] **Four of seven templates died on step 2.** A `tool` step's result reaches the resolver as a
      **string**: `_mcp_text` joins content blocks and `invoke_governed` returned the content.
      `${steps.forms.result.smiles}` asked for a field of a `str`. Reproduced, then fixed by taking
      the MCP adapter's structured artifact — the payload was on the wire all along.
- [x] **All eight templates died on step 1 when `solvent` was omitted.** `exclude_none=True` drops
      an unset optional input, and every template references it unconditionally.
      `conformer-refinement.yaml` had this since it shipped, so gas phase — the commonest call —
      never worked for any of them. Declared inputs now seed the scope as `None`.
- [x] **The agent could not select any of them.** The real names use underscores
      (`run_tautomer_resolution`); both skills wrote the file stem with dashes, twelve times. And
      `computation.yaml` advertised **zero** `run_*` names against `default`'s nine.
- [x] **Ensemble-averaged Fukui combined different atoms.** `_averaged` paired conformers by list
      position while `sites` is ranked by susceptibility and truncated. Now keyed by atom index.
- [x] The budget fence ran *after* the CREST search in two composites, and could not fire at all
      under defaults; the cost ladder said `thorough` adds a Hessian when it adds a search.
- [x] `bond_dissociation_survey` published `settings.xtb_method`, the exact bug `reaction_energy`
      documents having fixed. The truncation warning printed a negative count.
- [x] `species_ranking` fabricated σ=1, bypassing the disclosure machinery, on the one composite
      that ranks by the free energy σ shifts.
- [x] `RankedSpecies.conformers_found` hardcoded 0 beside `sampled=True`; `structure_id` never
      populated; `EnsembleProperty.refined` had no writer and no `warnings` field.
- [x] `RefinedEnsemble`'s entropy carried the ensemble-wide field names while describing the
      refined subset.

## Why CI was green through all of it

Every defect was invisible to a *different* gate, which is the finding worth keeping. The sharpest:
**the test fake was blind three separate ways at once**, and any one alone would have hidden the
Fukui bug — it discarded the geometry, returned sites in atom-index order and un-truncated (the one
shape in which position-pairing is accidentally correct), and made `f_zero` a constant `0.5` when
`f_zero` is the field being averaged. The same change had already fixed one instance of this class
and made the *dipole* geometry-dependent; Fukui, whose entire justification is that it moves with
geometry, was left constant.

## What was deliberately not done

- **No `CALCULATION_EPOCH` bump** — `ge=1` on `degeneracy` changed a persisted digest, but it
  tightens validation and rewrites no data, so nothing stored becomes wrong.
- **No ranking step for `run_degradant_triage`** — the prose was wrong, not the steps. Degradants
  do not interconvert, so an equilibrium distribution across them has no referent.
- **`_BARE` was not widened** — the skill-name hole is closed by a targeted test rather than by
  changing what every other skill is checked against.

Recorded in `D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`.
