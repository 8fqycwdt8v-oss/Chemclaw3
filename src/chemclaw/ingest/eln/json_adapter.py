"""A concrete adapter for a JSON-exporting ELN: one `*.json` file per entry in `eln_export_dir`.

Structured fields map deterministically, and only they become recorded conditions: a setpoint the
entry does not state is left absent rather than read out of the prose, where the first match is
usually the addition temperature (D-2026-08-26-a-transcription-may-not-infer-a-setpoint). Genuinely
unstructured cases belong to the `eln-reaction-extraction` skill.

The free-text procedure is kept verbatim (`procedure_text`) and segmented losslessly into ordered
steps (`OrdReaction.steps`), split on numbered markers or sentences, each labelled with a coarse
`StepKind` and any temperature/time it states. Steps carry no `components`: linking a SMILES to a
step from prose would be a guess.

Expected entry shape (this ELN's format — known only here):
    {"id": "...", "timestamp": "ISO-8601", "modified": "ISO-8601", "retracted": "ISO-8601",
     "reactants": [{"smiles": "...", "role": "reactant", "mass_mg": 460}, ...],
     "products":  [{"smiles": "...", "yield_percent": 85}, ...],
     "procedure": "free text", "operator": "..."}

`retracted` is the source withdrawing the entry while still exporting it; a removed file says
nothing, since a fetch is a delta.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import (
    ElnMappingError,
    RawEntry,
    entry_id_or_stem,
    entry_window,
    is_late_arrival,
    parse_iso_utc,
    refuse_colliding_ids,
    warn_late_arrivals,
)
from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    OutcomeClass,
    ReactionStep,
    Role,
    StepKind,
    unresolved_peak_name,
)
from chemclaw.ingest.rejections import record_refusals

logger = logging.getLogger(__name__)

# Every dash a procedure may use as a minus sign: house styles typeset U+2212 and autocorrect
# produces U+2013, and matching only ASCII `-` would read `−78 °C` as `+78 °C`.
_MINUS_SIGNS = "-‐‑‒–—―−"

# Requires the degree sign, so "13C NMR" or "pH 7 C" is never read as a temperature. The lookbehind
# stops a dash after a digit from being a minus: in "60-80 °C" the match is the upper bound 80, the
# deliberate reading of a range, while "-10 °C" keeps its sign.
_TEMPERATURE = re.compile(rf"(?<![\d.])([{_MINUS_SIGNS}]?\d+(?:\.\d+)?)\s*°\s*C\b")

# `str.translate` table mapping every one of them onto the ASCII hyphen-minus `float()` accepts.
_TO_ASCII_MINUS = str.maketrans(dict.fromkeys(_MINUS_SIGNS, "-"))
_TIME_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\b")

# Procedure segmentation: a numbered marker ("1.", "2)", "Step 3:") is the strongest step boundary,
# with sentence boundaries as fallback. Whitespace after `\d+[.)]` keeps "0.5 h" or "2.0 g" from
# being split points.
_STEP_MARKER = re.compile(r"(?:^|\s)(?:step\s*)?\d+[.)]\s+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.;])\s+")

# Coarse step labels in priority order: terminal operations (purification, workup) win over the
# ubiquitous "add", and the verbatim text is kept, so a mislabel loses nothing. Lowercased substring
# match tolerates inflections.
_STEP_KEYWORDS: tuple[tuple[StepKind, tuple[str, ...]], ...] = (
    (StepKind.PURIFICATION, ("crystalli", "chromatograph", "triturat", "distil", "slurr")),
    (
        StepKind.WORKUP,
        (
            "quench",
            "wash",
            "extract",
            "filter",
            "concentrat",
            "evaporat",
            "partition",
            "brine",
            "separat",
            "dry over",
            "dried over",
        ),
    ),
    (
        StepKind.ADDITION,
        ("add", "charg", "dissolv", "combin", "introduc", "treat with", "dropwise", "portionwise"),
    ),
    (StepKind.TEMPERATURE, ("cool", "chill", "warm", "heat", "reflux", "ice bath", "°c")),
    (StepKind.STIR, ("stir", "age", "hold", "maintain")),
)


class ElnFormatError(ElnMappingError):
    """A raw entry did not match this ELN's expected JSON shape."""


class JsonExportAdapter:
    """Read a JSON-export ELN directory and map entries to `OrdReaction`. An `ElnAdapter`."""

    def __init__(self, export_dir: str | None = None, name: str | None = None) -> None:
        """Read from the given directory, or the configured `eln_export_dir`.

        `name` is the data source this adapter is, passed by the registry from the manifest. It
        names every WARNING below and is the rejection ledger's `source`, so two JSON drop
        directories keep separate ledgers.
        """
        self._dir = Path(export_dir if export_dir is not None else settings.eln_export_dir)
        self._source = name or "eln-json"

    async def fetch_new_entries(
        self, since: datetime, limit: int | None = None, *, report_late_arrivals: bool = True
    ) -> list[RawEntry]:
        """Return entries whose `timestamp` is at or after `since`, oldest first.

        A file that cannot be read or parsed is skipped, logged at WARNING and written to the
        rejection ledger, since nothing downstream could otherwise know it existed. A late arrival
        (payload behind `since`, file arriving after it) is reported in one aggregated WARNING and
        filed the same way. The directory read runs in a thread, off the worker's event loop, which
        also carries Temporal heartbeats and the health endpoints.

        `limit` is accepted and ignored: the scan is ordered by filename while an entry's window is
        in its payload, so stopping early would return a non-prefix subset and the cursor would skip
        entries for good. A chunked drain therefore re-scans the directory per chunk; a source large
        enough for that to matter wants the bounded warehouse adapter.

        Args:
            since: The window floor; entries at or after it are returned.
            limit: Accepted for the protocol and unused — see above.
            report_late_arrivals: Whether `since` is the run's floor, so a late-arriving file behind
            it may be reported. False on a continuation chunk — see `is_late_arrival`.
        """
        entries, late, refused = await asyncio.to_thread(self._scan, since, report_late_arrivals)
        # Named by the source, not the format, so two JSON drop directories log distinguishable
        # lines.
        warn_late_arrivals(logger, self._source, late)
        await record_refusals(self._source, refused)
        return entries

    def _scan(
        self, since: datetime, report_late_arrivals: bool
    ) -> tuple[list[RawEntry], list[str], dict[str, str]]:
        """The whole blocking read, in one synchronous function so one thread can hold it.

        Args:
            report_late_arrivals: whether `since` is the run's floor — see `is_late_arrival`.
            since: the window floor; entries at or after it are returned.

        Returns:
            The entries in the window oldest first, the names of the late arrivals, and the refusals
            to file.
        """
        entries: list[RawEntry] = []
        late: list[str] = []
        # entry id -> why it was refused; for a payload that never parsed the file stem is the only
        # id.
        refused: dict[str, str] = {}
        # Every id this directory claims and the files claiming it, over the whole directory, for
        # `refuse_colliding_ids`.
        files_by_id: dict[str, list[str]] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    logger.warning(
                        "%s: skipping ELN export %s: not a JSON object", self._source, path.name
                    )
                    refused[path.stem] = f"{path.name} is not a JSON object, so it is not an entry"
                    continue
                created = _parse_timestamp(payload.get("timestamp"), path)
                # An in-place amendment keeps `timestamp` and moves this one, so filtering on
                # creation alone would never re-fetch a corrected entry.
                modified = _optional_timestamp(payload.get("modified"), path)
                # The source's own withdrawal, an explicit field, never a file's disappearance. It
                # joins the fetch window (`entry_window`) so a withdrawal stamped without touching
                # `modified` is still fetched.
                retracted = _optional_timestamp(payload.get("retracted"), path)
                entry_id = entry_id_or_stem(payload.get("id"), path, "id")
            # `UnicodeDecodeError` must be listed: it is a `ValueError`, neither a `JSONDecodeError`
            # nor an `OSError`, and a non-UTF-8 file would otherwise abort the whole fetch.
            # `ElnMappingError` covers `entry_id_or_stem`'s refusal.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ElnMappingError) as exc:
                logger.warning("%s: skipping ELN export %s: %s", self._source, path.name, exc)
                refused[path.stem] = f"refused ELN export {path.name}: {exc}"
                continue
            files_by_id.setdefault(entry_id, []).append(path.name)
            if entry_window(created, modified, retracted) >= since:
                entries.append(
                    RawEntry(
                        entry_id=entry_id,
                        created_at=created,
                        modified_at=modified,
                        payload=payload,
                        retracted_at=retracted,
                    )
                )
            elif report_late_arrivals and is_late_arrival(path, since):
                late.append(path.name)
                refused[path.stem] = (
                    f"{path.name} arrived after the sync cursor but carries an older timestamp "
                    f"({created.isoformat()}), so no scheduled run will fetch it; re-run the sync "
                    "from an explicit earlier `since` to backfill it"
                )
        collisions = refuse_colliding_ids(logger, self._source, files_by_id)
        entries = [entry for entry in entries if entry.entry_id not in collisions]
        refused.update(collisions)
        entries.sort(key=lambda e: e.created_at)
        return entries, late, refused

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one JSON entry to a canonical `OrdReaction` (structured + free-text).

        Any mapping failure (missing field, unknown role, schema violation, wrong-shaped value)
        becomes an `ElnFormatError`, so the sync rejects the entry rather than crashing.
        """
        try:
            return self._build(raw)
        except ElnFormatError:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise ElnFormatError(
                f"entry {raw.entry_id!r}: cannot map to a reaction: {exc}"
            ) from exc

    def _build(self, raw: RawEntry) -> OrdReaction:
        """Do the actual field mapping (structured fields win; prose fills the gaps)."""
        payload = raw.payload
        inputs = [_component(item, Role.REACTANT) for item in _require_list(payload, "reactants")]
        outcomes = [_component(item, Role.PRODUCT) for item in _require_list(payload, "products")]
        procedure = str(payload.get("procedure", ""))
        return OrdReaction(
            reaction_id=raw.entry_id,
            inputs=inputs,
            outcomes=outcomes,
            # The structured field or nothing: these are typed setpoint columns, so a number derived
            # from prose may not be written here. See `_number`; prose numbers go on the steps.
            temperature_c=_number(payload, "temperature_c"),
            time_h=_number(payload, "time_h"),
            yield_percent=_product_number(payload, "yield_percent"),
            purity_percent=_product_number(payload, "purity_percent"),
            impurities=_impurities(payload),
            # No `performed_at`: this export has no experiment date. `adapter.DatedIngest` supplies
            # the entry date with `date_source="entry"`, so the record does not claim a
            # chemist-stated date.
            outcome_class=_outcome_class(payload),
            failure_reason=payload.get("failure_reason"),
            provenance=_provenance(payload, raw),
            project=payload.get("project"),
            # What the run was testing, only from the entry's own field: a hypothesis
            # pattern-matched from prose would be indistinguishable downstream from one the chemist
            # wrote.
            hypothesis=payload.get("hypothesis"),
            steps=_segment_steps(procedure),
            procedure_text=procedure or None,
        )


def _segment_steps(procedure: str) -> list[ReactionStep]:
    """Split a free-text procedure into ordered, coarsely-labeled steps (lossless).

    Each step keeps its segment verbatim plus any temperature/time read from it; species are left
    unlinked. An empty procedure yields no steps.
    """
    return [
        ReactionStep(
            index=i,
            kind=_classify(segment),
            text=segment,
            temperature_c=_search(_TEMPERATURE, segment),
            duration_h=_search(_TIME_HOURS, segment),
        )
        for i, segment in enumerate(_split_segments(procedure), start=1)
    ]


def _split_segments(procedure: str) -> list[str]:
    """Break a procedure into step segments on numbered markers, else sentence boundaries."""
    text = procedure.strip()
    if not text:
        return []
    parts = _STEP_MARKER.split(text) if _STEP_MARKER.search(text) else _SENTENCE_END.split(text)
    return [stripped for part in parts if (stripped := part.strip(" .;\n\t"))]


def _classify(segment: str) -> StepKind:
    """Label a step by the first keyword group it matches, else `CUSTOM` (best-effort)."""
    low = segment.lower()
    for kind, keywords in _STEP_KEYWORDS:
        if any(word in low for word in keywords):
            return kind
    return StepKind.CUSTOM


def _search(pattern: re.Pattern[str], text: str) -> float | None:
    """First numeric group the pattern matches in `text`, as a float, else `None`.

    Typographic minus signs are normalised to ASCII for `float()`, here rather than in the text, so
    `procedure_text` stays faithful to the source.
    """
    match = pattern.search(text)
    return float(match.group(1).translate(_TO_ASCII_MINUS)) if match else None


def _require_list(payload: dict[str, Any], key: str) -> list[Any]:
    """Return a required list field, raising `ElnFormatError` if it is missing/empty."""
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ElnFormatError(f"entry missing non-empty {key!r}")
    return value


def _component(item: Any, default_role: Role) -> Component:
    """Build a `Component` from one JSON species (role defaults if unstated)."""
    if not isinstance(item, dict):
        # A bare string (["CCO"]) would raise AttributeError on .get and crash the sync instead of
        # rejecting one bad entry.
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


def _number(payload: dict[str, Any], key: str) -> float | None:
    """A recorded condition: the structured field, or `None` when the entry does not state one.

    Never a regex over the procedure, whose first match is typically the addition temperature or
    time rather than the reaction's; a missing number is a smaller harm than a wrong one stored as
    fact. Prose numbers are kept on each `ReactionStep`. A structured `0` is real, so the check is
    `is not None`.
    """
    value = payload.get(key)
    return float(value) if value is not None else None


def _product_number(payload: dict[str, Any], field: str) -> float | None:
    """Take a numeric outcome field from the first product (per-product in this ELN).

    Yield and purity share this path so they fail the same way. A non-object product item is a
    mapping error, not an `AttributeError`.
    """
    first = _require_list(payload, "products")[0]
    if not isinstance(first, dict):
        raise ElnFormatError(f"product is not an object: {first!r}")
    value = first.get(field)
    return float(value) if value is not None else None


def _outcome_class(payload: dict[str, Any]) -> OutcomeClass | None:
    """Read the entry's outcome, or `None` when the entry does not state one.

    Silence stays silence rather than success; see `OrdReaction.outcome_class` for why that differs
    from INCONCLUSIVE.
    """
    raw = payload.get("outcome")
    if raw is None:
        return None
    try:
        return OutcomeClass(str(raw).strip().lower())
    except ValueError as exc:
        raise ElnFormatError(f"unknown outcome {raw!r}") from exc


def _impurities(payload: dict[str, Any]) -> list[Impurity]:
    """Map the first product's impurity profile, skipping entries that identify nothing.

    A row with no name, structure or positive RRT is dropped rather than rejected, so one unusable
    row does not cost the reaction.
    """
    first = _require_list(payload, "products")[0]
    if not isinstance(first, dict):
        raise ElnFormatError(f"product is not an object: {first!r}")
    rows = first.get("impurities") or []
    if not isinstance(rows, list):
        raise ElnFormatError(f"impurities is not a list: {rows!r}")
    profile: list[Impurity] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ElnFormatError(f"impurity is not an object: {row!r}")
        name, smiles = row.get("name"), row.get("smiles")
        area = row.get("area_percent")
        # RRT is how a chemist names an unresolved peak (`Impurity.rrt`), so it is read like area%.
        rrt = float(row["rrt"]) if row.get("rrt") is not None else None
        # An RRT-only row is identified by where it eluted, so it is named (as
        # `Impurity._identifiable` prescribes) rather than dropped; the drop test therefore runs
        # after the RRT is read.
        if not name and not smiles and rrt is not None and rrt > 0.0:
            name = unresolved_peak_name(rrt)
        if not name and not smiles:
            # No name, structure or positive RRT: a blank row rather than a peak. Dropped, not
            # rejected.
            logger.warning("skipped an impurity row with neither name nor smiles: %r", row)
            continue
        profile.append(
            Impurity(
                name=name,
                smiles=smiles,
                area_percent=float(area) if area is not None else None,
                rrt=rrt,
            )
        )
    return profile


def _provenance(payload: dict[str, Any], raw: RawEntry) -> str:
    """Where this record came from: the source system, its entry id, and who ran it.

    Naming the source makes colliding entry ids from two systems visible and answers an auditor's
    first question. `eln-json` is the format, which is all a file-drop adapter can honestly claim; a
    connector to a real ELN should name its instance.
    """
    operator = payload.get("operator") or "unknown"
    return f"eln-json:{raw.entry_id}:{operator}"


def _optional_timestamp(value: Any, path: Path) -> datetime | None:
    """Parse an optional amendment timestamp; `None` when absent, `ElnFormatError` when malformed.

    Absent means "not reported", not "never amended". A present but unparseable value is raised
    rather than treated as absent.
    """
    return None if value is None else _parse_timestamp(value, path)


def _parse_timestamp(value: Any, path: Path) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z'), else `ElnFormatError`.

    A naive timestamp is read as UTC, so it compares with the sync's aware cursor.
    """
    if not isinstance(value, str):
        raise ElnFormatError(f"{path.name}: missing 'timestamp'")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        raise ElnFormatError(f"{path.name}: bad timestamp {value!r}: {exc}") from exc
