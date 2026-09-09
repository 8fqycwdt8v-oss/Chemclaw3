"""Calculation result store — compute once, never twice (plan Phase 1b, D-011).

Results are addressed by a **versioned** `CalculationKey`: the calculator's
version is part of the key, so bumping a model or method does not silently return
a stale result — it is a cache miss and recomputes. `CALCULATION_EPOCH` is the
other half of that guarantee, covering the changes a calculator version cannot
see because they are ours rather than the underlying program's. `ResultStore` is one
interface with swappable backends (in-memory for tests, Postgres for real), and
`cached_compute` is the single lookup-before-compute path every calculator shares
(DRY) — the one place that decides hit vs. miss and persists new results.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from math import isfinite
from typing import Any, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from pydantic import BaseModel, Field, model_validator

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# A result payload is any JSON-serializable mapping. Calculators own their typed
# models; the store persists the plain dict so it stays calculator-agnostic.
ResultPayload = dict[str, Any]


class CorruptCacheRow(ValueError):
    """A value in or headed for `calculation_results` is not a result this cache can hand back.

    Its own type because the two things it separates are acted on differently: "this row is not a
    result" is an operator's job (delete the row, let the next call recompute), while a
    `ValidationError` from a calculator's own model is a code change. `psycopg` raising
    `TypeError: the JSON object must be str, bytes or bytearray, not list` is neither, and is what
    a non-object row used to produce.
    """


# The version of **ChemClaw's own** contribution to a stored result — the half no `calc_version`
# covers.
#
# **The calculation server has a constant of the same name, and the two are composed rather than
# compared.** `remote_key` folds this value into a `params_hash` the server has *already* folded its
# own epoch into, so the stored address carries both and neither side has to know the other's
# number: a bump here re-addresses every `calc` row without the server moving, and a bump there does
# the same without this file moving. They are two independent invalidators, not one constant with
# two homes — which is why nothing here asserts equality with the constant of the same name in
# `Chemclaw3-mcp`'s calculation server, and why a test that did would go red on a legitimate
# one-sided bump. `tests/test_calc_remote.py` holds the relationship that actually exists, in
# `test_the_two_epochs_compose_rather_than_having_to_match`.
#
# **It is folded in wherever a key is assembled, and getting that wrong is silent.**
# `CalculationKey.build` is the original and folds it in; every `calc` key comes back from the
# calculation server as four parts and is assembled by `connectors.calc.remote.remote_key`, which
# folds the epoch into the params hash there instead. For one release after the physics moved it
# was folded in at *neither* place for `calc`, so a bump invalidated the DFT bundle's rows and
# nothing else while this comment, `science/calc/__init__.py` and a test's own failure message all
# prescribed bumping it as the remedy for a stored payload changing meaning. That bundle is gone
# (`D-2026-08-26-semiempirical-is-the-whole-tier`) and `remote_key` is the live path; the rule the
# episode leaves behind is that a new way of assembling a key is a new place to fold the epoch.
#
# Every calculator's `calc_version` answers one question: *would the program we shell out to
# produce a different number now?* It is built from a tblite build, an xtb/crest binary version, an
# RDKit version. Two things change what a stored row *means* that no such
# version can see, and they turned out to be the same defect reached from two directions:
#
# - **Our own arithmetic was wrong and then fixed.** `xtb_thermo._rotational` divided a linear
#   rotor's partition function by `2 * symmetry` instead of `symmetry`, so every N2/CO/CO2/HCN/
#   alkyne entropy and free energy already on disk is wrong. Nothing in an `xtb.hess` key would
#   ever move for that fix, so those rows would have kept serving the wrong S and G until tblite
#   happened to be upgraded for unrelated reasons.
# - **The payload's shape changed under a stable version.** `SolubilityResult` gained `estimate`,
#   which carries the applicability-domain flag, and nothing was bumped. The field is optional, so
#   a pre-change row validates back with `estimate=None` — an "OUT OF DOMAIN" salt silently
#   degrades to "not assessed". `durable/retention.py` deliberately never prunes
#   `calculation_results`, so such rows never self-heal.
#
# One component rather than two, because both are the single fact *what a stored result means
# changed on our side*, and a mechanism per symptom is how the second one is the one nobody
# remembers. It rides in `params_hash`, not in `calc_version`: the version string is also the
# REV-12 calibration ledger's key (`calc.calibration`), and a measured residual stays valid across
# a ChemClaw fix that a cached prediction does not.
#
# **Bump it whenever a ChemClaw-side change makes an already-written row wrong or incomplete**, and
# add a line to the log below. `tests/test_calc_payload_schemas.py` catches the shape half for you —
# it fails on any change to a persisted payload model. The arithmetic half is a judgement only the
# author of the fix can make, so the rule is written here rather than inferred.
#
#   1 — introduced. Invalidates every row written before it, deliberately: a pre-epoch cache cannot
#       be separated into "still correct" and "wrong linear-rotor thermochemistry / missing
#       applicability-domain flag", and serving the wrong half is the failure this exists to stop.
#   2 — the per-atom reactivity panel. `SiteReactivityResult` gained the conceptual-DFT global
#       descriptors (IP, EA, chemical potential, hardness, softness, electrophilicity) and four
#       local ones per site; `AtomCharge` gained its Wiberg and free valence. No stored number
#       moved — the calculation server runs the same three SCFs on the same geometry and simply
#       reads energies it used to discard — but every epoch-1 row is now *incomplete*, and the new
#       fields are required, so one cannot come back validating as a panel it never carried.
#       Bumped in `Chemclaw3-mcp/servers/calc/.../key.py` in the same change, as the rule requires.
CALCULATION_EPOCH = "2"


class CalculationKey(BaseModel):
    """Content-addressed identity of a calculation, versioned by the calculator.

    Two calculations share a key iff they are the same calculator *version* run on
    the same input with the same parameters, under the same `CALCULATION_EPOCH`.
    `calc_version` is what prevents a model/method update from returning a pre-update
    cached result; the epoch is what prevents a *ChemClaw*-side fix or payload change
    from doing the same.

    **`build` is not the live path, and a reader verifying an epoch bump through it verifies
    nothing.** Every `calc` key in a deployment comes back from the calculation server as four
    parts and is assembled by `connectors.calc.remote.remote_key`, which folds the epoch in there;
    `build` has no caller in `src/` since the physics left. It is kept rather than deleted because
    it is the one definition of the fold the suite can exercise — hand-folding the epoch across the
    seven test files that construct keys would put the rule in the tests and leave `src/` with
    none — but the two folds are separate code and a new way of assembling a key is a new place to
    fold the epoch.

    **`calc_version` names every program whose output survives into the payload, and no program
    that does not run** (D-2026-08-01-a-key-names-what-ran) — a calculation that composes two
    programs names both, because either one moving changes the number.

    That is a *different* axis from `CALCULATION_EPOCH`, and the two are deliberately not
    merged. The epoch is a source constant: it moves once per release and invalidates every
    deployment at the same moment. A backend is configuration — two deployments running the
    identical release resolve different ones, which is why `xtb_spec.resolve_backend` refuses to
    let `auto` reach a key. Folding a backend into the epoch would make switching one a code
    change; folding the epoch into a version string would make a ChemClaw-side fix invisible
    wherever the underlying programs did not also move, which is the failure the epoch exists
    for.
    """

    # **Constrained because `as_str()` below is the `calculation_results` primary key**, and its
    # four fields arrive verbatim from the calculation server's `calculation_key` answer — which
    # `connectors/calc/remote.py` is right to take rather than re-derive, and which means the
    # identity of every cached row was a string this process never checked. Unchecked, the flat form
    # was ambiguous: `calc_type="a@b", calc_version="c"` and `calc_type="a", calc_version="b@c"`
    # both flatten to `a@b@c:d:e`, so the second upserts over the first and `cached_compute` serves
    # the wrong payload under a key it believes it derived. `("", "", "", "")` built happily and
    # flattened to `"@::"`.
    #
    # **Only the ambiguity is closed, and `calc_version` is deliberately left free of both
    # delimiters' exclusion.** A real version carries them — `esol-delaney@2004` the `@`,
    # `cal-0.28733:-29.3116` the `:` — which is the measured fact that made the key cross the wire
    # as four parts. Barring `@` from `calc_type` fixes the parse from the left, barring `:` from
    # the two hashes fixes it from the right, and the version is then whatever lies between: the
    # encoding is a bijection without constraining the one field that needs to be free. Whitespace
    # is excluded everywhere because a newline inside a primary key would let one key's text carry
    # another's, and no producer has ever emitted one.
    #
    # A pattern here rather than at the reader alone: `kg/note.py::_CALC_REF` validates the same
    # shape on a note's citation, so the check used to exist only on the way *out* of the system.
    # The two are bound by a test that reads these four fields' own patterns, because `kg` may
    # import only `core` and so cannot import `CalculationKey` — the note side was narrower than
    # this one for long enough that no note could cite a calibrated calculation.
    calc_type: str = Field(min_length=1, pattern=r"^[^\s@:]+$")
    calc_version: str = Field(min_length=1, pattern=r"^\S+$")
    input_hash: str = Field(min_length=1, pattern=r"^[^\s:]+$")
    params_hash: str = Field(min_length=1, pattern=r"^[^\s:]+$")

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
    ) -> "CalculationKey":
        """Construct a key by hashing the inputs and parameters.

        Not the live path — see the class docstring: every `calc` key in a deployment is assembled
        by `connectors.calc.remote.remote_key` from the four parts the calculation server returns,
        which folds `CALCULATION_EPOCH` in there. This is the fold the suite can exercise, and the
        two are separate code: a new way of assembling a key is a new place to fold the epoch.
        """
        return cls(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=stable_hash(inputs),
            params_hash=stable_hash({"epoch": CALCULATION_EPOCH, "params": params}),
        )

    def as_str(self) -> str:
        """Flat string form for use as a storage/index key."""
        return f"{self.calc_type}@{self.calc_version}:{self.input_hash}:{self.params_hash}"


def checked_payload(key: CalculationKey, value: object) -> ResultPayload:
    """`value` as a result payload, or `CorruptCacheRow` naming the key it is stored under.

    **The one shape contract this store may hold, and the reason it is only this one.**
    `calculation_results` is a cache keyed by an opaque address onto an opaque payload, and its own
    query model refuses any predicate on that payload because "a `total_energy_hartree > x`
    predicate would put one calculator's schema inside the thing that persists all of them"
    (D-2026-08-25). The physics — and therefore every field name — lives in `Chemclaw3-mcp`
    (`D-2026-08-16-the-physics-leaves-the-cache-stays`), so this repository cannot check what a
    result *says*. What it can check is what `ResultPayload` already declares it to be: a non-empty
    JSON object. That is not one calculator's schema, it is the store's own type, and enforcing a
    declared type is not the predicate the query model refuses.

    **Both halves were measured reachable and neither failed usefully.** A `{"energy": "not a
    number"}` row returned `hit=True computes=0` and flowed out as the tool's answer, permanently,
    because D-011 never recomputes a persisted result and `calculation_results` is never pruned; a
    jsonb array, string, number or `null` — all storable under `JSONB NOT NULL` — reached
    `json.loads` with a non-string and raised `TypeError`, and one such row took `find`'s whole
    browse down with it. Emptiness is the half this can act on: `{}` is not a result under any
    calculator, and it is precisely what a truncated or failed call to the calculation server
    degrades into — `remote_compute` returns `dict[str, Any]` and is persisted unvalidated, so the
    sibling fleet could poison this cache permanently by returning `{}` once. A wrong *value* under
    a right key stays undetectable here and is stated rather than fixed: catching it needs the
    calculator's schema, which is deliberately not in this repository.

    **A third half was claimed by that paragraph and was not there.** "A non-empty JSON object" was
    checked one level deep: `isinstance(value, dict)` says nothing about what is *in* it, and three
    classes of value inside one reach `jsonb` and fail there rather than here
    (`D-2026-09-09-a-contract-checked-at-one-door-is-not-a-contract`, measured against a migrated
    database):

    - a **non-finite float** — `InvalidTextRepresentation: invalid input syntax for type json /
      DETAIL: Token "NaN" is invalid`. Reachable: the fleet's `max_gradient` is
      `float(np.max(np.abs(gradient)))`, a diverged SCF gives NaN, `float("inf")` is already a
      sentinel there, and `json.loads` accepts the literal `NaN` by default — so a server can put
      one into this process without anything raising on the way in;
    - a value with **no JSON form at all** — `TypeError: Object of type Decimal is not JSON
      serializable`, and the same for `datetime`. Asymmetric with the key: `stable_hash` uses
      `default=str`, so such a value *keys* fine and then cannot be stored;
    - a string carrying a **NUL** — `UntranslatableCharacter: unsupported Unicode escape sequence`,
      whose DETAIL names the NUL escape it cannot convert to text. That one must stay refused,
      because Postgres `text` cannot hold a NUL; what changes is that it is refused by name.

    Every one of them arrived out of `store.put` *inside* `cached_compute`, after the single-flight
    future exists — so every concurrent waiter on that key failed with a driver message naming a
    JSON token rather than the calculation or the field, and the value recomputed on every later
    call forever. Naming the field is the whole of the remedy available: a NaN energy is not a
    result, so it cannot be cached, and refusing it early is the difference between an operator
    reading "max_gradient holds the non-finite float nan" and reading a parser's DETAIL line.

    **The line drawn is "refuse what fails, document what converts".** Two conversions happen inside
    `jsonb` and are deliberately *not* refused, because the value survives them and refusing would
    narrow what a calculator may legitimately return:

    - a float of magnitude ≥ 1e16 reads back as an `int` (`1e16` → `10000000000000000`,
      `6.02214076e23` → `602214076000000000000000`), because psycopg reads a `jsonb` number with no
      decimal point as an integer. **`float()` of what comes back is the double that was stored**,
      for every case measured including `5e-324` and `1.797e308` — but "the value is preserved
      exactly" is the wrong way to say it, and the assertion that says it that way fails:
      `6.02214076e23` is the double `602214075999999987023872`, and the integer returned is
      `602214076000000000000000`, a different exact number and the same double. Avogadro's number
      is a legitimate thing for a calculator to return, so this is a fact about the column rather
      than a defect to close;
    - a `tuple` is stored as a JSON array and read back as a `list` — the same elements in the same
      order, nothing reinterpreted.

    A **non-string mapping key** is refused rather than documented alongside them, and the
    difference is what the change is *to*: `{1: "a"}` is written as `{"1": "a"}`, so the name a
    reader addresses the field by is rewritten, and a key type `json.dumps` does not coerce raises
    instead. `ResultPayload` is a `dict[str, Any]`; a key that is not a string is outside the
    declared type, which is the same licence the emptiness check runs on.

    Ordinary unicode is exact and is **not** touched: `α-pinene · Δ 25 °C — ünïcode 中文 🧪`
    round-tripped byte-identical.

    Args:
        key: The address the payload is stored under, so the message names the row to act on.
        value: The candidate payload — from the database on a read, from a calculator on a write.

    Returns:
        `value` unchanged, once it is a non-empty mapping a `jsonb` column can hold.

    Raises:
        CorruptCacheRow: `value` is not a JSON object, is an empty one, or holds a value the
            `result` column would reject — each named by the field it sits on.
    """
    if not isinstance(value, dict):
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} holds {type(value).__name__} where a JSON "
            "object is required. The column is bare `JSONB NOT NULL`, so this row was written by "
            "something other than this store; delete it and the next call recomputes."
        )
    if not value:
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} holds an empty result. No calculator "
            "produces one — an empty object is what a truncated or failed call to the calculation "
            "server degrades into — and D-011 would make it permanent, so it is refused rather "
            "than cached or handed back."
        )
    unstorable = _unstorable(value, "result")
    if unstorable is not None:
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} {unstorable}. The `result` column is "
            "`jsonb`, which cannot hold it, so persisting this would fail at the driver with a "
            "message naming a JSON token instead of the field — and D-011 would recompute it on "
            "every later call. Fix or drop the field named above."
        )
    return value


def _unstorable(value: object, path: str) -> str | None:
    """The first reason `value` could not be written to a `jsonb` column, or None.

    Depth-first over the whole payload rather than one level, because that is where every measured
    failure sat — `{"nested": [{"converged": -inf}]}` is as fatal as a top-level one, and a check
    that stops at the top level is the defect this closes. Iterative rather than recursive so a
    deeply nested payload cannot trade one crash for another; the cost is one pass over a structure
    the caller is about to serialize anyway.

    Returns a clause, not a sentence, so the caller owns the address and the remedy and this owns
    only the finding — the same split `_matches` makes with its query.

    **It runs on reads as well as writes, and the read side is provably a no-op** — `jsonb` cannot
    hold any of what this refuses, so a value that came out of the column has already passed. It is
    not split into a write-only door anyway, because a second door is the whole subject of the ADR
    above, and the cost was measured rather than assumed: **6.6 µs** on a typical 167-character
    result, **4.6 ms** on a 22 kB conformer ensemble (a `json.dumps` of the same payload is 1.6 ms).
    The first is nothing against a database round trip; the second is paid only by the shapes
    `CalculationRecord`'s own ceiling exists for, and only per row of a browse a chemist asked for.

    Args:
        value: The payload, or any value inside it.
        path: Dotted address of `value` within the payload, for the message.
    """
    pending: list[tuple[object, str]] = [(value, path)]
    while pending:
        item, where = pending.pop()
        # One check for both, because `bool` is a subclass of `int` and JSON holds either.
        if item is None or isinstance(item, bool | int):
            continue
        if isinstance(item, float):
            if not isfinite(item):
                return f"holds the non-finite float {item!r} at {where}"
            continue
        if isinstance(item, str):
            if "\x00" in item:
                return f"holds a string containing a NUL character at {where}"
            continue
        if isinstance(item, dict):
            for name, nested in item.items():
                if not isinstance(name, str):
                    return (
                        f"is keyed at {where} by {type(name).__name__} {name!r}, which is not the "
                        "string a JSON object's member name has to be"
                    )
                pending.append((nested, f"{where}.{name}"))
            continue
        if isinstance(item, list | tuple):
            pending.extend((nested, f"{where}[{index}]") for index, nested in enumerate(item))
            continue
        return f"holds {type(item).__name__} {item!r} at {where}, which has no JSON form"
    return None


class StoredResult(BaseModel):
    """A persisted calculation result plus its provenance.

    `provenance` records how the value came to be. For this compute cache it is always
    "computed" (the system ran the calculator) — retained as audit metadata on every
    persisted row, and the seam by which an externally *measured* value could be stored
    under the same key with `provenance="measured"`. It is audit trail, not a control
    signal: no code branches on it, so it is written and available to an auditor/query,
    not read back into logic.
    """

    key: CalculationKey
    result: ResultPayload
    provenance: str = "computed"
    # Wall time the calculation took on the miss that produced it, or None for a result that
    # arrived some other way (a measured value, a backfill). This is the cost policy
    # `durable/retention.py` says a cache needs and refuses to fake with an age cutoff: it is
    # what an artifact eviction orders by, and what tells an operator what the cache is worth.
    compute_seconds: float | None = None
    # When the row was written, for a caller that is *browsing* the store rather than addressing
    # one key. `get` leaves it None because a cache hit does not care; `find` fills it, since "what
    # do we already have on this molecule" is unanswerable without knowing when each was computed.
    created_at: datetime | None = None
    # The 3-D geometry this calculation ran *on* — never the one it produced — for the
    # structure-keyed families. The server's
    # own answer, carried on the row so that "have we already relaxed this conformer?" is a query
    # rather than an unanswerable question (D-2026-08-21). `input_hash` is a digest over the whole
    # argument payload and is not it: two calculations on one geometry in different solvents have
    # different input hashes and the same `structure_id`. Empty for a molecule-keyed calculator.
    structure_id: str = ""
    # The `CALCULATION_EPOCH` this row was written under, so the *browse* can tell a superseded row
    # from a current one. `get` never needs it — the epoch rides in `params_hash`, so a superseded
    # row is simply not at the address a lookup builds — but `find` filters on the four key
    # columns and a params digest is neither a filter nor invertible, so two rows for one molecule
    # under one `calc_version` arrived indistinguishable and the older, invalidated one was offered
    # to the model as evidence to cite
    # (`D-2026-09-09-a-contract-checked-at-one-door-is-not-a-contract`).
    #
    # **Empty means "written before migration 090 recorded this", never "epoch 0".** Such a row is
    # returned by the browse and *marked*, not hidden: no deployment's history can be classified
    # retroactively — `params_hash` cannot be inverted — and hiding every pre-migration row would
    # answer "nothing found" about a whole store the day the migration ran, which is the failure
    # `STRUCTURE_KEYED_PREFIXES` and `remote.py`'s `structure_id` rule both exist to refuse.
    #
    # **It records this repository's half of the epoch and cannot record the fleet's.** The stored
    # `params_hash` folds `CALCULATION_EPOCH` over a server digest that has already folded the
    # calculation server's own constant of the same name, and this process never sees that one. So
    # a row this column calls current may still have been superseded by a bump in `Chemclaw3-mcp`;
    # what the column gives is a sound "definitely superseded", not a complete "definitely
    # current". Stated rather than fixed, because the alternative is re-deriving somebody else's
    # identity locally, which `connectors/calc/remote.py` refuses for exactly this reason.
    epoch: str = ""


# Calculators whose `input_hash` is over a 3-D structure rather than a molecule: the xTB task
# family keys on `(structure_id, charge, multiplicity)` and the geometry pointer on its whole
# subject model. A molecule alone does not determine either hash, so `smiles` cannot address
# them — and answering "nothing found" for a molecule that has an xTB result on file would be the
# one failure this tool cannot afford. Matched as prefixes, since the types are `xtb.<task>`.
#
# **A `structure_id` filter does address them**, which is what makes the refusal below a redirection
# rather than a dead end (D-2026-08-21). It used to be neither: the message named alternatives that
# were all molecule-keyed, so a chemist asking whether a conformer had already been relaxed was
# told to ask a different question instead of a workable one.
#
# `geometry.` is kept and names nothing this deployment writes: the cross-method geometry pointer
# went with the optimizer in `D-2026-08-16-the-physics-leaves-the-cache-stays`. Rows written by an
# earlier release are still on disk — `calculation_results` is never pruned — so removing the
# prefix would make a molecule filter silently answer "nothing found" about them, which is exactly
# the failure the tuple exists to prevent.
STRUCTURE_KEYED_PREFIXES = ("xtb.", "geometry.")


def molecule_hash(smiles: str) -> str:
    """The `input_hash` a molecule-keyed calculator would produce for `smiles`.

    One definition, used by the query filter in both backends: the hash is over the same
    `{"smiles": <canonical>}` mapping the calculators build their keys from, so getting this
    shape wrong in one place cannot make a molecule findable in one store and not the other.

    **It re-derives an identity that is minted in another repository, which is the one thing this
    module otherwise refuses to do.** `find_calculations(smiles=…)` cannot scan — `input_hash` is
    not reversible — so the browse has to hash the query molecule the way the row was keyed, and
    the row was keyed in `Chemclaw3-mcp`, in a different image. `connectors/calc/remote.py` states
    the rule this sits beside: a locally-derived `calc_version` or `structure_id` "would be
    well-formed and would match nothing". The same is true here, and the failure is quieter — an
    empty listing reads as "nothing has been computed".

    The shape is not the risk; the **canonicalizer** is. Both sides call
    `Chem.MolToSmiles(require_molecule(...))`, and both pin RDKit with a `>=` — `rdkit>=2026.3.4`
    here, `rdkit>=2024.3.1` there — so nothing makes the two images run one version, and RDKit's
    canonical ranking is not contractually stable across releases.

    **Measured 2026-09-09 and it agrees**, over a 14-molecule corpus chosen for the features a
    release re-ranks (fused and bridged rings, hetero-aromatics, stereocentres, `E/Z`, a salt, an
    isotope, a radical), driven against the fleet's *own* `descriptors.cache_key` and
    `solubility.cache_key`: identical on all 14. `tests/test_ids.py` keeps it that way and skips
    loudly with no sibling checkout. A matching pin floor was the alternative and does nothing that
    matters — two `>=` floors do not equalise two images, and no floor reaches a row already
    written by an image that has since been upgraded.
    """
    return stable_hash({"smiles": require_canonical_smiles(smiles)})


class CalculationQuery(BaseModel):
    """A search over stored results — the browse half of a store built for exact lookup.

    Every field is a filter and every one is optional, so an empty query is "the most recent
    results" rather than an error. `smiles` is matched by hashing it the way a key is built:
    `input_hash` is not reversible, so a molecule is found by computing its hash, never by
    scanning rows and un-hashing them.

    **A molecule filter reaches the molecule-keyed calculators only** — see
    `STRUCTURE_KEYED_PREFIXES`. Combining it with a structure-keyed `calc_type` raises rather than
    returning an empty list, because the empty list would read as "nothing has been computed" when
    the truth is "that family cannot be looked up this way".

    A `structure_id` filter and a `smiles` filter compose rather than conflict — the first narrows
    to one geometry, the second to one molecule, and a geometry belongs to a molecule — but only
    the second is refused against a structure-keyed type, because only the second cannot address
    one.

    **A superseded epoch is excluded and is not a filter.** `CALCULATION_EPOCH` rides inside
    `params_hash`, so a row it invalidated is unreachable by `get` and was fully reachable by this
    — two rows for one molecule under one `calc_version`, indistinguishable, with the wrong one
    offered as evidence to cite. `_matches` drops a row whose recorded epoch is not the current
    one, with no way to ask for it back, because such a row is wrong rather than merely old. A row
    written before migration 090 records no epoch and is returned; `find_calculations` marks it.

    There is deliberately no filter on the result's *value*. The payload is an opaque
    calculator-owned mapping (`ResultPayload`) — the store has been calculator-agnostic since
    D-011, and a `total_energy_hartree > x` predicate would put one calculator's schema inside the
    thing that persists all of them. A caller that wants that filters the returned rows.
    """

    # Matched by hashing, never by scanning — see above.
    smiles: str | None = None
    # The geometry a calculation was about, as `optimize_geometry` and `sample_conformers` report
    # it. The filter the structure-keyed families needed and did not have (D-2026-08-21): a
    # molecule cannot address them, and until this the refusal below pointed only at calculators
    # that are not about geometries at all.
    structure_id: str | None = None
    calc_type: str | None = None
    calc_version: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 20

    @model_validator(mode="after")
    def _molecule_filter_addresses_the_type(self) -> "CalculationQuery":
        """Refuse a molecule filter on a family a molecule cannot address."""
        if self.smiles is None or self.calc_type is None:
            return self
        if self.calc_type.startswith(STRUCTURE_KEYED_PREFIXES):
            raise ValueError(
                f"{self.calc_type!r} is keyed by 3-D structure, not by molecule, so it cannot be "
                "found by SMILES. Give a `structure_id` instead — the st_... address a geometry "
                "calculation reports — or ask for a molecule-keyed calculation (pka, solubility, "
                "descriptors)."
            )
        return self


@runtime_checkable
class ResultStore(Protocol):
    """Persistence contract for calculation results. Backends implement this."""

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        ...

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        ...

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`."""
        ...


class InMemoryStore:
    """Process-local `ResultStore` — the reference the Postgres one is written to match.

    **A differential oracle, not a deployment backend.** No configuration returns it — every
    `default_*()` in this tree resolves to the Postgres implementation — and that is deliberate
    (`D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend`). It stays in
    `src/` because it is the executable statement of the contract its Postgres sibling is written
    to reproduce, and it is read beside that sibling; `tests/test_reference_stores.py` holds both
    halves of that — the absence of a shipped caller, and the absence of this claim.

    Proves the compute-once logic without a database; the Postgres backend
    implements the same interface for durable, cross-process caching.
    """

    def __init__(self) -> None:
        """Start with an empty cache."""
        self._data: dict[str, StoredResult] = {}

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        return self._data.get(key.as_str())

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        self._data[stored.key.as_str()] = stored

    async def known(self, keys: Sequence[str]) -> set[str]:
        """Which of `keys` the cache holds — parity with the Postgres store's existence probe."""
        return {key for key in keys if key in self._data}

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`.

        Insertion order stands in for time here: this store keeps no clock, and giving it one
        would make a test's ordering depend on how fast it ran. A row with an explicit
        `created_at` still sorts by it, so a fixture that sets one gets the ordering it asked for.

        Undated rows are held out of the comparison rather than given a sentinel date. They lead,
        because in a store whose only clock is insertion order a row nobody dated is the newest
        thing it knows — but a `datetime.max` sentinel to express that was naive while every real
        `created_at` here is timezone-aware, so `find` raised `TypeError: can't compare
        offset-naive and offset-aware datetimes` the moment one store held both kinds of row.
        Partitioning states the same policy and has no sentinel to get wrong.
        """
        matched = [stored for stored in self._data.values() if _matches(stored, query)]
        matched.reverse()  # newest first, since dict order is insertion order
        undated = [stored for stored in matched if stored.created_at is None]
        dated: list[tuple[datetime, StoredResult]] = [
            (stored.created_at, stored) for stored in matched if stored.created_at is not None
        ]
        dated.sort(key=lambda pair: pair[0], reverse=True)
        return (undated + [stored for _, stored in dated])[: query.limit]


def _matches(stored: StoredResult, query: CalculationQuery) -> bool:
    """Whether one stored result satisfies every filter set on `query`.

    Shared by the in-memory store and by the tests that pin the two backends agreeing; the
    Postgres store expresses the same predicate as SQL because it must filter before it fetches.
    """
    key = stored.key
    if query.calc_type is not None and key.calc_type != query.calc_type:
        return False
    if query.calc_version is not None and key.calc_version != query.calc_version:
        return False
    if query.smiles is not None and key.input_hash != molecule_hash(query.smiles):
        return False
    if query.structure_id is not None and stored.structure_id != query.structure_id:
        return False
    if query.since is not None and (stored.created_at is None or stored.created_at < query.since):
        return False
    if query.until is not None and (stored.created_at is None or stored.created_at > query.until):
        return False
    # Unconditional, and it is the one filter no `CalculationQuery` field can turn off. A row whose
    # recorded epoch is not the current one is, by the definition `CALCULATION_EPOCH` is written
    # under, *wrong or incomplete* — not an older number a chemist might legitimately want, which
    # is what `calc_version` is for and why that one is a filter with an "every version" default.
    # An operator who needs such a row still has `get` and `known`, which address it by key.
    if stored.epoch and stored.epoch != CALCULATION_EPOCH:
        return False
    return True


#: Computations currently in flight, by key, **per event loop**. The single-flight ledger: a second
#: miss on a key someone is already computing awaits the first computation instead of starting a
#: duplicate. `api/routes/ops._shared_probe` is the same shape for the readiness probes, and
#: `docs/planning/DEFERRED.md` keeps the *cross-process* half of the dedup — an advisory lock or
#: an in-flight row — which this deliberately does not attempt.
#:
#: **Per loop, because an `asyncio.Future` is.** One flat dict made this ledger process-wide, and a
#: caller on a second loop in the same process — `core/temporal_client.py`'s own docstring names
#: the shape, "an `asyncio.run` in a thread, a test that starts its own" — found the first loop's
#: future and raised `RuntimeError: Task ... attached to a different loop` on awaiting it. That
#: case was neither shared nor deferred: it simply failed, for a cache that exists to stay out of
#: the way. Weak on the loop so a ledger dies with the loop it belongs to rather than being keyed
#: by an `id()` a later loop can be handed again.
_Ledger = dict[str, "asyncio.Future[tuple[ResultPayload, float]]"]
_IN_FLIGHT: "WeakKeyDictionary[asyncio.AbstractEventLoop, _Ledger]" = WeakKeyDictionary()


def _in_flight() -> _Ledger:
    """This loop's slice of the single-flight ledger, created on first use."""
    return _IN_FLIGHT.setdefault(asyncio.get_running_loop(), {})


async def cached_compute(
    store: ResultStore,
    key: CalculationKey,
    compute: Callable[[], Awaitable[ResultPayload]],
    *,
    structure_id: str = "",
) -> tuple[ResultPayload, bool]:
    """Return a result for `key`, computing and persisting it only on a miss.

    This is the single lookup-before-compute path (plan step 1b.4): every
    calculator goes through it, so caching behavior is defined in exactly one
    place. `compute` is called only when the store has no entry for `key`.

    **Concurrent misses on one key in one process now share one computation.** The check-then-act
    was measured at 8 concurrent misses → 8 computes, benign only while every compute was
    milliseconds; a CREST search is 19 minutes of CPU, and two composites sharing a primitive — a
    reaction-energy job and a solvent screen relaxing the same species — could each pay it. The
    first miss computes under a future in `_IN_FLIGHT`; every later caller of the same key awaits
    that future and reports `was_cached=True`, because from its side the answer arrived without a
    computation being started. Cross-process misses still race — that half is deferred with its
    own trigger (`docs/planning/DEFERRED.md`), and identical *jobs* were already collapsed by
    Temporal's workflow-id reuse before either.

    A failed computation fails every waiter with the same exception and clears the slot, so the
    next attempt starts fresh rather than awaiting a corpse.

    **What `compute` returns is checked for shape before it is persisted, and only for shape.**
    `checked_payload` holds that argument; the reason it is *here* is that this is the one
    lookup-before-compute path, so it is the one door a calculation server's answer comes through,
    and D-011 makes anything that gets past it permanent — `calculation_results` is never pruned
    and a persisted result is never recomputed. Refusing `{}` costs a failed tool call; caching it
    costs that key forever.

    Args:
        store: The backend to read from and write to.
        key: The versioned identity of this calculation.
        compute: Zero-arg coroutine that produces the result on a miss.
        structure_id: The geometry this calculation is *about*, when it is about one, so the
            stored row can be found by it (D-2026-08-21). Recorded, never used to look up: the
            key is still the identity, and a second calculation on the same geometry is a
            different row. Empty for a molecule-keyed calculator, which is not about a geometry.

    **Metered here, and only here.** `was_cached` reaches `connectors/calc/compose.py` as a
    per-job field and never became a number, so D-011 — "a persisted result is never recomputed",
    the largest cost lever in this system — could be observed only by turning DEBUG on over the
    hottest read there is. `chemclaw_calc_cache_total{outcome}` separates three states the boolean
    collapses into two: a store `hit`, a `miss` this caller computed, and a `shared` miss that
    another caller in this process was already computing. The third is the single-flight working,
    and it reports `was_cached=True` to its caller, so on the boolean it was indistinguishable from
    a hit — which is exactly the distinction anyone asking "is the cache earning its keep" needs.

    Returns:
        `(result, was_cached)` — `was_cached` is True on a store hit *and* on a miss this call
        joined to another caller's in-flight computation, because from this caller's side the
        answer arrived without a computation being started.
    """
    hit = await store.get(key)
    if hit is not None:
        # DEBUG, not INFO: on the hot path (every calculator call), but it is the one place
        # that answers the recurring troubleshooting question "why did this recompute?".
        logger.debug("calc cache hit: %s", key.as_str())
        record_metric(lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "hit"}))
        _credit_saved_seconds(hit.compute_seconds)
        return hit.result, True
    slot = key.as_str()
    in_flight = _in_flight()
    waiting = in_flight.get(slot)
    if waiting is not None:
        logger.debug("calc cache miss already computing elsewhere, awaiting: %s", slot)
        # Counted before the await, not after: a waiter whose computer is cancelled raises here,
        # and the fact worth counting is that a caller *joined* an in-flight computation instead of
        # starting a second one — which happened whether or not that computation went on to
        # succeed.
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "shared"})
        )
        result, saved = await asyncio.shield(waiting)
        # Valued only once the computation it joined has finished, which is the earliest moment its
        # cost is known — and correctly never, if that computation was cancelled, because this
        # waiter then raises and saved nobody anything.
        _credit_saved_seconds(saved)
        return result, True
    # **The future carries the computation's own wall clock, not a `was_cached` flag.** It used to
    # carry `False`, which every waiter discarded — a constant the computer already knew. What a
    # waiter cannot know and needs is what the computation it joined *cost*, because that is what
    # its own single-flight join saved (see `_credit_saved_seconds`).
    future: asyncio.Future[tuple[ResultPayload, float]] = asyncio.get_running_loop().create_future()
    in_flight[slot] = future
    try:
        logger.debug("calc cache miss, computing: %s", slot)
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "miss"})
        )
        # Monotonic, so a clock adjustment mid-calculation cannot record a negative or absurd cost.
        started = time.perf_counter()
        result = checked_payload(key, await compute())
        elapsed = time.perf_counter() - started
        # **Offered before it is persisted, and the order is the whole of the guarantee.**
        # On the miss branch only — a cache *hit* returns above without touching either write,
        # which keeps them off the hottest read in the system, the same reason
        # `calculation_results` deliberately carries no `last_access_at`. A repeat call costs what
        # it always cost.
        #
        # Persist-then-publish is the intuitive order and it loses publications with no trace.
        # D-011 is what makes the loss permanent rather than self-healing: a crash between the two
        # leaves the row in the cache, so every later call for this key is a *hit* that returns
        # above and never reaches the publish again. Measured with a kill between them —
        # `computes=1 publishes=[]`, `chemclaw_results_queued_total` unmoved, indistinguishable
        # from a calculation nobody ran — and the only recovery is `publish backfill`, a CLI that
        # `durable/schedules.py` runs on no Schedule.
        #
        # Reversed, the same crash leaves a queued row and no cache row: the next call recomputes
        # (the cost D-011 exists to avoid, paid once) and re-enqueues onto the outbox's
        # `ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING`, so nothing duplicates and
        # nothing is lost. That trades an undetectable permanent gap for a bounded, self-healing
        # recompute, and it makes the invariant `publish_stored_result`'s docstring already claims
        # — "persisted implies offered" — true of the cache rather than merely intended.
        #
        # Nothing here depends on the row existing first: the outbox row carries the projected
        # payload itself, not a reference into `calculation_results`. And this cannot fail the
        # calculation, because `enqueue_payload` never raises — the polarity that is also why the
        # reverse order looked safe.
        await publish_stored_result(key, result, compute_seconds=elapsed, structure_id=structure_id)
        await store.put(
            StoredResult(
                key=key,
                result=result,
                compute_seconds=elapsed,
                structure_id=structure_id,
                # The epoch this process is running, which is the epoch `remote_key` (or `build`)
                # folded into `key` a moment ago off the same module constant. Stamped here rather
                # than defaulted on the model, because a `StoredResult` read back from a row
                # written before migration 090 must say "unrecorded" and not claim today's.
                epoch=CALCULATION_EPOCH,
            )
        )
        future.set_result((result, elapsed))
        return result, False
    except BaseException as exc:
        # Cancellation included: a waiter must never hang on a future its computer abandoned.
        if not future.done():
            future.set_exception(exc if isinstance(exc, Exception) else _Abandoned(slot))
            # A future nobody ends up awaiting must not warn on teardown.
            future.exception()
        raise
    finally:
        in_flight.pop(slot, None)


def _credit_saved_seconds(compute_seconds: float | None) -> None:
    """Count the wall clock a lookup did not have to spend, once it is known.

    **The cache's hits were counted and never valued.** `chemclaw_calc_cache_total` says how many
    lookups avoided a computation, and D-011 — "a persisted result is never recomputed" — is called
    the largest cost lever in this system; the only reading of it was a *count*, so a thousand
    avoided millisecond lookups and one avoided nineteen-minute CREST search were a thousand-to-one
    ratio in favour of the wrong one. `StoredResult.compute_seconds` was selected on every hit
    (`postgres_store._SELECT`) and discarded. Measured 2026-09-06: 8 concurrent misses plus 3 later
    reads on one 0.30 s key reported `miss=1, shared=7, hit=3` and nothing anywhere said 3 seconds.

    `None` is the honest reading for a row that arrived some other way — a measured value, a
    backfill — and books nothing rather than a zero, the same rule the token counters follow.

    Args:
        compute_seconds: What the avoided computation cost, or `None` where the row does not say.
    """
    if compute_seconds:
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_seconds_saved_total", float(compute_seconds))
        )


class _Abandoned(Exception):
    """The computing caller was cancelled, so this waiter's shared computation never finished."""

    def __init__(self, slot: str) -> None:
        super().__init__(
            f"the in-flight computation for {slot} was cancelled by the caller running it; "
            "retry to start a fresh one"
        )


async def publish_stored_result(
    key: CalculationKey,
    result: ResultPayload,
    *,
    compute_seconds: float | None = None,
    structure_id: str = "",
    payload_kind: str = "",
) -> None:
    """Offer a just-persisted primitive to the external results store, if one is configured.

    **Public, and paired with `put` rather than with `cached_compute`.** Every writer to the
    calculation store is a producer of publishable science, and this used to be private to the one
    writer that goes through `cached_compute` below. The pairing is *ordered*, and `cached_compute`
    states why: this runs **before** the `put`, so a crash between them costs a recompute instead
    of losing the publication under D-011's own guarantee. The second writer — the removed DFT
    bundle's
    `persist_qm_result`, which could not use `cached_compute` because its computation happened on a
    cluster rather than behind a callable — was therefore missed, and DFT published on backfill and
    never live. It stays public and stays paired with the write rather than with `cached_compute`,
    because that is what makes "persisted implies offered" checkable for the *next* writer that
    does not come through the cache.

    **Imported inside the function, and that is load-bearing rather than stylistic.** `science` may
    not import a capability layer at module scope (`tests/test_layering.py`), and the publish path
    pulls in the projection machinery and RDKit canonicalization — which a deployment with no sink
    configured should never load at all. `publishing_enabled()` is a list lookup, so the whole
    subsystem costs one comparison when it is off.

    Never raises. The calculation succeeded; a results store that cannot be queued to is strictly
    less important than returning the science. (This used to say "and is already persisted", which
    the ordering above makes false at the one call site that has to be right about it — the point
    of the order is that the persist has *not* happened yet.)
    """
    from chemclaw.publish.outbox import enqueue_payload

    await enqueue_payload(
        calc_ref=key.as_str(),
        calc_type=key.calc_type,
        payload=result,
        payload_kind=payload_kind,
        calc_version=key.calc_version,
        input_hash=key.input_hash,
        params_hash=key.params_hash,
        structure_id=structure_id,
        compute_seconds=compute_seconds,
    )
