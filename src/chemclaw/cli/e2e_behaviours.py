"""The browser suite's scripted workflows: what the mock model does on the kind cluster's e2e lane.

The UI repository's real-browser suite (`Chemclaw3_ui` `e2e/kind/`) runs whole workflows against the
whole system on kind with the scripted mock as its model, and eight of its scenarios used to skip
there because nothing in `cli/storm_behaviours.py` could drive them: proposing a plan, storing and
honouring a preference, citing a record in the answer's text, launching a job that stays running
long enough to cancel, and a turn that streams long enough for a second participant to queue behind
it. Each is a model *decision* a fixed plan cannot make, so these behaviours read the request
(`mock_llm.Conversation`) through a `script` — and each still declares its calls as templates, so
`_validate` checks every tool and argument name against the live surface at startup and
`_within_declared` holds every scripted pass to them.

**What a green scenario against this file proves, and what it does not.** It proves the plumbing:
the plan card, the approval and the gate; the preference store, the system-message section and its
delivery to the model; the citation chip over an id a real search returned; the durable launch, the
registry and the cancel; the shared-session queue. It says nothing about whether a model would
decide to do any of it — that is `CHEMCLAW_KIND_LLM=live`'s question, and the suite keeps its
real-model prompts for it.

Its own module, and its own catalogue name (`--catalogue e2e`, served as the storm's list plus this
one), for the reason `cli/delegation_behaviours.py` gives: `tests/test_live_storm.py` requires every
storm entry to be reached by a check in `cli/live_storm.py`, and these are reached by a browser.

**The markers.** Each is `[[e2e:<name>]]` in the user message — namespaced, so no storm name can
collide — and the newest marked user message decides (`MockLlm.select`), so an unmarked follow-up
("Go ahead with the approved plan.") continues the behaviour and a new marker replaces it.

* `[[e2e:plan]]` — on the marked turn, `write_todos` with one step declaring
  `compute_reaction_energy`, then "nothing runs until you approve it"; on any later turn of the
  conversation, `compute_reaction_energy` on a fixed N2 + 3 H2 -> 2 NH3, then "ran" or "refused".
* `[[e2e:remember]]` — `remember_preference` forbidding dichloromethane (DCM), then a confirmation.
* `[[e2e:conditions]]` — no tool; amide-coupling conditions opening with the standing-preferences
  entries the system message carried (or saying none arrived), in the first solvent none excludes.
* `[[e2e:cite]]` — `gather_evidence` on a reaction anchor (or `expand_note` on a `reaction-…` id the
  message names) plus `find_notes`; then cites the first record and the first note that came back.
* `[[e2e:long-job]]` — `start_optimization_campaign` on the `measured` objective, seeded from the
  message; then quotes the job id the launcher returned.
* `[[e2e:slow]]` — no tool; streams its answer over `SLOW_STREAM_SECONDS`.
* `[[e2e:artefact]]` — `create_exhibit` with a `document`, its arguments streamed in
  `ARTEFACT_FRAGMENTS` pieces so the turn stream carries `exhibit_draft` frames before the
  `exhibit` event; then a one-line answer pointing at the artefact.

`deploy/kind/README.md` carries the same list with what a browser test asserts on each, and what
the UI suite should send.
"""

from __future__ import annotations

import re
from dataclasses import replace

from chemclaw.cli.mock_llm import Behaviour, Conversation, ToolCall
from chemclaw.core.ids import stable_hash

# ------------------------------------------------------------------------------- plan

PLAN = "e2e:plan"

#: The reaction the plan proposes and then runs. A fixed `quick` payload on purpose: the scenario is
#: about the gate, not the job, so after the first run anywhere it is a D-011 cache hit and the
#: approved turn answers inside `compute_reaction_energy`'s inline wait.
PLAN_PAYLOAD: dict[str, object] = {
    "kind": "reaction",
    "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["N", "N"],
    "level": "quick",
    "temperature_k": 298.15,
    "symmetry_numbers": {"N#N": 2, "[H][H]": 2, "N": 3},
}
PLAN_STEP = "Compute the GFN2-xTB reaction energy of N2 + 3 H2 -> 2 NH3 at 298 K"
_PLAN_CALL = ToolCall(
    tool="write_todos",
    arguments={
        "todos": [{"content": PLAN_STEP, "status": "pending", "tools": ["compute_reaction_energy"]}]
    },
)
_RUN_CALL = ToolCall(
    tool="compute_reaction_energy",
    arguments={
        "params": PLAN_PAYLOAD,
        "rationale": "e2e: the step of the plan the chemist approved",
    },
)
PLAN_PROPOSED = (
    "I have written a one-step plan: compute the reaction energy of N2 + 3 H2 -> 2 NH3 with "
    "compute_reaction_energy. Nothing runs until you approve it and ask me to go ahead."
)
PLAN_RAN = "I ran the approved step with compute_reaction_energy; its result is on the card above."
PLAN_REFUSED = (
    "The plan gate refused compute_reaction_energy: this plan has no standing approval, so "
    "nothing ran."
)


def _plan(behaviour: Behaviour, conversation: Conversation) -> Behaviour:
    """Propose on the marked turn; execute on any later turn of the same behaviour.

    Which turn this is comes from the thread, not from state: a `write_todos` made after the marked
    message and before this turn's message means the plan was proposed already, so this turn is the
    one an approval (or a decline) was given for.
    """
    results = conversation.tool_results
    if "write_todos" not in conversation.called_since_marker(PLAN):
        return replace(behaviour, calls=[] if results else [_PLAN_CALL], text=PLAN_PROPOSED)
    if not results:
        return replace(behaviour, calls=[_RUN_CALL], text="")
    refused = any("approv" in result.lower() for result in results)
    return replace(behaviour, calls=[], text=PLAN_REFUSED if refused else PLAN_RAN)


# ------------------------------------------------------------------------------- preferences

REMEMBER = "e2e:remember"
CONDITIONS = "e2e:conditions"

PREFERENCE_KEY = "forbidden_solvent_dcm"
PREFERENCE_VALUE = "Never use dichloromethane (DCM) as a solvent: it is forbidden in this lab."

#: Candidate solvents for the amide coupling, in order of preference, each with the spellings an
#: entry excluding it would use. The first one the standing preferences do not name is chosen, so
#: with no preference the answer recommends DCM — which is what makes a lost section visible.
_SOLVENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dichloromethane (DCM)", ("dichloromethane", "dcm", "ch2cl2")),
    ("DMF", ("dmf", "dimethylformamide")),
    ("acetonitrile", ("acetonitrile", "mecn")),
    ("2-MeTHF", ("2-methf", "methyltetrahydrofuran")),
)

#: The opening words of the conditions answer in each case — exported for the tests and for the UI
#: suite, which asserts on them to tell "the section reached the model" from "it did not".
PREFERENCES_RECEIVED = "Standing preferences received:"
PREFERENCES_ABSENT = "No standing preferences reached me."


def _standing_entries(system_text: str) -> list[str]:
    """The entry lines of the standing-preferences section in `system_text`, or none.

    Found by the section's own heading constant, then every `- ` line that follows it, so this
    reads what `agent/preferences.standing_preferences_section` rendered and nothing else.
    """
    from chemclaw.agent.preferences import STANDING_PREFERENCES_HEAD

    _, found, rest = system_text.partition(STANDING_PREFERENCES_HEAD)
    if not found:
        return []
    entries: list[str] = []
    for line in rest.lstrip("\n").splitlines():
        if not line.startswith("- "):
            break
        entries.append(line[2:].strip())
    return entries


def _conditions(behaviour: Behaviour, conversation: Conversation) -> Behaviour:
    """Amide-coupling conditions that keep to the standing preferences this request carried.

    Deterministic by construction: the solvent is the first candidate no entry names. And it says
    what arrived, so a browser test can assert the plumbing rather than infer it from the solvent.
    """
    entries = _standing_entries(conversation.system_text)
    excluded = " ".join(entries).lower()
    solvent = next(
        (name for name, spellings in _SOLVENTS if not any(s in excluded for s in spellings)),
        _SOLVENTS[-1][0],
    )
    avoided = [name for name, spellings in _SOLVENTS if any(s in excluded for s in spellings)]
    heard = f"{PREFERENCES_RECEIVED} {'; '.join(entries)}." if entries else PREFERENCES_ABSENT
    conditions = (
        f"Conditions: EDC·HCl (1.2 equiv) and HOBt (1.2 equiv) with DIPEA (2 equiv) in {solvent} "
        "at room temperature for 12 h."
    )
    keeping = (
        f" This keeps to your standing preferences by avoiding {', '.join(avoided)}."
        if avoided
        else ""
    )
    return replace(behaviour, calls=[], text=f"{heard}\n\n{conditions}{keeping}")


# ------------------------------------------------------------------------------- citations

CITE = "e2e:cite"

#: The structural anchor when the message names none: benzoic acid + aniline to N-phenylbenzamide,
#: the transformation of the mock's seeded `uspto-amide-coupling-1/2` records.
#:
#: **The anchor decides whether, not only which.** `find_similar_reactions` drops every hit below
#: `fingerprint_similarity_threshold` (0.3), so an anchor the corpus does not hold returns nothing
#: and leaves no `reaction-…` id to cite. This used to be benzoic acid + *benzylamine*, on the
#: belief that the search had no threshold: measured on the kind cluster, its nearest seeded
#: reaction scored 0.168, the fingerprint leg returned 0 chunks, and the answer cited a note alone.
#: This anchor scores 0.387 against the seeded records (whose reaction keeps EDC and HOBt on the
#: left, which is why it is not 1.0). `tests/test_mock_llm_e2e.py` recomputes both scores offline
#: against that record's shape, so a change of anchor, threshold or fingerprint cannot quietly
#: return the lane to citing nothing.
CITE_ANCHOR = "O=C(O)c1ccccc1.Nc1ccccc1>>O=C(Nc1ccccc1)c1ccccc1"
CITE_QUERY = "amide coupling"
_CITE_TEMPLATES = [
    ToolCall(
        tool="gather_evidence", arguments={"query": CITE_QUERY, "reaction_smiles": CITE_ANCHOR}
    ),
    ToolCall(tool="expand_note", arguments={"note_id": "reaction-x", "hops": 1}),
    ToolCall(tool="find_notes", arguments={"text": CITE_QUERY}),
]

#: An ELN/ORD record id as `kg.note.note_id_for_reaction` mints it: `reaction-<source>.<id>` or a
#: bare `reaction-<id>`. A digit or a `.` qualifier is required, as the UI's chip pattern requires,
#: so "reaction-energy" in prose is not mistaken for a record.
_REACTION_ID = re.compile(
    r"\breaction-(?=[A-Za-z0-9_-]*(?:[0-9]|\.[A-Za-z0-9]))[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]"
)
#: A knowledge-note id: the prefixes `knowledge/` files its notes under (the UI's chip list).
_NOTE_ID = re.compile(
    r"\b(?:compound|rxn|playbook|campaign|opt|interaction|report|failure|proposal|bo-candidate|"
    r"job-result)-[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]"
)
_SMILES_ANCHOR = re.compile(r"\S+>>\S+")
CITE_NOTHING = "The search returned no ELN or ORD record and no knowledge note to cite."


def _cite(behaviour: Behaviour, conversation: Conversation) -> Behaviour:
    """Search the store first, then cite the record and the note that actually came back.

    A `reaction-…` id in the message is looked up directly (`expand_note` resolves a record the
    graph does not hold against the transcription store); otherwise the structure search runs on
    the message's reaction SMILES, or on `CITE_ANCHOR`. Nothing is cited that no tool returned.
    """
    asked = conversation.marked_text(CITE)
    results = conversation.tool_results
    named = _REACTION_ID.search(asked)
    if not results:
        anchor = _SMILES_ANCHOR.search(asked)
        lookup = (
            ToolCall(tool="expand_note", arguments={"note_id": named.group(0), "hops": 1})
            if named
            else ToolCall(
                tool="gather_evidence",
                arguments={
                    "query": CITE_QUERY,
                    "reaction_smiles": anchor.group(0) if anchor else CITE_ANCHOR,
                },
            )
        )
        notes = ToolCall(tool="find_notes", arguments={"text": CITE_QUERY})
        return replace(behaviour, calls=[lookup, notes], text="")
    returned = "\n".join(results)
    records = _REACTION_ID.findall(returned)
    record = named.group(0) if named and named.group(0) in records else next(iter(records), None)
    note = next(iter(_NOTE_ID.findall(returned)), None)
    if record is None and note is None:
        return replace(behaviour, calls=[], text=CITE_NOTHING)
    parts = []
    if record is not None:
        parts.append(f"The record the search returned is {record}, an experimental run on file")
    if note is not None:
        parts.append(f"the knowledge note {note} is what we know about the chemistry")
    return replace(behaviour, calls=[], text="; ".join(parts) + ".")


# ------------------------------------------------------------------------------- a long job

LONG_JOB = "e2e:long-job"

#: A campaign on the `measured` objective suspends on a person after its seed batch
#: (`connectors/bo/workflows._measure`) for `bo_measurement_deadline_days`, so it is *running* for
#: as long as a test needs and costs nothing while it waits — unlike a conformer search, whose
#: duration is the workstation's. `bo.start_optimization_campaign` is funded by the shipped
#: `connector_jobs_awaiting_answer`, and a cancel reaches the wait
#: (`ParentClosePolicy.REQUEST_CANCEL`).
_CAMPAIGN_PROBLEM: dict[str, object] = {
    "parameters": [
        {"kind": "continuous", "name": "temperature_c", "lower": 20.0, "upper": 100.0},
        {"kind": "continuous", "name": "equivalents", "lower": 1.0, "upper": 3.0},
    ],
    "objectives": [{"name": "yield", "direction": "maximize"}],
}


def campaign_spec(seed: int) -> dict[str, object]:
    """The `CampaignSpec` the long job launches, seeded so its workflow id is the seed's own."""
    return {
        "problem": _CAMPAIGN_PROBLEM,
        "objective_name": "measured",
        "n_initial": 4,
        "n_rounds": 3,
        "batch": 1,
        "seed": seed,
    }


def campaign_seed(text: str) -> int:
    """A seed derived from the message that asked, so each distinct ask is a distinct job.

    **Why the seed, and why from the message.** The workflow id is a hash of the payload (D-011), so
    a fixed payload rejoins the first campaign ever launched — after a cancel, a fresh one; while it
    runs, the same one, for every conversation. The mock sees no session id, so the message is the
    only per-conversation input it has: put a run tag in it (the UI suite's `runTag()`) and every
    run launches its own campaign, while the same message twice is deliberately the same job.
    """
    return int(stable_hash(text), 16) % 2**31


_LONG_JOB_TEMPLATE = ToolCall(
    tool="start_optimization_campaign",
    arguments={"params": campaign_spec(0), "rationale": "e2e"},
)
#: A durable job id as `connectors.jobs.job_workflow_id` mints it.
_JOB_ID = re.compile(r"\b(?:bo|calc)-[a-z_]+-[0-9a-f]{6,}\b")
LONG_JOB_NO_ID = "The launch returned no job id, so there is nothing running to point you at."


def _long_job(behaviour: Behaviour, conversation: Conversation) -> Behaviour:
    """Launch a campaign unique to this ask, then quote the id the launcher returned."""
    results = conversation.tool_results
    if not results:
        seed = campaign_seed(conversation.marked_text(LONG_JOB))
        launch = ToolCall(
            tool="start_optimization_campaign",
            arguments={
                "params": campaign_spec(seed),
                "rationale": "e2e: a job that stays running until it is cancelled",
            },
        )
        return replace(behaviour, calls=[launch], text="")
    job = _JOB_ID.search("\n".join(results))
    if job is None:
        return replace(behaviour, calls=[], text=LONG_JOB_NO_ID)
    return replace(
        behaviour,
        calls=[],
        text=(
            f"Launched the campaign as durable job {job.group(0)} — it is waiting for the first "
            "batch of measurements, so it stays running until you report them or cancel it."
        ),
    )


# ------------------------------------------------------------------------------- a slow turn

SLOW = "e2e:slow"
#: How long the slow turn streams. Long enough for a second participant to send, see their place in
#: the queue and withdraw; short enough that the scenario's own timeouts are not the bound.
SLOW_STREAM_SECONDS = 20.0
SLOW_TEXT = " ".join(
    f"Part {n} of a deliberately slow answer, streamed a few words at a time." for n in range(1, 13)
)


# ------------------------------------------------------------------------------- an artefact

ARTEFACT = "e2e:artefact"
ARTEFACT_TITLE = "Amide coupling plan"
#: Long enough that its fragments arrive over a visible stretch of the stream, so the pane can be
#: seen filling in rather than appearing whole — the property the `exhibit_draft` event exists for.
ARTEFACT_MARKDOWN = "\n".join(
    [
        "# Amide coupling plan",
        "",
        "## Goal",
        "",
        "Couple the acid and the amine with EDC/HOBt and isolate the amide by extraction.",
        "",
        "## Steps",
        "",
        *(
            f"{n}. Step {n} of the plan, written out so the draft has something to stream."
            for n in range(1, 13)
        ),
        "",
        "## Checks",
        "",
        "- Conversion by HPLC before work-up.",
        "- Residual EDC urea removed by the acid wash.",
    ]
)
#: How many pieces the call's arguments are streamed in.
ARTEFACT_FRAGMENTS = 40
ARTEFACT_ANSWER = "I drafted the plan as an artefact beside this answer."


E2E_BEHAVIOURS: list[Behaviour] = [
    Behaviour(name=PLAN, calls=[_PLAN_CALL, _RUN_CALL], text=PLAN_PROPOSED, script=_plan),
    Behaviour(
        name=REMEMBER,
        calls=[
            ToolCall(
                tool="remember_preference",
                arguments={"key": PREFERENCE_KEY, "value": PREFERENCE_VALUE},
            )
        ],
        text="Remembered: dichloromethane (DCM) is forbidden as a solvent in your lab.",
    ),
    Behaviour(name=CONDITIONS, text="", script=_conditions),
    Behaviour(name=CITE, calls=_CITE_TEMPLATES, text="", script=_cite),
    Behaviour(name=LONG_JOB, calls=[_LONG_JOB_TEMPLATE], text="", script=_long_job),
    Behaviour(name=SLOW, text=SLOW_TEXT, think_seconds=0.5, stream_seconds=SLOW_STREAM_SECONDS),
    Behaviour(
        name=ARTEFACT,
        calls=[
            ToolCall(
                tool="create_exhibit",
                arguments={
                    "title": ARTEFACT_TITLE,
                    "spec": {"kind": "document", "markdown": ARTEFACT_MARKDOWN},
                },
                fragments=ARTEFACT_FRAGMENTS,
            )
        ],
        text=ARTEFACT_ANSWER,
        # Spread over the fragments (`think_seconds / fragments` between them), so a browser sees
        # several throttled draft frames rather than one.
        think_seconds=1.0,
    ),
]
