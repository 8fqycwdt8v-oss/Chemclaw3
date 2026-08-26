# D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire — what the review of the GFN work found

**Status:** accepted · **Date:** 2026-08-26 · Supersedes one consequence of
`D-2026-08-25-the-loop-is-a-composite-not-a-template`, whose decision stands.

## Context

The multi-step GFN protocols merged (#211) with CI green: `make lint type cov`, seven validators,
`eval-strict`, the Helm render and the container build. An adversarial review of the merged diff
then found that **four of the seven shipped templates could not complete a run**, that the agent
could not select any of them, and that the headline per-atom average was combining different atoms.
None of it was reachable by the gate. This ADR records what the gap was, because the defects are
less interesting than the reason every one of them was invisible.

## The finding that supersedes a consequence

D-2026-08-25 argued that the enumeration tools must return container models with a hoisted `smiles`
list, because `templates/resolve.py` walks a dotted *attribute* path with no indexing, so a bare
`list[Tautomer]` is unreachable from a template. **That reasoning is correct and it was not
sufficient**, and the ADR's consequence bullet — that hoisting makes the field reachable — is false
as written.

A `tool` step's result does not arrive as a model at all. `template_activities._mcp_text` joins an
MCP tool's content blocks into text and `invoke_governed` returned that content, so the value in
scope was a **string**:

```
type: str
value: {"smiles": ["CC=O", "C=CO"], "count": 2}
UnresolvedReference: template references 'steps.forms.result.smiles',
                     but 'smiles' is not a field of the str it names
```

The constraint was never indexing alone. It was that **a tool result is not a model on the wire**,
and hoisting a field cannot fix a value whose type is `str` by the time the resolver sees it.

`data/templates/conformer-refinement.yaml` — the one shipped QM template, and the shape the seven
were modelled on — field-walks a **`job`** step, whose `ConnectorJobResult` is a real pydantic
model. It never touched this path. The seven were the first templates in the tree to field-walk a
*tool* result, and nothing had established that the shape survives.

**The structure was on the wire the whole time.** `langchain_mcp_adapters` builds every tool with
`response_format="content_and_artifact"` and puts the server's `structuredContent` in the artifact;
`ainvoke(args)` simply discards it. So `invoke_governed` gains an opt-in `want_message` and the tool
step takes it. Off by default, and the default is load-bearing: LangChain coerces a `ToolMessage`'s
content to text for a non-block return, so the `job` step's dict would become
`'{"subject": "benzene"}'` and `ResolvedJob` would reject it — three tests already pin that.

## Why CI was green through all of it

Six defects, six different reasons the gate could not see them. This is the part worth carrying.

| Defect | Why no gate saw it |
|---|---|
| Four templates die on step 2 | `template-validate` checks that step ids resolve backwards and that the tool exists. It has no way to know the *shape* a tool returns, and the failure is inside the workflow. |
| Every template dies on step 1 when `solvent` is omitted | Same: a reference to a *declared* input is valid. That the launcher drops it with `exclude_none=True` is a run-time fact. |
| Both skills named seven tools that do not exist | `skill-validate`'s "taught ⇒ declared" half extracts names with `validate_prose_contract._BARE`, whose lookbehind excludes a backticked name with no parens. It extracted **nothing** from `ensemble-workflows/SKILL.md` — the check was vacuous on the file that needed it. |
| The `computation` profile advertised none of them | `tool_names` is an allow-list. Nothing asserts that a profile can reach the workflows its own skills route to. |
| Ensemble-averaged Fukui paired atoms by rank | The fake was blind three ways at once (below). |
| The budget fence ran after the CREST search | Only `species_ranking` had a preflight test; the two composites with the defect were the two nobody asserted. |

**The fake deserves its own paragraph, because it is the sharpest lesson.**
`_averaged` paired conformers by list *position*, while `SiteReactivityResult.sites` is documented
as ranked by susceptibility and truncated to `top_n` — so position *k* was a different atom in each
conformer, and the mean was labelled with the first one's index. The bug fired hardest in exactly
the case the composite exists for: if the ranking did not move with geometry, `compute_fukui_at` and
the `DEFERRED.md` row it closed would have had no purpose. Three independent properties of the fake
each hid it on their own:

1. `_compute_fukui_at` delegated on the SMILES alone, so every conformer returned an identical list.
2. `_predict_site_reactivity` returned sites in *atom-index order and un-truncated* — the one shape
   in which position-pairing is accidentally correct, and not the contract the server keeps.
3. `f_zero` was the constant `0.5`, and `f_zero` is the field `_DEFAULT_FUKUI_MODE` reports — so the
   existing test compared `0.5` to `0.5`.

The same change had already fixed one instance of this class (the fake returned three ensemble
members sharing one geometry) and made the *dipole* geometry-dependent. Fukui — the one property
whose entire justification is that it moves with geometry — was left constant.

**The rule: a fake that cannot express the failure is not evidence, and the property a feature
exists to measure is the property its fake must vary.** Where a fake stands in for a contract, it
owes the contract's *shape* (ordering, truncation, cache key), not merely its field names — the same
fake also keyed `compute_fukui_at` with an empty params tuple while the server keys it on `solvent`.

## Decision

Fix all of it forward, in one PR, each defect with a test that fails on the parent commit. The
mechanisms added, as distinct from the individual repairs:

- **A tool step's structured result reaches the next step**, via the adapter's artifact, with the
  three shapes upstream never promised pinned in `tests/test_upstream_surface.py`.
- **A declared optional input resolves to `None`** rather than raising — the templates were not
  wrong, an optional input that was not given *is* `None`, and that is what the calc specs default
  to and what `require_supported_solvents` reads as gas phase.
- **A test asserts every `run_*` a skill names exists in the registry**, closing the specific hole
  rather than widening `_BARE` and changing what every other skill is checked against.
- **The per-atom average keys by atom index**, drops atoms absent from any member rather than
  part-averaging them, and says how many it dropped.
- **The budget counts before it computes**, on all four composites, against a ladder read off
  `_species_energy` rather than guessed — `thorough` adds a CREST search, not a Hessian.

## Two things this did not do

**No `CALCULATION_EPOCH` bump.** `EnsembleMember.degeneracy` gaining `ge=1` changed
`EnsemblePayload`'s persisted schema digest and the guard asked the right question. The constraint
tightens *validation* and rewrites no data, so no stored row becomes wrong or incomplete — a
degeneracy is a rotamer count and has always been at least 1. Bumping would discard every cached
CREST search to no end.

**`run_degradant_triage` was not given the ranking step its summary promised.** The prose was wrong,
not the steps: `rank_species` computes an *equilibrium* distribution, and degradants do not
interconvert — they are products of different irreversible reactions, so a Boltzmann weight across a
sulfoxide and a hydrolysis fragment is a number with no referent. What orders them is a rate nothing
here computes. The summary now says so.

## References

- `D-2026-08-25-the-loop-is-a-composite-not-a-template` — the decision this supersedes one
  consequence of; its three-tier placement rule stands unchanged.
- `D-2026-08-21-a-geometry-is-an-address-not-a-payload` — `RankedSpecies.structure_id` is now
  populated, which is what that decision asks of every composite.
- `tests/test_upstream_surface.py` — the artifact contract, now one of the shapes this tree reads
  that upstream never promised.
