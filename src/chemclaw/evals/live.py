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
    failed_loudly: bool = False
    error_code: str | None = None
    degraded: list[str] = Field(default_factory=list)
    jobs_started: list[str] = Field(default_factory=list)
    notes_proposed: list[str] = Field(default_factory=list)
    asked_clarifying: bool = False
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


def _score_citations(answer: str, tool_previews: list[str]) -> list[str]:
    """Note ids the answer cites that never appeared in a tool result.

    Checked against the *previews* rather than a retrieval call of our own, because the question
    is whether the answer is grounded in what this turn actually saw. Re-retrieving would let an
    id that the model produced from memory pass simply because the note happens to exist.
    """
    seen = "\n".join(tool_previews)
    return sorted({note_id for note_id in cited_ids(answer) if note_id not in seen})


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
    previews: list[str] = []
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
                    previews.append(preview)
                    outcome.tool_results.append(
                        ToolResult(tool=str(event.get("tool", "")), preview=preview)
                    )
                elif kind == "tool_failed":
                    outcome.tools_failed.append(str(event.get("tool", "")))
                elif kind == "capability_degraded":
                    outcome.degraded.append(str(event.get("capability", event.get("name", ""))))
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
    outcome.uncited_note_ids = _score_citations(outcome.answer, previews)
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
