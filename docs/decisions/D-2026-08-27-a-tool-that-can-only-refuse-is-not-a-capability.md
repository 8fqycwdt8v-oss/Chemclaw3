# D-2026-08-27-a-tool-that-can-only-refuse-is-not-a-capability — `fetch_artifact` stops advertising a spectrum

**Status:** accepted · **Date:** 2026-08-27

## Context

`list_artifacts` and `fetch_artifact` are the agent's route to what a finished calculation left
behind. Between them, four surfaces told the agent what that route reaches: the two tool docstrings,
the `evidence` profile's instructions, and `skills/computational-evidence`. All four named the same
three things — "the relaxed coordinates, the second derivatives, the raw vibrational spectrum".

**Measured first, on the real write path.** A `HessianPayload` was written through
`ArrayOffloadingStore` — the only artifact writer left in this repository — into a migrated Postgres
artifact store, and the two tools were then asked what was there:

```
=== list_artifacts('xtb.hess@gfn2-v1:0000000000000000:1111111111111111') ===
  name='hessian.npy' media_type='application/x-npy' byte_size=776
  -> 1 entry

=== fetch_artifact(...#hessian.npy) ===
  ValueError: '...#hessian.npy' is binary (application/x-npy, 776 bytes), not text.
              It is stored to seed a further calculation, not to be read.

=== fetch_artifact(...#vibspectrum) ===
  ValueError: no artifact 'vibspectrum' is stored for calculation '...'
              (stored under it: hessian.npy).
```

One entry, packed, refused. `fetch_artifact` refuses **every** artifact this release stores, and
it is not a matter of today's data: `ArrayOffloadingStore` offloads exactly `HESSIAN_ARRAYS`, both
values are `.npy`, and a `.npy` magic byte is not a valid UTF-8 start byte, which is what the
readability check keys on. `vibspectrum`, `xtbopt.xyz` and the two CREST ensembles survive only as
keys in `artifacts._MEDIA_TYPES`; their writers left with the engines
(`D-2026-08-16-the-physics-leaves-the-cache-stays`).

The geometry half of the promise is already answered elsewhere and is not reopened here:
`D-2026-08-21-a-geometry-is-an-address-not-a-payload` made `structure_id` the handle the next
calculation *takes*. What was left open was the spectrum, with two options: have the calculation
server return `vibspectrum` as an artifact, or stop advertising one.

## Decision

**Stop advertising it.** The spectrum is not missing — it is somewhere better — and an artifact
would be a second, *worse*, and sometimes *different* answer to a question already answered.

Four findings decided it, and each is about the code rather than about taste.

1. **The band list already reaches the model.** `compute_thermochemistry` returns
   `modes: list[VibrationalMode]` — a wavenumber and an IR intensity per mode — ranked by
   `strongest_bands`, bounded by the caller's `top_bands`, with `mode_count` stating how many there
   were and `imaginary_frequencies_cm` / `lowest_wavenumbers_cm` never truncated. That *is* the band
   list a measured spectrum is compared against. A `vibspectrum` file would be the same numbers in a
   Turbomole text format the model must parse, and this family already records what a second
   rendering of one answer costs: two live definitions of `predict_pka`, differing in one of them
   (`Chemclaw3-mcp`'s `connectors/README.md`).

2. **It would not even be the same numbers.** `xtb_cli._read_vibspectrum`'s own docstring: the
   file's leading entries are the projected-out translations and rotations, "kept here and dropped
   by the caller, which knows how many modes its own projection found". `science/calc/thermo.py`
   does its own projection. So the file's list and the tool's list differ in length and in
   contents, and reconciling them is exactly what the caller does in code and what a model reading
   the raw file cannot do.

3. **It would exist on one backend only.** `vibspectrum` is written by the `xtb` binary path. The
   in-process tblite path (`xtb_hessian._finite_difference`) collects dipole derivatives and writes
   no file at all. So `fetch_artifact` would answer or refuse depending on `CHEMCLAW_XTB_ENGINE` —
   which is the defect this ADR is closing, relocated rather than fixed.

4. **It would frequently name a geometry the system rejected.** An artifact hangs off the
   `xtb.hess` primitive key, and `compose.relax_to_minimum` runs that primitive **once per
   refinement iteration** — displacing along an imaginary mode and re-optimizing until the geometry
   settles. Every discarded iteration keeps its cache row and would keep its artifact. An agent
   fetching one would quote the spectrum of a structure the system threw away while
   `compute_thermochemistry` reported the settled one. That is strictly worse than a refusal.

Two further facts made the build side unattractive rather than merely unnecessary. The receiving
end has no writer *by an argued deletion*, not by omission — `xtb_cli.py` records it: "There is no
store here, nothing would ever read them, and keeping the code would be a copy of a cache with none
of the value." And the durable record already carries the frequencies:
`publish/project.py::_thermochemistry` publishes every mode as `series="modes"`, and
`publish/hooks.py` names `ThermochemistryResult` as "the only place a vibrational frequency exists
in this system".

Nothing asked for the artifact. Checked across the tree: no template, no eval probe and no job spec
names one; `skills/computed-spectra-comparison` reads `compute_thermochemistry`'s output, not a
file. The only things naming `vibspectrum` were the dead media-type table and the docstrings under
review.

## What ships

- **Both docstrings say what the tool does.** `fetch_artifact` states that it refuses *every*
  stored artifact rather than "more often than it answers", drops the filenames nobody can obtain,
  and redirects both questions people bring it: a geometry is a `structure_id`, a spectrum is
  `compute_thermochemistry`'s `modes`. `list_artifacts` gains the symmetric "Not spectra."
- **The `evidence` profile stops instructing the agent to fetch one.** It told the specialist to
  "fetch_artifact to quote a stored geometry or spectrum exactly rather than describing it" — false
  on both halves. It now names `find_calculations` as the citation and leaves `fetch_artifact` the
  one thing it can still do: open an `artifact_refs` reference an older note carries.
- **`computation.yaml` is untouched.** It lists both tools and makes no claim about them; once the
  docstring is honest, an allow-list entry is not a promise.
- **Three tests hold the end state**, and the first two are *derived* rather than transcribed.
  `test_every_artifact_this_release_can_write_is_refused_by_fetch_artifact` builds its subject from
  `HESSIAN_ARRAYS` and drives the real tool, so adding a text writer turns it red.
  `test_no_agent_facing_surface_names_an_artifact_without_a_producer` scans the two docstrings and
  both profiles for `set(_MEDIA_TYPES) - set(HESSIAN_ARRAYS.values())` — the names the table knows
  and nothing writes — so a name becomes sayable the moment it gains a producer, and not before.
  `test_the_spectrum_the_docstrings_redirect_to_is_one_the_code_returns` is the other half: a
  removed promise must leave a reachable answer behind, so it drives `strongest_bands` rather than
  reading the claim off prose.

## What was rejected

- **Returning `vibspectrum` as an artifact** (the row's option (a)) — the four findings above. It
  would have cost a `CliResult.artifacts` field, a wire field on `HessianPayload`, a second
  offloading map, an artifact store on the serving side that was deliberately deleted, and a
  `CALCULATION_EPOCH` bump on both sides invalidating every stored `xtb.hess` row, to deliver
  numbers that already arrive by two other routes.
- **Deleting the two tools.** They are correct for what they do. `list_artifacts` truthfully
  reports the arrays a run offloaded, and `fetch_artifact` is the only opener for the
  `artifact_refs` a knowledge note may cite — a real reference shape with a real validator
  (`kg/note.py`) and a real crosslink consumer (`kg/crosslink.py`). The defect was the promise, not
  the pair.
- **Removing the text path from `fetch_artifact`.** It is not dead: the store accepts any
  producer-given name, and the decode-and-clamp path is what would serve a text artifact the moment
  one has a writer. Deleting it would make re-adding a text by-product a change to the reader as
  well as to the writer.

## Follow-ups this ADR does not carry

Both are the same defect in files outside this change's scope, and neither is load-bearing for the
fix above:

- `skills/computational-evidence/SKILL.md` still promises "the relaxed coordinates, the second
  derivatives, the raw vibrational spectrum" and tells the agent to fetch a geometry or a spectrum.
  It is the largest remaining statement of the removed promise.
- `science/calc/artifacts.py::_MEDIA_TYPES` still carries eight names with no producer. They are
  harmless as a table — `media_type_for` falls back to opaque bytes — and they are what the absence
  test above derives its dead set *from*, so removing them is a change to that test as well.

## References

- `docs/decisions/D-2026-08-21-a-geometry-is-an-address-not-a-payload.md` — the geometry half,
  settled and not reopened here.
- `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` — the split that left the
  by-product writers behind.
- `docs/decisions/D-2026-08-26-semiempirical-is-the-whole-tier.md` — why `density.restart` and
  `orbitals.molden` will not gain producers either.
