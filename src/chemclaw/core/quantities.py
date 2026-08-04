"""The numbers a payload returned, the numbers a prose answer states, and whether one is the other.

The numeric counterpart to `chemclaw.kg.note`'s `mentioned_ids` / `cited_ids` pair, and here for
the same reason that pair lives in one module: two readers of one syntax drift, and a gate then
disagrees with the thing it gates. A tool result and an answer are scanned by *different*
functions — one reads machine-generated JSON, the other reads Markdown prose — over **one** number
grammar, so a literal either side can see is a literal the other can see.

`chemclaw.core` because both callers need it and they are in unrelated layers: the API's
`runner_trace` fills `ToolResultEvent.numbers` from a tool's full result, and `chemclaw.evals.live`
reads an answer. `api` importing `evals` would be backwards, and a private copy in each is exactly
the drift above.

**Why a rounding comparison and not equality.** `compute_electronic_properties` returns a dipole of
4.557929224414533 and a correct answer writes "4.56 D". String equality, prefix matching and a
fixed tolerance each fail here: the prefix is "4.55", and a tolerance tight enough to mean anything
at 0.13 eV is meaningless at 7112 g. The precision an answer *chose* is the only scale that is
right at both ends, so a stated figure is grounded when some returned value, rounded to that
precision, is exactly it. Half-up, not Python's half-even `round`: a model writing 4.55 from 4.545
is rounding the way a person does, where banker's rounding gives 4.54 and calls the figure invented.
"""

import math
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# A decimal literal, optionally signed, optionally in exponent form, with thousands separators
# allowed so an answer's "14,224 g" is one number rather than two.
#
# The lookarounds are what keep this from reading digits that are part of a name — without them a
# `structure_id` yields four numbers and a SMILES yields two, and either could then vouch for a
# figure no tool ever computed. A digit run is part of an identifier when it touches a word
# character (`st_8addd23b880dff9b`), follows a dot (`Table A.2.1`), or is reached through a hyphen
# from one (`reaction-liu-orgsyn-procedure-1`, `2026-08-03`). The same lookbehinds settle the sign:
# a table cell's "| -7.95 |" is a negative value, and "10-20 %" is *not* a −20.
#
# The second lookbehind takes a range's upper bound with the slug tails — "10-20 %" yields 10
# alone. That is the trade taken knowingly: slugs are everywhere in a retrieval result and ranges
# are rare, and the strict direction only costs a match, where the permissive one would vouch for
# figures no tool computed.
#
# The lookbehind is deliberately *not* extended to Markdown emphasis: "**2000 g**" is a quantity,
# and an earlier draft that whitelisted the preceding characters instead of blacklisting them
# dropped five of the six masses in a real charge table for want of an asterisk.
_NUMBER = re.compile(
    r"(?<![\w.])(?<!\w-)(?P<sign>[-+]?)"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<exponent>[eE][-+]?\d+)?(?![\w])"
)

# Markdown spans whose contents are never a quantity claim: an inline/fenced code span (a SMILES, a
# structure id, a snippet) and a wikilink (a note id, which `_score_citations` scores separately).
# Stripped before the scan so `[[reaction-nielsen-deoxyfluorination-0019]]` does not contribute 19.
_NOT_A_QUANTITY = re.compile(r"```.*?```|`[^`]*`|\[\[[^\]]*\]\]", re.DOTALL)


def returned_values(text: str) -> list[float]:
    """Every number a tool result contains, deduplicated, in first-seen order.

    Deliberately generous: this is the *evidence* side, and a value collected here can only ever
    let a figure in an answer be recognised as quoted. Missing one, by contrast, makes a verbatim
    quotation look unsupported — which is the failure this whole seam exists to stop — so where the
    two errors are not symmetric, this errs toward collecting.

    Deduplication is not cosmetic: one 18-chunk `gather_evidence` sweep holds 133 numeric literals
    and 35 distinct values, and it is the distinct set that a comparison needs.
    """
    seen: dict[float, None] = {}
    for match in _NUMBER.finditer(text):
        value = _as_float(match)
        if value is not None:
            seen.setdefault(value, None)
    return list(seen)


def stated_numerals(text: str) -> list[str]:
    """Every figure a prose answer states, as the answer wrote it, deduplicated in first-seen order.

    Returned as the *literal* rather than as a float for two reasons, and both matter. The written
    form carries the precision the answer chose, which `is_rounding_of` needs; and it is the string
    a reader — or a judge — can find in the answer, where a re-formatted `4.56` might not be there
    at all.
    """
    stripped = _NOT_A_QUANTITY.sub(" ", text)
    seen: dict[str, None] = {}
    for match in _NUMBER.finditer(stripped):
        if _as_float(match) is not None:
            seen.setdefault(match.group(0), None)
    return list(seen)


def is_rounding_of(numeral: str, values: Iterable[float]) -> bool:
    """Whether some `value`, rounded to the precision `numeral` was written at, is exactly it.

    The module docstring argues the rule; this is where its edges live. "4.56" fixes the scale at
    two decimals, so 4.557929224414533 grounds it and 4.5 does not. "3000" fixes it at units, so
    2999.6 grounds it. An exponent form carries its own scale — "1.5e3" is quantized to hundreds —
    which is the significant-figure reading a chemist means by writing it that way.

    Signed, not absolute. This function's answer is quoted to a consumer as "that figure is
    verbatim tool output", and a value whose sign the answer flipped is not that.
    """
    try:
        stated = Decimal(numeral.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - `_NUMBER` cannot produce one
        return False
    exponent = stated.as_tuple().exponent
    if not isinstance(exponent, int):  # a NaN or an infinity, which no literal here can be
        return False
    step = Decimal(1).scaleb(exponent)
    for value in values:
        try:
            if Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP) == stated:
                return True
        except InvalidOperation:
            continue  # the value needs more digits than the context allows; it is not this figure
    return False


def _as_float(match: re.Match[str]) -> float | None:
    """One regex match as a float, or None when it is not a finite number.

    Non-finite is unreachable from `_NUMBER` (no literal spells an infinity) but a `float()` that
    can raise must not take a scan down with it: a tool result is arbitrary text.
    """
    text = f"{match['sign']}{match['digits'].replace(',', '')}{match['exponent'] or ''}"
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None
