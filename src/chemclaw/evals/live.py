"""Ask a running ChemClaw3 real questions over the real front door, and record what it did.

This is the eval the rest of `chemclaw.evals` cannot be. Every other behaviour test in this
repository drives a *scripted* chat client — `chemclaw.evals.autonomy` says so in its own module
docstring — so it gates the harness around the model and never the model's judgement. `AG-13` in
`docs/planning/DEFERRED.md` names the gap exactly: a faithful behaviour eval has to run against a
real LLM, because a mock LLM tests only the mock. This module is that runner.

**Why the HTTP/SSE front door and not `build_agent()` in-process.** The in-process agent skips
identity, authorization, budget admission, the audit sink, the durable session store and the
streaming assembler that reconstructs tool calls from name-first fragments. Three of the five
defects the fifty-question live pass found lived in exactly that layer
(`docs/archive/vibe-test-2026-07.md`): tool-call events that carried no arguments, a failing tool
that was invisible to the asker, and a turn that ended mid-sentence. An eval that bypasses the
layer where the defects live is an eval that cannot find them.

**What it records.** One transcript per probe holding the whole event stream, because a finding
has to be reproducible from disk rather than from a claim about what was seen. The scoring split
is deliberate: everything that can be decided from the event stream is decided there, and only
the question "did this answer actually serve the asker" goes to a judge. A mechanical signal
cannot be argued with, and it is what makes "the model never called the tool that exists" an
observation instead of an opinion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.quantities import is_rounding_of, stated_numerals
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.kg.note import cited_ids

logger = logging.getLogger(__name__)

# Citations are extracted with `chemclaw.kg.note.cited_ids` — the same function the note schema and
# the answer verifier use — never a private regex. A stricter local copy reported a clean citation
# record for an answer whose nine `[[**id**]]` links were every one of them dangling: the production
# pattern matched them as targets containing `*`, the local one matched nothing, and "cites nothing"
# scored identically to "every citation grounded". Two readers for one syntax is how a gate comes to
# disagree with the thing it gates. `cited_ids` also strips a typed edge down to its target, so
# `[[evidence-for:x]]` and `[[x]]` are one citation of `x`.


class ToolResult(BaseModel):
    """One tool result as it appeared on the stream: which tool, and what it returned.

    Kept on the outcome because the judge needs it. Passing tool *names* alone made a grader
    unable to tell a number quoted from a merged note from one invented whole, and it called
    verbatim quotations "fabricated" at a 40% rate on one slice. The preview is truncated by the
    front door's own UI budget, so absence here is weak evidence of invention — which is exactly
    what the judge is told.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    preview: str = ""


class ProbeOutcome(BaseModel):
    """Everything one probe produced, mechanically derived from its event stream.

    `answered` and `failed_loudly` are kept apart because their combination is the finding. An
    unanswered turn with a `tool_failed` or `error` event is a system that broke *visibly*, which
    a user can act on; an unanswered turn with neither is the silent death that the last live pass
    found and that no passing test could see.
    """

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    section: int
    persona: str
    bucket: str
    question: str
    answer: str = ""
    answered: bool = False
    tools_called: list[str] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    tools_failed: list[str] = Field(default_factory=list)
    expected_tools_met: bool | None = None
    # Note ids the answer cites that no tool result ever returned. The highest-severity signal in
    # the run: a citation that resolves to nothing is worse than no citation, because it reads as
    # evidence.
    uncited_note_ids: list[str] = Field(default_factory=list)
    # Figures the answer states that a tool in this turn really returned, as the answer wrote them.
    # A whitelist, deliberately, and `_verified_numbers` argues why the blacklist this looks like
    # the inverse of was measured and dropped.
    verified_numbers: list[str] = Field(default_factory=list)
    failed_loudly: bool = False
    error_code: str | None = None
    degraded: list[str] = Field(default_factory=list)
    jobs_started: list[str] = Field(default_factory=list)
    notes_proposed: list[str] = Field(default_factory=list)
    asked_clarifying: bool = False
    # The same act down the other path: the turn ended on a question written as prose rather than
    # raised through `ask_clarifying_question`. Counted separately, not folded in, because the
    # difference is the finding — a live run had 3 turns on the tool and 10 in prose, so a single
    # flag reported a third of the clarifying the system was actually doing, and every metric built
    # on it was wrong in that one direction (`docs/archive/live-grounded-2026-08-03.md`).
    asked_clarifying_in_prose: bool = False
    latency_seconds: float = 0.0
    event_counts: dict[str, int] = Field(default_factory=dict)
    transport_error: str | None = None


def load_probes(probe_dir: str | None = None) -> list[Probe]:
    """Every probe under `probe_dir`, id-checked across files.

    Duplicate ids are fatal rather than deduplicated: two probes sharing an id would silently
    overwrite one another's transcript, and the run would report a coverage it did not have.
    """
    directory = Path(probe_dir if probe_dir is not None else settings.live_probe_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"live probe directory not found: {directory}")

    probes: list[Probe] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        for probe in ProbeSet.model_validate(payload).probes:
            if probe.id in seen:
                raise ValueError(f"duplicate probe id {probe.id!r} in {path} and {seen[probe.id]}")
            seen[probe.id] = path
            probes.append(probe)
    if not probes:
        raise ValueError(f"no probes found in {directory}")
    return probes


def _decode(chunk: str) -> dict[str, Any] | None:
    """One SSE `data:` line as an event dict, or `None` for a keepalive or unparseable frame."""
    if not chunk.startswith("data:"):
        return None
    try:
        decoded = json.loads(chunk[5:].strip())
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _score_citations(answer: str, returned_ids: set[str]) -> list[str]:
    """Note ids the answer cites that no tool in this turn returned.

    Checked against what the turn's tools returned rather than a retrieval call of our own, because
    the question is whether the answer is grounded in what this turn actually saw. Re-retrieving
    would let an id the model produced from memory pass simply because the note happens to exist.

    **`returned_ids`, not the previews.** This scanned `ToolResultEvent.preview` — 200 characters,
    the browser's budget — while `gather_evidence` returns up to 40 chunks, so every citation past
    the first chunk was reported as ungrounded. A live run then graded 19 of 36 answers as
    fabrication and nine of nine checked verdicts were false: the "invented" ICH PDEs, the
    "entirely fabricated" property table and the "fabricated" hazard controls were all verbatim
    tool output that had simply scrolled past character 200
    (`docs/archive/live-grounded-2026-08-03.md`). The event now carries an untruncated `note_ids`
    for exactly this, and a set membership test replaces the substring scan — which also closes the
    hyphen-suffix hole the substring form had, where a returned `playbook-degassing-old` grounded a
    cited `playbook-degassing`.
    """
    return sorted(set(cited_ids(answer)) - returned_ids)


def _verified_numbers(answer: str, returned: list[float]) -> list[str]:
    """Figures the answer states that a tool in this turn returned, as the answer wrote them.

    The numeric counterpart to `_score_citations`, and **inverted on purpose**: that one names the
    citations nothing grounds, this one names the figures something does. The inversion is the
    whole design, it was measured rather than assumed, and the measurement is worth keeping here
    because the obvious symmetric version is a trap.

    **The problem this solves.** With `note_ids` fixed, a live re-run still had the judge writing
    "the answer invents specific PDE numbers (Pd: 100/10/1 µg/day; Cu: 3000/300/30 µg/day) … the
    tool results shown are truncated previews that do not display the numerical limits" — about six
    values `ich_impurity_limit` had returned in full. Same for gr-18's dipoles and LUMOs and
    gr-29's charge masses. The judge was not being careless; it was reasoning correctly from an
    evidence block it had been told was incomplete. It needed a way to check a number, so it gets
    one.

    **Why not "numbers no tool returned".** That signal was built and measured against the three
    probes above, with the real tools called for their real return values. It produced **eleven
    flags and not one fabrication**: two figures the asker had put in the question (40 %, 99 %),
    six the model derived arithmetically from values it had been handed (+1.11 D, −0.59 eV,
    +0.13 eV, +24 %, 59 points, a 13.6 kg total), two textbook constants (van der Waals radii), and
    one plate yield a reconstruction of the turn's evidence sweep did not reproduce. Precision
    zero. A citation is a claim with a syntax — `[[id]]` says "I got this from you" and there is no
    other way to write one — and a number has none: subtraction, the question, and general chemical
    knowledge all produce figures no tool returned, and no scan can tell them from invention.
    Shipping that list under a heading the judge is told to trust would have rebuilt the defect the
    fix exists to remove, one field over.

    So the harness asserts only what it can: *this figure is in the evidence*. Everything else is
    left to the judge's reading, and the prompt says so where the list is presented. Absent from
    here means unchecked, never suspect.
    """
    return [numeral for numeral in stated_numerals(answer) if is_rounding_of(numeral, returned)]


def _asked_in_prose(outcome: ProbeOutcome) -> bool:
    """Did the turn end on a question it never raised through `ask_clarifying_question`?

    Two signals together, because either alone is wrong. A question mark is not enough — an answer
    may pose one rhetorically on its way to answering it. Calling no tool is not enough either — a
    turn can legitimately answer from what it already knows. It is the pair that names the shape
    this exists to count: the system reached for nothing and handed the question back.

    Deliberately not folded into `asked_clarifying`. Keeping the two apart is what makes "the tool
    exists and the model asks around it" visible as a routing problem rather than averaging into
    a clarification rate that looks healthy.
    """
    if outcome.asked_clarifying or outcome.tools_called or not outcome.answered:
        return False
    return "?" in outcome.answer


async def run_probe(client: httpx.AsyncClient, probe: Probe) -> ProbeOutcome:
    """Ask one probe over the front door and fold its event stream into an outcome.

    A transport failure is recorded on the outcome instead of raised: a run of 150 probes must
    not lose 149 results because one turn's connection dropped, and "the front door stopped
    answering" is itself a finding worth having on disk.
    """
    outcome = ProbeOutcome(
        probe_id=probe.id,
        section=probe.section,
        persona=probe.persona,
        bucket=probe.bucket,
        question=probe.question,
    )
    counts: dict[str, int] = {}
    # Every note id this turn's tools returned, untruncated — see `_score_citations`. Accumulated
    # here rather than derived from `outcome.tool_results`, whose previews are the browser's
    # 200-character budget and were exactly what made the old citation score meaningless.
    returned_ids: set[str] = set()
    # Every value this turn's tools returned, untruncated — see `_verified_numbers`. A list rather
    # than a set because the comparison is a rounding, not a membership test, so there is nothing
    # to hash it by; duplicates across calls are cheap at this size (tens of values per result).
    returned_values: list[float] = []
    started = time.monotonic()

    try:
        created = await client.post("/sessions", json={})
        created.raise_for_status()
        session_id = str(created.json()["session_id"])

        async with client.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": probe.question},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                event = _decode(line)
                if event is None:
                    continue
                kind = str(event.get("type", "unknown"))
                counts[kind] = counts.get(kind, 0) + 1

                if kind == "tool_call":
                    outcome.tools_called.append(str(event.get("tool", "")))
                elif kind == "tool_result":
                    preview = str(event.get("preview", ""))
                    returned_ids.update(str(note_id) for note_id in event.get("note_ids", []))
                    returned_values.extend(float(value) for value in event.get("numbers", []))
                    outcome.tool_results.append(
                        ToolResult(tool=str(event.get("tool", "")), preview=preview)
                    )
                elif kind == "tool_failed":
                    outcome.tools_failed.append(str(event.get("tool", "")))
                elif kind == "capability_degraded":
                    # The event's field is `connectors`, a list. It was read as a scalar
                    # `capability`/`name`, neither of which the event has ever carried, so every
                    # degraded turn recorded one empty string — enough to make `failed_loudly`
                    # true while naming nothing. Harmless while only an unreachable bundle raised
                    # the event; the per-turn Temporal probe now raises it on any deployment
                    # without a broker, which is every offline run.
                    outcome.degraded.extend(str(name) for name in event.get("connectors", []))
                elif kind == "job_started":
                    outcome.jobs_started.append(str(event.get("job_id", event.get("job", ""))))
                elif kind == "note_proposed":
                    outcome.notes_proposed.append(str(event.get("note_id", "")))
                elif kind == "question":
                    outcome.asked_clarifying = True
                elif kind == "answer":
                    outcome.answer = str(event.get("text", ""))
                elif kind == "error":
                    outcome.error_code = str(event.get("code", "unknown"))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        outcome.transport_error = f"{type(exc).__name__}: {exc}"

    outcome.latency_seconds = round(time.monotonic() - started, 2)
    outcome.event_counts = counts
    outcome.answered = bool(outcome.answer.strip())
    outcome.failed_loudly = bool(outcome.tools_failed or outcome.error_code or outcome.degraded)
    outcome.uncited_note_ids = _score_citations(outcome.answer, returned_ids)
    outcome.verified_numbers = _verified_numbers(outcome.answer, returned_values)
    outcome.asked_clarifying_in_prose = _asked_in_prose(outcome)
    if probe.expects_tools:
        outcome.expected_tools_met = any(t in outcome.tools_called for t in probe.expects_tools)
    return outcome


async def run_probes(
    probes: list[Probe],
    *,
    base_url: str | None = None,
    transcript_dir: str | None = None,
) -> list[ProbeOutcome]:
    """Run every probe with bounded concurrency, writing one transcript per probe as it lands.

    Written as each result arrives rather than at the end: a run of this size is long enough that
    a crash three quarters through must not cost the three quarters that succeeded.
    """
    url = base_url if base_url is not None else settings.live_probe_base_url
    out_dir = Path(
        transcript_dir if transcript_dir is not None else settings.live_probe_transcript_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(settings.live_probe_concurrency)
    timeout = httpx.Timeout(settings.live_probe_timeout_seconds)

    async with httpx.AsyncClient(base_url=url, timeout=timeout) as client:

        async def one(probe: Probe) -> ProbeOutcome:
            async with semaphore:
                outcome = await run_probe(client, probe)
            (out_dir / f"{probe.id}.json").write_text(
                json.dumps(
                    {"probe": probe.model_dump(), "outcome": outcome.model_dump()},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            logger.info(
                "probe %s: answered=%s tools=%s %.1fs",
                probe.id,
                outcome.answered,
                ",".join(outcome.tools_called) or "-",
                outcome.latency_seconds,
            )
            return outcome

        return list(await asyncio.gather(*(one(probe) for probe in probes)))
