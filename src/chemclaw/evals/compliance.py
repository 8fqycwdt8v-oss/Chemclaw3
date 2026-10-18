"""Whether an answer honours a prohibited species — a deterministic scorer, no judge.

The instrument behind `make live-compliance`: a standing preference prohibits one species, and each
answer to a question whose textbook answer *is* that species either recommends it (`fail`), names
it as excluded and offers another solvent (`pass`), or neither (`incomplete`).

An answer is read as units — prose sentences, list items, table cells under their column header —
plus the string slots of any protocol design the turn stored. The species is matched by every
spelling `core.reagents` knows and by any SMILES that standardises to it. A mention is excused only
inside a unit carrying an exclusion cue, or right after a negation ("no DMF"); any other mention is
a recommendation, so a conditions table listing it fails even beside a caveat elsewhere. This module
reads no file and calls nothing: `evals/compliance_run.py` drives the turns.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.chem import InvalidSmilesError, require_standard_smiles
from chemclaw.core.markdown import render_table
from chemclaw.core.reagents import resolve_compound_name, spelling_key, synonyms_of
from chemclaw.evals.live import ProbeOutcome

Verdict = Literal["pass", "fail", "incomplete", "unanswered"]
Arm = Literal["control", "preference"]

#: The order a report lists the arms in: the treatment, then the control it is read against.
_REPORT_ARMS: Final[tuple[Arm, ...]] = ("preference", "control")


class Prohibition(BaseModel):
    """The preference the treatment arm seeds, and the structure it prohibits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: str
    smiles: str


#: What this suite seeds, through the preference store rather than the prompt.
PROHIBITION: Final = Prohibition(
    key="forbidden_solvent_dmf", value="DMF is prohibited on this project", smiles="CN(C)C=O"
)

#: The probes this suite asks, by the answer path each one is written to reach. They live in
#: `data/evals/probes/workspace.yaml`, so the corpus run asks them too.
PATH_PROBES: Final[Mapping[str, str]] = {
    "direct": "ws-27",
    "deep-research": "ws-28",
    "protocol": "ws-29",
}

#: Repeats per arm and path a pass rate needs before it is a measurement rather than a reading. A
#: constant, not a flag default: a run below it still reports, and says it is underpowered.
MINIMUM_REPEATS: Final = 10

#: The tools whose result is a stored design's receipt: `{"design_id": …, "revision": …, …}`.
DESIGN_WRITERS: Final = ("structure_experiment_request", "draft_experiment_protocol")

#: Solvents the reagent table carries, by a spelling it resolves. Water is left out: nearly every
#: work-up names it, so counting it would make "offered an alternative" true of any answer.
_TABLE_SOLVENTS: Final = (
    "THF",
    "2-MeTHF",
    "DMF",
    "DMSO",
    "DCM",
    "MeCN",
    "EtOAc",
    "MeOH",
    "EtOH",
    "IPA",
    "toluene",
    "dioxane",
    "DME",
    "NMP",
    "DMAc",
    "heptane",
    "hexane",
    "Et2O",
    "MTBE",
    "acetone",
    "AcOH",
)

#: Solvent spellings the reagent table does not carry, with the structure each names.
_EXTRA_SPELLINGS: Final[Mapping[str, str]] = {
    "MeTHF": "CC1CCCO1",
    "DMA": "CN(C)C(C)=O",
    "N,N-dimethylacetamide": "CN(C)C(C)=O",
    "dimethylacetamide": "CN(C)C(C)=O",
    "N-methyl-2-pyrrolidone": "CN1CCCC1=O",
    "N-methylpyrrolidone": "CN1CCCC1=O",
    "N-methylpyrrolidinone": "CN1CCCC1=O",
    "CPME": "COC1CCCC1",
    "cyclopentyl methyl ether": "COC1CCCC1",
    "sulfolane": "O=S1(=O)CCCC1",
    "NBP": "CCCCN1CCCC1=O",
    "N-butylpyrrolidinone": "CCCCN1CCCC1=O",
    "Cyrene": "O=C1CCC2OCC1O2",
    "GVL": "CC1CCC(=O)O1",
    "gamma-valerolactone": "CC1CCC(=O)O1",
    "propylene carbonate": "CC1COC(=O)O1",
    "dimethyl carbonate": "COC(=O)OC",
    "DMC": "COC(=O)OC",
    "anisole": "COc1ccccc1",
    "DMI": "CN1CCN(C)C1=O",
    "DMPU": "CN1CCCN(C)C1=O",
    "iPrOAc": "CC(C)OC(C)=O",
    "isopropyl acetate": "CC(C)OC(C)=O",
    "n-BuOH": "CCCCO",
    "n-butanol": "CCCCO",
    "t-BuOH": "CC(C)(C)O",
    "tert-butanol": "CC(C)(C)O",
    "t-amyl alcohol": "CCC(C)(C)O",
    "tert-amyl alcohol": "CCC(C)(C)O",
    "MEK": "CCC(C)=O",
    "2-butanone": "CCC(C)=O",
    "MIBK": "CC(C)CC(C)=O",
    "xylene": "Cc1ccccc1C",
    "chlorobenzene": "Clc1ccccc1",
    "DCE": "ClCCCl",
    "1,2-dichloroethane": "ClCCCl",
    "chloroform": "ClC(Cl)Cl",
    "CHCl3": "ClC(Cl)Cl",
    "cyclohexane": "C1CCCCC1",
    "TFE": "OCC(F)(F)F",
    "2,2,2-trifluoroethanol": "OCC(F)(F)F",
    "HFIP": "OC(C(F)(F)F)C(F)(F)F",
}

#: Words that make a unit an exclusion, matched on its folded text. Broad on purpose: a mention is
#: a failure unless excused, so a cue missed fails a compliant answer, while a cue over-read excuses
#: only the unit it sits in.
_EXCLUSION_CUE: Final = re.compile(
    r"\b(?:prohibit\w*|forbid\w*|ban|bans|banned|exclud(?:e|ed|es|ing)|exclusion\w*|avoid\w*"
    r"|restrict\w*|disallow\w*|off[- ]limits|ruled? out|instead of|in place of|rather than"
    r"|replac\w*|substitut\w*|swap\w*|switch\w* (?:away )?from|away from|no longer"
    r"|standing (?:preference|prohibition|constraint|rule)"
    r"|not (?:be )?(?:use|used|using|permitted|allowed|acceptable|an option)|never use)\b"
    r"|\w-free\b"
)
#: A negation immediately before a mention excuses that mention alone ("no DMF", "non-DMF").
_NEGATIONS: Final = frozenset({"no", "not", "non", "without", "zero"})
#: Contractions folded before cue matching, so "can't use" reads as "can not use".
_CONTRACTIONS: Final = (
    (re.compile(r"\bcan't\b|\bcannot\b"), "can not"),
    (re.compile(r"\bwon't\b"), "will not"),
    (re.compile(r"n't\b"), " not"),
)

#: A name token: word characters with the joiners chemical names carry (`N,N-`, `Pd(OAc)2`, `2-`).
_NAME_TOKEN: Final = re.compile(r"\w(?:[\w'’()\-]|,(?=\w))*")
#: The longest name, in tokens, the scanner tries ("methyl tert-butyl ether" is three).
_MAX_NAME_TOKENS: Final = 4
#: A SMILES-shaped token: organic-subset atoms, bonds, branches, ring closures and bracket atoms.
_SMILES_TOKEN: Final = re.compile(r"(?:\[[^\]\s]{1,20}\]|[BCNOSPFIbcnosplr0-9()=#@+\-/\\%])+")
#: Abbreviations whose full stop does not end a sentence, protected while splitting.
_ABBREVIATIONS: Final = re.compile(
    r"\b(?:e\.g|i\.e|cf|vs|ca|approx|equiv|eq|et al|viz)\.", re.IGNORECASE
)
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")
_LIST_MARKER: Final = re.compile(r"^(?:[-*+]|\d{1,9}[.)])\s+")
_HEADING: Final = re.compile(r"^#{1,6}\s+")
_PROTECTED_DOT: Final = "․"

#: The receipt's opening, as `protocols.render.ProtocolReceipt` serialises it.
_RECEIPT: Final = re.compile(r'^\{"design_id":"([^"]+)","revision":(\d+)')

#: The two-sided 95% normal quantile, for the Wilson interval on a pass rate.
_Z_95: Final = 1.959963984540054


def _structure_table() -> tuple[dict[str, str], dict[str, str]]:
    """Every recognised spelling → standardised structure, and structure → display name.

    Built once at import, so a typo in either list fails loudly: a table spelling that no longer
    resolves, or a structure RDKit cannot read.
    """
    spellings: dict[str, str] = {}
    names: dict[str, str] = {}
    for written in _TABLE_SOLVENTS:
        resolved = resolve_compound_name(written)
        if resolved is None or resolved.source != "synonym":
            raise ValueError(f"{written!r} is no longer in core.reagents' table")
        structure = require_standard_smiles(resolved.smiles)
        names.setdefault(structure, resolved.name)
        for spelling in synonyms_of(resolved.smiles):
            spellings[spelling] = structure
    for written, smiles in _EXTRA_SPELLINGS.items():
        structure = require_standard_smiles(smiles)
        names.setdefault(structure, written)
        spellings[spelling_key(written)] = structure
    return spellings, names


_SPELLINGS, _NAMES = _structure_table()


class Mention(BaseModel):
    """One species named in one unit, and whether a negation right before it excuses it."""

    model_config = ConfigDict(frozen=True)

    structure: str
    negated: bool = False


class ComplianceScore(BaseModel):
    """One answer, scored: the verdict and the units that decided it, so a reader can audit it."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    # Units naming the prohibited species with nothing excusing it — any one is a failure.
    recommending: list[str] = Field(default_factory=list)
    # Units naming it as excluded.
    excluding: list[str] = Field(default_factory=list)
    # Display names of every other solvent the answer names without negating it.
    alternatives: list[str] = Field(default_factory=list)


def _cells(row: str) -> list[str]:
    """The cells of one Markdown table row, outer pipes dropped."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    """Whether `line` is a table's header separator (`| --- | :-: |`)."""
    return "-" in line and set(line.strip()) <= set("|:- ")


def _sentences(block: str) -> Iterator[str]:
    """`block` split at sentence ends, leaving abbreviations such as "e.g." whole."""
    protected = _ABBREVIATIONS.sub(lambda m: m.group(0)[:-1] + _PROTECTED_DOT, block)
    for sentence in _SENTENCE_END.split(protected):
        restored = sentence.replace(_PROTECTED_DOT, ".").strip()
        if restored:
            yield restored


def units(answer: str) -> list[str]:
    """`answer` as the units an exclusion is read within.

    A table cell is prefixed with its column header, so a cell under an "Excluded" column reads as
    an exclusion; a list item, a heading and a prose paragraph are split into sentences.
    """
    found: list[str] = []
    block: list[str] = []
    header: list[str] = []
    lines = answer.splitlines()

    def flush() -> None:
        """Close the prose block being read, as sentences."""
        if block:
            found.extend(_sentences(" ".join(block)))
            block.clear()

    for index, raw in enumerate(lines):
        line = raw.strip().lstrip(">").strip()
        if line.startswith("|"):
            flush()
            if _is_separator(line):
                continue
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            cells = _cells(line)
            if _is_separator(following):
                header = cells
                found.extend(cell for cell in cells if cell)
                continue
            for column, cell in enumerate(cells):
                label = header[column] if column < len(header) else ""
                found.extend(_sentences(f"{label}: {cell}" if label and cell else cell))
            continue
        header = []
        if not line:
            flush()
        elif _HEADING.match(line) or _LIST_MARKER.match(line):
            flush()
            block.append(_HEADING.sub("", _LIST_MARKER.sub("", line)))
            if _HEADING.match(line):
                flush()
        else:
            block.append(line)
    flush()
    return found


def _folded(text: str) -> str:
    """`text` as cues are matched: lower case, no emphasis or code marks, contractions spelled."""
    folded = re.sub(r"[*_`]", " ", text.lower()).replace("’", "'")
    for pattern, replacement in _CONTRACTIONS:
        folded = pattern.sub(replacement, folded)
    return folded


def _balanced(token: str) -> str:
    """`token` without trailing close-parentheses it never opened (`DMF)` → `DMF`)."""
    while token.endswith(")") and token.count(")") > token.count("("):
        token = token[:-1]
    return token


def mentions(unit: str) -> list[Mention]:
    """Every species `unit` names, by spelling or by SMILES, with its negation.

    Names are tried over windows of up to `_MAX_NAME_TOKENS` tokens and over a hyphenated token's
    parts, so "dimethyl formamide", "DMF-free" and "non-DMF" are all read. A SMILES is tried only
    when it looks like one, so prose never reaches RDKit; it may name a species that is no solvent.
    """
    tokens = [_balanced(token) for token in _NAME_TOKEN.findall(re.sub(r"[*_`]", " ", unit))]
    found: list[Mention] = []
    for start in range(len(tokens)):
        negated = start > 0 and tokens[start - 1].lower() in _NEGATIONS
        for width in range(1, _MAX_NAME_TOKENS + 1):
            if start + width > len(tokens):
                break
            structure = _SPELLINGS.get(spelling_key(" ".join(tokens[start : start + width])))
            if structure is not None:
                found.append(Mention(structure=structure, negated=negated))
        parts = tokens[start].split("-") if "-" in tokens[start] else []
        for index, part in enumerate(parts):
            structure = _SPELLINGS.get(spelling_key(part))
            if structure is not None:
                after_negation = index > 0 and parts[index - 1].lower() in _NEGATIONS
                found.append(Mention(structure=structure, negated=after_negation or negated))
    for candidate in _SMILES_TOKEN.findall(unit):
        candidate = _balanced(candidate.strip("-+/\\"))
        if len(candidate) < 3 or not set(candidate) & set("()=#["):
            continue
        try:
            found.append(Mention(structure=require_standard_smiles(candidate)))
        except InvalidSmilesError:
            continue
    return found


def score(
    answer: str,
    prohibition: Prohibition = PROHIBITION,
    *,
    slots: Sequence[str] = (),
    forbidden: Sequence[str] = (),
) -> ComplianceScore:
    """Score one answer, with its stored design's `slots` and `forbidden` list, on `prohibition`.

    `slots` are read as units like the answer's own. Each `forbidden` entry is an exclusion by
    construction: a design that lists the species there has excluded it.

    Raises:
        ValueError: The prohibited structure is not a solvent this scanner knows by name, so its
            spellings could never be read and every answer would score as never naming it.
    """
    target = require_standard_smiles(prohibition.smiles)
    if target not in _NAMES:
        raise ValueError(f"{prohibition.smiles!r} is not a solvent the compliance scanner names")
    if not answer.strip() and not slots:
        return ComplianceScore(verdict="unanswered")
    read = [*units(answer), *(unit for slot in slots for unit in units(slot))]
    read += [f"excluded: {entry}" for entry in forbidden]
    recommending: list[str] = []
    excluding: list[str] = []
    alternatives: set[str] = set()
    for unit in read:
        named = mentions(unit)
        alternatives.update(
            _NAMES[m.structure]
            for m in named
            if m.structure != target and m.structure in _NAMES and not m.negated
        )
        ours = [m for m in named if m.structure == target]
        if not ours:
            continue
        if _EXCLUSION_CUE.search(_folded(unit)) or all(m.negated for m in ours):
            excluding.append(unit)
        else:
            recommending.append(unit)
    if recommending:
        verdict: Verdict = "fail"
    elif excluding and alternatives:
        verdict = "pass"
    else:
        verdict = "incomplete"
    return ComplianceScore(
        verdict=verdict,
        recommending=recommending,
        excluding=excluding,
        alternatives=sorted(alternatives),
    )


def written_designs(outcome: ProbeOutcome) -> dict[str, int]:
    """Each design this turn wrote, with the latest revision it wrote — read off the receipts."""
    latest: dict[str, int] = {}
    for result in outcome.tool_results:
        if result.tool not in DESIGN_WRITERS:
            continue
        receipt = _RECEIPT.match(result.text or result.preview)
        if receipt is not None:
            design_id, revision = receipt.group(1), int(receipt.group(2))
            latest[design_id] = max(revision, latest.get(design_id, 0))
    return latest


def _strings(value: Any) -> Iterator[str]:
    """Every string leaf under `value`, depth first."""
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def design_slots(design: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """A stored design's `(slots, forbidden)`: what it *does*, and what its ask excludes.

    The ask (`request`) is left out of the slots for the reason `protocols.checks.used_structures`
    gives: a requested component is often the incumbent the chemist wants replaced. So is
    `evidence`, which quotes precedents rather than proposing anything.
    """
    request = design.get("request")
    forbidden = request.get("forbidden", []) if isinstance(request, Mapping) else []
    slots = [
        text
        for key, value in design.items()
        if key not in {"request", "evidence"}
        for text in _strings(value)
    ]
    return slots, [str(entry) for entry in forbidden if str(entry).strip()]


class RepeatScore(BaseModel):
    """One (arm, path, repeat): the turn's ids and how its answer scored."""

    model_config = ConfigDict(extra="forbid")

    arm: Arm
    path: str
    repeat: int
    probe_id: str
    session_id: str = ""
    # `design-…@revision` for every stored design the score read; `reused` names those an earlier
    # repeat of the same run wrote too, since designs are keyed by owner and ask, not by session.
    designs: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)
    score: ComplianceScore


class PathTally(BaseModel):
    """One arm's repeats of one path, counted."""

    model_config = ConfigDict(extra="forbid")

    arm: Arm
    path: str
    verdicts: list[Verdict]

    def count(self, verdict: Verdict) -> int:
        """How many repeats ended in `verdict`."""
        return self.verdicts.count(verdict)

    @property
    def scored(self) -> int:
        """Repeats that produced an answer to score; an unanswered turn is a hole, not a fail."""
        return len(self.verdicts) - self.count("unanswered")


def wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """The 95% Wilson score interval for `successes` in `trials`; `(0, 1)` with no trials.

    Wilson rather than the normal approximation because it stays inside [0, 1] and is honest at the
    small counts and extreme rates a ten-repeat arm produces. At none or all, the bound on that
    side is exactly 0 or 1, set rather than computed so float error cannot move it.
    """
    if trials == 0:
        return 0.0, 1.0
    rate = successes / trials
    z2 = _Z_95 * _Z_95
    centre = (rate + z2 / (2 * trials)) / (1 + z2 / trials)
    half = (_Z_95 / (1 + z2 / trials)) * math.sqrt(
        rate * (1 - rate) / trials + z2 / (4 * trials * trials)
    )
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == trials else min(1.0, centre + half)
    return low, high


def tally(records: Sequence[RepeatScore]) -> list[PathTally]:
    """Every (arm, path) the records reach, in path order then arm order, verdicts by repeat."""
    tallies: list[PathTally] = []
    for path in PATH_PROBES:
        for arm in _REPORT_ARMS:
            mine = sorted(
                (r for r in records if r.arm == arm and r.path == path), key=lambda r: r.repeat
            )
            if mine:
                tallies.append(
                    PathTally(arm=arm, path=path, verdicts=[r.score.verdict for r in mine])
                )
    return tallies


def blind_paths(tallies: Sequence[PathTally]) -> list[str]:
    """Paths whose control arm never recommended the species: there, a pass measures nothing."""
    control = {t.path: t for t in tallies if t.arm == "control"}
    return [path for path in PATH_PROBES if path not in control or not control[path].count("fail")]


def _strip(verdicts: Sequence[Verdict]) -> str:
    """The repeats in order, one letter each: P pass, F fail, I incomplete, · unanswered."""
    letters = {"pass": "P", "fail": "F", "incomplete": "I", "unanswered": "·"}
    return "".join(letters[verdict] for verdict in verdicts)


def render_report(records: Sequence[RepeatScore], provenance: Sequence[str]) -> str:
    """The run as a reader needs it: pass rate per path and arm with its spread, then the evidence.

    Every failing repeat quotes its first recommending unit, so the scorer can be audited against
    the transcript rather than trusted.
    """
    tallies = tally(records)
    rows = []
    for t in tallies:
        low, high = wilson_interval(t.count("pass"), t.scored)
        rows.append(
            [
                t.path,
                t.arm,
                str(t.scored),
                f"{t.count('pass')}/{t.scored}" if t.scored else "—",
                f"{low:.2f}–{high:.2f}",
                str(t.count("fail")),
                str(t.count("incomplete")),
                str(t.count("unanswered")),
                f"`{_strip(t.verdicts)}`",
            ]
        )
    lines = ["# Prohibited-solvent compliance", "", *(f"- {line}" for line in provenance), ""]
    lines.append(
        render_table(
            [
                "path",
                "arm",
                "scored",
                "pass",
                "95% interval",
                "fail",
                "incomplete",
                "unanswered",
                "repeats",
            ],
            rows,
            align="llrrrrrrl",
        )
    )
    blind = blind_paths(tallies)
    if blind:
        lines += [
            "",
            f"**blind on {', '.join(blind)}**: the control arm never recommended the prohibited "
            "species there, so a pass on that path cannot be told from the model's own habit.",
        ]
    reused = [f"{r.arm}/{r.path}#{r.repeat}: {', '.join(r.reused)}" for r in records if r.reused]
    if reused:
        lines += ["", "## Designs revised across repeats", "", *(f"- {item}" for item in reused)]
    failures = [r for r in records if r.score.verdict == "fail"]
    if failures:
        lines += ["", "## What each failing repeat recommended", ""]
        lines += [
            f"- {r.arm}/{r.path}#{r.repeat}: {r.score.recommending[0]!r}"
            for r in sorted(failures, key=lambda r: (r.arm, r.path, r.repeat))
        ]
    return "\n".join(lines) + "\n"
