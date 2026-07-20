"""A concrete adapter for a JSON-exporting ELN (plan step 4.3).

One real adapter, not a universal abstraction: many ELNs export each experiment as a JSON
file, so this reads `*.json` from a directory (`settings.eln_export_dir`), one file per
entry. It shows both mapping paths the plan calls for (step 4.4): **structured** fields map
deterministically, and headline conditions missing from the structured fields are recovered
from the **free-text** procedure by deterministic regex (temperature, time). Genuinely
unstructured cases the regex cannot resolve are escalated to the `eln-reaction-extraction`
skill (per-field LLM), which is judgment and lives outside this deterministic adapter.

Expected entry shape (this ELN's format — known only here):
    {"id": "...", "timestamp": "ISO-8601",
     "reactants": [{"smiles": "...", "role": "reactant", "mass_mg": 460}, ...],
     "products":  [{"smiles": "...", "yield_percent": 85}, ...],
     "procedure": "free text", "operator": "..."}
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from chemclaw.config import settings
from eln.adapter import RawEntry
from eln.ord import Component, OrdReaction, Role

# Deterministic free-text extractors for the two conditions an ELN reliably states in prose.
_TEMPERATURE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*C\b")
_TIME_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\b")


class ElnFormatError(ValueError):
    """A raw entry did not match this ELN's expected JSON shape (G4)."""


class JsonExportAdapter:
    """Read a JSON-export ELN directory and map entries to `OrdReaction`. An `ElnAdapter`."""

    def __init__(self, export_dir: str | None = None) -> None:
        """Read from the given directory, or the configured `eln_export_dir`."""
        self._dir = Path(export_dir if export_dir is not None else settings.eln_export_dir)

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return entries whose `timestamp` is strictly after `since`, oldest first."""
        entries: list[RawEntry] = []
        for path in sorted(self._dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            created = _parse_timestamp(payload.get("timestamp"), path)
            if created > since:
                entries.append(
                    RawEntry(
                        entry_id=str(payload.get("id") or path.stem),
                        created_at=created,
                        payload=payload,
                    )
                )
        entries.sort(key=lambda e: e.created_at)
        return entries

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one JSON entry to a canonical `OrdReaction` (structured + free-text)."""
        payload = raw.payload
        inputs = [_component(item, Role.REACTANT) for item in _list(payload, "reactants")]
        outcomes = [_component(item, Role.PRODUCT) for item in _list(payload, "products")]
        procedure = str(payload.get("procedure", ""))
        return OrdReaction(
            reaction_id=raw.entry_id,
            inputs=inputs,
            outcomes=outcomes,
            temperature_c=_first_field(payload, "temperature_c")
            or _extract_float(_TEMPERATURE, procedure),
            time_h=_first_field(payload, "time_h") or _extract_float(_TIME_HOURS, procedure),
            yield_percent=_yield(outcomes, payload),
            provenance=f"eln:{payload.get('operator', 'unknown')}",
        )


def _list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return a required list field, raising `ElnFormatError` if it is missing/empty."""
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ElnFormatError(f"entry missing non-empty {key!r}")
    return value


def _component(item: dict[str, Any], default_role: Role) -> Component:
    """Build a `Component` from one JSON species (role defaults if unstated)."""
    smiles = item.get("smiles")
    if not smiles:
        raise ElnFormatError(f"component missing 'smiles': {item!r}")
    role = Role(item["role"]) if item.get("role") else default_role
    return Component(
        smiles=str(smiles),
        role=role,
        amount_mmol=item.get("amount_mmol"),
        mass_mg=item.get("mass_mg"),
    )


def _first_field(payload: dict[str, Any], key: str) -> float | None:
    """Return a structured float field if present (the deterministic path wins)."""
    value = payload.get(key)
    return float(value) if value is not None else None


def _extract_float(pattern: re.Pattern[str], text: str) -> float | None:
    """Return the first regex-captured float in `text`, or None (free-text fallback)."""
    match = pattern.search(text)
    return float(match.group(1)) if match else None


def _yield(outcomes: list[Component], payload: dict[str, Any]) -> float | None:
    """Take the yield from the first product's structured field (per-product in this ELN)."""
    products = payload.get("products") or [{}]
    value = products[0].get("yield_percent")
    return float(value) if value is not None else None


def _parse_timestamp(value: Any, path: Path) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z'), else `ElnFormatError`."""
    if not isinstance(value, str):
        raise ElnFormatError(f"{path.name}: missing 'timestamp'")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ElnFormatError(f"{path.name}: bad timestamp {value!r}: {exc}") from exc
