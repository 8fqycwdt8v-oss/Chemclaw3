---
name: eln-reaction-extraction
description: >-
  Judgment for turning a raw ELN entry into a validated canonical reaction: map structured
  fields deterministically, fall back to per-field LLM extraction only for genuinely
  free-text conditions, and never let an unvalidated reaction into the graph.
---

# ELN reaction extraction

Holds the *judgment* for the ELN ingestion capability (`eln.json_adapter` maps entries,
`eln.validate` checks them, `eln.ingest` indexes + PR-gates them). The adapter and validator
are deterministic code; this skill governs the one place discretion is needed — recovering
reaction *conditions and structures from free text* — and the discipline that keeps bad data
out of the graph.

## Deterministic first, LLM only per field

- **Prefer structured fields.** If the ELN records `temperature_c`, `time_h`, a component
  `smiles`, or `yield_percent` as a field, use it. Never overwrite a structured value with a
  guess from prose — the adapter already enforces this precedence; respect it.
- **Free text is a fallback, not the default.** Only when a needed condition is absent from
  the structured fields do you read the procedure prose. The adapter's regex already handles
  the common, unambiguous cases (`80 °C`, `3 h`). Escalate to an LLM extraction **per field**
  — one narrow question at a time ("what solvent?", "what temperature?") — not a single
  "parse this paragraph" prompt, which invents structure that isn't there.
- **A field you cannot determine stays empty.** An absent condition is honest; a hallucinated
  one corrupts every downstream metric. Leave it `None` and say so.

## Structures are the hard line

- Every component needs a **valid SMILES**. If the ELN gives a name, not a structure, resolve
  it deterministically (a name→SMILES tool) — do not free-hand a SMILES from a name by guess.
- **Never invent a product or a reactant** to make a reaction look complete. Missing species
  is a data-quality problem to flag, not to paper over.

## Validation is not optional

- Every mapped reaction goes through `validate_ord` (RDKit parse + atom/mass balance) **before**
  it is indexed or proposed. A reaction whose product contains an element the inputs never
  supply is wrong — reject it and record why; do not "fix" it silently.
- Ingestion is a proposal, not a fact: the reaction note enters through the PR-gate for human
  sign-off (D-005), and the fingerprint index is a derived serving copy. Keep provenance
  (`eln:<operator>`) on every record so a reviewer can trace it back to the source entry.
