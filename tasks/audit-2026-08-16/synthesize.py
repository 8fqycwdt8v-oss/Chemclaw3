"""Aggregate the audit's verdict files into one survivor table.

The audit's rule is that a finding survives only if **neither** refuter lens refuted it, and that
its agreed severity is the **lower** of the two lenses — one sceptic can de-escalate, neither can
escalate. That rule has to be applied mechanically rather than by reading, because the whole point
of the two-lens design is that the aggregation is not a judgement call.

Reads `verdicts/*.md`, which are written by the refuters in a fixed section format, and reports:
  * every finding with a verdict, its per-lens verdicts, and the agreed severity
  * which findings have only ONE lens reporting (not yet decided — never treat as survivors)
  * which crit/high findings in `findings/round1/` have NO verdict at all (the coverage gap)

Run: `uv run python tasks/audit-2026-08-16/synthesize.py`
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERDICTS = ROOT / "verdicts"
FINDINGS = ROOT / "findings" / "round1"

RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

_TITLE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.M)
_VERDICT = re.compile(r"^-\s+\*\*Verdict\*\*:\s*(?P<v>CONFIRMED|OVERSTATED|REFUTED|UNPROVEN)", re.M)
_SEV = re.compile(r"^-\s+\*\*Severity I would assign\*\*:\s*(?P<s>critical|high|medium|low)", re.M)
_FIND_SEV = re.compile(r"^-\s+\*\*Severity\*\*:\s*(?P<s>critical|high|medium|low)", re.M | re.I)


def _sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown file into (heading, body) pairs on `## ` headings."""
    marks = list(_TITLE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group("title"), text[m.end() : end]))
    return out


def _norm(title: str) -> str:
    """Titles are re-typed by each verifier, so compare on a loose key rather than verbatim."""
    t = title.lower()
    t = re.sub(r"[`*_]", "", t)
    t = re.sub(r"^\d+\.\s*", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split()[:12])


def collect_verdicts() -> dict[str, dict]:
    """finding key -> {title, lenses: {lens: (verdict, severity)}, files: [...]}"""
    acc: dict[str, dict] = defaultdict(lambda: {"title": "", "lenses": {}, "files": []})
    for path in sorted(VERDICTS.glob("*.md")):
        stem = path.stem
        lens = "repro" if stem.endswith("--repro") else (
            "reachability" if stem.endswith("--reachability") else "unknown"
        )
        for title, body in _sections(path.read_text(encoding="utf-8", errors="replace")):
            v = _VERDICT.search(body)
            if not v:
                continue  # a prose section such as "The deployment question, settled first"
            s = _SEV.search(body)
            key = _norm(title)
            e = acc[key]
            e["title"] = e["title"] or title
            e["lenses"][lens] = (v.group("v"), s.group("s") if s else "?")
            e["files"].append(path.name)
    return acc


def collect_filed_crit_high() -> dict[str, tuple[str, str]]:
    """finding key -> (title, filed severity) for every crit/high finding filed in round 1."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted(FINDINGS.glob("*.md")):
        for title, body in _sections(path.read_text(encoding="utf-8", errors="replace")):
            m = _FIND_SEV.search(body)
            if m and m.group("s").lower() in ("critical", "high"):
                out[_norm(title)] = (title, m.group("s").lower())
    return out


def main() -> int:
    acc = collect_verdicts()
    filed = collect_filed_crit_high()

    survivors, killed, undecided = [], [], []
    for key, e in acc.items():
        verdicts = [v for v, _ in e["lenses"].values()]
        sevs = [s for _, s in e["lenses"].values() if s in RANK]
        agreed = min(sevs, key=lambda s: RANK[s]) if sevs else "?"
        row = (agreed, e["title"], e["lenses"])
        if "REFUTED" in verdicts:
            killed.append(row)
        elif len(e["lenses"]) < 2:
            undecided.append(row)
        else:
            survivors.append(row)

    survivors.sort(key=lambda r: -RANK.get(r[0], -1))
    killed.sort(key=lambda r: -RANK.get(r[0], -1))
    undecided.sort(key=lambda r: -RANK.get(r[0], -1))

    print(f"verdict files read: {len(list(VERDICTS.glob('*.md')))}")
    print(f"findings with >=1 verdict: {len(acc)}\n")

    print(f"=== SURVIVORS ({len(survivors)}) — neither lens refuted; severity is the lower of the two")
    for sev, title, lenses in survivors:
        marks = " ".join(f"{k[:5]}={v}" for k, (v, _) in sorted(lenses.items()))
        print(f"  [{sev:8s}] {title[:95]}")
        print(f"             {marks}")

    print(f"\n=== KILLED ({len(killed)}) — at least one lens refuted")
    for sev, title, lenses in killed:
        marks = " ".join(f"{k[:5]}={v}" for k, (v, _) in sorted(lenses.items()))
        print(f"  [{sev:8s}] {title[:95]}  ({marks})")

    print(f"\n=== UNDECIDED ({len(undecided)}) — only one lens has reported; NOT survivors yet")
    for sev, title, lenses in undecided:
        marks = " ".join(f"{k[:5]}={v}" for k, (v, _) in sorted(lenses.items()))
        print(f"  [{sev:8s}] {title[:95]}  ({marks})")

    unverified = {k: v for k, v in filed.items() if k not in acc}
    print(f"\n=== FILED crit/high WITH NO VERDICT ({len(unverified)}) — the coverage gap")
    for _key, (title, sev) in sorted(unverified.items(), key=lambda kv: -RANK[kv[1][1]]):
        print(f"  [{sev:8s}] {title[:95]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
