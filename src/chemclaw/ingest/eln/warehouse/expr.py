"""Reading a value out of a warehouse row, and reshaping it — the only computation a binding does.

Two halves, both pure:

- **A path** names a value in the row bundle: `root.YIELD_PCT`, `analytics[0].PURITY_PCT`, or a bare
  column when the binding is already scoped to a child row. A path that does not resolve yields
  `None`, never an error — a NULL column, an absent optional child table and a view that dropped a
  column are the same thing to a binding, and all three mean "the source is silent here". Whether
  silence is acceptable is the *field's* question, answered where the field is mapped.
- **A transform chain** reshapes it: minutes to hours, `SM` to `reactant`, a string to a number.

**The vocabulary is closed, and that is the security property.** A binding is a configuration file;
if a transform name could reach arbitrary code, every deployment that mounts a manifest directory
would be mounting an execution surface. So transforms are looked up in one table of pure functions,
an unknown name fails validation rather than run time, and there is no `eval`, no `import`, and no
format string anywhere in this module. The one import a binding may name is its driver, which is
the same trust boundary the data-source seam already takes for `ingest:`/`retrieve:` themselves.

**Why not JSONPath or a small expression language.** Both were the obvious reach, and both buy
generality this problem does not have: a binding maps columns onto a fixed schema, so every
expression it needs is "one value, optionally reshaped". A filter or a projection language would let
a binding compute things the mapper has no field to receive. `chemclaw.templates.resolve` made the
same call for the same reason, and this file deliberately mirrors its two substitution modes — a
bare `path` yields the *value* with its type; `${path}` inside a template interpolates its text.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from chemclaw.ingest.eln.adapter import ElnMappingError, parse_iso_utc

# One path segment: a column or block name, optionally indexed. `$` is legal in a warehouse
# identifier and shows up in generated views, so it is allowed in a name but never as its first
# character.
_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_$]*)(?:\[(\d+)\])?$")

# `${...}` in a provenance template. Non-greedy so two references on one line stay separate.
_REFERENCE = re.compile(r"\$\{([^}]+)\}")


class TransformError(ElnMappingError):
    """A transform could not be applied to the value the row actually held.

    An `ElnMappingError` by inheritance rather than by wrapping, so it lands in the reject-and-
    continue arm of `chemclaw.ingest.eln.sync` with no adapter-side translation: one bad row is
    rejected with its reason and the batch keeps going, which is the behaviour every other adapter
    already gets from raising that type.
    """


class PathSyntaxError(ElnMappingError):
    """A binding declared a path that is not a path. Raised at validation time, not per row."""


def validate_path(path: str) -> None:
    """Raise `PathSyntaxError` unless `path` is a well-formed dotted, optionally-indexed path."""
    if not path or not path.strip():
        raise PathSyntaxError("a path may not be empty")
    for segment in path.split("."):
        if not _SEGMENT.match(segment):
            raise PathSyntaxError(
                f"{path!r} is not a valid path: the segment {segment!r} must be a name, "
                "optionally followed by a numeric index like 'analytics[0]'"
            )


def resolve_path(path: str, scope: Mapping[str, Any]) -> Any:
    """Read the value `path` names out of `scope`, or `None` if anything along the way is absent.

    Absence is deliberately not an error here; see the module docstring. The path is assumed
    well-formed — `validate_path` runs once when the binding is loaded, so this stays a walk.
    """
    current: Any = scope
    for segment in path.split("."):
        match = _SEGMENT.match(segment)
        if match is None:  # pragma: no cover - validate_path has already rejected this
            raise PathSyntaxError(f"{path!r} is not a valid path")
        name, index = match.group(1), match.group(2)
        if not isinstance(current, Mapping) or name not in current:
            return None
        current = current[name]
        if index is not None:
            if not isinstance(current, Sequence) or isinstance(current, str | bytes):
                return None
            position = int(index)
            if position >= len(current):
                return None
            current = current[position]
    return current


def as_text(value: Any) -> str:
    """Render a value for a template or an attribute bag, without inventing a format.

    `str()` for everything except dates, which get their ISO form: a `datetime.date` renders as
    `2026-08-04` either way, but a `datetime` renders with a space instead of a `T` under `str`,
    which would put a non-ISO timestamp into a provenance string that other systems parse.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _number(value: Any, options: Mapping[str, Any]) -> Any:
    """Coerce to `float`. A blank string is silence, not a zero."""
    del options
    if value is None:
        return None
    if isinstance(value, bool):
        raise TransformError(f"'number' refuses a boolean ({value!r}) — it is not a measurement")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise TransformError(f"'number' cannot read {value!r} as a number") from exc


def _scale(value: Any, options: Mapping[str, Any]) -> Any:
    """Multiply by a constant — the unit conversion a site's own units need (g→mg, min→h)."""
    if value is None:
        return None
    number = _number(value, {})
    if number is None:
        return None
    return float(number) * float(options["factor"])


def _value_map(value: Any, options: Mapping[str, Any]) -> Any:
    """Translate the site's vocabulary into this schema's (`SM` → `reactant`).

    An unmapped value raises unless the binding declared a `default`. Silently yielding `None`
    would turn a vocabulary the site extended — a new material type, a new status code — into
    rows that ingest with a field quietly missing, which is the failure mode a mapping layer
    exists to prevent. `default:` is how a binding says "and everything else is this".

    **Both sides are compared as text, and that is not cosmetic.** A transform's options are
    untyped (`transform: list[dict[str, Any]]`), so YAML's own scalar rules decide what a map key
    becomes: a site with numeric material-type codes writes `map: {1: reactant, 2: solvent}` and
    gets *integer* keys. Comparing the row's text against those matched nothing, so every row was
    rejected — with `no entry for '1'; known: [1, 2]`, a message showing the key apparently
    present. Stringifying both sides is what makes a numeric vocabulary work at all.
    """
    if value is None:
        return None
    table = {as_text(name): mapped for name, mapped in options["map"].items()}
    key = as_text(value).strip()
    if key in table:
        return table[key]
    if "default" in options:
        return options["default"]
    raise TransformError(
        f"'value_map' has no entry for {key!r} and no default; known: {sorted(table)}"
    )


def _iso_date(value: Any, options: Mapping[str, Any]) -> Any:
    """Read a calendar date, from a date, a timestamp, or an ISO string."""
    del options
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = as_text(value).strip()
    if not text:
        return None
    try:
        return parse_iso_utc(text).date()
    except (ValueError, TypeError) as exc:
        raise TransformError(f"'iso_date' cannot read {value!r} as a date") from exc


def _iso_datetime(value: Any, options: Mapping[str, Any]) -> Any:
    """Read an instant, normalised to UTC — a naive timestamp is read as UTC, as everywhere else."""
    del options
    if value is None:
        return None
    if isinstance(value, datetime):
        return parse_iso_utc(value.isoformat())
    text = as_text(value).strip()
    if not text:
        return None
    try:
        return parse_iso_utc(text)
    except (ValueError, TypeError) as exc:
        raise TransformError(f"'iso_datetime' cannot read {value!r} as a timestamp") from exc


def _regex(value: Any, options: Mapping[str, Any]) -> Any:
    """Pull one group out of a free-text column. No match is silence, not an error."""
    if value is None:
        return None
    match = re.search(str(options["pattern"]), as_text(value))
    if match is None:
        return None
    group = int(options.get("group", 0))
    try:
        return match.group(group)
    except (IndexError, re.error) as exc:
        raise TransformError(f"'regex' has no group {group} in {options['pattern']!r}") from exc


def _strip(value: Any, options: Mapping[str, Any]) -> Any:
    """Trim surrounding whitespace, and read an all-whitespace column as silence."""
    del options
    if value is None:
        return None
    return as_text(value).strip() or None


def _upper(value: Any, options: Mapping[str, Any]) -> Any:
    """Upper-case, for a vocabulary the site records inconsistently."""
    del options
    return None if value is None else as_text(value).upper()


def _lower(value: Any, options: Mapping[str, Any]) -> Any:
    """Lower-case, for a vocabulary the site records inconsistently."""
    del options
    return None if value is None else as_text(value).lower()


def _default(value: Any, options: Mapping[str, Any]) -> Any:
    """Substitute a constant for silence. The one transform that acts *on* `None`."""
    return options["value"] if value is None else value


def _clamp(value: Any, options: Mapping[str, Any]) -> Any:
    """Hold a number inside a range.

    For the columns whose site convention differs from this schema's bounds — a yield recorded as
    101.3% after rounding, which `OrdReaction` would reject outright. Clamping is a binding-author's
    explicit decision to keep such a row rather than lose it, never a default.
    """
    number = _number(value, {})
    if number is None:
        return None
    if "min" in options:
        number = max(number, float(options["min"]))
    if "max" in options:
        number = min(number, float(options["max"]))
    return number


@dataclass(frozen=True)
class _Transform:
    """One entry in the closed vocabulary: what it does, and what options it accepts."""

    apply: Callable[[Any, Mapping[str, Any]], Any]
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = field(default_factory=frozenset)


TRANSFORMS: dict[str, _Transform] = {
    "number": _Transform(_number),
    "scale": _Transform(_scale, required=frozenset({"factor"})),
    "value_map": _Transform(_value_map, frozenset({"map"}), frozenset({"default"})),
    "iso_date": _Transform(_iso_date),
    "iso_datetime": _Transform(_iso_datetime),
    "regex": _Transform(_regex, frozenset({"pattern"}), frozenset({"group"})),
    "strip": _Transform(_strip),
    "upper": _Transform(_upper),
    "lower": _Transform(_lower),
    "default": _Transform(_default, required=frozenset({"value"})),
    "clamp": _Transform(_clamp, optional=frozenset({"min", "max"})),
}


def validate_transform(step: Mapping[str, Any]) -> None:
    """Raise unless `step` is one known transform with option keys it accepts.

    Called when the binding is loaded, so a typo in a manifest fails at worker startup naming the
    offending transform, rather than on whichever row first reaches it.
    """
    if len(step) != 1:
        raise PathSyntaxError(
            f"each transform is a single-key mapping like {{scale: {{factor: 1000}}}}; got {step!r}"
        )
    ((name, options),) = step.items()
    transform = TRANSFORMS.get(name)
    if transform is None:
        raise PathSyntaxError(f"unknown transform {name!r}; known: {sorted(TRANSFORMS)}")
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise PathSyntaxError(f"transform {name!r} takes a mapping of options, got {options!r}")
    missing = sorted(transform.required - set(options))
    if missing:
        raise PathSyntaxError(f"transform {name!r} needs {missing}")
    unknown = sorted(set(options) - transform.required - transform.optional)
    if unknown:
        raise PathSyntaxError(
            f"transform {name!r} does not accept {unknown}; "
            f"it takes {sorted(transform.required | transform.optional)}"
        )
    if name == "clamp" and not set(options):
        raise PathSyntaxError("transform 'clamp' needs at least one of 'min' or 'max'")
    if name == "value_map":
        _check_map_keys(options["map"])
    if name == "regex":
        _check_pattern(options)


def _check_pattern(options: Mapping[str, Any]) -> None:
    """Compile a `regex` transform's pattern at load, and check the group it asks for exists.

    Both failures are otherwise invisible until a row reaches them: an unbalanced bracket raises
    `re.error` on the first row of the first sync, and a `group:` the pattern does not have raises
    on the first row that *matches* — which may be days later and on a subset of the corpus.
    """
    try:
        compiled = re.compile(str(options["pattern"]))
    except re.error as exc:
        raise PathSyntaxError(f"transform 'regex' has an invalid pattern: {exc}") from exc
    group = int(options.get("group", 0))
    if group > compiled.groups:
        raise PathSyntaxError(
            f"transform 'regex' asks for group {group} but {options['pattern']!r} has "
            f"{compiled.groups}"
        )


def _check_map_keys(table: Any) -> None:
    """Reject a `value_map` whose keys YAML turned into booleans, naming the fix.

    `_value_map` compares as text, so an integer key is fine — `1` and `"1"` agree. A *boolean* one
    does not: `ON`, `OFF`, `YES`, `NO`, `Y` and `N` are all YAML booleans, so a site whose status
    flags use any of them arrives here as `True`/`False` with the original spelling already gone.
    Worse, `True` and `1` are the same dict key in Python, so a map carrying both silently loses one
    entry before this code ever sees it.

    Neither is recoverable, so this refuses at load and says which line to quote, rather than
    letting every row fail later against a vocabulary that reads correctly in the file.
    """
    if not isinstance(table, Mapping):
        raise PathSyntaxError(f"transform 'value_map' needs a mapping for 'map', got {table!r}")
    booleans = [name for name in table if isinstance(name, bool)]
    if booleans:
        raise PathSyntaxError(
            f"transform 'value_map' has boolean key(s) {booleans} — YAML reads ON/OFF/YES/NO/Y/N "
            'as booleans, losing the spelling the source actually uses. Quote them: "Y": ...'
        )


def apply_transforms(value: Any, chain: Sequence[Mapping[str, Any]]) -> Any:
    """Run a validated chain left to right, each step receiving what the last one produced."""
    for step in chain:
        ((name, options),) = step.items()
        value = TRANSFORMS[name].apply(value, options or {})
    return value


def render_template(template: str, scope: Mapping[str, Any]) -> str:
    """Interpolate `${path}` references in `template` against `scope`.

    An unresolved reference renders as the empty string rather than raising, which is the opposite
    of `chemclaw.templates.resolve` and deliberate: that module feeds arguments into calculations,
    where a silent `None` becomes a confident wrong answer. This one builds a provenance string,
    where a missing operator name should degrade to a slightly shorter citation rather than reject a
    reaction that is otherwise complete. The mapper separately refuses a provenance that renders
    to nothing at all.
    """

    def _render(match: re.Match[str]) -> str:
        # An explicit `is None` rather than `or ""`: a reaction id of `0`, or any other falsy value
        # the source legitimately recorded, is a value and must render as one.
        value = resolve_path(match.group(1).strip(), scope)
        return "" if value is None else as_text(value)

    return _REFERENCE.sub(_render, template)


def template_paths(template: str) -> list[str]:
    """Every `${path}` a template references, so the binding can validate them up front."""
    return [reference.strip() for reference in _REFERENCE.findall(template)]
