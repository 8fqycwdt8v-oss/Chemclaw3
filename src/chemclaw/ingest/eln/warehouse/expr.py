"""Reading a value out of a warehouse row, and reshaping it: the only computation a binding does.

Two pure halves:

- A path names a value in the row bundle (`root.YIELD_PCT`, `analytics[0].PURITY_PCT`, or a bare
  column in a child-scoped binding). An unresolved path yields `None`: a NULL column, an absent
  child table and a dropped column all mean "the source is silent", and whether that is acceptable
  is the mapped field's question.
- A transform chain reshapes it: minutes to hours, `SM` to `reactant`, text to a number.

The vocabulary is closed, which is the security property: a binding is configuration, so transforms
come from one table of pure functions, an unknown name fails at load, and there is no `eval`, import
or format string here. Deliberately not JSONPath or an expression language: a binding maps columns
onto a fixed schema and needs only "one value, optionally reshaped". Mirrors
`chemclaw.templates.resolve`: a bare `path` yields the typed value, `${path}` in a template
interpolates its text.
"""

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from time import monotonic
from typing import Any

import regex

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import ElnMappingError, parse_iso_utc

# One path segment: a column or block name, optionally indexed. `$` (seen in generated views) is
# allowed except as the first character.
_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_$]*)(?:\[(\d+)\])?$")

# `${...}` in a provenance template. Non-greedy so two references on one line stay separate.
_REFERENCE = re.compile(r"\$\{([^}]+)\}")


class TransformError(ElnMappingError):
    """A transform could not be applied to the value the row actually held.

    An `ElnMappingError`, so the sync's reject-and-continue arm rejects just this row with its
    reason.
    """


class PathSyntaxError(ElnMappingError):
    """A binding declared a path that is not a path. Raised at validation time, not per row."""


class PatternBudgetError(Exception):
    """A `regex` transform's pattern spent its whole budget on one cell.

    Deliberately outside `ChemclawError`: the cost belongs to the pattern, not the data, so the
    sync's per-entry reject-and-continue handler must not swallow it and pay the budget again on
    every row. It reaches the activity boundary and fails loudly. Listed by name in
    `durable/publish._BAD_DATA_TYPES` (non-retryable), since a retry would run the same pattern over
    the same page.
    """


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

    The path is assumed well-formed; `validate_path` runs when the binding is loaded.
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

    `str()` for everything except dates and datetimes, which get ISO form (`str(datetime)` uses a
    space, not `T`), since provenance strings are parsed by other systems.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _number(value: Any, options: Mapping[str, Any]) -> Any:
    """Coerce to `float`. A blank string is silence, not a zero, and NaN is neither.

    A non-finite value (parsed `"NaN"`/`"Infinity"` or a stored Spark NaN) is refused as bad data,
    like a boolean: missingness arrives as `None`, so a NaN is the source saying something that is
    not a measurement. Refusing here makes it one rejected entry; reaching the `jsonb` column it
    would fail the whole ingest pass. No binding option admits it.
    """
    del options
    if value is None:
        return None
    if isinstance(value, bool):
        raise TransformError(f"'number' refuses a boolean ({value!r}) — it is not a measurement")
    if isinstance(value, int | float):
        return _finite(float(value), value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError as exc:
        raise TransformError(f"'number' cannot read {value!r} as a number") from exc
    return _finite(parsed, value)


def _finite(number: float, original: Any) -> float:
    """Return `number`, or raise naming what the row actually held.

    Quotes `original`, the column's own text, so a site can search its warehouse for it.
    """
    if not math.isfinite(number):
        raise TransformError(f"'number' refuses {original!r} — it is not a measurement")
    return number


def _scale(value: Any, options: Mapping[str, Any]) -> Any:
    """Multiply by a constant — the unit conversion a site's own units need (g→mg, min→h)."""
    if value is None:
        return None
    number = _number(value, {})
    if number is None:
        return None
    return float(number) * float(options["factor"])


def _value_map(value: Any, options: Mapping[str, Any]) -> Any:
    """Translate the site's vocabulary into this schema's (`SM` -> `reactant`).

    An unmapped value raises unless the binding declared a `default`, so a vocabulary the site
    extended is not ingested with a field silently missing. Both sides are compared as text, because
    YAML turns numeric map keys into integers.
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


# Largest literal repeat count allowed, checked before compiling. `regex` expands bounded repeats at
# compile time (`a{1000000}` takes hundreds of MB), and compilation runs at binding load on manifest
# text, outside every match timeout and activity deadline. A realistic binding bounds a repeat at a
# field width, orders of magnitude below this.
_MAX_REPEAT_COUNT = 10_000

# Bound on the whole pattern's expansion, summed over siblings. Looser than one repeat's bound so a
# single maximal repeat plus literals still passes; it stops many legal repeats side by side.
_MAX_EXPANDED_ATOMS = 10 * _MAX_REPEAT_COUNT

# One `{n}` or `{n,m}` quantifier. Anchored on a `{` the scan below has already established is
# neither escaped nor inside a character class, so this never has to decide that itself.
_REPEAT_BOUND = re.compile(r"\{(\d*)(?:,(\d*))?\}")

# The flag run of an inline flag group — `(?x)`, `(?i-s:` — read from just after its `(?`. Only
# the positions `regex` reads as flags: `(?P<`, `(?:`, `(?=` and the rest fail the terminator.
_INLINE_FLAGS = re.compile(r"([A-Za-z0-9-]*)[):]")


# What follows `(?` when it opens a group whose body starts after it: a named group, a lookaround,
# an atomic group or a branch reset. Matched as syntax so it adds no atom weight.
_GROUP_PREFIX = re.compile(r"P?<(?![=!])[^>]*>|<[=!]|[:=!>|]")

# What follows `(?` when the whole parenthesis is one atom rather than a group: a backreference
# or call by name (`P=n`, `P>n`, `&n`) or a recursion (`R`, `1`, `+1`, `-1`).
_GROUP_ATOM = re.compile(r"(?:P[=>]|&)[^)]*\)|R\)|[+-]?\d+\)")


def _refuse_past_the_expansion_bound(pattern: str, expanded: int, bound: int) -> None:
    """Raise `PathSyntaxError` if `expanded` atoms is more than `regex` should build at load."""
    if expanded > bound:
        raise PathSyntaxError(
            f"transform 'regex' expands to {expanded} atoms in {pattern!r}, over the "
            f"{bound} this engine will expand. A bounded repeat is expanded at "
            "compile time, and nested repeats multiply, so a count this size is memory rather "
            "than a pattern — write the repeat unbounded (`+`, `*`) or bound it at the width of "
            "the field being read"
        )


def _refuse_an_unbounded_expansion(pattern: str) -> None:
    r"""Raise `PathSyntaxError` if `pattern` names a repeat `regex` would expand into the heap.

    Scanned, because compiling is what is being guarded and `re` cannot parse all `regex` syntax.
    Nested repeats expand multiplicatively, so the walk keeps a running total per open group: an
    atom adds its weight, `{n,m}` multiplies the preceding atom or group by `max(n, m)`, and `)`
    hands the group's total up as one atom. One repeat's product is held to `_MAX_REPEAT_COUNT` and
    the whole pattern's sum to `_MAX_EXPANDED_ATOMS`; alternation branches are summed (a safe
    over-estimate). Group prefixes and inline flags weigh nothing; backreferences weigh one.

    Escapes, character classes (where `{` is literal) and `(?#...)` comments are tracked so a `{` or
    `[` is read correctly. Verbose mode is refused outright, since `#` comments and insignificant
    whitespace would hide quantifiers from this scan.
    """
    totals = [0]  # the expanded size of each open group, outermost first
    last = 0  # the weight of the atom a following quantifier would repeat
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            if not in_class:
                last = 1
                totals[-1] += last
            continue
        if in_class:
            in_class = char != "]"
            index += 1
            continue
        if char == "[":
            in_class = True
            last = 1
            totals[-1] += last
            index += 1
            continue
        if char == "(":
            if pattern.startswith("(?#", index):
                close = pattern.find(")", index)
                index = len(pattern) if close < 0 else close + 1
                continue
            flags = (
                _INLINE_FLAGS.match(pattern, index + 2) if pattern.startswith("(?", index) else None
            )
            # Only a flag *set*: `(?-x:` turns verbose mode off, which hides nothing.
            if flags is not None and "x" in flags.group(1).partition("-")[0]:
                raise PathSyntaxError(
                    f"transform 'regex' turns on verbose mode in {pattern!r}. Under `x` a `#` "
                    "comments out the rest of the line, which hides what the pattern would "
                    "expand to from the check that bounds it — write the pattern without `x`"
                )
            if pattern.startswith("(?", index):
                atom = _GROUP_ATOM.match(pattern, index + 2)
                if atom is not None:
                    last = 1
                    totals[-1] += last
                    index = atom.end()
                    continue
                if flags is not None and flags.group(0).endswith(")"):
                    index = flags.end()  # `(?i)`: a flag set, which opens nothing
                    continue
                prefix = _GROUP_PREFIX.match(pattern, index + 2) or flags
                index = prefix.end() if prefix is not None else index + 1
            else:
                index += 1
            totals.append(0)
            continue
        if char == ")":
            last = totals.pop() if len(totals) > 1 else 0
            totals[-1] += last
            _refuse_past_the_expansion_bound(pattern, totals[-1], _MAX_EXPANDED_ATOMS)
            index += 1
            continue
        if char == "{":
            bound = _REPEAT_BOUND.match(pattern, index)
            # A `{` that does not open a quantifier is a literal in both engines. `{2,}` is
            # unbounded and costs nothing to compile.
            if bound is not None:
                counts = [int(part) for part in bound.groups() if part]
                if counts:
                    repeated = last * max(counts)
                    # `{0,1}` and `{1}` multiply nothing; the group's total was already checked at
                    # its `)`.
                    if max(counts) > 1:
                        _refuse_past_the_expansion_bound(pattern, repeated, _MAX_REPEAT_COUNT)
                    totals[-1] += repeated - last
                    last = repeated
                    _refuse_past_the_expansion_bound(pattern, totals[-1], _MAX_EXPANDED_ATOMS)
                index = bound.end()
                continue
        if char not in "*+?|":
            last = 1
            totals[-1] += last
        index += 1


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> regex.Pattern[str]:
    """One site-supplied pattern, compiled once by the engine that will run it.

    Cached because options arrive as parsed YAML, so the pattern string arrives on every cell; keys
    are manifest text, so rows cannot grow the cache. The expansion guard runs inside the cache, so
    every route to a compiled pattern passes it.
    """
    _refuse_an_unbounded_expansion(pattern)
    return regex.compile(pattern)


@dataclass
class _PageBudget:
    """Matching time this page has left, and what it spent: an accumulator, not a wall clock.

    Only the time `regex` itself is given is charged, per search. A deadline would also bill the
    store writes and source fetches inside the page loop, and since `PatternBudgetError` is
    non-retryable, would permanently fail pages whose patterns cost microseconds. `searches` counts
    applications of the transform, not cells (a value may run several steps, a NULL runs none).
    """

    #: The budget this page was opened with, so a refusal quotes the number actually in force.
    budget: float
    spent: float = 0.0
    searches: int = 0

    def remaining(self) -> float:
        """Matching seconds left, negative once a clamped search has overrun slightly."""
        return self.budget - self.spent

    def charge(self, seconds: float) -> None:
        """Bill one search, whether it matched, missed or timed out.

        A timeout spent the allowance too; not billing it would let a page of timeouts run
        unbounded.
        """
        self.spent += seconds
        self.searches += 1


#: The budget for the page in flight, or `None` where no page has opened one. A contextvar because
#: `sync_entries` opens the page and `_regex` spends it several per-cell frames down. An activity
#: runs the walk synchronously, so pages in one worker do not interleave.
_page_budget: ContextVar[_PageBudget | None] = ContextVar("eln_regex_page_budget", default=None)


@contextmanager
def pattern_budget(seconds: float | None = None) -> Iterator[None]:
    """Bound what every `regex` transform together may spend on one page of entries.

    `eln_regex_timeout_seconds` bounds one search, but a page runs one per field, attribute and
    child row of every entry, so the per-cell ceiling multiplies. A pattern that exceeds the
    per-cell budget is already refused; the case this bounds is a polynomially slow pattern that
    completes under it on many cells, which could otherwise run past the activity deadline as
    uninterruptible CPU work. An honest page uses a tiny fraction of the default budget, so the page
    bound costs honest bindings nothing and yields a refusal naming its cause instead of a killed
    activity.

    Re-entrant: a nested call keeps the outer budget.

    Args:
        seconds: The budget, defaulting to `eln_regex_page_budget_seconds`; passed explicitly only
            by tests.
    """
    if _page_budget.get() is not None:
        yield
        return
    budget = settings.eln_regex_page_budget_seconds if seconds is None else seconds
    token = _page_budget.set(_PageBudget(budget=budget))
    try:
        yield
    finally:
        _page_budget.reset(token)


def _cell_budget() -> tuple[float, bool]:
    """How long the next `search` may run, and whether the page's budget is what bounds it.

    Clamped to what the page has left, so the last cell cannot overshoot the page bound.

    Returns:
        The seconds to pass `regex` as `timeout`, and True when the page budget is the binding one
        (so a timeout is the page's fault rather than this cell's, and can say so).
    """
    cell = settings.eln_regex_timeout_seconds
    page = _page_budget.get()
    if page is None:
        return cell, False
    remaining = page.remaining()
    if remaining <= 0.0:
        return 0.0, True
    return min(cell, remaining), remaining < cell


def _charge(seconds: float) -> None:
    """Bill one search against the page in flight, where a page opened a budget at all."""
    page = _page_budget.get()
    if page is not None:
        page.charge(seconds)


def _page_refusal(pattern: str, *, cut_short: float | None = None) -> str:
    """What a page that spent its whole matching budget says, naming how far it got.

    The search count separates the two causes: many searches means the binding is too expensive for
    this page size, a few means one pattern is pathological.

    Args:
        pattern: The pattern the budget ran out on, for a site to look at first.
        cut_short: The clamped allowance this search was given, when the budget ran out inside it;
            the message then must not claim the pattern alone is at fault or innocent.
    """
    page = _page_budget.get()
    searches = page.searches if page is not None else 0
    budget = page.budget if page is not None else settings.eln_regex_page_budget_seconds
    remedy = (
        "reduce CHEMCLAW_ELN_SYNC_BATCH_SIZE, simplify the patterns this binding runs per row, or "
        "raise CHEMCLAW_ELN_REGEX_PAGE_BUDGET_SECONDS — which must stay inside "
        "CHEMCLAW_ELN_SYNC_TIMEOUT_SECONDS to be worth anything"
    )
    if cut_short is not None:
        return (
            f"the 'regex' transforms on this page spent their whole {budget}s matching budget, and "
            f"it ran out inside {pattern!r} — which was given only the {cut_short:.3f}s the page "
            "had left rather than its full CHEMCLAW_ELN_REGEX_TIMEOUT_SECONDS, so whether that "
            f"pattern alone is too expensive is not established here. {searches} transform(s) ran. "
            f"Either way this page is over its matching budget: {remedy}"
        )
    return (
        f"the 'regex' transforms on this page spent their whole {budget}s matching budget after "
        f"{searches} transform(s), the last of them {pattern!r}. No single one exceeded "
        "CHEMCLAW_ELN_REGEX_TIMEOUT_SECONDS, so this is the page's total matching cost rather than "
        f"one bad pattern: {remedy}"
    )


def _regex(value: Any, options: Mapping[str, Any]) -> Any:
    """Pull one group out of a free-text column. No match is silence, not an error.

    Run under a timeout because the pattern comes from a site's manifest and the subject is a
    free-text column, so a backtracking pattern is unbounded work. `regex` checks a deadline inside
    its matching loop, which `re` cannot.
    """
    if value is None:
        return None
    text = as_text(value)
    pattern = str(options["pattern"])
    budget, page_bound = _cell_budget()
    if budget <= 0.0:
        raise PatternBudgetError(_page_refusal(pattern))
    started = monotonic()
    try:
        match = _compiled(pattern).search(text, timeout=budget)
    except TimeoutError as exc:
        _charge(monotonic() - started)
        # Three outcomes. Clamped: the search never had its full per-cell allowance, so the
        # pattern's own cost is not established and the message says so. Unclamped: it had the whole
        # per-cell budget and blew it, which names a pattern to rewrite.
        if page_bound:
            raise PatternBudgetError(_page_refusal(pattern, cut_short=budget)) from exc
        raise PatternBudgetError(
            f"the 'regex' transform {pattern!r} did not finish within "
            f"{settings.eln_regex_timeout_seconds}s on one {len(text)}-character cell, so it "
            "cannot be run over this source at all. Rewrite the pattern — a nested unbounded "
            "quantifier such as `(a+)+` is the usual cause — or raise "
            "CHEMCLAW_ELN_REGEX_TIMEOUT_SECONDS if the pattern is genuinely this expensive"
        ) from exc
    _charge(monotonic() - started)
    if match is None:
        return None
    group = int(options.get("group", 0))
    try:
        return match.group(group)
    except (IndexError, regex.error) as exc:
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

    For site conventions outside this schema's bounds (e.g. a rounded 101.3% yield). An explicit
    binding decision, never a default. NaN never reaches here (`_number` refuses it), since
    `max(nan, x)` would return a value outside every range.
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

    Called at binding load, so a manifest typo fails at worker startup, not on some later row.
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

    Both failures would otherwise surface only when a row reaches them, possibly days later.
    Compiled by `_compiled`, so the engine that accepts the pattern is the one that runs it, and the
    cache is warmed.
    """
    try:
        compiled = _compiled(str(options["pattern"]))
    except regex.error as exc:
        raise PathSyntaxError(f"transform 'regex' has an invalid pattern: {exc}") from exc
    except RecursionError as exc:
        # `regex` recurses where `re` iterates, so deeply nested groups raise here. Caught by name:
        # anything else is a defect in this module, not the site's pattern.
        raise PathSyntaxError(
            f"transform 'regex' nests groups too deeply for this engine to compile: {exc}"
        ) from exc
    group = int(options.get("group", 0))
    if group > compiled.groups:
        raise PathSyntaxError(
            f"transform 'regex' asks for group {group} but {options['pattern']!r} has "
            f"{compiled.groups}"
        )


def _check_map_keys(table: Any) -> None:
    """Reject a `value_map` whose keys YAML turned into booleans, naming the fix.

    Integer keys are fine (compared as text), but `ON`/`OFF`/`YES`/`NO`/`Y`/`N` become
    `True`/`False` with the spelling lost, and `True` collides with `1` as a dict key. Neither is
    recoverable, so the binding is refused at load with advice to quote the key.
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

    An unresolved reference renders as the empty string, unlike `chemclaw.templates.resolve`: this
    builds a provenance string, where a missing part should shorten the citation rather than reject
    an otherwise complete reaction. The mapper refuses a provenance that renders empty.
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
