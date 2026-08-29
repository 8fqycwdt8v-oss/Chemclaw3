# D-2026-08-29-a-composite-s-identity-is-its-result-not-its-request — the hook's ref scheme, reversed

**Status:** accepted · **Date:** 2026-08-29

**Supersedes** the `calc_ref` scheme in
[`D-2026-08-27-a-composite-needs-a-hook-not-a-projector`](D-2026-08-27-a-composite-needs-a-hook-not-a-projector.md),
and nothing else in it. That ADR's decision — that a tool composite needs a *hook* at
`connectors/server.py`'s choke point rather than a projector nothing calls, and that `TOOL_COMPOSITES`
is a declaration the suite derives — stands unchanged and is why this seam exists at all.

## What that ADR decided, and what it now says that is no longer true

It states, in the present tense: *"A tool composite's identity is the request that produced it"*, and
gives the scheme as

```
calc_ref = f"{connector}.{tool}#{stable_hash(arguments)}"
```

`publish/hooks.py::_composite_ref` does not do that any more. It hashes the **result payload**. The
change was made when the shipped hook was audited, and this ADR is the record of it — the reversal
went in without one, which left a merged ADR describing a scheme the code had abandoned, in the
present tense, which is the exact failure `D-2026-08-29-connector-validate-never-dials-a-server`
records one seam over.

## Why the request hash was wrong, in both directions

Both were reproduced before the change, and each is now a test in
`tests/test_publish_reaches_the_hooks.py`.

- **Two requests, one measurement.** Both tool composites take a *sentinel* default: `predict_logd`
  resolves `ph=None` to `settings.logd_default_ph`, `compute_thermochemistry` resolves
  `temperature_k=0.0` to `settings.xtb_thermo_temperature_k`. So the caller who omits the parameter
  and the caller who passes exactly the value it resolves to send **different arguments and get the
  identical answer**. On a request hash that is two permanent rows for one measurement, in a store
  this system does not own and cannot de-duplicate afterwards. The result restates the parameter it
  actually used (`LogdResult.ph`, `ThermochemistryResult.temperature_k`), so on the payload it is one.

- **One request, two measurements.** The outbox's identity is `(sink, calc_ref, schema_version)` and
  a delivered row is kept forever, so a ref that does not move when the science moves makes the
  *first* computation permanent: re-running the same question after a calculator or epoch change
  queues a genuinely different result and `ON CONFLICT DO NOTHING` **drops it silently**. The two
  older hooks do not have this problem because their refs carry a version — the cache key's
  `calc_version` and epoch-folded `params_hash`, the job's workflow id. A composite has no version of
  its own to carry: its parts each have one, they are not visible at this seam, and `publish` may not
  import `science` to reach them (`tests/test_layering.py`). What *is* visible is that the numbers
  came out different, which is the same fact one step later.

The route stays in front of the hash rather than folded into it, so a `calc_ref` still says where it
came from when a person reads it.

## The defect the reversal admitted, and how it is closed

Moving the identity onto the result put a **presentational argument** inside it.
`compute_thermochemistry` takes `top_bands`, which changes no thermodynamic value: it truncates
`modes` to `strongest_bands(limit)` on the way out, for a caller's context budget. Reproduced: the
same molecule at the same temperature with `top_bands=200` and with the default gave identical
`structure_id`, `temperature_k` and `gibbs_free_energy_hartree`, and **two different permanent
`calc_ref`s** — the "two requests, one measurement" defect above, returning through the other seam.

`_PRESENTATIONAL` names `modes`, and it is dropped before the hash. Dropped rather than
canonicalised: the truncation is lossy and this seam holds neither the full list nor the limit that
produced the subset, so there is nothing here to reconstruct it from. No physics is lost —
`mode_count` is the honest count of the full set, `imaginary_frequencies_cm` and
`lowest_wavenumbers_cm` are stated as always coming from the full set, and every energy is in the
payload unchanged. A genuinely different spectrum is a different Hessian and moves all of those.

The set is one key rather than a policy, and the rule for adding to it is narrow: **a field belongs
in `_PRESENTATIONAL` only when a caller's argument decides it and no measurement does.**

## What stays as it was

`input_hash` still hashes the raw validated arguments, deliberately and unchanged. It is a different
fact from the identity: `calc_ref` is what came back, `input_hash` is what was asked for, and an
unstated default reads as unstated there because that is what the caller actually sent.

## Consequences

- The residual the request scheme could not fix is unfixed and unfixable at this seam: a composite
  still has no version of its own, and the payload changing is the only signal available that the
  science moved. That is a weaker signal than the cache key's `calc_version` — a calculator change
  that happens to produce bit-identical numbers is one record, correctly — and it is the best this
  layer can see without importing `science`.
- Anything added to `TOOL_COMPOSITES` inherits both properties: its identity is its result, and any
  argument of its that is purely presentational must be declared in `_PRESENTATIONAL` or it forks
  the record. The suite derives the first set; the second is a review rule, and saying so is the point.
