"""What recorded answers are made of: tables, document-shaped prose and structure lists.

Why this exists: the artefacts decision
(`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`) had to know what answers spend
their tokens on before deciding what an artefact holds, and one of its two `Revisit when:` lines is
a re-run of exactly this measurement over a corpus recorded with artefacts on. A trigger nobody can
re-run is a sentence, so the measurement is a module rather than a script in a scratch directory.

It reads the live-probe outcomes `evals/live.py` writes (`{"probe": ..., "outcome": ...}` JSON, one
per probe) and reports, per directory:

- answers carrying a Markdown table, and carrying one of at least `min_rows` data rows;
- the tables' share of answer tokens, counted with `count_tokens_approximately` — the counter
  `tests/test_context_floor.py` and compaction use, so the figures compare with the prefix's;
- of the figures stated inside tables, how many are verbatim tool values. **Only where the run
  recorded `verified_numbers`**, and a figure absent from that list is *unchecked*, never wrong:
  `_verified_numbers` measured "numbers no tool returned" at precision zero, so the complement is
  deliberately not reported;
- document-shaped answers — at least `document_tokens` tokens under at least two Markdown headings;
- answers listing at least `min_structures` distinct structures as backticked SMILES (an RDKit
  parse with at least `min_structures` heavy atoms, so `CO` or a code word does not count);
- how often `render_structure` ran.

Run: `python -m chemclaw.evals.answer_shape tasks/live-test*/ ...` (directories are searched
recursively; each directory holding outcomes is one row).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages.utils import count_tokens_approximately

from chemclaw.core.chem import InvalidSmilesError, element_counts, require_canonical_smiles
from chemclaw.core.quantities import stated_numerals

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CODE_SPAN = re.compile(r"`([^`\s]{3,200})`")
_HEADING = re.compile(r"^#{1,4} \S", re.MULTILINE)


@dataclass(frozen=True)
class Thresholds:
    """What counts as a big table, a document and a structure list.

    Parameters of the measurement rather than settings of the system: nothing at runtime reads
    them, and a re-run that changes one says so on its command line.
    """

    min_rows: int = 4
    document_tokens: int = 600
    min_structures: int = 3


#: The thresholds the decision's figures were measured at.
DEFAULT_THRESHOLDS = Thresholds()


def tables(answer: str) -> list[list[str]]:
    """Every GitHub-flavoured Markdown table in `answer`, as its lines (header, separator, rows)."""
    found: list[list[str]] = []
    block: list[str] = []
    for line in [*answer.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            block.append(line)
            continue
        if len(block) >= 2 and _SEPARATOR.match(block[1]):
            found.append(block)
        block = []
    return found


def tokens(text: str) -> int:
    """Tokens in one assistant message, as the context-floor ratchet counts them."""
    return int(count_tokens_approximately([AIMessage(text)])) if text else 0


def structures(answer: str, min_atoms: int) -> set[str]:
    """Distinct canonical SMILES the answer writes as code spans of at least `min_atoms` atoms.

    Parsed through `core.chem`'s strict helpers rather than RDKit directly: evals is not a layer
    that may import RDKit (`tests/test_third_party_layering.py`), and the strict form refuses the
    truncating parses (`"CCO junk"` as ethanol) a bare parser would count as a structure.
    """
    seen: set[str] = set()
    for span in _CODE_SPAN.findall(answer):
        try:
            key = require_canonical_smiles(span)
            heavy = sum(n for element, n in element_counts(span).items() if element != "H")
        except InvalidSmilesError:
            continue
        if heavy >= min_atoms:
            seen.add(key)
    return seen


def measure(
    outcomes: Iterable[dict[str, Any]], limits: Thresholds = DEFAULT_THRESHOLDS
) -> Counter[str]:
    """The counts behind every reported figure, over one set of recorded outcomes."""
    c: Counter[str] = Counter()
    for o in outcomes:
        answer = o.get("answer") or ""
        if not answer:
            continue
        c["answers"] += 1
        answer_tokens = tokens(answer)
        c["answer_tokens"] += answer_tokens
        found = tables(answer)
        c["with_table"] += bool(found)
        c["with_big_table"] += any(len(t) - 2 >= limits.min_rows for t in found)
        recorded = "verified_numbers" in o
        verified = set(o.get("verified_numbers") or [])
        for table in found:
            c["table_tokens"] += tokens("\n".join(table))
            c["table_rows"] += len(table) - 2
            for numeral in (n for row in table[2:] for n in stated_numerals(row)):
                c["table_figures"] += 1
                c["table_figures_checked"] += recorded
                c["table_figures_verified"] += recorded and numeral in verified
        c["document_shaped"] += (
            answer_tokens >= limits.document_tokens and len(_HEADING.findall(answer)) >= 2
        )
        c["with_structure_list"] += (
            len(structures(answer, limits.min_structures)) >= limits.min_structures
        )
        called = o.get("tools_called") or []
        c["tool_calls"] += len(called)
        c["render_structure_calls"] += called.count("render_structure")
    return c


def outcomes_in(directory: Path) -> list[dict[str, Any]]:
    """The recorded outcomes directly in `directory`, skipping JSON that is not one."""
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("outcome"), dict):
            found.append(data["outcome"])
    return found


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


HEADER = (
    "| set | answers | with a table | with a table >= min rows | table share of answer tokens "
    "| figures in tables | checked → verbatim tool values | listing >= min structures "
    "| document-shaped | render_structure / tool calls |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)


def row(name: str, c: Counter[str]) -> str:
    """One Markdown table row of `HEADER`."""
    n = c["answers"]
    return (
        f"| {name} | {n} | {c['with_table']} ({_pct(c['with_table'], n)}) "
        f"| {c['with_big_table']} ({_pct(c['with_big_table'], n)}) "
        f"| {_pct(c['table_tokens'], c['answer_tokens'])} | {c['table_figures']} "
        f"| {c['table_figures_checked']} → "
        f"{_pct(c['table_figures_verified'], c['table_figures_checked'])} "
        f"| {c['with_structure_list']} | {c['document_shaped']} ({_pct(c['document_shaped'], n)}) "
        f"| {c['render_structure_calls']} / {c['tool_calls']} |"
    )


def report(roots: Sequence[Path], limits: Thresholds = DEFAULT_THRESHOLDS) -> str:
    """The per-directory and overall table for every outcome under `roots`."""
    directories = sorted({p.parent for root in roots for p in root.rglob("*.json")})
    lines = [HEADER]
    total: Counter[str] = Counter()
    for directory in directories:
        counts = measure(outcomes_in(directory), limits)
        if counts["answers"]:
            total.update(counts)
            lines.append(row(str(directory), counts))
    lines.append(row("**all**", total))
    with_table = max(total["with_table"], 1)
    lines.append("")
    lines.append(f"table tokens per table-bearing answer: {total['table_tokens'] / with_table:.0f}")
    lines.append(f"table rows per table-bearing answer: {total['table_rows'] / with_table:.1f}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the report for the directories named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("roots", nargs="+", type=Path)
    defaults = DEFAULT_THRESHOLDS
    parser.add_argument("--min-rows", type=int, default=defaults.min_rows)
    parser.add_argument("--document-tokens", type=int, default=defaults.document_tokens)
    parser.add_argument("--min-structures", type=int, default=defaults.min_structures)
    args = parser.parse_args(argv)
    limits = Thresholds(args.min_rows, args.document_tokens, args.min_structures)
    print(report(args.roots, limits))
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI entry
    sys.exit(main())
