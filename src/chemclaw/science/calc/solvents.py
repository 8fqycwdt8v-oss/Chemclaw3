"""Which solvent names GFN2-xTB's ALPB model actually has, and the launch-time check on them.

**Why this is its own module, and a leaf one.** A chemist asked `compare_solvents` for "2-MeTHF" —
among the most common process solvents there is — the model passed it through faithfully, the turn
reported the job running, and ~30 s later an activity died deep inside the durable path on tblite's
`String value for epsilon was not found among database of solvents` (live full-stack pass,
2026-08-04). Nothing was wrong with the durability: the name was simply not a name the method knows,
and the only thing that could tell the chemist so was the calculation itself.

`JobSpec.preconditions` exists for exactly this and runs in `chemclaw.connectors.jobs`'s
`prepare_job_launch` *before* any workflow is started, so the check belongs there. But the
precondition is resolved by importing the module that holds it, in the **chat service's** process —
so it must not drag `tblite` in with it (D-118, `tests/test_connector_isolation.py`). Hence a module
that imports nothing but the standard library, sitting beside the calculators that use the same
names, exactly as `chemclaw.science.bo.problem:require_campaign_startable` sits beside its own
spec.

**The names were measured, not recalled.** `ALPB_SOLVENTS` is every name tblite accepts for
`alpb-solvation`, obtained by probing the solvent-name table compiled into `_libtblite` against a
live `Calculator`. That distinction matters, because tblite has *two* tables and rejects a name from
each with a different message: a name absent from the dielectric database fails with "String value
for epsilon was not found" (`2-methyltetrahydrofuran`, `mtbe`), while a name present there but
lacking Born parameters for the Hamiltonian fails with "No ALPB/GBSA parameters found for the
method/solvent" (`heptane`, `cyclohexane`, `xylene`). Only the intersection works, and it is
identical for GFN1-xTB and GFN2-xTB — so the set does not depend on `settings.xtb_method` and is
written here as one constant rather than a per-method map that would have one entry twice.
`tests/test_solvents.py` re-derives it against the installed tblite, so an upgrade that adds or
drops a solvent fails a test instead of surfacing as a wrong refusal.

This is also the one home for the *shortlist* an error message quotes. `xtb_engine` used to keep its
own curated tuple for that, which had quietly diverged from what the method supports in both
directions: it omitted `dmf`, `dioxane`, `benzene` and `nitromethane` — all valid, all ordinary
process solvents — while its comment claimed to be "the solvents process chemistry actually asks
about". A shortlist that is a *subset of a measured set* cannot drift that way, and a test pins it.
"""

from difflib import get_close_matches
from typing import Any

# Every name `Calculator.add("alpb-solvation", ...)` accepts, lowercase. Aliases are included
# because a chemist and a model both write them: `h2o`, `mecn`, `nhexane` and `dichlormethane` (sic
# — tblite's own spelling) are all real keys, not typos this module should be normalising away.
# Comparison is case-insensitive and whitespace-trimmed (`_normalize`) because tblite is.
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

# What a refusal quotes: one canonical spelling per distinct solvent a process chemist reaches for,
# in polarity order so the list reads as a range rather than an alphabet. Every entry is in
# `ALPB_SOLVENTS` by construction (asserted in `tests/test_solvents.py`), so this can never again
# advertise a name the method rejects, nor silently omit one it supports — the aliases are what is
# left out, deliberately, since naming `h2o` beside `water` spends a line saying nothing.
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

# How many spelling suggestions a single unknown name earns. Three is the point where the list stops
# reading as "did you mean this?" and starts reading as a second menu — `SUGGESTED_SOLVENTS` is
# already the menu, and the message carries both.
_MAX_SUGGESTIONS = 3


def _normalize(name: str) -> str:
    """The form `ALPB_SOLVENTS` is keyed in: tblite matches case-insensitively and trims, so do we.

    Written once rather than inlined at both call sites, because a membership test and an error
    message that disagreed about normalisation would refuse a name and then fail to explain why.
    """
    return name.strip().lower()


def is_supported(name: str) -> bool:
    """Whether GFN2-xTB's ALPB model has parameters for this solvent name."""
    return _normalize(name) in ALPB_SOLVENTS


def unsupported(names: list[str]) -> list[str]:
    """The names ALPB has no parameters for, in the order given, deduplicated by normalised form.

    Order is the caller's rather than sorted, so a chemist reading the refusal can line the bad
    names up against the list they sent. Deduplicated because a screen naming one typo twice should
    be told about it once.
    """
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

    Worth the four lines because the single measured failure this module exists for —
    "2-methyltetrahydrofuran" — is one edit family away from `tetrahydrofuran`, which is both the
    closest supported solvent and very often the right substitution for a chemist who reached for
    2-MeTHF. Silence when nothing matches, rather than a floor-scraping guess: proposing `phenol`
    for `mtbe` would be worse than proposing nothing.
    """
    close = get_close_matches(_normalize(name), sorted(ALPB_SOLVENTS), n=_MAX_SUGGESTIONS)
    return f" (did you mean {', '.join(close)}?)" if close else ""


def require_supported_solvents(spec: Any) -> None:
    """Refuse a durable calc job naming a solvent the method cannot model, before it starts.

    Duck-typed over the params object because five job specs carry a solvent between them in two
    shapes — `SolventScreenJobSpec.solvents` is a list, while the reaction/scan/ensemble/complex
    specs each carry a single optional `solvent` — and `connector.yaml` names one precondition per
    job. Reading both attributes is what lets one function cover all five without
    `chemclaw.connectors.calc.specs` importing anything (it is a leaf by contract, D-118) and
    without five near-identical rules.

    Gas phase is not a solvent and is spelled `solvent: null`, so `None` passes untouched.

    Args:
        spec: The validated params object, whatever the job declared.

    Raises:
        ValueError: One or more named solvents have no ALPB parameters. The message names each bad
            one, offers the closest supported spellings, and lists the common ones — everything the
            chemist needs to correct the call in the same turn.
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
