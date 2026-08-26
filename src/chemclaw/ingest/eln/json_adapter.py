"""A concrete adapter for a JSON-exporting ELN (plan step 4.3).

One real adapter, not a universal abstraction: many ELNs export each experiment as a JSON
file, so this reads `*.json` from a directory (`settings.eln_export_dir`), one file per
entry. It shows both mapping paths the plan calls for (step 4.4): **structured** fields map
deterministically, and headline conditions missing from the structured fields are recovered
from the **free-text** procedure by deterministic regex (temperature, time). Genuinely
unstructured cases the regex cannot resolve are escalated to the `eln-reaction-extraction`
skill (per-field LLM), which is judgment and lives outside this deterministic adapter.

A detailed development recipe is more than its headline conditions, so the free-text
procedure is also **segmented into ordered steps** (`OrdReaction.steps`) and preserved
verbatim (`procedure_text`). Segmentation is deterministic and lossless: it splits the
prose on numbered markers or sentence boundaries, keeps each segment's exact text, and
labels it with a coarse `StepKind` plus any per-step temperature/time the regex finds.
Linking a SMILES to a step from prose alone would be a guess, so free-text steps carry no
`components` — that (like any genuinely unstructured field) is the LLM skill's job.

Expected entry shape (this ELN's format — known only here):
    {"id": "...", "timestamp": "ISO-8601",
     "reactants": [{"smiles": "...", "role": "reactant", "mass_mg": 460}, ...],
     "products":  [{"smiles": "...", "yield_percent": 85}, ...],
     "procedure": "free text", "operator": "..."}
"""

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
    entry_window,
    is_late_arrival,
    parse_iso_utc,
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
)

logger = logging.getLogger(__name__)

# Deterministic free-text extractors for the two conditions an ELN reliably states in prose.
# The temperature pattern *requires* the degree sign: "80 °C" is unambiguously a temperature,
# whereas a space-less/degree-less "13C" (as in "13C NMR") or "pH 7 C" is not — demanding `°`
# avoids fabricating a temperature from spectroscopy or label text. The lookbehind stops a
# `-` preceded by a digit/dot from being read as a minus sign: in a range like "60-80 °C"
# the dash is a separator, so the match is the upper bound 80, never a sign-flipped -80.
# Extracting the upper bound is the deliberate (documented) reading of a range; a genuine
# "-10 °C" still matches because nothing numeric precedes its sign.
# **Every dash a real procedure uses as a minus sign, not only the ASCII one.** A cryogenic
# temperature is typeset with U+2212 MINUS SIGN by ACS and RSC house style, and Word's autocorrect
# turns a typed hyphen into U+2013 EN DASH. The sign used to be `-?`, which matches U+002D alone, so
# the dash was simply not consumed and the number read bare: measured on this tree, seven of the
# eight dash characters that occur in practice silently dropped the sign, and `−78 °C` — a
# dry-ice/acetone lithiation, one of the most common cryogenic conditions there is — was ingested as
# `+78 °C`. A 156-degree error in the wrong direction, rendered into the proposed note as
# `temperature: 78.0 °C`, and entirely plausible to the reviewer at the PR-gate because the verbatim
# prose beside it still reads `−78`.
_MINUS_SIGNS = "-‐‑‒–—―−"

# The temperature pattern *requires* the degree sign: "80 °C" is unambiguously a temperature,
# whereas a space-less/degree-less "13C" (as in "13C NMR") or "pH 7 C" is not — demanding `°`
# avoids fabricating a temperature from spectroscopy or label text. The lookbehind stops a dash
# preceded by a digit/dot from being read as a minus sign: in a range like "60-80 °C" — or
# "60–80 °C" — the dash is a separator, so the match is the upper bound 80, never a flipped -80.
# Extracting the upper bound is the deliberate (documented) reading of a range; a genuine
# "-10 °C" still matches because nothing numeric precedes its sign.
_TEMPERATURE = re.compile(rf"(?<![\d.])([{_MINUS_SIGNS}]?\d+(?:\.\d+)?)\s*°\s*C\b")

# `str.translate` table mapping every one of them onto the ASCII hyphen-minus `float()` accepts.
_TO_ASCII_MINUS = str.maketrans(dict.fromkeys(_MINUS_SIGNS, "-"))
_TIME_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\b")

# Procedure segmentation. A numbered marker ("1.", "2)", "Step 3:") is the strongest signal
# of an author-intended step boundary; absent numbering, fall back to sentence boundaries.
# `\d+[.)]` needs whitespace after it so a decimal ("0.5 h") or amount ("2.0 g") is never a
# split point — only a genuine list marker is.
_STEP_MARKER = re.compile(r"(?:^|\s)(?:step\s*)?\d+[.)]\s+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.;])\s+")

# Coarse step labels, checked in this priority order. Distinctive terminal operations
# (purification, workup) win over the ubiquitous "add"; the verbatim text is always kept on
# the step, so a mislabel loses nothing. Substring match (not word) tolerates inflections
# ("crystallized", "washing"). Lowercased before matching.
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
        `chemclaw.kg.graph.load_notes`). Such a file cannot become a `RawEntry`, so it never reaches
        the sync report — instead it is logged at WARNING here, the one signal an admin gets
        that a specific export file was dropped.

        A file whose payload predates `since` but which *arrived* after it is a late arrival: it
        is filtered out here and on every later run, so it is collected and reported in one
        aggregated WARNING (`warn_late_arrivals`) instead of vanishing silently.
        """
        entries: list[RawEntry] = []
        late: list[str] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    logger.warning("skipping ELN export %s: not a JSON object", path.name)
                    continue
                created = _parse_timestamp(payload.get("timestamp"), path)
                # An in-place amendment keeps `timestamp` and moves this one, so filtering on
                # creation alone would never re-fetch a corrected entry.
                modified = _optional_timestamp(payload.get("modified"), path)
            except (OSError, json.JSONDecodeError, ElnFormatError) as exc:
                logger.warning("skipping unreadable ELN export %s: %s", path.name, exc)
                continue
            if entry_window(created, modified) >= since:
                entries.append(
                    RawEntry(
                        entry_id=str(payload.get("id") or path.stem),
                        created_at=created,
                        modified_at=modified,
                        payload=payload,
                    )
                )
            elif is_late_arrival(path, since):
                late.append(path.name)
        warn_late_arrivals(logger, "ELN JSON export", late)
        entries.sort(key=lambda e: e.created_at)
        return entries

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one JSON entry to a canonical `OrdReaction` (structured + free-text).

        Any mapping failure — a missing field, an unknown role, a schema violation
        (e.g. a reactant tagged as a product), or a field of the wrong shape (a nested
        object where a number belongs raises `TypeError` from `float`) — becomes an
        `ElnFormatError`, so the sync's reject-and-continue handler treats one bad entry
        as a rejection, not a crash (G4).
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
            temperature_c=_condition(payload, "temperature_c", _TEMPERATURE, procedure),
            time_h=_condition(payload, "time_h", _TIME_HOURS, procedure),
            yield_percent=_product_number(payload, "yield_percent"),
            purity_percent=_product_number(payload, "purity_percent"),
            impurities=_impurities(payload),
            # The entry's own timestamp is the date the experiment was run (gap KNW-1); it already
            # drives the sync cursor, it was simply never carried onto the record.
            performed_at=raw.created_at.date(),
            outcome_class=_outcome_class(payload),
            failure_reason=payload.get("failure_reason"),
            provenance=_provenance(payload, raw),
            project=payload.get("project"),
            # What the run was testing, when the entry records it (D-162). Read from the entry's
            # own field rather than guessed out of the procedure prose: a hypothesis extracted by
            # pattern-matching would be indistinguishable, downstream, from one the chemist wrote.
            hypothesis=payload.get("hypothesis"),
            steps=_segment_steps(procedure),
            procedure_text=procedure or None,
        )


def _segment_steps(procedure: str) -> list[ReactionStep]:
    """Split a free-text procedure into ordered, coarsely-labeled steps (lossless).

    Each returned step keeps its source segment verbatim and carries any temperature/time
    the regex can read from that segment; species are left unlinked (see the module
    docstring). An empty or whitespace-only procedure yields no steps.
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

    Typographic dashes are normalised to the ASCII hyphen-minus first: `_TEMPERATURE` now *matches*
    the whole minus family (see `_MINUS_SIGNS`), and `float("−78")` raises `ValueError` on every one
    of them but U+002D. Normalising here rather than in the pattern keeps the matched text faithful
    to the source prose, which is what the reviewer at the PR-gate compares against.
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
    return _search(pattern, text)


def _product_number(payload: dict[str, Any], field: str) -> float | None:
    """Take a numeric outcome field from the first product (per-product in this ELN).

    Generalized from the yield-only reader so purity rides the identical path (DRY): both are
    per-product outcome numbers and must fail the same way. `_build` already guarantees
    `products` is a non-empty list, but not that its items are objects — a bare string here must
    be a mapping error, not an AttributeError.
    """
    first = _require_list(payload, "products")[0]
    if not isinstance(first, dict):
        raise ElnFormatError(f"product is not an object: {first!r}")
    value = first.get(field)
    return float(value) if value is not None else None


def _outcome_class(payload: dict[str, Any]) -> OutcomeClass | None:
    """Read the entry's outcome, or `None` when the entry does not state one (gap KNW-3).

    Silence is passed through as silence rather than read as success: this export format has an
    `outcome` key or it does not, and an entry without one has told us nothing about how the run
    turned out. See `OrdReaction.outcome_class` for why that is not the same as INCONCLUSIVE.
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

    An impurity row with neither a name nor a structure records nothing an chemist could act on,
    so it is dropped rather than rejected: one unusable row must not cost the whole reaction, and
    the surrounding rows are still real data (the reject-and-continue discipline, applied within
    an entry).
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
        if not name and not smiles:
            logger.warning("skipped an impurity row with neither name nor smiles: %r", row)
            continue
        area = row.get("area_percent")
        profile.append(
            Impurity(
                name=name, smiles=smiles, area_percent=float(area) if area is not None else None
            )
        )
    return profile


def _provenance(payload: dict[str, Any], raw: RawEntry) -> str:
    """Where this record came from: the source system, its entry id, and who ran it.

    It used to be `eln:<operator>` — a person's name and nothing else. With two ELN sources
    enabled, two entries whose `entry_id`s collide produce the same note id `reaction-<id>` and the
    second silently loses to the already-merged check, with nothing in either record to say they
    came from different systems. Naming the source is what makes that visible, and it is also the
    first thing an auditor asks of a piece of evidence: which system, which record.

    `eln-json` is the *format* this adapter reads rather than an instance name, which is as much as
    a file-drop adapter can honestly claim — it is handed a directory, not a tenant. A connector
    that talks to a real ELN knows its instance and should say so here.
    """
    operator = payload.get("operator") or "unknown"
    return f"eln-json:{raw.entry_id}:{operator}"


def _optional_timestamp(value: Any, path: Path) -> datetime | None:
    """Parse an optional amendment timestamp; `None` when absent, `ElnFormatError` when malformed.

    Absent is the normal case and means "this source does not report amendments" — not "never
    amended". A *present but unparseable* value is bad data and is raised, because silently
    treating it as absent would reinstate the exact silence this field exists to break.
    """
    return None if value is None else _parse_timestamp(value, path)


def _parse_timestamp(value: Any, path: Path) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z'), else `ElnFormatError`.

    A naive timestamp (no UTC offset) is read as UTC: exports from tools that omit the
    offset are common, UTC is the least-surprising reading, and a naive datetime would
    later raise `TypeError` when compared against the sync's offset-aware cursor.
    """
    if not isinstance(value, str):
        raise ElnFormatError(f"{path.name}: missing 'timestamp'")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        raise ElnFormatError(f"{path.name}: bad timestamp {value!r}: {exc}") from exc
