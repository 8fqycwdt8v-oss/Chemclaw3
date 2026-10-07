"""The scripted double's catalogue for the delegation suite — what the mock model does, per arm.

Separate from `cli/storm_behaviours.py` because every storm behaviour must be driven by a check in
`cli/live_storm.py`; `tests/test_delegation_run.py` holds the same property here against
`cli/live_probes.py`.

Scripting a call to `task` is honest: it is a first-party tool resolved against the live surface,
reaching a graph `build_langgraph_agent` compiled. The double supplies only the decision to
delegate, so a run is evidence that the runner observes a real delegation (audit row, helper model
call, bill) — not that a model would delegate or that delegating pays.

Every behaviour's `text` is judge verdict JSON: `evals/live_judge` grades with a second call whose
prompt quotes the probe's question, so the same marker selects the same behaviour and its text is
the verdict. No marker may appear in an answer, since `[[…]]` parses as a note citation; the one
marker below sits in a `task` argument.
"""

from __future__ import annotations

import json

from chemclaw.cli.mock_llm import Behaviour, ToolCall

#: The helper's own behaviour, selected by the marker the `task` description carries. It calls
#: no tool and writes a short report, the shape a helper actually returns.
HELPER_MARKER = "d-helper-report"


def _verdict(reason: str) -> str:
    """One judge verdict, as `evals/live_judge.judge_outcome` parses it.

    `json.dumps`, not a literal: an unparseable reply is `ungraded`, which turns a repeat into a
    hole rather than an observation.
    """
    return json.dumps({"verdict": "served", "reason": reason, "fabricated_claims": []})


#: The catalogue, in selection order. `MockLlm.select` takes the first `[[name]]` in the request,
#: and a caller's second pass carries both its question and the `task` arguments it wrote, so
#: `d-delegates` must precede `d-helper-report`.
DELEGATION_BEHAVIOURS: list[Behaviour] = [
    # ------------------------------------------------------------------ the baseline's compliance
    Behaviour(
        name="d-no-helper",
        calls=[ToolCall(tool="find_notes", arguments={"text": "palladium coupling conditions"})],
        text=_verdict("the baseline read the corpus itself and answered"),
        # Billed from the serialized request, not a constant: the experiment's cost axis is that a
        # delegating caller's context is smaller, which a constant bill could never show.
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
                    # `general-purpose` is the name `agent/subagents.py` claims so
                    # `create_deep_agent` inserts no ungoverned helper; any other name would reach
                    # upstream's roster entry.
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
    # It calls nothing: `transfer_to_<peer>` tools are minted from deployment-chosen profile names
    # (`agent/handoff.py`), which this catalogue cannot know. Against this double the peer arm is
    # reported `undelegated` (intention to treat). A real handoff needs `CHEMCLAW_AGENT_PEER_ROSTER`
    # on both processes and a behaviour naming that deployment's tool.
    Behaviour(
        name="d-hands-off",
        text=_verdict("the peer arm answered without handing the conversation on"),
        input_tokens=None,
    ),
]
