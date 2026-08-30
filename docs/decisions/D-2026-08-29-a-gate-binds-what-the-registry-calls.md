# D-2026-08-29-a-gate-binds-what-the-registry-calls — datasource-validate passed a config the worker refuses

**Status:** accepted · **Date:** 2026-08-29

## The defect

The `DataSource` seam's deliberate trade is that **the callable's signature is the config schema**:
there is no second pydantic model to keep in step with a site's adapter, and what makes that safe is
`make datasource-validate` binding the manifest's kwargs against the real signature, offline, in CI.

`_check_half` bound `name` for the `retrieve` field only — correct when it was written, because only
`_build_retrieve_half` passed one. `_build_ingest_half` has passed `name=manifest.name` since
`D-2026-08-27`'s ledger needed a per-source identity, and `_build_commitments_half` since the third
half landed. The gate did not move with them.

So an external site's own ingest adapter, written to exactly the contract
`ingest/sources/README.md` documented — `fetch_new_entries` and `map_to_ord`, no constructor
argument — **passed the gate cleanly and then failed at worker startup** with
`TypeError: unexpected keyword argument 'name'`. Reproduced before the fix
(`tests/test_datasource_seam.py::test_the_gate_binds_every_half_as_the_registry_actually_calls_it`):
`registry.make_data_source` raised naming `name`, and `validate_datasources()` returned `[]`.

**A gate that passes what the runtime refuses is worse than no gate**, because it is the reason the
adapter was shipped. It fails in both directions at once: it would also have reported a half that
correctly *requires* `name` as broken.

Beside it, `commitments` was not in the validator's loop at all — the third half has had no gate
since it was added, so its first report of a typo was a worker crash.

## The decision

`_check_half` binds `{**config, "name": name}` for every field, and the loop covers all three
declared halves. There is no per-field branch left to drift, which is the point: the registry passes
the same thing to all three halves, so the gate binds the same thing for all three. The condition
that made the old code correct — "only retrieve halves are named" — was a fact about one moment,
written as a rule.

Asserted as an **agreement** rather than as a message: the test drives the registry and the gate over
the same manifest and requires them to reach the same verdict, so it stays true whichever way a
future change moves the contract.

`ingest/sources/README.md` now states the `name` keyword as part of the contract for all three
halves, with what each one's identity is used for. It documented only `fetch_new_entries`/
`map_to_ord`, which is what made an adapter written to it wrong.

## Not done

`--construct` already covers commitments through `make_data_source`, so nothing new was needed
there. Constructing remains opt-in for the reason it always was: construction is a half's own code.
