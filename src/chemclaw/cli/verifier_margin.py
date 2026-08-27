"""Measure the judge's roll-to-roll margin at the review threshold.

The DEFERRED row this exists to close: re-scoring 39 unchanged answers cleared 5.1% of flags per
roll (D-2026-08-16's null control), so a `review_required` flip can mean the judge rolled again
rather than that anything changed — and the hysteresis band that would absorb it had "a magic
number" for a width until somebody re-rolled the judge and measured the spread. This is the
re-roll.

What is measured is exactly the call the turn makes: `agent.verifier.judge_once` — one structured
scoring roll, no band, no degrade — repeated `--rolls` times per (answer, evidence) pair. The band
must not be in the loop, because the band is the thing the measurement sizes.

The corpus is generated over the repository's **own knowledge notes**: each pair takes a real note
body as its evidence and asks the same model for an answer in one of three classes — grounded,
drifting (one specific number the evidence does not carry) and contradicted — because those are
the flag classes the 2026-08-16 run observed on live answers. Stated plainly so nobody over-reads
the number: this measures the judge's *stability per answer* (the spread of repeated rolls on one
input, which is what a band width is made of), not the deployment's *distribution* of answers near
the threshold (how often the band is entered — that needs a deployment's own answers, and re-running
this command against a `--pairs` file of them is the standing way to re-fit).

Needs a model credential; refuses without one rather than measuring a mock. The output is JSON on
stdout — per-pair rolls plus the summary the ADR cites — so the artifact a decision rests on is
the run's own record rather than prose about it.
"""

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.verifier import judge_once
from chemclaw.core.config import settings
from chemclaw.retrieval.evidence import EvidenceChunk

# The three answer classes, named after what the 2026-08-16 live run actually flagged. Each prompt
# asks for the *answer only*, so the generated text is prose over the evidence rather than a
# meta-discussion of the task.
_CLASSES: dict[str, str] = {
    "grounded": (
        "Write a 3-5 sentence answer to a colleague's question about the topic below, using ONLY "
        "facts stated in the evidence, and citing the note id in [[...]] form after each factual "
        "claim. Do not add any number, name or claim the evidence does not state. Reply with the "
        "answer text only."
    ),
    "drifting": (
        "Write a 3-5 sentence answer to a colleague's question about the topic below, mostly using "
        "facts stated in the evidence and citing the note id in [[...]] form — but include exactly "
        "one specific numeric value (a temperature, a yield, a concentration) that the evidence "
        "does NOT state, presented as fact with the same citation. Reply with the answer text only."
    ),
    "contradicted": (
        "Write a 3-5 sentence answer to a colleague's question about the topic below that cites "
        "the note id in [[...]] form but asserts, as its central claim, the OPPOSITE of something "
        "the evidence states. Reply with the answer text only."
    ),
}


@dataclass
class Pair:
    """One (answer, evidence) input to the judge, with the class it was generated as."""

    label: str
    answer_class: str
    answer: str
    evidence: list[EvidenceChunk]


def _note_evidence(limit: int) -> list[tuple[str, EvidenceChunk]]:
    """Real note bodies as evidence chunks, oldest-path-first for a stable corpus."""
    notes = sorted(p for p in Path(settings.knowledge_path).rglob("*.md") if p.stem != "README")
    out: list[tuple[str, EvidenceChunk]] = []
    for path in notes:
        body = path.read_text(encoding="utf-8").strip()
        # A stub note grounds nothing worth judging; the floor keeps the corpus at real bodies.
        if len(body) < 400:
            continue
        note_id = path.stem
        out.append(
            (
                note_id,
                EvidenceChunk(
                    content=body[:4000], source_note_id=note_id, retriever="verifier-margin"
                ),
            )
        )
        if len(out) >= limit:
            break
    return out


async def _generate_pairs(client: Any, per_class: int) -> list[Pair]:
    """Ask the model for one answer per (note, class) until each class has `per_class` pairs."""
    notes = _note_evidence(per_class)
    if len(notes) < per_class:
        raise SystemExit(
            f"only {len(notes)} usable notes under {settings.knowledge_path}; asked for {per_class}"
        )
    pairs: list[Pair] = []
    for answer_class, instruction in _CLASSES.items():
        for note_id, chunk in notes:
            prompt = f"{instruction}\n\nEvidence note id: {note_id}\nEvidence:\n{chunk.content}\n"
            reply = await client.ainvoke(prompt)
            answer = getattr(reply, "content", None)
            text = answer if isinstance(answer, str) else str(answer)
            pairs.append(
                Pair(
                    label=f"{answer_class}:{note_id}",
                    answer_class=answer_class,
                    answer=text.strip(),
                    evidence=[chunk],
                )
            )
    return pairs


async def _roll(client: Any, pairs: list[Pair], rolls: int) -> dict[str, Any]:
    """Roll the raw judge `rolls` times per pair and reduce to the numbers the band needs."""
    threshold = settings.verifier_confidence_threshold
    results: list[dict[str, Any]] = []
    for pair in pairs:
        confidences: list[float] = []
        failed = 0
        for _ in range(rolls):
            try:
                verdict = await judge_once(pair.answer, pair.evidence, client=client)
                confidences.append(verdict.confidence)
            except Exception:
                failed += 1
        entry: dict[str, Any] = {
            "label": pair.label,
            "class": pair.answer_class,
            "rolls": confidences,
            "failed_rolls": failed,
        }
        if confidences:
            median = statistics.median(confidences)
            entry["median"] = median
            entry["spread"] = max(confidences) - min(confidences)
            entry["max_dev_from_median"] = max(abs(c - median) for c in confidences)
            entry["flips"] = _flips(confidences, threshold)
        results.append(entry)
        print(
            f"{pair.label}: rolls={[f'{c:.2f}' for c in confidences]} "
            f"spread={entry.get('spread', 'n/a')}",
            file=sys.stderr,
        )
    return {"threshold": threshold, "pairs": results, "summary": _summary(results, threshold)}


def _flips(confidences: list[float], threshold: float) -> int:
    """How many rolls disagree with the majority about which side of the threshold this is."""
    below = sum(1 for c in confidences if c < threshold)
    return min(below, len(confidences) - below)


def _summary(results: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    """The two numbers the band is sized from, plus the honesty counters around them.

    `recommended_band` is the width that would have absorbed every observed roll of every pair
    whose median sits near the threshold (within 0.25): the max deviation-from-median there,
    rounded up to 0.05. Pairs far from the threshold do not size the band — the DEFERRED row's own
    finding is that the judge is stable there — but their spread is reported so that claim stays
    re-checkable.
    """
    scored = [r for r in results if r.get("rolls")]
    near = [r for r in scored if abs(r["median"] - threshold) <= 0.25]
    far = [r for r in scored if abs(r["median"] - threshold) > 0.25]
    deviations = [r["max_dev_from_median"] for r in near]
    raw = max(deviations, default=0.0)
    return {
        "pairs_scored": len(scored),
        "pairs_near_threshold": len(near),
        "flip_rate_near_threshold": (
            sum(r["flips"] for r in near) / sum(len(r["rolls"]) for r in near) if near else None
        ),
        "max_spread_far_from_threshold": max((r["spread"] for r in far), default=0.0),
        "max_dev_from_median_near_threshold": raw,
        "recommended_band": round(-(-raw // 0.05) * 0.05, 2) if raw else 0.0,
    }


def main() -> None:
    """Generate (or load) the pair corpus, roll the raw judge, and print the measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rolls", type=int, default=4, help="judge rolls per pair (default 4)")
    parser.add_argument(
        "--per-class", type=int, default=8, help="pairs per answer class when generating"
    )
    parser.add_argument(
        "--pairs",
        type=Path,
        default=None,
        help="JSON file of pairs to re-roll instead of generating (the re-fit path: a list of "
        "{label, class, answer, evidence: [{content, source_note_id}]})",
    )
    args = parser.parse_args()

    async def _run() -> None:
        client = build_chat_model("verifier")
        if args.pairs is not None:
            raw = json.loads(args.pairs.read_text(encoding="utf-8"))
            pairs = [
                Pair(
                    label=str(entry["label"]),
                    answer_class=str(entry.get("class", "stored")),
                    answer=str(entry["answer"]),
                    evidence=[
                        EvidenceChunk(
                            content=str(chunk["content"]),
                            source_note_id=str(chunk["source_note_id"]),
                            retriever="verifier-margin",
                        )
                        for chunk in entry["evidence"]
                    ],
                )
                for entry in raw
            ]
        else:
            pairs = await _generate_pairs(client, args.per_class)
        report = await _roll(client, pairs, args.rolls)
        report["generated"] = {
            "per_class": args.per_class if args.pairs is None else None,
            "pairs_file": str(args.pairs) if args.pairs is not None else None,
            "rolls": args.rolls,
            "judge_model": settings.model_routes.get("verifier", settings.llm_model),
            "provider": settings.llm_provider,
        }
        # The pairs themselves ride along so the run is repeatable against the same corpus.
        report["corpus"] = [
            {
                "label": p.label,
                "class": p.answer_class,
                "answer": p.answer,
                "evidence": [
                    {"content": c.content, "source_note_id": c.source_note_id} for c in p.evidence
                ],
            }
            for p in pairs
        ]
        print(json.dumps(report, indent=2))

    asyncio.run(_run())


if __name__ == "__main__":
    main()
