# D-2026-08-01-a-key-that-cannot-see-our-own-fix — A key that cannot see our own fix

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-011 (a result is persisted once and
never recomputed) · **Implements:** the full-codebase review's solubility cache finding

## Context

Every calculator's `calc_version` answers one question: *would the program we shell out to produce a
different number now?* It is built from a tblite build, an xtb/crest binary version, an RDKit
version, an HPC pipeline tag. `CalculationKey` folds it in, so a model or method upgrade is a cache
miss rather than a stale hit.

Two things change what a stored row *means* that no such version can see. They arrived from opposite
directions and turned out to be one fact.

**The payload's shape changed under a stable version.** `SolubilityResult` gained `estimate`,
carrying the applicability-domain flag, and nothing was bumped — correctly, since the ESOL
arithmetic was untouched. The field is optional, so every row already written validates back with
`estimate=None`: an out-of-domain salt reads as *not assessed* rather than *OUT OF DOMAIN*.
`durable/retention.py` deliberately never prunes `calculation_results`, so those rows never
self-heal.

**Our own arithmetic was wrong and then fixed.** `xtb_thermo._rotational` divided a linear rotor's
partition function by `2 * symmetry` instead of `symmetry`, so every N2 / CO / CO2 / HCN / alkyne
entropy and free energy already on disk is wrong. Nothing in an `xtb.hess` key would ever move for
that fix — the tblite build is the same, the geometry is the same, the method is the same — so those
rows would have kept serving the wrong S and G until tblite happened to be upgraded for unrelated
reasons.

The second one is the reason this is a mechanism rather than a one-line version bump. A fix to our
own code is invisible to every version string we key on, by construction.

## Decision

**One key component for ChemClaw's own contribution to a stored result:** `CALCULATION_EPOCH`,
folded into `params_hash` by `CalculationKey.build` — the single place a key is assembled, so no
calculator can be keyed without it and none has to remember to ask.

**Bump it whenever a ChemClaw-side change makes an already-written row wrong or incomplete**, and log
the reason beside it. Epoch 1 is the first entry, invalidating every pre-epoch row deliberately: a
pre-epoch cache cannot be separated into "still correct" and "wrong linear-rotor thermochemistry or
missing applicability-domain flag", and serving the wrong half is the failure this exists to stop.

**It rides in `params_hash`, not in `calc_version`.** The version string is also the REV-12
calibration ledger's key, and a measured residual stays valid across a ChemClaw fix that a cached
prediction does not. Bumping `calc_version` for a payload change would have been wrong twice over:
it would discard calibration data that is still good, and it would claim the underlying program
changed when it did not.

**One component rather than two**, because "our arithmetic was wrong" and "the payload's shape
changed" are the same fact — *what a stored result means changed on our side* — and a mechanism per
symptom is exactly how the second one becomes the one nobody remembers.

## Consequences

- `tests/test_calc_payload_schemas.py` catches the shape half mechanically: it digests every
  persisted payload model and fails on any change, naming the model, the epoch to bump and the new
  digest to record. Verified by mutation — injecting a field into `SolubilityResult` fails it.
- The arithmetic half cannot be caught by a machine. It is a judgement only the author of a fix can
  make, so the rule is written at the constant rather than inferred from a test.
- One cold cache on upgrade, once. That is the intended cost, and it is bounded: the epoch changes
  per release, not per deployment.

## Alternatives rejected

- **Bump `calc_version` per calculator.** Discards valid calibration data, misattributes the change
  to the external program, and has to be remembered separately by every calculator — the failure
  mode that produced the solubility defect.
- **Backfill the affected rows.** Possible for the payload-shape half (recompute `estimate` from the
  stored SMILES) and impossible for the arithmetic half without recomputing the calculation, which
  is the thing the cache exists to avoid. A mechanism that works for one half only would leave the
  worse half uncovered.
- **A per-payload schema version.** More precise and more forgettable: it puts the decision at every
  result model instead of at one constant, and the defect being fixed is precisely that someone did
  not think about the cache when adding a field.
