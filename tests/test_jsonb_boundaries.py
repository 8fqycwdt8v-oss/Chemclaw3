"""Every value this system writes into a `jsonb` column goes through one guard, or says why not.

**The rule existed and was applied by hand, which is how it was applied unevenly.**
`protocols/models.py` sets `allow_inf_nan=False` on ten models, and the BO campaign store wrote
the argument `core/jsonb.py` now carries — and wave 11 still found five paths without it, each
failing differently: the calculation cache raised inside `cached_compute` so every concurrent waiter
failed and the value recomputed forever; the ELN sync's `psycopg` error was neither `ChemclawError`
nor `ValidationError`, so it escaped the per-entry reject-and-continue, aborted the pass and never
advanced the cursor; the publish outbox lost the good records batched beside the poison one; and a
campaign recorded against the in-memory oracle while raising against Postgres.

So the enumeration is **derived**: this walks `src/` and finds every construction of psycopg's
`Jsonb` there is. A tenth site added next year lands in one of the two sets below and its author has
to say which. A hand-written list of "the paths we fixed" is a list of what the tree looked like the
afternoon somebody fixed them, which is the shape this file exists to end.

**On `NOT_YET_MEASURED`.** Those nine are not endorsed and not converted. Wave 11 measured
reachability on the five paths it reviewed, not on these subsystems, and this repository's own rule
is that a check is built after confirming the data it reads exists — converting them on the
strength of "it compiles" would be a change with no measurement behind it. What the entry buys is
that the gap is *stated* and counted rather than invisible, and that a tenth site cannot arrive
unnoticed while it stands.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: Sites wave 11 measured, fixed, and routed through `chemclaw.core.jsonb.json_column`.
#:
#: Each of these was reproduced against a live Postgres before it was changed, and each has a test
#: beside it that was watched failing against the unfixed source.
GUARDED = {
    "chemclaw/science/calc/postgres_store.py",
    "chemclaw/publish/outbox.py",
    "chemclaw/science/bo/campaign_record_store.py",
}

#: Sites that still construct `Jsonb` directly, with the reason each is not yet converted.
#:
#: Not an endorsement. `jsonb` cannot hold a non-finite float at any of them either, so each is the
#: same latent shape — what is missing is the measurement that says whether a non-finite value can
#: *reach* it, which is what decides whether the fix is a guard or a model constraint one layer up.
NOT_YET_MEASURED = {
    "chemclaw/science/calc/postgres_structures.py": "geometry from a validated Structure model",
    "chemclaw/agent/session_store.py": "LangChain message dicts, shape-stamped on write",
    "chemclaw/agent/message_migration.py": "re-writes rows already stored, so already jsonb-legal",
    "chemclaw/agent/session_events.py": "event payloads this system authors",
    "chemclaw/publish/drivers/postgres.py": "generic param adapter under the guarded outbox",
    "chemclaw/durable/job_record_store.py": "Temporal job records, authored here",
    "chemclaw/ingest/eln/records.py": "belt behind ProcessConditions' allow_inf_nan=False",
    "chemclaw/protocols/store.py": "protocol documents, whose models already refuse non-finite",
}


#: The one module that is *supposed* to construct `Jsonb`: it is the guard's definition.
_THE_GUARD = "chemclaw/core/jsonb.py"


def _jsonb_sites() -> set[str]:
    """Every module under `src/chemclaw` that constructs psycopg's `Jsonb` directly.

    `core/jsonb.py` is excluded by name rather than by adding it to a set below, because it is
    neither a guarded boundary nor an unmeasured one — it is the guard, and putting it in either
    table would make that table read as one entry longer than the surface actually is.

    **What this cannot see, stated rather than implied**: an aliased import
    (`from psycopg.types.json import Jsonb as J`) called as `J(...)`, because the walk matches the
    callee's name. Driven, a plain new site in an undeclared module fails this correctly; the alias
    arm does not. Resolving it would mean following bindings, which is the same import-tracking a
    linter does better — and the shape being guarded against here is somebody adding a `jsonb`
    write the ordinary way, not somebody hiding one.
    """
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        if str(path.relative_to(SRC.parent)) == _THE_GUARD:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            called = node.func if isinstance(node, ast.Call) else None
            name = (
                called.id
                if isinstance(called, ast.Name)
                else called.attr
                if isinstance(called, ast.Attribute)
                else None
            )
            if name == "Jsonb":
                found.add(str(path.relative_to(SRC.parent)))
    return found


def test_the_scan_finds_something() -> None:
    """Guard the guard: a walk matching nothing would pass every assertion below."""
    assert _jsonb_sites(), "no `Jsonb` construction found in src/ — the scan stopped working"


def test_every_jsonb_boundary_is_guarded_or_declared() -> None:
    """The partition is total, so a new boundary cannot arrive undeclared.

    This is what makes the rule durable rather than dated. The next module that writes a `jsonb`
    column fails here until its author decides which set it belongs in — which is a smaller ask than
    the four failures wave 11 measured, and the only one that arrives before a deployment does.
    """
    declared = GUARDED | set(NOT_YET_MEASURED)
    actual = _jsonb_sites()
    assert actual <= declared, (
        f"undeclared `jsonb` write boundary: {sorted(actual - declared)}. Route it through "
        "`chemclaw.core.jsonb.json_column` and add it to GUARDED, or add it to NOT_YET_MEASURED "
        "with the reason a non-finite value cannot reach it."
    )


def test_no_declared_site_has_quietly_disappeared() -> None:
    """A declaration that names nothing is a rule about a tree that has moved on.

    `NOT_YET_MEASURED` in particular must shrink as those subsystems are measured; an entry whose
    file no longer constructs `Jsonb` has been fixed, and leaving it here would let the count read
    as outstanding work that is already done.
    """
    stale = sorted(set(NOT_YET_MEASURED) - _jsonb_sites())
    assert not stale, (
        f"these no longer construct `Jsonb` and should leave NOT_YET_MEASURED: {stale}"
    )


def test_the_shared_guard_refuses_what_postgres_refuses() -> None:
    """The guard's whole purpose, driven rather than assumed — in both directions."""
    import pytest

    from chemclaw.core.jsonb import STRICT_JSON

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            STRICT_JSON({"x": value})
    # And a finite double survives at full precision, which is the half a refusal must not cost.
    assert STRICT_JSON({"x": -40.123456789012345}) == '{"x": -40.123456789012344}'
