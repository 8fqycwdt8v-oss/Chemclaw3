"""Scratch: what a per-role species-set column on reaction_records costs, and what it would add."""

import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["MOCK_ELN_EXPORT_DIR"] = str(tmp / "json")
os.environ["MOCK_ORD_EXPORT_DIR"] = str(tmp / "ord")
sys.path.insert(0, os.environ["CHEMCLAW_MOCK_REPO"])
from app.eln.seed import seed_all  # noqa: E402

print("seeded", seed_all())

from chemclaw.ingest.eln.json_adapter import JsonExportAdapter  # noqa: E402
from chemclaw.ingest.eln.ord_adapter import OrdJsonAdapter  # noqa: E402
from chemclaw.ingest.eln.record import record_from_ord_reaction  # noqa: E402
from chemclaw.memory.optimization import find_optimization_campaigns  # noqa: E402
from chemclaw.memory.progression import _DIFFED_ROLES, _species, changes_between  # noqa: E402


async def load() -> list:
    out = []
    failed = Counter()
    for adapter in (JsonExportAdapter(str(tmp / "json")), OrdJsonAdapter(str(tmp / "ord"))):
        entries = await adapter.fetch_new_entries(datetime(1970, 1, 1, tzinfo=UTC))
        for raw in entries:
            try:
                out.append(adapter.map_to_ord(raw))
            except Exception as exc:  # noqa: BLE001
                failed[type(exc).__name__] += 1
    print("mapped", len(out), "failed", dict(failed))
    return out


reactions = asyncio.run(load())
by_id = {r.reaction_id: r for r in reactions}

row_bytes = []
col_bytes = []
for r in reactions:
    rec = record_from_ord_reaction(r)
    row = rec.model_dump(mode="json")
    row_bytes.append(len(json.dumps(row)))
    proj = {role.value: sorted(_species(r, role)) for role in _DIFFED_ROLES}
    col_bytes.append(len(json.dumps(proj)))
print(
    f"records {len(reactions)}; row json bytes total {sum(row_bytes):,} "
    f"(mean {sum(row_bytes) / len(row_bytes):.0f}); species column total {sum(col_bytes):,} "
    f"(mean {sum(col_bytes) / len(col_bytes):.0f}) = {100 * sum(col_bytes) / sum(row_bytes):.1f}% of row"
)

SETPOINTS = {"temperature", "time"}
from app.eln.fixtures_data import ord_style_records, uspto_style_records  # noqa: E402
from app.eln.real_procedures import real_uspto_style_records  # noqa: E402

curated = {r["id"] for r in uspto_style_records() + real_uspto_style_records()} | {
    r["reactionId"] for r in ord_style_records()
}
only = os.environ.get("ONLY")
if only == "curated":
    reactions = [r for r in reactions if r.reaction_id in curated]
elif only == "hte":
    reactions = [r for r in reactions if r.reaction_id not in curated]
print("subset", only, len(reactions))
campaigns = find_optimization_campaigns(reactions)
pairs = 0
species_any = 0
species_only = 0  # a species moved and neither setpoint did: turn-time says unchanged / —
non_solvent = 0  # a reagent/catalyst/reactant moved: no prose-solvent reading can recover it
solvent_only_species = 0
setpoint_any = 0
says_unchanged = 0
role_counts = Counter()
for c in campaigns:
    members = sorted((by_id[i] for i in c.reaction_ids), key=lambda r: r.reaction_id)
    for a, b in zip(members, members[1:], strict=False):
        pairs += 1
        ch = changes_between(a, b)
        sp = [x for x in ch if x.variable not in SETPOINTS]
        st = [x for x in ch if x.variable in SETPOINTS]
        role_counts.update(x.variable for x in sp)
        setpoint_any += bool(st)
        if sp and (
            (a.temperature_c is not None and b.temperature_c is not None)
            or (a.time_h is not None and b.time_h is not None)
        ):
            says_unchanged += 1
        if sp:
            species_any += 1
            if not st:
                species_only += 1
            if any(x.variable != "solvent" for x in sp):
                non_solvent += 1
            else:
                solvent_only_species += 1
sizes = sorted(len(c.reaction_ids) for c in campaigns)
print(f"campaigns {len(campaigns)} sizes min/median/max {sizes[0]}/{sizes[len(sizes)//2]}/{sizes[-1]}")
print(f"adjacent pairs {pairs}; setpoint moved {setpoint_any}; species moved {species_any}")
print(f"species moved, no setpoint moved {species_only}; non-solvent species moved {non_solvent}; solvent-only species {solvent_only_species}")
print("species changes by role", dict(role_counts))
print("species moved but setpoints comparable and equal (turn-time renders unchanged):", says_unchanged)
