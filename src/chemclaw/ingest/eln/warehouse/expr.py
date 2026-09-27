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


class PatternBudgetError(Exception):
    """A `regex` transform's pattern spent its whole budget on one cell.

    **Outside `ChemclawError` deliberately, and the first spelling of this class got that wrong in
    a way that made it a rename of the failure rather than a fix.** It descended from
    `ChemclawError` and its docstring claimed to escape the per-entry handler because it was not an
    `ElnMappingError` — but the handler a transform actually runs under is
    `ingest/eln/sync.py`'s `except (ChemclawError, ValidationError)`, one layer further out than
    the `ElnMappingError` arm in `src/chemclaw/ingest/eln/warehouse/adapter.py` that the claim was
    checked against. Driven
    on the real `sync_entries` with a `(a+)+$` transform over ten entries at a 0.05 s budget:
    nothing escaped, all ten were booked as data refusals, and the page cost 0.503 s — `rows x
    budget`, which is the exact outcome this class exists to prevent.

    `SubsystemUnavailableError` is the precedent and the argument is the same one: this is not bad
    *data*. The cost belongs to the **pattern**, so every remaining row of every remaining page
    would pay it again, and a reject-and-continue handler is the wrong reader for it. Outside the
    hierarchy it reaches the activity boundary, where one catastrophic pattern costs one budget and
    one loud failure naming itself.

    Listed in `durable/publish._BAD_DATA_TYPES` by **name** — Temporal matches the outermost
    failure's class name, so leaving the hierarchy does not remove it from that list — because the
    pattern is the same string in the manifest on the next attempt and the page is the same page.
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
    """Coerce to `float`. A blank string is silence, not a zero, and NaN is neither.

    **A non-finite value is refused, on the same ground as the boolean above: it is not a
    measurement.** `float("NaN")` and `float("Infinity")` both parse, so the string form arrived
    here as a number, and a Spark `DOUBLE` can hold a stored NaN outright — while *missingness*
    from every driver in this seam arrives as `None`. So a NaN is the source saying something that
    is not a value, which is bad data with a reason, not silence.

    That distinction is the whole of it, and refusing here is what makes it survivable. A NaN
    reaching `reaction_records.conditions` failed at the `jsonb` wall as
    `psycopg.errors.InvalidTextRepresentation` — neither `ChemclawError` nor `ValidationError` — so
    it escaped `chemclaw.ingest.eln.sync`'s per-entry reject-and-continue, aborted the pass and
    advanced no cursor: one entry holding an entire corpus at a fixed date, deterministically, on
    every scheduled run after it. As a `TransformError` it is one rejected entry with its reason in
    the ledger.

    **No opt-in.** A binding cannot ask for a non-finite number, because nothing in the schema this
    engine maps onto has a field an infinity is an answer to — the same reason the boolean refusal
    takes no option.
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

    `original` rather than the parsed float, so the message quotes the column's own text: a site
    reading `'NaN'` in a rejection ledger can search its warehouse for it, and `nan` is not what it
    would search for.
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


# A literal repeat count above this is refused before the pattern is compiled.
#
# **`regex` expands a bounded repeat where `re` does not, which is a cost the engine swap brought
# with it and the first version of this change did not see.** Measured: `re.compile` is ~0.1 ms for
# every count below, flat; `regex.compile` is 0.17 ms at `a{100}`, 2.9 ms at `a{10000}`, 34 ms at
# `a{100000}`, and **431 ms and 290 MB** at `a{1000000}`, with `a{100000000}` not finishing in two
# minutes. That runs in `_check_pattern`, at binding load, on manifest text nobody here wrote —
# outside `eln_regex_timeout_seconds`, which bounds a match, and outside every Temporal deadline.
# So an ingest worker could be taken down by a `datasource.yaml` before a single row was read.
#
# 10,000 because it is ~3 ms to compile and four orders of magnitude above what a binding writes: a
# realistic pattern bounds a repeat at a field width (`\d{3,6}`, `[A-Z]{2,4}`). A site that needs
# more can say so; what it cannot do is say a number that never finishes.
_MAX_REPEAT_COUNT = 10_000

# The whole pattern's expansion, summed over siblings — looser than one repeat's, because it is a
# second, different bound: one repeat at `_MAX_REPEAT_COUNT` followed by a literal is the pattern a
# binding bounded at a field width writes, and refusing it was refusing correct input. Ten times
# over is ~34 ms to compile (`regex.compile("a{100000}")`, measured beside the figures above), paid
# once per pattern behind `_compiled`'s cache; what it exists to stop is the unbounded sum, a
# hundred legal repeats side by side.
_MAX_EXPANDED_ATOMS = 10 * _MAX_REPEAT_COUNT

# One `{n}` or `{n,m}` quantifier. Anchored on a `{` the scan below has already established is
# neither escaped nor inside a character class, so this never has to decide that itself.
_REPEAT_BOUND = re.compile(r"\{(\d*)(?:,(\d*))?\}")

# The flag run of an inline flag group — `(?x)`, `(?i-s:` — read from just after its `(?`. Only
# the positions `regex` reads as flags: `(?P<`, `(?:`, `(?=` and the rest fail the terminator.
_INLINE_FLAGS = re.compile(r"([A-Za-z0-9-]*)[):]")


# What follows `(?` when it opens a group whose body starts after it: a named group (`P<n>`,
# `<n>`), a lookaround, an atomic group or a branch reset. Matched rather than counted, because
# the prefix is syntax — counting `P<name>` as seven atoms refused `(?P<name>a){2000}`.
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

    **Scanned rather than parsed, and rather than compiled.** Compiling is the thing being
    guarded, so it cannot be the guard; and `re.compile` first — which is cheap and does not
    expand — only establishes that the pattern is *valid*, not what it costs the other engine
    (and `re` refuses legal `regex` syntax such as `\p{L}`, so it cannot be the parser either).

    **What is bounded is the expanded size, twice.** `regex` expands nested bounded repeats
    multiplicatively, so `(?:a{1000}){1000}` is a million atoms although neither count is over the
    limit. The walk keeps one running total per open group: an atom adds its weight, a `{n,m}`
    multiplies the atom (or group) before it by `max(n, m)`, and a closing `)` hands the group's
    total up as one atom's weight. One repeat's product is held to `_MAX_REPEAT_COUNT` — the old
    per-count limit, now seeing through nesting — and the whole pattern's sum to the looser
    `_MAX_EXPANDED_ATOMS`, so `a{9000}` written a dozen times over is refused while `[^,]{0,10000},`
    is not. An alternation's branches are summed rather than maxed, an over-estimate, which is the
    safe direction for a guard. A group's prefix (`?:`, `?P<name>`, a lookaround) is syntax and
    weighs nothing; a backreference or recursion is one atom; an inline flag set such as `(?i)`
    is neither.

    Besides that, it tracks the ways a `{` or `[` is not what it looks like: a backslash escape; a
    character class, where `{` is an ordinary member (`[{]{1}` is a literal brace repeated once,
    the shape `tasks/lessons.md` rule 96 is about); and an inline comment `(?#...)`, skipped to
    its first `)` — a `[` there opens nothing, and ending at the *first* `)` can only make the
    scan see more of the pattern, never less. **Verbose mode is refused outright**: under `x` a
    `#` comments out the rest of the line and whitespace is insignificant, so a scanner that does
    not model both is blind to a quantifier behind `#[` — and a binding that needs verbose mode
    to read a column is not one this has seen.
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
            # A `{` that does not open a quantifier is a literal in both engines, and neither
            # expands it. `{2,}` is unbounded, which is the *other* remedy's subject and costs
            # nothing to compile.
            if bound is not None:
                counts = [int(part) for part in bound.groups() if part]
                if counts:
                    repeated = last * max(counts)
                    # `{0,1}` and `{1}` duplicate nothing: the repeat bound is about what a
                    # quantifier *multiplies*, and the group's own total was already held to the
                    # expansion bound at its `)` — so they read the same as a `?` would.
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

    Cached because the binding hands its options down as the mapping YAML parsed, so the pattern
    arrives as a string on every cell of every row and there is nowhere in that plumbing to keep a
    compiled object. Measured on this box: `regex.search(pattern_string, ...)` is 4.6 us per call
    against `re`'s 0.38 us, and going through this cache is **1.2 us** — so the cache is most of
    what the engine swap costs. `maxsize` is generous against the handful of patterns a manifest
    declares; the keys are manifest text, so the cache cannot be grown by a row.

    The expansion guard runs **inside** the cache rather than beside it, so every route to a
    compiled pattern passes it — `_check_pattern` at load and `_regex` on a cell alike — and a
    pattern that reaches this function from somewhere added later cannot skip it.
    """
    _refuse_an_unbounded_expansion(pattern)
    return regex.compile(pattern)


@dataclass
class _PageBudget:
    """Matching time this page has left, and what it spent — **an accumulator, not a wall clock**.

    **The first spelling was a deadline, and a review measured what that charged.** It held
    `monotonic() + budget` and compared against the clock, while the manager below is opened
    *around the page loop* — a loop whose body awaits five stores per entry and, in
    `durable/memory_jobs.read_corpus`, every `fetch_new_entries` of every page of every source.
    So a "budget for every `regex` transform together" was billing Postgres and the source:
    driven, **1.13 ms** of actual matching exhausted a 500 ms budget, and the refusal then told
    the site to simplify patterns costing microseconds.

    That was worse than a wrong message. `PatternBudgetError` is non-retryable by name in
    `durable/publish._BAD_DATA_TYPES`, so a page that used to reach `eln_sync_timeout_seconds`
    and be **retried** would instead fail permanently at half of it, with no cursor advanced —
    the wedge `ingest/eln/sync.py` documents at length for a different cause.

    So this accumulates the time `regex` itself is given, charged per search. Nothing between two
    matches is billed, which is what makes the name true and what makes exhausting it mean what
    the refusal says it means.

    `searches` is deliberately not called `cells`: a binding may run several `regex` steps on one
    value and a NULL column runs none, so this counts applications of the transform. The earlier
    field was `cells` and the message said "cell(s)", which the same review falsified — two steps
    per row inflated it twofold, and mostly-empty columns deflated it arbitrarily.
    """

    #: The budget this page was opened with, carried so a refusal quotes the number actually in
    #: force. It read `settings.eln_regex_page_budget_seconds` first, which is a different number
    #: whenever a caller passed one — driven at a 2 s budget, the refusal said 150 s.
    budget: float
    spent: float = 0.0
    searches: int = 0

    def remaining(self) -> float:
        """Matching seconds left, negative once a clamped search has overrun slightly."""
        return self.budget - self.spent

    def charge(self, seconds: float) -> None:
        """Bill one search, whether it matched, missed or timed out.

        A timed-out search is billed for the same reason a successful one is: it spent the page's
        allowance. Not billing it would let a page of timeouts run forever, one per-cell budget
        at a time, which is the multiplication this bound exists to stop.
        """
        self.spent += seconds
        self.searches += 1


#: The budget for the page in flight, or `None` where no page has opened one.
#:
#: A contextvar rather than a parameter because the thing that knows a page has begun
#: (`ingest/eln/sync.sync_entries`) and the thing that spends the budget (`_regex`, several frames
#: down through `adapter._read` and `apply_transforms`) are separated by the whole binding walk, and
#: every frame between them is per-*cell* code with no business carrying a page's deadline.
#: `contextvars` is also what makes it correct under the one concurrency this path has: an activity
#: runs the walk synchronously, so a second page in the same worker cannot be inside this one.
_page_budget: ContextVar[_PageBudget | None] = ContextVar("eln_regex_page_budget", default=None)


@contextmanager
def pattern_budget(seconds: float | None = None) -> Iterator[None]:
    """Bound what every `regex` transform *together* may spend on one page of entries.

    **The per-cell bound does not compose into a page bound, and this is the missing half.**
    `eln_regex_timeout_seconds` bounds one `search`; `warehouse/adapter._read` runs one per reaction
    field, per attribute, and per component and impurity *row*, so a page is `eln_sync_batch_size x
    cells_per_entry` matches and the ceiling multiplies.

    **What makes it reachable is a pattern that is slow and *completes*.** One that exceeds the
    per-cell budget is refused, and since `PatternBudgetError` is in
    `durable/publish._BAD_DATA_TYPES` that refusal is non-retryable and ends the page after a single
    cell — so the accumulating case is not the catastrophic pattern the per-cell bound was written
    for. It is the polynomial one. Measured: `a*a*a*$` over a 6,000-character cell is **165 ms**,
    66% of the per-cell budget and never refused; twenty such cells across the shipped 100-entry
    batch is **330 s**, past `eln_sync_timeout_seconds` and past the heartbeat, with 1,818 of 2,000
    cells reached before the activity's own deadline. `map_to_ord` is synchronous CPU work, so no
    asyncio timer interrupts it and the retry runs the identical page.

    **An honest binding cannot notice this.** An honest cell measures **0.0024 ms** warm, so a whole
    honest page of 2,000 cells is **0.0048 s** against a 150 s ceiling — ~31,000x of headroom, and
    the pathological pattern is ~68,000x an honest one. The trade the row behind this named —
    refusing an honest slow pattern against bounding total work — is settled by that ratio rather
    than argued: four orders of magnitude apart, a budget generous enough never to touch an honest
    page still bounds the pathological one well inside the activity deadline, which is what buys a
    *refusal naming its cause* instead of a killed activity with nothing to say.

    **Those figures are the corrected ones.** The first version said 0.472 ms and ~160x, and in the
    same table also said 0.042 s for the page — two numbers 22x apart for one measurement, neither
    right. The 0.472 ms was the *first* call, including this module's `lru_cache` compile miss
    (0.3 ms on its own). A review falsified it; the corrected ratio makes the argument far more
    strongly,
    which is why the wrong number was never load-bearing and is recorded here anyway.

    Re-entrant by design: a nested call keeps the outer budget, because the outer one is the bound
    that matters and a page is not made cheaper by being processed in parts.

    Args:
        seconds: The budget, defaulting to `eln_regex_page_budget_seconds`. Passed explicitly only
            by a test, which is why there is no second setting for a caller to disagree over.
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

    Clamped to what the page has left, so the *last* cell of a page cannot overshoot the page bound
    by a whole per-cell budget — the difference between a ceiling and a ceiling plus one.

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

    The count is applications of the `regex` transform rather than cells — a binding may run
    several on one value, and a NULL column runs none — and it is what separates the two causes a
    site acts on differently: many searches means the *binding* is too expensive for this page
    size, a handful means one pattern is pathological in a way the per-cell ceiling was too
    generous to catch.

    Args:
        pattern: The pattern the budget ran out on, for a site to look at first.
        cut_short: The clamped allowance this search was given, when the budget ran out *inside*
            it. **The message must not then claim the pattern is innocent**, which is what the
            single-message version did for every clamped timeout — measured, the arm that would
            have blamed the pattern fired 0 times in 39 clamped runs against `(a+)+$`.
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

    **Run under a wall clock, because this is the one transform whose cost is not a function of
    anything this repository chose.** The pattern comes from a site's `datasource.yaml` and the
    subject is a free-text warehouse column, so a `(a+)+$`-shaped pattern against a long cell is
    unbounded work — and `re` has no timeout at any layer, which made the only real bound the
    activity's `start_to_close`, after which the retry ran the identical pattern over the identical
    page. `regex` checks a deadline inside its own matching loop, which is why it is the engine
    here and `re` is still the engine everywhere else in this module
    (`D-2026-09-21-a-pattern-that-cannot-be-timed-out-is-run-by-an-engine-that-can`).
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
        # **Three outcomes, not two, and the middle one is what a review falsified.** The first
        # version had two and claimed to tell them apart by re-reading the remaining budget. That
        # re-read is always spent by then: a clamped search is given exactly what was left, so it
        # times out at the instant the page runs dry. Driven over 39 clamped remainings against
        # `(a+)+$`, the "pattern is at fault" arm fired **0** times — every catastrophic pattern was
        # reported as a page that cost too much in aggregate, under a sentence asserting no single
        # cell had exceeded its ceiling.
        #
        # Clamped means this search never had its full per-cell allowance, so what the pattern alone
        # costs is *not established*. Saying so is both honest and the actionable thing, because the
        # remedies differ. Unclamped means it had the whole per-cell budget and blew it, which is
        # the only case that names a pattern to rewrite.
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

    For the columns whose site convention differs from this schema's bounds — a yield recorded as
    101.3% after rounding, which `OrdReaction` would reject outright. Clamping is a binding-author's
    explicit decision to keep such a row rather than lose it, never a default.

    **The one number it cannot hold is refused before it gets here**, by `_number`. Every
    comparison against NaN is false, so `max(nan, 0.0)` is `nan` and this — the transform whose
    entire job is guaranteeing a value inside a range — used to return one outside every range.
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
    on the first row of the first sync, and a `group:` the pattern does not have raises on the
    first row that *matches* — which may be days later and on a subset of the corpus.

    **Compiled by `_compiled`, so the engine that accepts a pattern here is the engine that runs
    it.** Two compilers would mean a pattern this gate accepts failing on row 1 anyway, which is
    the failure this function exists to have ended. It also warms the cache: a manifest's patterns
    are compiled at load rather than on the first row.
    """
    try:
        compiled = _compiled(str(options["pattern"]))
    except regex.error as exc:
        raise PathSyntaxError(f"transform 'regex' has an invalid pattern: {exc}") from exc
    except RecursionError as exc:
        # `regex` recurses where `re` iterates, so ~180 nested groups raise here on a pattern `re`
        # compiles without complaint. Caught by name rather than folded into a bare `except`,
        # because everything else this call can raise is a defect in *this* module and should not
        # be reported to a site as a pattern they wrote wrongly.
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
