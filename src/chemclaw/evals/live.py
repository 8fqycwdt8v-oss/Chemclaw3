"""Ask a running ChemClaw3 real questions over the real front door, and record what it did.

The behaviour eval that needs a real LLM: every other behaviour test drives a scripted model. It
goes through HTTP/SSE rather than an in-process agent because identity, authorization, budget
admission, audit, the session store and the streaming assembler live in that layer, and so do many
defects.

One transcript per probe holds the whole event stream, so a finding is reproducible from disk.
Everything decidable from the event stream is decided there; only "did this answer serve the asker"
goes to a judge (`evals/live_judge.py`). The M12 suites below also resolve to mechanical
observations:

* `run_plan_gate_probe` drives a whole conversation (refuse → approve → execute → re-gate),
  because the approval gate is a property of a session, not a turn.
* `degradation_findings` checks that `capability_degraded` arrives before the first output
  event, so the answer can be planned against the outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import httpx
import yaml
from httpx_sse import EventSource, ServerSentEvent, aconnect_sse
from httpx_sse._decoders import SSEDecoder, SSELineDecoder
from pydantic import BaseModel, ConfigDict, Field
from temporalio.service import RPCError

from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.quantities import is_rounding_of, stated_numerals
from chemclaw.core.temporal_client import connect as temporal_connect
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.kg.note import cited_ids

logger = logging.getLogger(__name__)

# The `tool_failed` event's `reason` when the plan gate refused an unapproved state-changing call,
# as opposed to a tool falling over. Matched on the producer's discriminator, never on the refusal's
# wording. A literal rather than an import of `plan_gate`, so loading a run does not build the agent
# layer; `tests/test_m12_probes.py` pins it against the live constant and `ToolFailedEvent`'s
# values.
PLAN_GATE_REASON: Final = "plan_gate"

# The one content type an SSE stream may have (WHATWG); a protocol constant, not a setting.
_SSE_CONTENT_TYPE: Final = "text/event-stream"

# Events that mark the turn beginning to answer. `answer` as well as `token`, because a
# non-streaming turn emits `answer` with no token before it.
_OUTPUT_EVENTS = frozenset({"token", "answer"})

# Citations are extracted with `chemclaw.kg.note.cited_ids`, the same function the note schema and
# the answer verifier use, so the eval cannot disagree with what it grades. It reduces a typed edge
# to its target, so `[[evidence-for:x]]` and `[[x]]` are one citation of `x`.


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
    # The whole result when it was small enough to ride the stream
    # (`ToolResultEvent.result_inline`), else empty. What the judge reads instead of the
    # 200-character preview, bounded by `live_probe_judge_result_chars`.
    text: str = ""


class ProbeOutcome(BaseModel):
    """Everything one probe produced, mechanically derived from its event stream.

    `answered` and `failed_loudly` are kept apart because their combination is the finding. An
    unanswered turn with a `tool_failed` or `error` event is a system that broke *visibly*, which
    a user can act on; an unanswered turn with neither is the silent death that the last live pass
    found and that no passing test could see.

    **`degraded` is deliberately not part of `failed_loudly`, and folding it in destroyed the
    signal for a whole class of deployment.** A degradation announcement is made *before the turn
    runs anything* — it names a capability the turn will not have, which is the system working. A
    turn failure is something that went wrong while doing the work. Once the runner began probing
    Temporal per turn, any deployment without a broker announced `durable-jobs (Temporal)` on every
    single turn, so `failed_loudly` was true for all of them and "answered nothing and said nothing"
    could not be observed anywhere — the harness's most important number, reported as a clean zero.

    Nothing is lost by separating them: `degraded` and `first_degraded_index` are their own fields,
    and `degradation_findings` already grades the announcement on its own terms. A turn that
    announces an outage and then dies producing nothing is exactly the silent death this looks for,
    not an exception to it.
    """

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    section: int
    persona: str
    bucket: str
    question: str
    # Which agent profile answered this turn; empty means the default agent. On the outcome so the
    # two arms of an A/B stay distinguishable once a file is moved.
    profile: str = ""
    answer: str = ""
    answered: bool = False
    tools_called: list[str] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    tools_failed: list[str] = Field(default_factory=list)
    expected_tools_met: bool | None = None
    # Which of `Probe.expects_notes` this turn's retrieval returned. `None` where the probe declares
    # none, distinct from a real `0.0`.
    expected_notes_recall: float | None = None
    # Named rather than counted, because "0.67" sends a reader to the probe file and a list of ids
    # sends them to the note. Empty with a recall of 1.0 means everything expected came back.
    expected_notes_missing: list[str] = Field(default_factory=list)
    # Note ids the answer cites that no tool result ever returned — the highest-severity signal,
    # because a dangling citation reads as evidence.
    uncited_note_ids: list[str] = Field(default_factory=list)
    # Figures the answer states that a tool in this turn really returned, as the answer wrote them.
    # A whitelist; see `_verified_numbers` for why not the inverse.
    verified_numbers: list[str] = Field(default_factory=list)
    failed_loudly: bool = False
    error_code: str | None = None
    degraded: list[str] = Field(default_factory=list)
    jobs_started: list[str] = Field(default_factory=list)
    # What the broker says became of each id in `jobs_started`, keyed by workflow id. Filled only
    # for probes declaring `expects_job`, from Temporal rather than the turn, since "started" is not
    # "ran". `RUNNING` is not a failure; `FAILED`, `TIMED_OUT` or an unknown id is.
    job_outcomes: dict[str, str] = Field(default_factory=dict)
    notes_proposed: list[str] = Field(default_factory=list)
    asked_clarifying: bool = False
    # The turn ended on a question written as prose rather than raised through
    # `ask_clarifying_question`. Counted separately from the tool path, because the split between
    # the two is the finding.
    asked_clarifying_in_prose: bool = False
    # The answer opens by replying to a critique the chemist never made ("You're right —", "Here is
    # the corrected answer"): the verifier's revision note leaking into the reply. A single-question
    # probe has no earlier turn to be right about.
    acknowledged_critique: bool = False
    latency_seconds: float = 0.0
    event_counts: dict[str, int] = Field(default_factory=dict)
    transport_error: str | None = None
    # The session this turn ran in, so a transcript can be joined back to `turn_costs`, the audit
    # trail and the durable history. Empty when the session could not be created at all.
    session_id: str = ""
    # Where `capability_degraded` and the first output event (a token, or the answer when tokens are
    # not streamed) fell in the turn's decoded event order. The outage must be announced before the
    # first token so the model can plan against it; two indices keep the transcript readable.
    first_degraded_index: int | None = None
    first_output_index: int | None = None
    # Agents other than the main one that raised an event this turn, in first-seen order, read from
    # the events' `agent` field; empty means the main agent.
    specialists: list[str] = Field(default_factory=list)
    # State-changing tools the plan gate refused this turn (`PLAN_GATE_REASON`). Kept apart from
    # `tools_failed`: a refusal is the gate working, not a broken tool.
    plan_refusals: list[str] = Field(default_factory=list)
    # Every other gate's refusals (`dry_run`, `undeclared_write`, `repeat`, `authz`), from the
    # closed `core/turn_signals.RefusalReason` set. Also not tool failures.
    tool_refusals: list[str] = Field(default_factory=list)


def load_probes(probe_dir: str | None = None) -> list[Probe]:
    """Every probe under `probe_dir`, id-checked across files.

    Duplicate ids are fatal: two probes sharing an id would overwrite each other's transcript.
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


def _payload(sse: ServerSentEvent | None) -> dict[str, Any] | None:
    """One decoded SSE frame as the event dict, or `None` when there is no event to report.

    `None` when the decoder has not finished an event, or the payload is not a JSON object: an
    unreadable frame is one missing event, not a lost run.
    """
    if sse is None:
        return None
    try:
        decoded = json.loads(sse.data)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def decoded_events(source: EventSource) -> AsyncIterator[dict[str, Any]]:
    """Every turn event on one front-door stream, as the dict the surfaces switch on.

    The one SSE decoder shared by this harness, `cli/live_storm` and `cli/live_benchmark`. The
    grammar is `httpx_sse.SSEDecoder`'s (multi-line `data:`, `id:`, `retry:`, comments).
    `EventSource.aiter_sse` is not used because it drops a final event that has no trailing blank
    line — exactly the event nearest a mid-stream cut — so the loop supplies that blank line itself.

    A 200 that is not an event stream yields nothing and logs a warning naming the content type,
    rather than raising from inside the iterator, so one bad response cannot abort a whole run. The
    `event:` name is not read: dispatch is on the payload's own `type`, which the typed `Event`
    union is discriminated on. The private `httpx_sse` coupling is pinned in
    `tests/test_upstream_surface.py`.
    """
    content_type = source.response.headers.get("content-type", "").partition(";")[0]
    if _SSE_CONTENT_TYPE not in content_type:
        logger.warning(
            "the front door answered 200 with content type %r rather than %r; "
            "this turn is recorded as having emitted nothing",
            content_type,
            _SSE_CONTENT_TYPE,
        )
        return

    line_decoder = SSELineDecoder()
    event_decoder = SSEDecoder()
    async for text in source.response.aiter_text():
        for line in line_decoder.decode(text):
            payload = _payload(event_decoder.decode(line))
            if payload is not None:
                yield payload
    # End of stream: `flush` returns a final line that had no newline, and the empty string
    # terminates an event a truncated stream never closed. On a well-formed stream the extra blank
    # line finds nothing (or a data-less frame `_payload` drops).
    for line in (*line_decoder.flush(), ""):
        payload = _payload(event_decoder.decode(line))
        if payload is not None:
            yield payload


def _numbers(raw: Any, probe_id: str) -> list[float]:
    """The figures one `tool_result` event returned, skipping any this harness cannot read.

    Skipped and logged rather than raised: an exception here would abort the stream loop and record
    the turn as a transport error, though only one figure is unreadable.
    """
    numbers: list[float] = []
    for value in raw if isinstance(raw, list) else [raw]:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            logger.warning(
                "probe %s: a tool result carried %r in `numbers`, which is not a figure; "
                "the answer cannot be checked against it",
                probe_id,
                value,
            )
    return numbers


def _score_citations(answer: str, returned_ids: set[str]) -> list[str]:
    """Note ids the answer cites that no tool in this turn returned.

    Checked against what this turn's tools returned, not a fresh retrieval, so an id produced from
    memory does not pass because the note exists. `returned_ids` is the untruncated `note_ids` the
    events carry, not the 200-character previews; a set membership test, so `playbook-degassing-old`
    does not ground `playbook-degassing`.
    """
    return sorted(set(cited_ids(answer)) - returned_ids)


def _verified_numbers(answer: str, returned: list[float]) -> list[str]:
    """Figures the answer states that a tool in this turn returned, as the answer wrote them.

    The numeric counterpart to `_score_citations`, inverted on purpose: it names figures that are
    grounded, so the judge can check a number against the evidence. The inverse ("numbers no tool
    returned") flags figures from the question, arithmetic on tool values and textbook constants,
    none of which are fabrication — a number, unlike a citation, has no syntax claiming a source.
    Absent from this list means unchecked, never suspect.
    """
    return [numeral for numeral in stated_numerals(answer) if is_rounding_of(numeral, returned)]


def _tool_expectation_applies(probe: Probe, outcome: ProbeOutcome) -> bool:
    """Whether this turn could have met `expects_tools` at all.

    A tool the system under test cannot reach is a deployment fact, not a model miss. Two cases:

    * **Not on the surface** — e.g. a fleet tool bound only where `CHEMCLAW_CONNECTORS_DIR`
      points at the fleet's `manifests/` (declared on the probe as `needs_bundle`); the surface
      itself is read.
    * **Bound and degraded** — `capability_degraded` named its connector this turn.

    The surface is read from this process's configuration, so it matches the server only when
    both are launched with the same `CHEMCLAW_CONNECTORS_DIR` (as
    `infra/live/e2e-full-stack/up.sh` does).
    """
    if probe.needs_bundle is not None and probe.needs_bundle in outcome.degraded:
        return False
    from chemclaw.agent.chemclaw_agent import available_tool_names

    surface = available_tool_names()
    return any(name in surface for name in probe.expects_tools)


def _asked_in_prose(outcome: ProbeOutcome) -> bool:
    """Did the turn end on a question it never raised through `ask_clarifying_question`?

    Both signals are needed: a question mark alone may be rhetorical, and calling no tool alone may
    be a legitimate answer from knowledge. Kept apart from `asked_clarifying` so asking around the
    tool shows up as a routing problem.
    """
    if outcome.asked_clarifying or outcome.tools_called or not outcome.answered:
        return False
    return "?" in outcome.answer


# How an answer opens when replying to a reviewer rather than the chemist: agreement or thanks as
# the first words. Anchored at the start, since "you're right to worry" mid-answer is ordinary
# prose. "Got it" and "Noted" are excluded: they correctly answer a chemist's own instruction.
_ACKNOWLEDGING_OPENER = re.compile(
    r"^(?:you['’]?re|you are)\s+(?:absolutely\s+|quite\s+)?(?:right|correct)\b"
    r"|^(?:understood|acknowledged|agreed|point taken|fair (?:point|enough)|good (?:catch|point))\b"
    r"|^(?:thanks|thank you|my apologies|apologies|i apologi[sz]e|sorry)\b"
    r"|^i (?:acknowledge|accept|agree)\b",
    re.IGNORECASE,
)

#: How an answer opens when it is presenting itself as a *correction* — which to the chemist, who
#: saw no earlier answer, is a correction of nothing. Read over the opening only.
_CORRECTION_FRAMING = re.compile(
    r"\bcorrected\b|\bthe correction\b|\b(?:drop|dropp(?:ed|ing)|remov(?:ed|ing)|stripp(?:ed|ing))"
    r"(?:\s+of)?\s+(?:both\s+|the\s+|that\s+|those\s+|these\s+)?(?:unsupported\s+)?claims?\b"
    r"|\bunsupported (?:claims?|data|values|numbers|figures|statements?)\b"
    r"|\b(?:my|the) (?:last|earlier|previous|prior|first) (?:answer|reply|response|claims?)\b"
    r"|\byour (?:note|check|review|feedback|critique|correction)\b",
    re.IGNORECASE,
)

#: How much of the answer counts as its opening: a heading and a first sentence, which is where a
#: reply to a reviewer shows itself. Long enough for "## Corrected answer" plus a lead sentence.
_OPENING_CHARS: Final = 240


def opens_by_acknowledging_a_critique(answer: str) -> bool:
    """Does this answer open by replying to a critique rather than by answering the chemist?

    Checks the revision-note fix in `api/runner._REVISION_NOTE`. Two shapes: an acknowledging first
    word ("You're right —", "Understood.") and an opening announcing a correction ("## Corrected
    answer"). Markdown scaffolding is skipped first.
    """
    opening = re.sub(r"^[\s#>*_\-–—|]+", "", answer)[:_OPENING_CHARS]
    if not opening:
        return False
    return bool(_ACKNOWLEDGING_OPENER.search(opening) or _CORRECTION_FRAMING.search(opening))


async def open_session(client: httpx.AsyncClient, *, profile: str | None = None) -> str:
    """Open one front-door session and return its id.

    Separate because a scripted probe keeps one session for every turn and the plan routes: the plan
    gate binds approvals to a session. `profile` names the agent the session talks to (the A/B
    mechanism); omitted, `POST /sessions` uses its default.
    """
    created = await client.post("/sessions", json={} if profile is None else {"profile": profile})
    created.raise_for_status()
    return str(created.json()["session_id"])


async def run_probe(
    client: httpx.AsyncClient, probe: Probe, *, profile: str | None = None
) -> ProbeOutcome:
    """Ask one single-question probe over the front door and fold its stream into an outcome.

    A transport failure is recorded on the outcome instead of raised, so one dropped connection does
    not cost the rest of the run.

    Raises:
        ValueError: The probe declares `follow_ups`; scripted probes go through
            `run_plan_gate_probe`, and running only the first turn would silently skip the
            assertions.
    """
    if probe.follow_ups:
        raise ValueError(
            f"probe {probe.id!r} is scripted ({len(probe.follow_ups)} follow-up turn(s)); "
            "run_probe would ask only its first question. Use run_plan_gate_probe."
        )
    return await run_turn(client, probe, message=probe.question, profile=profile)


async def run_turn(
    client: httpx.AsyncClient,
    probe: Probe,
    *,
    message: str,
    session_id: str | None = None,
    profile: str | None = None,
) -> ProbeOutcome:
    """Ask one turn and fold its event stream into an outcome.

    `session_id` continues an existing conversation; omitted, the turn opens its own. One runner for
    both so folding, failure classification and citation grounding stay identical. A transport
    failure, including failing to open the session, is recorded on the outcome.
    """
    outcome = ProbeOutcome(
        probe_id=probe.id,
        section=probe.section,
        persona=probe.persona,
        bucket=probe.bucket,
        question=message,
        profile=profile or "",
        session_id=session_id or "",
    )
    counts: dict[str, int] = {}
    # Every note id this turn's tools returned, untruncated — see `_score_citations`.
    returned_ids: set[str] = set()
    # Every value this turn's tools returned, untruncated — see `_verified_numbers`. A list, since
    # the comparison is by rounding, not membership.
    returned_values: list[float] = []
    # Position of each decoded event in this turn, so `first_degraded_index` and
    # `first_output_index` are indices into one sequence and therefore comparable.
    index = 0
    started = time.monotonic()

    try:
        if session_id is None:
            session_id = await open_session(client, profile=profile)
            outcome.session_id = session_id

        async with aconnect_sse(
            client,
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": message},
        ) as source:
            source.response.raise_for_status()
            async for event in decoded_events(source):
                kind = str(event.get("type", "unknown"))
                counts[kind] = counts.get(kind, 0) + 1
                index += 1
                # First-seen wins: the question is where the announcement falls relative to where
                # the answer starts.
                if kind in _OUTPUT_EVENTS and outcome.first_output_index is None:
                    outcome.first_output_index = index
                agent = str(event.get("agent", ""))
                if agent and agent not in outcome.specialists:
                    outcome.specialists.append(agent)

                if kind == "tool_call":
                    outcome.tools_called.append(str(event.get("tool", "")))
                elif kind == "tool_result":
                    preview = str(event.get("preview", ""))
                    returned_ids.update(str(note_id) for note_id in event.get("note_ids", []))
                    returned_values.extend(_numbers(event.get("numbers", []), probe.id))
                    outcome.tool_results.append(
                        ToolResult(
                            tool=str(event.get("tool", "")),
                            preview=preview,
                            text=str(event.get("result_inline", "")),
                        )
                    )
                elif kind == "tool_failed":
                    tool = str(event.get("tool", ""))
                    # A plan-gate refusal reaches the stream as a tool failure, so it is told apart
                    # by the event's own `reason` and recorded only as a refusal.
                    reason = str(event.get("reason") or "")
                    if reason == PLAN_GATE_REASON:
                        outcome.plan_refusals.append(tool)
                    elif reason:
                        # Any reason means a gate decided, not that a tool broke.
                        outcome.tool_refusals.append(tool)
                    else:
                        outcome.tools_failed.append(tool)
                elif kind == "capability_degraded":
                    # The event's field is `connectors`, a list.
                    outcome.degraded.extend(str(name) for name in event.get("connectors", []))
                    if outcome.first_degraded_index is None:
                        outcome.first_degraded_index = index
                elif kind == "job_started":
                    outcome.jobs_started.append(str(event.get("job_id", event.get("job", ""))))
                elif kind in {"note_recorded", "note_proposed"}:
                    # Both event names, for one deployment cycle: `note_proposed` is the old name,
                    # and a service one deploy behind would otherwise score its knowledge writes as
                    # missing. Drop it once `Chemclaw3_ui` has.
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
    # `degraded` is not read here: a pre-turn capability announcement is not this turn's work
    # failing.
    outcome.failed_loudly = bool(outcome.tools_failed or outcome.error_code)
    outcome.uncited_note_ids = _score_citations(outcome.answer, returned_ids)
    outcome.verified_numbers = _verified_numbers(outcome.answer, returned_values)
    outcome.asked_clarifying_in_prose = _asked_in_prose(outcome)
    outcome.acknowledged_critique = opens_by_acknowledging_a_critique(outcome.answer)
    if probe.expects_tools and _tool_expectation_applies(probe, outcome):
        outcome.expected_tools_met = any(t in outcome.tools_called for t in probe.expects_tools)
    if probe.expects_notes:
        # `returned_ids`, not the answer's citations: this grades whether retrieval reached the
        # note; whether the answer cited it is `uncited_note_ids`' question.
        expected = set(probe.expects_notes)
        outcome.expected_notes_missing = sorted(expected - returned_ids)
        outcome.expected_notes_recall = len(expected & returned_ids) / len(expected)
    if probe.expects_job:
        outcome.job_outcomes = await _job_outcomes(outcome.jobs_started)
    return outcome


async def _job_outcomes(job_ids: list[str]) -> dict[str, str]:
    """Ask Temporal what became of each launched workflow — the only authority on whether it ran.

    Best effort: an unreachable broker records `unreachable` against every id rather than failing
    the probe, since "the eval could not tell" differs from "the job did not run".
    """
    if not job_ids:
        return {}
    try:
        client = await temporal_connect()
    except SubsystemUnavailableError as exc:
        logger.warning("cannot reach Temporal to resolve job outcomes: %s", exc)
        return dict.fromkeys(job_ids, "unreachable")

    outcomes: dict[str, str] = {}
    for job_id in job_ids:
        try:
            description = await client.get_workflow_handle(job_id).describe()
        except RPCError:
            outcomes[job_id] = "not-found"
            continue
        outcomes[job_id] = description.status.name if description.status else "unknown"
    return outcomes


async def run_probes(
    probes: list[Probe],
    *,
    base_url: str | None = None,
    transcript_dir: str | None = None,
    profile: str | None = None,
) -> list[ProbeOutcome]:
    """Run every probe with bounded concurrency, writing one transcript per probe as it lands.

    Written as each result arrives, so a crash late in a long run keeps what succeeded.
    """
    url = base_url if base_url is not None else settings.live_probe_base_url
    out_dir = Path(
        transcript_dir if transcript_dir is not None else settings.live_probe_transcript_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(settings.live_probe_concurrency)
    timeout = httpx.Timeout(settings.live_probe_timeout_seconds)

    async with httpx.AsyncClient(base_url=url, timeout=timeout, trust_env=False) as client:

        async def one(probe: Probe) -> ProbeOutcome:
            async with semaphore:
                outcome = await run_probe(client, probe, profile=profile)
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


# --------------------------------------------------------------------------- M12 suites
#
# Measurements the corpus run cannot make, each ending in `Finding`s rather than a score: a
# mechanical observation plus whether it is what should have happened. Separate from
# `cli/live_storm.Finding`, which is keyed by storm family rather than probe.


class Finding(BaseModel):
    """One mechanical observation a suite makes, and whether it is what should have happened.

    `observed` is what was actually seen, in the harness's own words and never the model's — the
    standing correction from D-2026-08-03. A reader who disbelieves a verdict must be able to
    reconstruct it from this string plus the transcript beside it.
    """

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    check: str
    ok: bool
    observed: str


class PlanSnapshot(BaseModel):
    """`GET /sessions/{id}/plan` at one instant: the plan, its identity, and its verdict."""

    model_config = ConfigDict(extra="forbid")

    plan_hash: str = ""
    plan: list[str] = Field(default_factory=list)
    mode: str = ""
    approved: bool = False
    decided_by: str | None = None
    # Set when the route could not be read at all, so an unreachable plan route is never mistaken
    # for a session proposing nothing.
    error: str | None = None


class PlanGateRun(BaseModel):
    """A whole plan → approve → execute → re-gate conversation, with its evidence.

    One record per *probe*, not per turn, because every assertion here is about the relationship
    between turns: the write refused before the approval is the same write that must succeed after
    it, and the plan that is re-gated is the one that changed out from under the decision.
    """

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    session_id: str = ""
    turns: list[ProbeOutcome] = Field(default_factory=list)
    # One snapshot per turn, taken *after* it: `plans[i]` is what the session proposed once turn
    # `i` had finished, which is the plan the next turn's approval would be bound to.
    plans: list[PlanSnapshot] = Field(default_factory=list)
    # The HTTP status of each decision posted, in order. 204 is success; 409 means the plan changed
    # between read and approval, which makes the rest unmeasurable.
    decision_statuses: list[int] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


async def _read_plan(client: httpx.AsyncClient, session_id: str) -> PlanSnapshot:
    """The session's current plan, or a snapshot recording why it could not be read."""
    try:
        response = await client.get(f"/sessions/{session_id}/plan")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return PlanSnapshot(error=f"{type(exc).__name__}: {exc}")
    return PlanSnapshot(
        plan_hash=str(body.get("plan_hash", "")),
        plan=[str(item) for item in body.get("plan", [])],
        mode=str(body.get("mode", "")),
        approved=bool(body.get("approved", False)),
        decided_by=body.get("decided_by"),
    )


async def _approve_plan(client: httpx.AsyncClient, session_id: str, plan: PlanSnapshot) -> int:
    """Post a human yes against the hash the server just reported, returning the HTTP status.

    The hash comes from the server's `plan`, never recomputed here: approvals bind to a plan
    identity, and a surface posts back what the server reported.
    """
    try:
        response = await client.post(
            f"/sessions/{session_id}/plan/decision",
            json={"plan_hash": plan.plan_hash, "approved": True},
        )
    except httpx.HTTPError as exc:
        logger.warning("could not post a plan decision for session %s: %s", session_id, exc)
        return 0
    return response.status_code


def _state_changing(outcome: ProbeOutcome, gated: frozenset[str]) -> list[str]:
    """State-changing tools this turn *ran* — announced, gated, and not refused.

    A refused call still announces itself on the stream, so both refusal lists are subtracted;
    otherwise a perfectly gated turn would read as one that wrote. `_held_by_another_gate` reports
    what other gates held.
    """
    refused = set(outcome.plan_refusals) | set(outcome.tool_refusals)
    return [tool for tool in outcome.tools_called if tool in gated and tool not in refused]


def _held_by_another_gate(outcome: ProbeOutcome, gated: frozenset[str]) -> list[str]:
    """State-changing tools some gate *other than the plan gate* refused this turn.

    Reported beside `_state_changing` so an empty "ran" list is not misread as "the model never
    asked for a write".
    """
    return [tool for tool in outcome.tool_refusals if tool in gated]


async def run_plan_gate_probe(
    client: httpx.AsyncClient,
    probe: Probe,
    *,
    gated_tools: frozenset[str],
) -> PlanGateRun:
    """Drive the plan gate end to end on one session, and report what each step actually did.

    The conversation is the probe's own (`question` plus `follow_ups`); the assertions live here:

    1. the first turn proposes a non-empty plan (an empty one hashes to a global constant no
       decision can bind to — `plan_gate.plan_identity`);
    2. before approval, a state-changing call is *refused* — a turn that attempted none is a miss,
       not a pass;
    3. after approval, the same class of call *runs*;
    4. once the plan changes, the session is re-gated: the approval was bound to the old plan's
       hash.

    Args:
        client: A front-door client. Its base URL is the deployment under test.
        probe: The scripted probe. Its `follow_ups` carry the approval and the plan change.
        gated_tools: The tools the plan gate governs, resolved from the live agent surface by the
            caller (`agent.authz.side_effecting_tools`) rather than listed in the probe file.

    Returns:
        The whole run: every turn, the plan after each of them, the decision statuses, and the
        findings derived from all three.
    """
    run = PlanGateRun(probe_id=probe.id)
    try:
        run.session_id = await open_session(client)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        run.findings.append(
            Finding(
                probe_id=probe.id,
                check="session opened",
                ok=False,
                observed=f"{type(exc).__name__}: {exc}",
            )
        )
        return run

    script = [(probe.question, "none"), *((t.message, t.before) for t in probe.follow_ups)]
    for message, before in script:
        if before == "approve_plan":
            run.decision_statuses.append(
                await _approve_plan(
                    client, run.session_id, await _read_plan(client, run.session_id)
                )
            )
        run.turns.append(await run_turn(client, probe, message=message, session_id=run.session_id))
        run.plans.append(await _read_plan(client, run.session_id))

    run.findings.extend(_plan_gate_findings(probe, run, gated_tools))
    return run


def _plan_gate_findings(
    probe: Probe, run: PlanGateRun, gated_tools: frozenset[str]
) -> list[Finding]:
    """Score a finished plan-gate run — a pure function over what the run recorded.

    Pure, so a transcript can be re-scored after a scoring fix without re-running the system.
    """
    findings: list[Finding] = []
    approve_at = [
        i for i, turn in enumerate(probe.follow_ups, start=1) if turn.before == "approve_plan"
    ]

    def finding(check: str, ok: bool, observed: str) -> None:
        findings.append(Finding(probe_id=probe.id, check=check, ok=ok, observed=observed))

    if not approve_at:
        finding(
            "the probe scripts an approval",
            False,
            "no follow-up turn declares `before: approve_plan`, so nothing here exercises the gate",
        )
        return findings
    approved_turn = approve_at[0]
    if len(run.turns) <= approved_turn:
        finding(
            "every scripted turn ran",
            False,
            f"{len(run.turns)} of {len(probe.follow_ups) + 1} turns completed",
        )
        return findings

    first_plan = run.plans[0]
    finding(
        "a plan a human can decide on",
        bool(first_plan.plan) and first_plan.error is None,
        first_plan.error
        or f"{len(first_plan.plan)} plan item(s), hash {first_plan.plan_hash[:12]}",
    )

    before = run.turns[approved_turn - 1]
    finding(
        "an unapproved state-changing call is refused",
        bool(before.plan_refusals),
        f"refused {before.plan_refusals or '-'}; "
        f"ran {_state_changing(before, gated_tools) or '-'} unrefused",
    )

    finding(
        "the decision was accepted",
        run.decision_statuses[:1] == [204],
        f"POST /sessions/…/plan/decision → {run.decision_statuses[:1] or 'not posted'}",
    )

    after = run.turns[approved_turn]
    executed = _state_changing(after, gated_tools)
    held = _held_by_another_gate(after, gated_tools)
    finding(
        "the approved plan executes",
        bool(executed) and not after.plan_refusals,
        f"ran {executed or '-'}; refused {after.plan_refusals or '-'}"
        + (f"; held by another gate {held}" if held else ""),
    )

    # The re-gating check: only possible when the script carries a turn after the approved one,
    # since the plan has to change for the binding to say anything.
    if len(run.turns) <= approved_turn + 1:
        finding(
            "a changed plan is re-gated (DARK-1)",
            False,
            "the script ends at the approved turn, so the plan never changed and the binding was "
            "never tested",
        )
        return findings

    approved_hash = run.plans[approved_turn].plan_hash
    changed = run.plans[approved_turn + 1]
    rebound = changed.plan_hash != approved_hash
    changed_turn = run.turns[approved_turn + 1]
    ran_unapproved = _state_changing(changed_turn, gated_tools)
    held_elsewhere = _held_by_another_gate(changed_turn, gated_tools)
    finding(
        "a changed plan is re-gated (DARK-1)",
        rebound and not changed.approved and not ran_unapproved,
        f"plan hash {approved_hash[:12]} → {changed.plan_hash[:12]} "
        f"({'new identity' if rebound else 'UNCHANGED'}), approved={changed.approved}, "
        f"ran {ran_unapproved or '-'} under the earlier decision"
        + (f" (another gate held {held_elsewhere})" if held_elsewhere else ""),
    )
    return findings


def degradation_findings(probe: Probe, outcome: ProbeOutcome) -> list[Finding]:
    """Score one durable-launcher turn on *where* the outage was announced, not merely whether.

    The announcement only helps if it arrives before the first output event. Three findings for
    three failure modes: not announced, announced late, or the durable launcher never reached
    (nothing to be degraded about).
    """
    findings: list[Finding] = []

    def finding(check: str, ok: bool, observed: str) -> None:
        findings.append(Finding(probe_id=probe.id, check=check, ok=ok, observed=observed))

    finding(
        "the outage was announced",
        bool(outcome.degraded),
        f"capability_degraded named {outcome.degraded or 'nothing'}",
    )
    if outcome.first_degraded_index is None:
        finding(
            "announced before the first token",
            False,
            "no capability_degraded event, so there is no ordering to check",
        )
    elif outcome.first_output_index is None:
        # Degraded and then silent. The ordering claim is vacuously satisfied and reporting it as a
        # pass would be the harness's own kind of fabrication, so it is reported as unmeasurable.
        finding(
            "announced before the first token",
            False,
            f"degraded at event {outcome.first_degraded_index}; the turn produced no token or "
            "answer at all, so the ordering cannot be read",
        )
    else:
        finding(
            "announced before the first token",
            outcome.first_degraded_index < outcome.first_output_index,
            f"degraded at event {outcome.first_degraded_index}, first token/answer at "
            f"{outcome.first_output_index}",
        )
    if probe.expects_tools:
        finding(
            "the durable launcher was reached",
            bool(outcome.expected_tools_met),
            f"called {outcome.tools_called or '-'}; expected any of {probe.expects_tools}",
        )
    return findings
