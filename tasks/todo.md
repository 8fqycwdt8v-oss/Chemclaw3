# Process development, HTE campaigns and protocol prediction — early PD to kilo lab

**Status:** in progress. Ideation merged as
`docs/archive/IDEATION-2026-09-20-process-development-hte-and-protocol-prediction.md`; this is the
implementation of it.

The previous occupant, the computable-discriminating-check plan (#425), is
`docs/archive/plans/computable-discriminating-check.md`.

## The ask

"Implement and fix everything" from the ideation. That document's own §7 orders it, and this plan
follows that order rather than the document's.

## What the ideation found, that the plan is shaped by

1. Five fleet bundles (`thermalsafety`, `kinetics`, `unitops`, `props`, `suitability`, 33 tools)
   are unreachable from every chart deployment — and declaring them the ordinary way costs 21,913
   tokens of prefix on **every** model call, which four `test_context_floor.py` entries already
   declined.
2. The prescriptive tier is bench-scale by construction: `_MAX_MASS_MG = 1_000_000` (1 kg),
   `_MAX_VOLUME_ML = 20_000` (20 L), and `ChargeLine` is three fixed-unit floats.
3. Nothing attaches a plate's results back to the design that prescribed them.

## Steps

- [x] **S1. Split declaring from binding.** `ConnectorManifest.default_enabled`, read in exactly
      one place (`registry.enabled()` when `connectors_enabled` is empty). Explicit lists are not
      filtered by it.
- [x] **S2. The declared basis for validators.** `declared_connector_tool_names`,
      `declared_tool_names`, `declared_skills_dirs` — so an opt-in bundle's skill is still
      validated on a checkout that never binds it. Runtime keeps the enabled basis.
- [x] **S3. Declare the five bundles**, `default_enabled: false`, manifest text carried from the
      fleet (authoritative there), each with a `README.md`.
- [x] **S4. The four skills that could not previously exist** — `thermal-safety-assessment`,
      `kinetics-and-reactor-choice`, `unit-operation-sizing`, `system-suitability`. `props` gets
      none: `skills/solvent-selection` already holds that judgment.
- [x] **S5. ADR** `D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`.
- [ ] **S6. Tests** for the split: opt-in bundles stay out of `enabled()` and out of the measured
      prefix; an explicit list reaches them; a declared-but-unbound tool resolves for a validator
      and not for the runtime verifier.
- [ ] **S7. Chart**: five `connectors.<name>` entries at `enabled: false`, rendered config
      unchanged for an existing release.
- [ ] **S8. Context-floor prose**: the four "did not move for the Nth time" entries now have a
      fifth reason, and it is a different one.
- [ ] **S9. Unfreeze the envelope's quantities** — `ChargeLine` on `core/units.Measurement`,
      plausibility bands relative to the declared scale, vessel/working volume, addition rate,
      IPC-gated hold. Own ADR.
- [ ] **S10. `rescale_protocol`** — deterministic charge scaling plus the structured list of what
      did *not* scale.
- [ ] **S11. `attach_plate_results`** — close the design → results loop.
- [ ] **S12. Step templates** for the compositions the skills describe.
- [ ] **S13. Design generators** — response-surface, mixture, D-optimal, blocking.

## Verification

`make lint type test` green, plus every validator (`connector-validate`, `skill-validate`,
`prose-validate`, `template-validate`, `kg-validate`, `helm-validate`). Postgres-backed tests must
actually run — `sudo -n dockerd`, `make up`, `make db-migrate` — because a local run that skips them
prints green and proves nothing.

## Review

(to be written at the end)
