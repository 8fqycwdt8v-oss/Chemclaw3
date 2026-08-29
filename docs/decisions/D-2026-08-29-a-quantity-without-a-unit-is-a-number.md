# D-2026-08-29-a-quantity-without-a-unit-is-a-number — the `Measurement` type

**Status:** accepted · **Date:** 2026-08-29 · Fourth of the eight infrastructure findings from the
2026-08-28 audit (F5).

## Context

`infra/sql/030_measurements.sql` declares `unit TEXT NOT NULL DEFAULT ''`.
`science.calc.calibration.record_observation` has taken a `unit` argument since it was written and
writes it through unexamined. The one tool that calls it —
`connectors/calc/server/tools.py::report_measurement` — never passed one.

So the column existed, the parameter existed, and nothing filled either. Every measured value this
system has ever stored carries an empty unit, and a chemist reporting `0.5` for solubility was
stored identically whether they meant log S or mg/mL. The ledger then scores a prediction against
it, and the resulting bias figure — *"the solubility model has run about 0.4 log units low over 18
measurements"*, which the tool's own docstring offers as the reason to trust it — is arithmetic over
numbers that may not be the same kind of thing.

For a chemist that is survivable most days, because most of what flows internally is energies in one
fixed unit. For **analytical development it is the foundation**: a specification is a quantity with a
limit and a comparison, an ICH limit is a quantity with a basis, a stability trend is a series of
quantities with an extrapolation, and an analytical target profile under ICH Q14 is a set of
quantities with acceptance criteria. None of that is expressible over a bare float, and an assistant
that silently compares 0.15% area to 1.5 ppm is worse than one that refuses.

## Decision

**`chemclaw.core.units` — `Measurement`: value, unit, uncertainty, basis; conversions; and a
comparison that refuses across dimensions.**

It is not a general unit library, and the narrowness is deliberate. There is no algebra over derived
units, because nothing here multiplies a mass by a length and a registry that could would be an
abstraction with no caller. What it holds is the units this domain writes down and the three
operations this system performs: convert, compare, refuse.

### Four things that are the decision rather than the implementation

**1. Case is significant, and folding it is a hazard rather than a pedantry.** `M` is molar and `m`
is metre; `mM` is millimolar and `mm` is millimetre. A case-insensitive registry makes each pair one
spelling and answers with whichever was registered first — so a limit stated in `mM` would be read
as millimetres, refuse nothing, and be compared against a concentration as though it were a length.
This was **measured while building it**: a folded registry raised on import, unable to hold molarity
and length at once, and the error message that exposed it is now the guard's. Lookup is exact;
`_FOLDED` is a convenience layer holding a lowercase spelling only while it is unambiguous, and a
fold two units claim is refused by name rather than guessed at.

**2. A log scale is its own dimension, and each one is its own.** `log S` and `pKa` both carry no
units, and calling either "dimensionless" would make them interconvertible with each other and with
`%` — so a pKa reported into the solubility ledger would be accepted silently. Nothing converts into
a log scale, which is exactly what a dimension of its own expresses.

**3. Mass concentration is not molarity.** Turning mg/mL into M needs the molar mass of the
substance, which is a fact about the *sample* rather than about the units. They are separate
dimensions, so the conversion refuses — which is the right answer, because it needs an input nobody
supplied. The alternative is a module that invents one.

**4. An uncertainty is scaled and never offset.** A spread of 2 °C is a spread of 2 K. Adding 273.15
to it is the classic temperature bug in the one field nobody re-reads, and it would leave the value
beside it correct.

`offset` exists in the first place for the same reason: every other unit here converts by a factor,
and a factor-only registry converts 25 °C to 25 K silently.

### The wiring

`report_measurement` gains a `unit` argument, looks the property's ledger unit up in `_CALIBRATED`,
and calls `reconcile`, which converts within a dimension and raises across one. `_CALIBRATED` moved
above its readers in the same change — it was defined at line 515 and is now needed at line 174,
which resolves fine at runtime and reads as an accident.

`reconcile` accepts an **unstated** unit as "the ledger's own" rather than refusing it. Every
measurement stored before this carries an empty unit, so refusing the unstated case would break the
common path while the case this exists to catch — a number in the *wrong* unit, silently stored — is
the one that now raises.

`tests/test_units.py` asserts that `_CALIBRATED`'s two unit strings parse. That is the one coupling
worth a test: if the two spellings ever diverge, the reconciliation raises `unknown unit` on every
reported measurement instead of checking one.

## Why `Measurement` and not `Quantity`

`core/quantities.py` already has a `Quantity`, and the two are deliberately not merged. That one is
*a number a payload returned, under the key the tool gave it* — a label and a float, feeding the
check that a figure stated in an answer is grounded in one a tool produced. It knows nothing about
dimensions and must not: it reports what a tool said rather than what is true, and its own docstring
argues at length that the moment it decides `sd` means "the uncertainty of the value above it", it
is asserting a relationship the tool did not state. The first is evidence about a payload; the
second is a fact about the world. `ARCHITECTURE.md` records the pair beside the other three
name-collisions that are not duplicates.

## Consequences

- A chemist reporting a solubility in mg/mL is now **refused** with the ledger's unit named, rather
  than having the number stored as a log S. That is a new failure mode for a call that used to
  always succeed, and it is the point.
- The type has three call sites today and is a prerequisite rather than a companion for the
  analytical and regulatory tranches: a specification, an ICH limit and a stability trend each need
  it, and each would otherwise invent its own.
- It is also where the fleet's ASM/AnIML story lands if that is ever taken up, because both
  standards are, at bottom, quantities with provenance. Nothing is built for that now.
