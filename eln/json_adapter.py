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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.config import settings
from eln.adapter import ElnMappingError, RawEntry
from eln.ord import Component, OrdReaction, Role

# Deterministic free-text extractors for the two conditions an ELN reliably states in prose.
# The temperature pattern *requires* the degree sign: "80 °C" is unambiguously a temperature,
# whereas a space-less/degree-less "13C" (as in "13C NMR") or "pH 7 C" is not — demanding `°`
# avoids fabricating a temperature from spectroscopy or label text. The lookbehind stops a
# `-` preceded by a digit/dot from being read as a minus sign: in a range like "60-80 °C"
# the dash is a separator, so the match is the upper bound 80, never a sign-flipped -80.
# Extracting the upper bound is the deliberate (documented) reading of a range; a genuine
# "-10 °C" still matches because nothing numeric precedes its sign.
_TEMPERATURE = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*°\s*C\b")
_TIME_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\b")


class ElnFormatError(ElnMappingError):
    """A raw entry did not match this ELN's expected JSON shape (G4)."""


class JsonExportAdapter:
    """Read a JSON-export ELN directory and map entries to `OrdReaction`. An `ElnAdapter`."""

    def __init__(self, export_dir: str | None = None) -> None:
        """Read from the given directory, or the configured `eln_export_dir`."""
        self._dir = Path(export_dir if export_dir is not None else settings.eln_export_dir)

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return entries whose `timestamp` is at or after `since`, oldest first.

        A file that cannot be read or parsed at all (I/O error, corrupt JSON, non-object
        payload, missing/bad timestamp) is skipped, not raised: one broken export file
        must not abort the whole fetch (same skip-and-continue stance as
        `kg.graph.load_notes`). Reporting those broken files is out of scope here — this
        method cannot even build a `RawEntry` for them to reject through the sync report.
        """
        entries: list[RawEntry] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                created = _parse_timestamp(payload.get("timestamp"), path)
            except (OSError, json.JSONDecodeError, ElnFormatError):
                continue
            if created >= since:
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
        """Map one JSON entry to a canonical `OrdReaction` (structured + free-text).

        Any mapping failure — a missing field, an unknown role, or a schema violation
        (e.g. a reactant tagged as a product) — becomes an `ElnFormatError`, so the sync's
        reject-and-continue handler treats one bad entry as a rejection, not a crash (G4).
        """
        try:
            return self._build(raw)
        except ElnFormatError:
            raise
        except (ValueError, ValidationError) as exc:
            raise ElnFormatError(
                f"entry {raw.entry_id!r}: cannot map to a reaction: {exc}"
            ) from exc

    def _build(self, raw: RawEntry) -> OrdReaction:
        """Do the actual field mapping (structured fields win; prose fills the gaps)."""
        payload = raw.payload
        inputs = [_component(item, Role.REACTANT) for item in _list(payload, "reactants")]
        outcomes = [_component(item, Role.PRODUCT) for item in _list(payload, "products")]
        procedure = str(payload.get("procedure", ""))
        return OrdReaction(
            reaction_id=raw.entry_id,
            inputs=inputs,
            outcomes=outcomes,
            temperature_c=_condition(payload, "temperature_c", _TEMPERATURE, procedure),
            time_h=_condition(payload, "time_h", _TIME_HOURS, procedure),
            yield_percent=_yield(payload),
            provenance=f"eln:{payload.get('operator', 'unknown')}",
            project=payload.get("project"),
        )


def _list(payload: dict[str, Any], key: str) -> list[Any]:
    """Return a required list field, raising `ElnFormatError` if it is missing/empty."""
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ElnFormatError(f"entry missing non-empty {key!r}")
    return value


def _component(item: Any, default_role: Role) -> Component:
    """Build a `Component` from one JSON species (role defaults if unstated)."""
    if not isinstance(item, dict):
        # A bare string (["CCO"]) would AttributeError on .get and crash the sync
        # instead of being rejected as one bad entry (G4).
        raise ElnFormatError(f"component is not an object: {item!r}")
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


def _condition(
    payload: dict[str, Any], key: str, pattern: re.Pattern[str], text: str
) -> float | None:
    """A condition value: the structured field if present, else the prose regex fallback.

    The structured field wins whenever it is present — including a legitimate `0` (an
    ice-bath 0 °C), which a truthiness check would wrongly discard and overwrite with a
    prose match.
    """
    structured = payload.get(key)
    if structured is not None:
        return float(structured)
    match = pattern.search(text)
    return float(match.group(1)) if match else None


def _yield(payload: dict[str, Any]) -> float | None:
    """Take the yield from the first product's structured field (per-product in this ELN).

    `_build` already guarantees `products` is a non-empty list, but not that its items
    are objects — a bare string here must be a mapping error, not an AttributeError.
    """
    first = _list(payload, "products")[0]
    if not isinstance(first, dict):
        raise ElnFormatError(f"product is not an object: {first!r}")
    value = first.get("yield_percent")
    return float(value) if value is not None else None


def _parse_timestamp(value: Any, path: Path) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z'), else `ElnFormatError`.

    A naive timestamp (no UTC offset) is read as UTC: exports from tools that omit the
    offset are common, UTC is the least-surprising reading, and a naive datetime would
    later raise `TypeError` when compared against the sync's offset-aware cursor.
    """
    if not isinstance(value, str):
        raise ElnFormatError(f"{path.name}: missing 'timestamp'")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ElnFormatError(f"{path.name}: bad timestamp {value!r}: {exc}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
