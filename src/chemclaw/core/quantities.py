"""The numbers a payload returned, the numbers a prose answer states, and whether one is the other.

`returned_values` scans arbitrary text generously (grounding: a missed value makes a verbatim
quotation look invented); `labelled_values` walks parsed JSON strictly (display: a number under the
wrong name is worse than none); `stated_numerals` reads an answer. They share one number grammar so
a literal one side sees, the other sees too. In `chemclaw.core` because the API and
`chemclaw.evals.live` both need it.

A stated figure is grounded when some returned value, rounded half-up to the precision the answer
chose, equals it exactly. The answer's own precision is the only scale right at every magnitude
(4.557929 grounds "4.56"), and half-up matches how a person rounds.
"""

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from chemclaw.core.result_handle import without_handle_line, without_handles

# A decimal literal, optionally signed, optionally in exponent form, with thousands separators so
# "14,224 g" is one number.
#
# The lookarounds exclude digits that belong to a name: touching a word character
# (`st_8addd23b880dff9b`), following a dot (`Table A.2.1`), or reached through a hyphen from one
# (`reaction-liu-orgsyn-procedure-1`, `2026-08-03`). They also settle the sign: "| -7.95 |" is
# negative, while "10-20 %" yields 10 alone (accepted: ranges are rare and slugs common). Markdown
# emphasis is not excluded, so "**2000 g**" still reads as a quantity.
_NUMBER = re.compile(
    r"(?<![\w.])(?<!\w-)(?P<sign>[-+]?)"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<exponent>[eE][-+]?\d+)?(?![\w])"
)

# Markdown spans that are never a quantity claim: code spans (SMILES, ids, snippets) and wikilinks
# (note ids), stripped before the scan.
_NOT_A_QUANTITY = re.compile(r"```.*?```|`[^`]*`|\[\[[^\]]*\]\]", re.DOTALL)


def returned_values(text: str) -> list[float]:
    """Every number a tool result contains, deduplicated, in first-seen order.

    Deliberately generous: the evidence side errs toward collecting, since a missed value makes a
    verbatim quotation look unsupported. Result handles (`core.result_handle`) are skipped: they are
    addresses whose hex can be all digits.
    """
    seen: dict[float, None] = {}
    for match in _NUMBER.finditer(without_handles(text)):
        value = _as_float(match)
        if value is not None:
            seen.setdefault(value, None)
    return list(seen)


@dataclass(frozen=True, slots=True)
class Quantity:
    """One number a *structured* result returned, under the name the tool gave it.

    `label` is the payload's own key path (`pka`, `limits.0.value`), never prettified or inferred,
    so no relationship the tool did not state is asserted. `unit` is only the payload's own, from a
    `unit`/`units` string beside the number in the same object; empty when the result did not say.
    """

    label: str
    value: float
    unit: str = ""


# How deep into a result the walk goes; a pathological payload can nest without bound.
_MAX_DEPTH = 6


def labelled_values(text: str) -> list[Quantity]:
    """Every number a JSON result returned, with the key the tool filed it under.

    Strict, unlike `returned_values`: a non-JSON result yields nothing rather than labels guessed
    from prose; its figures still appear unnamed in `numbers`. Deduplicated on (label, value), in
    first-seen order.
    """
    try:
        # A stamped result is JSON followed by its handle line, which is no part of the payload.
        parsed = json.loads(without_handle_line(text))
    except (ValueError, TypeError, RecursionError):
        # `json.loads` raises `RecursionError` (not a `ValueError`) on deep nesting; a pathological
        # result must yield no labels, never end the turn's stream.
        return []
    seen: dict[tuple[str, float], Quantity] = {}
    for quantity in _walk(parsed, prefix="", depth=0):
        seen.setdefault((quantity.label, quantity.value), quantity)
    return list(seen.values())


def _walk(
    node: object, *, prefix: str, depth: int, unit: str = "", named: bool = False
) -> Iterator[Quantity]:
    """Yield every finite number under `node`, labelled by the path that reaches it.

    `named` is whether any path segment came from a key rather than a list position; purely
    positional labels are not names and are left to `returned_values`.
    """
    if depth > _MAX_DEPTH:
        return
    # `bool` is an `int` in Python, and `{"converged": true}` is a state rather than a quantity —
    # a value strip listing "converged 1" would be reporting a number nobody computed.
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        # An unlabelled number is one the caller cannot name: a bare `[1, 2, 3]` at the top level
        # reaches here with nothing but its position and belongs to `returned_values`, not to this.
        if named and math.isfinite(node):
            yield Quantity(label=prefix, value=float(node), unit=unit)
        return
    if isinstance(node, Mapping):
        # A `unit` in the same object qualifies its numbers. A nested object states its own or has
        # none; a nested list inherits (`{"unit": "eV", "values": [...]}`).
        own = _unit_of(node)
        for key, value in node.items():
            yield from _walk(
                value, prefix=_join(prefix, str(key)), depth=depth + 1, unit=own, named=True
            )
        return
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(
                value, prefix=_join(prefix, str(index)), depth=depth + 1, unit=unit, named=named
            )


def _join(prefix: str, key: str) -> str:
    """`limits.0.value` — the path as the payload spells it, dotted, with no prettifying."""
    return f"{prefix}.{key}" if prefix else key


# The keys a tool uses to say what its numbers are measured in. Two spellings and no more: a
# guessing list ("dimension", "scale", "basis") would attach units nobody wrote.
_UNIT_KEYS = ("unit", "units")


def _unit_of(node: Mapping[str, object]) -> str:
    """The unit this object states for its own numbers, or `""` when it states none."""
    for key in _UNIT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def stated_numerals(text: str) -> list[str]:
    """Every figure a prose answer states, as the answer wrote it, deduplicated in first-seen order.

    Literals, not floats: the written form carries the precision `is_rounding_of` needs and is the
    string a reader can find in the answer. A quoted result handle is an address, not a figure.
    """
    stripped = _NOT_A_QUANTITY.sub(" ", without_handles(text))
    seen: dict[str, None] = {}
    for match in _NUMBER.finditer(stripped):
        if _as_float(match) is not None:
            seen.setdefault(match.group(0), None)
    return list(seen)


def is_rounding_of(numeral: str, values: Iterable[float]) -> bool:
    """Whether some `value`, rounded to the precision `numeral` was written at, is exactly it.

    "4.56" fixes two decimals, so 4.557929 grounds it and 4.5 does not; "3000" fixes units, so
    2999.6 grounds it; "1.5e3" is quantized to hundreds. Signed: a flipped sign is not verbatim tool
    output.
    """
    stated = _stated(numeral)
    if stated is None:
        return False
    figure, step = stated
    return any(_quantized(value, step) == figure for value in values)


def ungrounded(numerals: Iterable[str], values: Iterable[float]) -> list[str]:
    """The numerals that no value is a rounding of — `is_rounding_of` over many figures at once.

    Same rule and helpers as `is_rounding_of`, but each value is quantized once per distinct
    precision and each figure is a set lookup, instead of figures x values quantizations.

    Returns:
        The ungrounded numerals, in the order given.
    """
    pool = list(values)
    by_step: dict[Decimal, set[Decimal]] = {}
    left: list[str] = []
    for numeral in numerals:
        stated = _stated(numeral)
        if stated is None:
            left.append(numeral)
            continue
        figure, step = stated
        if step not in by_step:
            by_step[step] = {q for value in pool if (q := _quantized(value, step)) is not None}
        if figure not in by_step[step]:
            left.append(numeral)
    return left


def _stated(numeral: str) -> tuple[Decimal, Decimal] | None:
    """A written figure as its exact value and the step its precision fixes, or `None`."""
    try:
        stated = Decimal(numeral.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - `_NUMBER` cannot produce one
        return None
    exponent = stated.as_tuple().exponent
    if not isinstance(exponent, int):  # a NaN or an infinity, which no literal here can be
        return None
    return stated, Decimal(1).scaleb(exponent)


def _quantized(value: float, step: Decimal) -> Decimal | None:
    """`value` rounded half-up to `step`, or `None` when it needs more digits than the context."""
    try:
        return Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None  # the value needs more digits than the context allows; it is not this figure


def _as_float(match: re.Match[str]) -> float | None:
    """One regex match as a float, or None when it is not a finite number.

    Guarded although `_NUMBER` cannot match an infinity: a scan of arbitrary text must never raise.
    """
    text = f"{match['sign']}{match['digits'].replace(',', '')}{match['exponent'] or ''}"
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None
