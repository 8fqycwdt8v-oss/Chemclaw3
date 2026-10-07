"""Which solvent names GFN2-xTB's ALPB model actually has, and the launch-time check on them.

A job precondition run in the chat service before any workflow starts, so this imports only the
standard library. `ALPB_SOLVENTS` is the intersection of tblite's dielectric and ALPB parameter
tables; `tests/test_solvents.py` re-derives it against the installed tblite.
"""

from difflib import get_close_matches
from typing import Any

# Every name `Calculator.add("alpb-solvation", ...)` accepts, lowercase, aliases included (`h2o`,
# `mecn`, and tblite's own `dichlormethane`). Matching is case-insensitive and trimmed, as tblite's
# is.
ALPB_SOLVENTS = frozenset(
    {
        "acetone",
        "acetonitrile",
        "aniline",
        "benzaldehyde",
        "benzene",
        "carbondisulfide",
        "ch2cl2",
        "chcl3",
        "chloroform",
        "cs2",
        "dichlormethane",
        "dichloromethane",
        "diethylether",
        "dimethylformamide",
        "dimethylsulfoxide",
        "dioxane",
        "dmf",
        "dmso",
        "ethanol",
        "ether",
        "ethyl acetate",
        "ethylacetate",
        "furan",
        "furane",
        "h2o",
        "hexadecane",
        "hexane",
        "mecn",
        "methanol",
        "methylenechloride",
        "n-hexan",
        "n-hexane",
        "nhexan",
        "nhexane",
        "nitromethane",
        "octanol",
        "phenol",
        "tetrahydrofuran",
        "thf",
        "toluene",
        "water",
        "woctanol",
    }
)

# What a refusal quotes: one canonical spelling per common process solvent, in polarity order.
# Every entry is in `ALPB_SOLVENTS` (asserted in `tests/test_solvents.py`); aliases are omitted.
SUGGESTED_SOLVENTS = (
    "water",
    "methanol",
    "ethanol",
    "acetonitrile",
    "dmso",
    "dmf",
    "acetone",
    "thf",
    "dioxane",
    "ethylacetate",
    "ch2cl2",
    "chcl3",
    "toluene",
    "benzene",
    "ether",
    "hexane",
)

# Spelling suggestions per unknown name; `SUGGESTED_SOLVENTS` is already the menu.
_MAX_SUGGESTIONS = 3


def _normalize(name: str) -> str:
    """The form `ALPB_SOLVENTS` is keyed in: tblite matches case-insensitively and trims, so do we.

    One definition, so membership and the error message agree.
    """
    return name.strip().lower()


def unsupported(names: list[str]) -> list[str]:
    """The names ALPB has no parameters for, in the order given, deduplicated by normalised form."""
    seen: set[str] = set()
    bad: list[str] = []
    for name in names:
        key = _normalize(name)
        if key in seen or key in ALPB_SOLVENTS:
            continue
        seen.add(key)
        bad.append(name)
    return bad


def _did_you_mean(name: str) -> str:
    """A `(did you mean …)` clause for one unknown name, or empty when nothing is close.

    Silent when nothing is close, since a far-fetched guess is worse than none.
    """
    close = get_close_matches(_normalize(name), sorted(ALPB_SOLVENTS), n=_MAX_SUGGESTIONS)
    return f" (did you mean {', '.join(close)}?)" if close else ""


def require_supported_solvents(spec: Any) -> None:
    """Refuse a durable calc job naming a solvent the method cannot model, before it starts.

    Duck-typed over `solvents` (a list) or `solvent` (optional); `None` is gas phase.

    Raises:
        ValueError: Naming each unsupported solvent with the closest spellings and the common ones.
    """
    named: list[str] = list(getattr(spec, "solvents", None) or [])
    single = getattr(spec, "solvent", None)
    if single is not None:
        named.append(single)
    bad = unsupported(named)
    if not bad:
        return
    detail = "; ".join(f"{name!r}{_did_you_mean(name)}" for name in bad)
    raise ValueError(
        f"GFN2-xTB's ALPB solvation model has no parameters for {detail}. It is an implicit "
        f"model with a fixed set of parameterized solvents, so an unlisted one cannot be "
        f"approximated — pick the closest supported solvent, or run in the gas phase. "
        f"Commonly used supported solvents: {', '.join(SUGGESTED_SOLVENTS)}."
    )
