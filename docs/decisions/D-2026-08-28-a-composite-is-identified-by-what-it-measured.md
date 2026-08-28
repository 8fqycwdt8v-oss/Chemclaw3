# D-2026-08-28-a-composite-is-identified-by-what-it-measured — a tool composite's publish ref is its result, not its request

## Status

Accepted. Corrects the identity `D-2026-08-27-a-composite-needs-a-hook-not-a-projector` shipped with;
that ADR's hook, its placement at the tool boundary and its `TOOL_COMPOSITES` declaration all stand.

## Context

`publish/hooks.py::_composite_ref` derived a tool composite's `calc_ref` as
`f"{connector}.{tool}#{stable_hash(arguments)}"` — a hash of the validated keyword arguments the
tool body ran on. Its docstring made the claim that justified it: "a default the caller omitted and
a default the caller passed explicitly derive one ref rather than two."

That claim is true for a *literal* default, because pydantic fills an omitted argument in before the
tool body sees it. It is false for a **sentinel** default, and both tool composites use one.

### Measured, on the shipped derivation

```
predict_logd            ph=None      -> calc.predict_logd#1677c5556d3891f4
predict_logd            ph=7.4       -> calc.predict_logd#a357791989b0e1fe
compute_thermochemistry temp=0.0     -> calc.compute_thermochemistry#44005f1f6014fab5
compute_thermochemistry temp=298.15  -> calc.compute_thermochemistry#f93f5448d1dfed3b
```

`predict_logd(ph=None)` *means* 7.4 — the docstring says so and `logd_from_pka` resolves it —
and `compute_thermochemistry(temperature_k=0.0)` means 298.15, resolved as
`temperature_k or settings.xtb_thermo_temperature_k`. Four refs, two measurements, and a duplicate
row in every results store this deployment writes to.

### And the same defect runs the other way, where it loses science

`publish_tool_result` passes no `calc_version` and no `params_hash`, while the outbox's identity is
`(sink, calc_ref, schema_version)` with `ON CONFLICT DO NOTHING`. So a request-derived ref is the
*same string* after a calculator revision, a `CALCULATION_EPOCH` bump, or a starting geometry that
re-embedded differently — and the different answer the re-run produced is dropped on the way in.

Measured against a migrated Postgres, paracetamol's logD recomputed with the pKa backend answering
differently:

```
first enqueue rows=1   second enqueue rows=0
rows in result_publications=1   stored payload={"log_d": -1.849673934881497}
```

The +1.349 the second run computed never lands, and the store stays pinned to the superseded number
with nothing counted and nothing logged. `CONTRACT_VERSION`'s own comment had already measured this
mechanism for a corrected *document*; nobody had asked what it does to a re-computed *result*.

## Decision

**A tool composite is identified by what it measured, not by what was asked for it.**
`_composite_ref` hashes the result payload:

```python
return f"{connector}.{tool}#{stable_hash(payload)}"
```

The route (`calc_type = f"{connector}.{tool}"`) still says where it came from and `payload_kind`
still says what shape it is, so the identity is complete with nothing inferred. Both composites
report their own resolved conditions — `LogdResult.ph`, and `ThermochemistryResult`'s temperature,
medium, symmetry number and the geometry it ran on — which is what makes the result a statement
about a measurement rather than a bare number.

Both defects fall out of the one rule. The same question asked twice is one record exactly when it
produced one answer, which is the only case in which one record is honest.

## The determinism this rests on, measured

Hashing the *result* makes the record's identity depend on the result being stable for a repeated
question. The change was merged with that stated as unverified — "`embed_structure` determinism is
unobservable from this repo" — and argued correct on the grounds that a different conformer is a
different measurement and the record says which.

The argument stands and the gap is now closed rather than left open, because the fact lives one
repository over and that repository is checked out. Measured 2026-08-28 against
`Chemclaw3-mcp`'s `servers/calc`, calling `embed_structure("CC(=O)Nc1ccccc1")` three times:

    st_b19d49353e298dac
    st_b19d49353e298dac
    st_b19d49353e298dac

and `O=C(C)Nc1ccccc1` gives the same id, because the tool canonicalises before embedding.
`engine/xtb_engine.geometry` takes an explicit seed and `embed_structure` exposes none, so a given
RDKit build and configured seed produce one geometry.

**So a repeated `compute_thermochemistry` with no `structure_id` re-embeds to the same geometry, the
payload is stable, and the outbox collapses the repeat** — the case this decision needs to be one
record is one record. What legitimately mints a second row is what should: a second temperature, a
changed calculator, a `CALCULATION_EPOCH` bump, or a caller supplying a genuinely different
`structure_id`.

The dependency is real, though, and it is now a dependency *across a repository boundary*: were
`embed_structure` ever made stochastic, or its seed made caller-supplied and defaulted to something
varying, this record's identity would change per call and the results store would grow without
bound. That is the restart condition for this decision.

## Consequences

- A sentinel default and the value it stands for now address one row.
- A composite recomputed to a different answer is a second record rather than a silent drop.
- **A composite that landed on a different conformer is also a second record**, which is the same
  correction seen from a third side: `compute_thermochemistry` with no `structure_id` starts from a
  fresh embedding, and the result reports the `structure_id` it actually used. Two such runs are two
  measurements and were previously one row.
- `input_hash` still records the request, and is explicitly not the identity. Where two spellings
  collapse to one row, the spelling kept is the first one to land — stated in the hook's docstring
  rather than left to be discovered.
- `test_asking_the_same_composite_twice_is_one_record` is unchanged in its assertions and only its
  rationale moved, which is the useful signal that this narrows rather than widens what is
  published.

## Alternatives considered

**Normalise the arguments before hashing.** It needs each tool's own sentinel resolution restated in
`publish/hooks.py`, which is a second definition of `ph or 7.4` in a module that must not know what
a pH is — and it closes only the duplicate half, leaving the re-computation silently dropped.

**Add `calc_version`/`params_hash` to the tool hook.** A tool composite has neither: that is the
definition of one here (its key would name its own output), and the calculation server refuses to
key logD at all. Synthesising a version from this side would be a number nothing else agrees with.
