"""ICH Q3C / Q3D limit lookup: the number comes from a committed table or not at all.

Why this exists. In a live run a chemist asked for the palladium limit and the system recited a
PDE from training as though it were the record. The value was correct, which makes it worse rather
than better: a correct recalled limit trains a reader to trust the next one, and there is nothing
behind either. Two user stories that turn entirely on these numbers — process risk scoping and
technique selection — were answered from the same place, with no citation and nothing to check.

What this is. Two transcribed reference tables (`ich_q3c.yaml`, `ich_q3d.yaml`) and one lookup over
both. Q3C residual-solvent classes and limits, Q3D elemental-impurity permitted daily exposures.
Every row carries the guideline, its revision, and the table it came from, so a reader can open the
source document at the right page and check the figure. A substance the tables do not carry returns
a **miss** that says so — never a nearby value, never a recalled one.

What this is emphatically *not*: a risk assessment. Deciding whether a given process needs a given
control, what specification an intermediate should carry, or how a PDE converts into a limit on an
API is judgement about a process. The tool's job is to supply the number that judgement needs.

**Why these two paths are not configurable, when `safety_rules_path` is.** A site extends the
hazard rule table with its own process knowledge, so that table is a setting. Nobody has their own
Q3C: the values are fixed by a published guideline, and a deployment quietly substituting a
different PDE table is the failure mode rather than a feature. The files therefore ship inside the
package and are resolved against `__file__`, the same way `science/bo/benchmarks/reizman_suzuki.py`
resolves its pinned benchmark data. A malformed table is likewise a packaging fault rather than an
operator mistake, so it raises pydantic's own validation error rather than a bespoke one.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, computed_field

from chemclaw.core.reagents import resolve_compound_name

# The two tables ship beside this module — see the module docstring for why they are not settings.
_DIRECTORY = Path(__file__).resolve().parent
_Q3C_PATH = _DIRECTORY / "ich_q3c.yaml"
_Q3D_PATH = _DIRECTORY / "ich_q3d.yaml"


class LimitValue(BaseModel):
    """One number from a guideline table, with the basis it is quoted on and its unit.

    A list of these rather than named fields per guideline, because Q3C quotes a concentration in
    ppm and a PDE in mg/day while Q3D quotes three route-specific PDEs in µg/day. One shape lets
    the agent read either answer without knowing in advance which guideline covers the substance —
    which is exactly the knowledge it was missing when it invented the numbers instead.
    """

    basis: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)


class ImpurityLimit(BaseModel):
    """One transcribed row: what the guideline calls it, what class it is in, and its limits."""

    substance: str
    guideline: str
    limit_class: str
    class_meaning: str
    limits: list[LimitValue]
    citation: str


class ImpurityLimitLookup(BaseModel):
    """The result of one lookup — a row, or an explicit miss.

    The miss is the load-bearing half, and `verdict` is why this is a model rather than an
    `ImpurityLimit | None`. `screen.py` learned the lesson the expensive way: a caveat that lives
    only in a tool docstring is read once when the tool is defined, while the *payload* is what
    sits in the context window as the answer is written. So the distinction between "this system
    has no row for that" and "no limit exists" is carried in the result itself.
    """

    query: str
    limit: ImpurityLimit | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """A one-line summary for a human — and, on a miss, the sentence that prevents a guess."""
        if self.limit is None:
            return (
                f"No entry for {self.query!r} in the transcribed ICH Q3C/Q3D tables. That means "
                "this system does not carry the number — not that no limit exists. Read it from "
                "the guideline; do not state one from memory."
            )
        return (
            f"{self.limit.substance}: {self.limit.citation}. Quote the citation with the number, "
            "and note that a limit is not a risk assessment."
        )


class _Q3cSolvent(BaseModel):
    """One row of ICH Q3C Table 1, 2 or 3 as transcribed."""

    name: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    solvent_class: Literal["1", "2", "3"]
    concentration_limit_ppm: float
    # Class 1 is quoted as a concentration limit only; Classes 2 and 3 also carry a PDE.
    pde_mg_per_day: float | None = None
    concern: str | None = None


class _ClassNote(BaseModel):
    """Which table a class lives in, and what membership of it means."""

    table: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class _Q3cTable(BaseModel):
    """The parsed residual-solvent file."""

    guideline: str = Field(min_length=1)
    classes: dict[str, _ClassNote]
    solvents: list[_Q3cSolvent] = Field(min_length=1)


class _Q3dElement(BaseModel):
    """One row of ICH Q3D Table A.2.1 as transcribed."""

    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    element_class: Literal["1", "2A", "2B", "3"]
    oral_pde_ug_per_day: float
    parenteral_pde_ug_per_day: float
    inhalation_pde_ug_per_day: float


class _Q3dTable(BaseModel):
    """The parsed elemental-impurity file."""

    guideline: str = Field(min_length=1)
    table: str = Field(min_length=1)
    classes: dict[str, str]
    elements: list[_Q3dElement] = Field(min_length=1)


def _fold(text: str) -> str:
    """Fold a written name to its lookup key: case, whitespace and separator punctuation.

    Deliberately the same *idea* as `core/reagents.py`'s fold but not the same function: this one
    also drops commas and periods, so `N,N-Dimethylformamide` and `NN dimethylformamide` land on
    one key. Both the table's spellings and the query go through it, so the two agree by
    construction.
    """
    return "".join(character for character in text.lower() if character.isalnum())


def _register(index: dict[str, ImpurityLimit], keys: list[str], limit: ImpurityLimit) -> None:
    """Index one row under every spelling it answers to, refusing a collision.

    A collision means two rows claim one name, and whichever loaded second would silently win —
    the reader would get a limit for a different substance with a real citation attached to it.
    That is the one failure worse than a miss, so it stops the load instead.
    """
    for key in keys:
        folded = _fold(key)
        existing = index.get(folded)
        if existing is not None and existing.substance != limit.substance:
            raise ValueError(
                f"ICH table entries {existing.substance!r} and {limit.substance!r} "
                f"both answer to {key!r}"
            )
        index[folded] = limit


@lru_cache(maxsize=1)
def _index() -> dict[str, ImpurityLimit]:
    """Both tables flattened to one name→row index, built once per process.

    One index over both guidelines because the caller's question is "what is the limit for X", and
    knowing that solvents are Q3C and metals are Q3D is precisely the knowledge the agent lacked.
    """
    q3c = _Q3cTable.model_validate(yaml.safe_load(_Q3C_PATH.read_text(encoding="utf-8")))
    q3d = _Q3dTable.model_validate(yaml.safe_load(_Q3D_PATH.read_text(encoding="utf-8")))
    index: dict[str, ImpurityLimit] = {}
    for solvent in q3c.solvents:
        note = q3c.classes[solvent.solvent_class]
        limits = [
            LimitValue(
                basis="concentration limit", value=solvent.concentration_limit_ppm, unit="ppm"
            )
        ]
        if solvent.pde_mg_per_day is not None:
            limits.insert(0, LimitValue(basis="PDE", value=solvent.pde_mg_per_day, unit="mg/day"))
        meaning = note.meaning
        if solvent.concern is not None:
            meaning = f"{meaning} Concern for this solvent: {solvent.concern.lower()}."
        _register(
            index,
            [solvent.name, *solvent.synonyms],
            ImpurityLimit(
                substance=solvent.name,
                guideline=q3c.guideline,
                limit_class=f"Class {solvent.solvent_class}",
                class_meaning=meaning,
                limits=limits,
                citation=f"{q3c.guideline}, {note.table}",
            ),
        )
    for element in q3d.elements:
        _register(
            index,
            [element.symbol, element.name, *element.synonyms],
            ImpurityLimit(
                substance=f"{element.name} ({element.symbol})",
                guideline=q3d.guideline,
                limit_class=f"Class {element.element_class}",
                class_meaning=q3d.classes[element.element_class],
                limits=[
                    LimitValue(basis="oral PDE", value=element.oral_pde_ug_per_day, unit="µg/day"),
                    LimitValue(
                        basis="parenteral PDE",
                        value=element.parenteral_pde_ug_per_day,
                        unit="µg/day",
                    ),
                    LimitValue(
                        basis="inhalation PDE",
                        value=element.inhalation_pde_ug_per_day,
                        unit="µg/day",
                    ),
                ],
                citation=f"{q3d.guideline}, {q3d.table}",
            ),
        )
    return index


def impurity_limit(substance: str) -> ImpurityLimitLookup:
    """Look up one substance in the transcribed ICH Q3C / Q3D tables.

    Accepts the guideline's own spelling, an element symbol, an abbreviation a chemist writes, or a
    SMILES: an unmatched query is resolved through `core/reagents.py` first, so `THF`, `2-MeTHF`
    and `C1CCOC1` all reach the tetrahydrofuran row without that table's synonyms having to be
    copied into the guideline files.

    A miss returns a lookup whose `limit` is `None`, never a nearby row.
    """
    index = _index()
    hit = index.get(_fold(substance))
    if hit is None:
        resolved = resolve_compound_name(substance)
        if resolved is not None:
            hit = index.get(_fold(resolved.name))
    return ImpurityLimitLookup(query=substance, limit=hit)
