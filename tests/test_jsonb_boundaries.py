"""Every value this system writes into a `jsonb` column goes through one guard, or says why not.

`jsonb` cannot hold a non-finite float, and the resulting `psycopg` error escapes handlers written
for domain errors. The enumeration is derived: this walks `src/` for every construction of
psycopg's `Jsonb`, and each site must be in `GUARDED` (routed through
`chemclaw.core.jsonb.json_column`) or `NOT_YET_MEASURED` (declared, with a reason). A new site fails
until its author decides which.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: Sites routed through `chemclaw.core.jsonb.json_column`. `test_no_guarded_site_has_reverted` holds
#: each one to that.
GUARDED = {
    "chemclaw/science/calc/postgres_store.py",
    "chemclaw/publish/outbox.py",
    "chemclaw/science/bo/campaign_record_store.py",
    # An artefact's spec refuses non-finite numbers in its own model, and this guard is the second
    # wall rather than the first: a revision is append-only, so a bad row could never be tidied.
    "chemclaw/exhibits/store.py",
    # A relayed turn frame is the same event the local SSE stream sends; refusing a non-finite
    # number at the write keeps a remote view from carrying a frame no client could parse.
    "chemclaw/agent/turn_remotes.py",
}

#: Sites that still construct `Jsonb` directly, with the reason each is not yet converted.
#:
#: Not an endorsement: whether a non-finite value can reach each one has not been measured, and that
#: decides whether the fix is a guard or a model constraint one layer up.
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

    `core/jsonb.py` is excluded by name: it is the guard, not a boundary. The walk matches the
    callee's name, so an aliased import (`Jsonb as J`) is not seen; the guarded-against shape is an
    ordinary new write, not a hidden one.
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
    """Every `jsonb` boundary is guarded or declared: the partition is total.

    The next module that writes a `jsonb` column fails here until its author places it.
    """
    declared = GUARDED | set(NOT_YET_MEASURED)
    actual = _jsonb_sites()
    assert actual <= declared, (
        f"undeclared `jsonb` write boundary: {sorted(actual - declared)}. Route it through "
        "`chemclaw.core.jsonb.json_column` and add it to GUARDED, or add it to NOT_YET_MEASURED "
        "with the reason a non-finite value cannot reach it."
    )


def test_no_guarded_site_has_reverted() -> None:
    """No guarded site has reverted to a bare `Jsonb`.

    The partition test asserts `actual <= declared`, which a regressed guarded module still
    satisfies, so membership is checked: no guarded module constructs `Jsonb` directly.
    """
    reverted = sorted(GUARDED & _jsonb_sites())
    assert not reverted, (
        f"these are declared GUARDED and construct `Jsonb` directly again: {reverted}. Either the "
        "fix was reverted — route the write back through `chemclaw.core.jsonb.json_column` — or a "
        "second, genuinely different boundary was added to the same module, in which case route "
        "that one too rather than moving the file to NOT_YET_MEASURED: the rest of it is measured."
    )


def test_no_declared_site_has_quietly_disappeared() -> None:
    """No declared site has quietly disappeared.

    An entry whose file no longer constructs `Jsonb` has been fixed and must leave the declaration.
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
