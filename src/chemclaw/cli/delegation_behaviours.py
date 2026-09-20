"""The scripted double's catalogue for the delegation suite — what the mock model does, per arm.

Its own module rather than an addition to `cli/storm_behaviours.py`, for the reason that file
gives for existing at all — these are a *measurement's* content and the mock is its mechanism — and
for one this file has of its own: `tests/test_live_storm.py` requires every entry of the storm
catalogue to be driven by some check in `cli/live_storm.py`, so a delegation behaviour declared
there would be a permanently dead entry in a catalogue whose whole point is that it has none.
`tests/test_delegation_run.py` holds the same property over this file against
`cli/live_probes.py`.

**Why a double can be scripted to call `task` at all, and why that is honest.** `task` is not an
adversarial call: it is a first-party tool the agent advertises on every turn, `_validate` resolves
it against the live surface exactly as it resolves `find_notes`, and the graph it reaches is one
`build_langgraph_agent` compiled. What the double supplies is the *decision* to call it, which is
the one thing no credential-free lane can obtain from a model. So a run against this catalogue is
evidence that the runner observes a real delegation correctly — the `audit_events` row, the helper's
own model call, the turn's bill with the helper's spend inside it — and is **not** evidence about
whether a model would delegate, or about whether delegating pays.

**Every behaviour's `text` is the judge's own verdict JSON, and that is a property of the wire
rather than a trick.** `evals/live_judge` grades by making a second model call to the same gateway,
whose prompt quotes the probe's question — so the marker the runner put on the question selects this
same behaviour for the grading call, and whatever `text` says comes back as the verdict. A behaviour
whose text were prose would be `ungraded` on every probe, every repeat would be a hole, and the
suite would record nothing. Writing the JSON here is what makes the answer and the grade one
scripted fact instead of two, and the report says in its first line that the run was against this
double.

**No marker may appear in an answer.** `evals/live._score_citations` parses `[[…]]` out of the
answer as a note citation, so a selector left in `text` would be recorded as an answer citing a note
no tool returned — the highest-severity signal this harness has. The one marker below sits in a
`task` **argument**, where it selects the helper's own behaviour and reaches no answer.
"""

from __future__ import annotations

import json

from chemclaw.cli.mock_llm import Behaviour, ToolCall

#: The helper's own behaviour, selected by the marker the `task` description carries. It calls no
#: tool and writes a short report, which is the shape a helper actually returns: one report, in its
#: caller's thread, defanged rather than framed.
HELPER_MARKER = "d-helper-report"


def _verdict(reason: str) -> str:
    """One judge verdict, as `evals/live_judge.judge_outcome` parses it.

    `json.dumps` rather than a literal, because the parser reads JSON and a hand-written brace is
    the failure that mislabelled 65 of 190 probes in the first live run: an unparseable reply is
    `ungraded`, which is the absence of a grade rather than a bad one, and the repeat then becomes a
    hole instead of an observation.
    """
    return json.dumps({"verdict": "served", "reason": reason, "fabricated_claims": []})


#: The catalogue, in selection order. `MockLlm.select` scans for the first `[[name]]` it finds in
#: the serialized request, so order decides which marker wins when a payload carries two — and one
#: payload does: the caller's second pass carries both its own question and the `task` arguments it
#: wrote on the first. `d-delegates` therefore precedes `d-helper-report`, or a caller would answer
#: as its own helper.
DELEGATION_BEHAVIOURS: list[Behaviour] = [
    # ------------------------------------------------------------------ the baseline's compliance
    Behaviour(
        name="d-no-helper",
        calls=[ToolCall(tool="find_notes", arguments={"text": "palladium coupling conditions"})],
        text=_verdict("the baseline read the corpus itself and answered"),
        # The bill follows the serialized request rather than a constant, because the whole cost
        # axis of this experiment is that a delegating turn's caller context is smaller. With the
        # default constant every arm would bill 900 and `median_token_ratio` would read exactly
        # 1.000 whatever the arms did — a metric with no reachable falsifying value, which is the
        # defect `tasks/lessons.md` rule 6 names.
        input_tokens=None,
    ),
    # ------------------------------------------------------------------ the treatment
    Behaviour(
        name="d-delegates",
        calls=[
            ToolCall(
                tool="task",
                arguments={
                    "description": (
                        f"[[{HELPER_MARKER}]] Read the corpus for what recurs in these conditions "
                        "and report back the recurring conditions, the failures, and where the "
                        "evidence is thin. Cite every note id you used."
                    ),
                    # `general-purpose` is the name `agent/subagents.py` claims so that
                    # `create_deep_agent` does not insert its own ungoverned helper beside ours; a
                    # `subagent_type` naming anything else would reach upstream's roster entry
                    # rather than a graph this repository compiled.
                    "subagent_type": "general-purpose",
                },
            )
        ],
        text=_verdict("the helper read the corpus and its caller answered from the report"),
        input_tokens=None,
    ),
    Behaviour(
        name=HELPER_MARKER,
        text=(
            "Three conditions recur across the entries I read: Pd(OAc)2 with XPhos in toluene at "
            "100 C, the same with SPhos, and a Pd2(dba)3 route that failed twice. The evidence on "
            "deactivated aryl chlorides is thin: two entries, both at one scale."
        ),
        input_tokens=None,
    ),
    # ------------------------------------------------------------------ the peer arm
    #
    # It calls nothing. A `transfer_to_<peer>` tool is minted per peer by `agent/handoff.py` from a
    # profile name the *deployment* chooses, so this catalogue cannot name one — and `_validate`
    # resolves every behaviour's tools against `available_tool_names()`, which does not carry the
    # handoff name space at all. So against this double the peer arm takes no treatment and is
    # reported as `undelegated`, which is the honest intention-to-treat reading of an arm whose
    # posture the front door was not started with. Driving a real handoff needs
    # `CHEMCLAW_AGENT_PEER_ROSTER` and a behaviour naming that deployment's own tool.
    Behaviour(
        name="d-hands-off",
        text=_verdict("the peer arm answered without handing the conversation on"),
        input_tokens=None,
    ),
]
