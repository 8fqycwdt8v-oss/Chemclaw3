# D-2026-09-09-a-contract-checked-at-one-door-is-not-a-contract — the calculation cache's four declared invariants, each checked at one of its two doors

**Status:** accepted · **Date:** 2026-09-09

## Context

The calculation store states four contracts in prose, in the present tense, in its own docstrings.
Each one is real. Each one was enforced at exactly one of the places it applies to, and the other
place was measured rather than argued about.

**1. `CALCULATION_EPOCH` re-addresses `get` and does not reach `find`.** The epoch is the half of a
key no `calc_version` covers — our own arithmetic being wrong and then fixed, or a persisted
payload's shape changing under a stable version — and it rides inside `params_hash`. Measured on
the reference store with the epoch moved between two writes of one molecule:

```
epoch 1 -> key thermo@xtb-6.7:a7d334ebee616d78:a075a6029c28d314
epoch 2 -> key thermo@xtb-6.7:a7d334ebee616d78:3ba6ef80c850abd1
get(k1) hit? True
find(smiles='CCO', calc_type='thermo') returned 2 rows:
    3ba6ef80c850abd1 {'g': -40.222} | a075a6029c28d314 {'g': -40.111}
```

`CalculationQuery` filters `calc_type`, `calc_version`, `input_hash`, `structure_id` and the dates
and **never `params_hash`** — and `calc_version` not moving is the entire reason the epoch exists,
so the two rows are indistinguishable in the result. `find_calculations` then tells the model to use
a listed value instead of recomputing and to cite its `calc_ref` in a knowledge note, and
`durable/retention.py` never prunes this table. `science/calc/store.py` already stated the failure —
*"a pre-epoch cache cannot be separated into 'still correct' and 'wrong linear-rotor
thermochemistry' … and serving the wrong half is the failure this exists to stop"* — about the
lookup path, which stops it. The browse did not.

**2. "A non-empty JSON object" was checked one level deep.** `checked_payload` is documented as
*"the one shape contract this store may hold"* and claims *"both halves were measured reachable"*.
`isinstance(value, dict)` says nothing about what is inside one, and three classes of value inside
one reach `jsonb` and fail there. Measured against a migrated database, through `cached_compute`:

```
checked_payload accepts it: True
cached_compute RAISED InvalidTextRepresentation: invalid input syntax for type json
  DETAIL: Token "NaN" is invalid.
row now in cache: None
decimal   PUT-FAILED  TypeError: Object of type Decimal is not JSON serializable
datetime  PUT-FAILED  TypeError: Object of type datetime is not JSON serializable
nul       PUT-FAILED  UntranslatableCharacter: unsupported Unicode escape sequence
```

Reachable rather than hypothetical: the fleet's `max_gradient` is `float(np.max(np.abs(gradient)))`,
a diverged SCF gives NaN, `float("inf")` is already a sentinel there, and `json.loads` — how a
calculation server's answer enters this process — accepts the literal `NaN` by default. Every one
of these raised out of `store.put` **inside** `cached_compute`, after the single-flight future
exists, so every concurrent waiter on that key failed with a driver message naming a JSON token and
neither the calculation nor the field, and the value recomputed on every later call forever. The
asymmetry that makes the second class easy to write: `stable_hash` uses `default=str`, so a
`Decimal` or a `datetime` **keys** fine and then cannot be **stored**.

**3. `molecule_hash` re-derives an identity minted in another repository, and nothing checked it.**
`find_calculations(smiles=…)` cannot scan, so the browse hashes the query molecule the way the row
was keyed — and the row was keyed in `Chemclaw3-mcp`, in a different image, under `rdkit>=2024.3.1`
against this tree's `rdkit>=2026.3.4`. `connectors/calc/remote.py` refuses exactly this for
`calc_version` and `structure_id`: *"a locally-derived value would be well-formed and would match
nothing"*. Here the failure is quieter — an empty listing reads as "nothing has been computed".

**4. "`default=str` lets values that are not JSON-native serialize deterministically" is false for
two classes.** A `set` iterates in an order Python randomises per process: one six-element set gave
five digests under five `PYTHONHASHSEED` values. An object overriding neither `__str__` nor
`__repr__` renders its memory address: one class gave `668cc845d7aa6ec5`, `f5fdffea01ba84c2` and
`f4a76c115ad11869` in three processes. Neither is on a live path — every caller reaches
`stable_hash` with JSON-parsed data — which is what makes it a trap rather than an outage: the
sentence invites the call, and this hash keys the calculation cache, a BO campaign's decision space,
a report id and a workflow id.

## Decision

**A contract is enforced at every door it applies to, or it is prose.**

1. **The epoch becomes a column** (`infra/sql/090_calculation_epoch.sql`), written by
   `cached_compute` and excluded by both backends' browse. `params_hash` is a digest: there is no
   filter to derive and no row to classify after the fact, so the only honest mechanism is to record
   it at write time. **It is not a `CalculationQuery` field** — a row a later epoch invalidated is
   *wrong*, not merely old, which is what `calc_version` is for and why that one is a filter with an
   "every version" default. `get` and `known` still address such a row by key.

2. **`''` means "not recorded", never "epoch 0", and such a row is returned and marked.** Hiding
   every pre-migration row would answer "nothing found" about an entire existing store on the day
   the migration ran — the silent-empty-answer failure `STRUCTURE_KEYED_PREFIXES` and `remote.py`'s
   `structure_id` rule are both written to refuse. Backfilling them to the current epoch was the
   other alternative and is a lie: this repository does not know which epoch wrote them.
   `CalculationRecord.epoch_recorded` is the mark, paid for out of `find_calculations`' own prose
   (a stale "including the expensive DFT jobs" sentence, false since
   `D-2026-08-26-semiempirical-is-the-whole-tier`, and one clause the `smiles` argument already
   said) so the tool went 848 → 845 tokens rather than up.

3. **The column records this repository's half of the epoch and cannot record the fleet's**, and
   that is stated wherever it is read. `remote_key` folds `CALCULATION_EPOCH` over a digest the
   calculation server has already folded *its* constant of the same name into, and that one never
   crosses the wire. The column is therefore a sound "definitely superseded" and not a complete
   "definitely current". The alternative is re-deriving somebody else's identity locally, which is
   finding 3's defect.

4. **`checked_payload` walks the whole payload, and the line is "refuse what fails, document what
   converts".** Refused, each naming the field: a non-finite float, a value with no JSON form, a
   string carrying a NUL, and a non-string mapping key — the last because `{1: "a"}` is written as
   `{"1": "a"}`, so the name a reader addresses the field by is rewritten. Documented and pinned by
   a test rather than refused: a float ≥ 1e16 comes back as an `int`, and a `tuple` comes back as a
   `list`. **The over-claim in that documentation was caught by its own assertion**: "the value is
   preserved exactly" is false — `6.02214076e23` is the double `602214075999999987023872` and the
   integer returned is `602214076000000000000000`, a different exact number and the same double. The
   true statement is `float(returned) == stored`, which is what the test asserts.

5. **`PostgresStore.put` writes through `chemclaw.core.jsonb.json_column`**, as a backstop rather
   than as the check. `put` is public and has writers that never pass `cached_compute` —
   `ArrayOffloadingStore`'s rewrite, a backfill — by the same argument `publish_stored_result`
   already makes about being paired with `put`. It turns the wall into a `ValueError` raised in this
   process with a stack naming the caller.

6. **`molecule_hash`'s agreement with the fleet is measured, not asserted.** Over a 14-molecule
   corpus chosen for the features an RDKit release re-ranks — fused and bridged rings,
   hetero-aromatics, stereocentres, `E/Z`, a salt, an isotope, a radical — driven against the
   fleet's **own** `descriptors.cache_key` and `solubility.cache_key` in its own interpreter, and
   skipping loudly with no sibling checkout. **The claimed divergence does not exist today**:
   identical on all 14, both checkouts at rdkit 2026.03.5. That is the result, and the control is
   what the finding was actually worth. **A matching pin floor was rejected**: two `>=` floors do
   not equalise two images, and no floor reaches a row an upgraded image already wrote.

7. **`stable_hash` refuses the two values whose `str()` is a property of the run**, by name, and
   keeps `str` for everything else. An allow-list of accepted types was the alternative and is
   worse: `datetime`, `Decimal`, `Enum`, `UUID` and `Path` are what actually reaches the hook and
   would have to be refused to close a hole none of them is in.

8. **Two prose claims are corrected rather than kept.** `_log_prediction` said its subject key was
   *"the same identity the calculation cache uses"*; measured on `CCO` the ledger hashes the bare
   string (`f29e20f49d416e54`) and the cache hashes the mapping (`a7d334ebee616d78`). Nothing joins
   the two tables on it, so the cost is the next reader who writes that join.
   `science/calc/calibration.py` carries the same false sentence and is not this change's to fix.
   And `calculator_trust` told the model only that *"a low value means the stated error bars are too
   narrow"* for a coverage that is the fraction inside **±1σ** — so a correctly calibrated Gaussian
   scores ≈**0.68** and read as badly miscalibrated. The tool now names the target.

## Consequences

- **A deployment's browse loses nothing today and is protected from the next epoch bump.** Every row
  currently on disk carries `epoch = ''` and is still listed; rows written from now on carry the
  epoch and a later bump strands them from the listing rather than from `get` alone.
- **A NaN result is still not cached and still recomputes** — it is not a result, so that is
  correct. What changed is that it fails in this process, before the write, naming the field, so
  every waiter on the single-flight future sees the calculation rather than a JSON parser.
- **The `default` prefix went 64,618 → 64,686 tokens** against a 65,000 ceiling: +45 on
  `calculator_trust` for the ±1σ clause, −3 on `find_calculations` after the epoch mark was paid for
  out of its own stale prose, and the rest rounding. Headroom 382 → 314, recorded because this file
  keeps discovering that a number nobody re-measures is a memory.
- **`stable_hash` can now raise where it previously returned a digest.** No caller in `src/` passes a
  set or a bare object — that was measured before the change — so nothing shipped changes behaviour;
  what changes is that writing such a caller fails immediately instead of minting an id that moves.

## What keeps it true

- `tests/test_store.py::test_the_browse_does_not_serve_a_row_the_epoch_invalidated`
- `tests/test_store.py::test_a_value_postgres_cannot_store_is_refused_by_name_before_the_write`
- `tests/test_store.py::test_a_storable_payload_is_still_returned_unchanged`
- `tests/test_postgres_store.py::test_the_two_backends_agree_about_a_superseded_epoch`
- `tests/test_postgres_store.py::test_a_rewrite_that_carries_no_epoch_does_not_blank_a_recorded_one`
- `tests/test_postgres_store.py::test_put_refuses_a_non_finite_float_in_this_process_not_at_the_wall`
- `tests/test_postgres_store.py::test_a_large_float_keeps_its_value_and_loses_only_its_python_type`
- `tests/test_calc_find.py::test_the_browse_marks_a_row_whose_epoch_was_never_recorded`
- `tests/test_ids.py::test_the_molecule_hash_this_repo_derives_is_the_one_the_fleet_writes`
- `tests/test_ids.py::test_a_value_whose_str_is_not_stable_is_refused_rather_than_hashed`
- `tests/test_ids.py::test_the_value_types_that_do_reach_this_still_hash`
