# D-2026-08-21-a-geometry-is-an-address-not-a-payload — a computed geometry is a handle the next calculation takes

**Status:** accepted · **Date:** 2026-08-21

## Context

A review of how information is translated between agentic steps traced every path a result can take
from one tool to the next — the chat turn, the durable job, the template, the subagent, the
in-process composite — and asked the question the tree is built to answer: *when a chemist runs a
cheap conformer search and then wants the expensive method on the conformer that matters, what
carries the structure across?*

Nothing did. The finding, stated once:

> This system has exactly one full-fidelity data channel between two calculation steps, and the
> agent cannot reach it.

`connectors/calc/compose.py` passes typed `Structure` objects between remote primitives and keys
each on the geometry it ran at. That channel is first-party Python. Every path an *agent* can
select routes its data through the model's token stream and requires the model to re-type the next
call's arguments from what it read.

The system already derives the primitive that would fix this. `Structure.structure_id` is a content
address over normalized coordinates, computed byte-identically on both sides of the wire
(`st_739a222f45be0c3a` for `CCO`, here and on `Chemclaw3-mcp`). Four agent-facing result models
report one. **Zero tools or job specs accept one.** A handle onto a store with no reader is exactly
the defect `resume_campaign` was built to close for campaigns and nobody closed for geometries.

Six measurements, taken rather than argued:

| What | Measured |
| --- | --- |
| `sample_conformers` envelope, celecoxib (40 atoms), 20 members | **29,086 characters ≈ 7,300 tokens** of Cartesians, reaching the turn three times over |
| distinct numeric values in it | **2,400**, against `stream_max_result_numbers` = 512 ("unreachable in normal traffic") |
| one stored `xtb.conformers` row | **66,520 characters**; `calc_find_max_results` is 50, so one `find_calculations` call renders **~830,000 tokens** |
| `structure_id` in a serialized `Structure` | **absent** — a bare `@property` here, a `computed_field` on the server |
| campaign id under a re-cased category label | **forks**, silently, into a campaign with no history |
| a `job` step's result in a later template step | `"summary='…' data={…} note=None"` — a pydantic `repr` inside a JSON string |

The rule was already written down, one module away from where it was needed, and applied to exactly
one result shape (`OptimizationSummary`): *a model cannot read 3N Cartesians; `structure_id` is what
makes a geometry referable.* `optimize_geometry` obeys it. The five durable `calc` jobs and the
three geometry-bearing result models did not, and `find_calculations` — which holds a **stored
payload of unknown type** out of a JSONB column — could not have, because a per-model projection
cannot help it.

## Decision

**A geometry crosses into the model's context as an address, and an address is something the next
calculation takes.**

1. **`structures`** (migration 047) is a content-addressed geometry store, keyed by `structure_id`
   and never pruned. Not `artifact_blobs`: a geometry's identity is *narrower* than its bytes
   (`smiles` and `origin` are excluded, so two identical geometries are one structure whatever
   produced them), and a byte address would fork on the provenance the identity ignores.
2. **`science/calc/geometry.py`** is one walker used twice — `structures_in` finds geometries so
   they can be kept, `without_geometry` replaces those same geometries with their addresses. Two
   halves of one act, which is what makes the invariant structural rather than conventional:
   **every `structure_id` the agent is shown resolves.** Generic over a payload, not per model,
   because the third caller holds a `dict`.
3. **The handle is an argument.** `optimize_geometry`, `compute_thermochemistry`,
   `compute_electronic_properties`, `ScanJobSpec`, `EnsembleJobSpec` and `ComplexJobSpec` take a
   `structure_id`, resolved against the store and checked against the molecule the request names.
4. **`find_calculations` gains a `structure_id` filter** and a per-record listing budget with an
   honest `result_omitted` flag. The filter records the geometry each calculation *ran on*, taken
   from the server's own `calculation_key` answer — which this client was already receiving and
   dropping.
5. **The template resolver takes a dotted field path** (`${steps.search.result.data.structure_id}`)
   and dumps a pydantic model as JSON. Addressing, not computation: no indexing, no expressions.
6. **`ConnectorJobResult.calc_refs`** carries the calculation keys a run rested on, collected
   through a contextvar in `connectors/calc/remote.py` and persisted on the job record
   (migration 049).
7. **The campaign key is canonicalised** (`core/ids.canonical_text`, shared with `_report_id`), and
   `chemclaw.cli.rekey_campaigns` moves existing rows rather than orphaning them.
8. **A context reduction clears the repeat guard**, because after a tool result is cleared an
   identical call is a re-read and not a repeat.

## The two halves that ship off, and why

**DFT.** `QmJobSpec` takes a `structure_id` and the launcher sends `params.geometry_xyz`, but a
request naming one is **refused** unless `hpc_pipeline_accepts_geometry` is set. Nextflow silently
drops a param no process consumes, and the pipeline's contract lives on a cluster this repository
cannot reach — so shipping it enabled would mean a chemist told their DFT ran on the conformer they
chose when it ran on a fresh embedding. Every other half of the chain ships on because it is
verifiable here; this one ships as a loud refusal until an operator states otherwise.

**Fukui at a chosen geometry.** `predict_site_reactivity` does *not* take a `structure_id`: the
calculation server has `compute_properties_at` and no `compute_fukui_at`, so the argument would be a
promise this repository cannot keep. `DEFERRED.md` carries it with the trigger.

## What was rejected

- **A `.of()` summary model per result shape.** It cannot serve `find_calculations`, which is the
  largest exposure and holds an untyped stored payload.
- **Routing `compute_electronic_properties`' SMILES path through `compute_properties_at`.** The two
  agree today — both are `structure_from_smiles(smiles, optimize=True)` for every molecule the tool
  accepts — but "agree today" is not a property a cache may rest on, and forking it would orphan
  every `xtb.properties` row for a benefit the named-geometry branch already delivers.
- **An agent tool over `tool_result_blobs`** (the review's proposal for the compaction dead end).
  `tests/test_layering.py` forbids `agent → api`, and clearing the repeat counters at the one place
  that can *see* a reduction is both smaller and more precise.
- **Granting the runtime role DELETE on `bo_campaigns` and UPDATE on `bo_suggestions`** so the
  re-key could run as the app. A campaign's suggestions are its history and the sequence *is* the
  history (031); a one-off operator run is not a reason to hand a chat turn that privilege for the
  life of the deployment. `tests/test_database_privileges.py` names the module and the reason.

## Consequences

- A conformer search's envelope drops from ~29,000 characters to a few hundred, and the chain the
  review said was inexpressible is one hop: `sample_conformers` → `conformers[i].structure_id` →
  `optimize_geometry(structure_id=…)` / `compute_thermochemistry(structure_id=…)`.
- `find_calculations` is bounded by `limit x calc_find_max_result_chars` instead of by nothing.
- Every campaign whose decision space carries a capital letter changes id. **`make db-migrate` must
  be followed by `python -m chemclaw.cli.rekey_campaigns`** on any deployment with recorded
  campaigns; a `--dry-run` reports what would move. `continuous-only` spaces are unaffected, which
  bounds the re-partition to exactly the spaces that were forking.
- `structure_id` divergence between this deployment and the calculation server is now *visible*
  (`chemclaw_degraded_total{subsystem="structure_id"}`). It was not, and could not be: our
  rounding is a frozen constant while the server's is an ENV-overridable setting, so the
  cross-repository agreement `science/calc/models.py` protects held on one side only. The local
  derivation still wins — raising would turn one operator's configuration mistake into a total
  outage — and the counter is what makes the disagreement findable.

## References

- `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` — the split that left the
  geometry writers behind and made `fetch_artifact` a tool that can only refuse.
- `docs/decisions/D-2026-08-08-a-partial-answer-must-say-so.md` — the rule `result_omitted` follows.
- `docs/planning/DEFERRED.md` — `compute_fukui_at`, and the DFT pipeline's geometry param.
