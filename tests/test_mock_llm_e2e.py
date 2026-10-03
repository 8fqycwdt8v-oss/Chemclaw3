"""The mock's selection rule and the browser suite's scripted workflows (`cli/e2e_behaviours.py`).

Two halves. The first holds `MockLlm.select` and `already_has_tool_results` to the *turn*: the
newest marked user message decides, and only this turn's tool results end a pass — the rule the
kind suite measured broken (an `[[f-slow]]` asked after an `[[a-cheap]]` answered in 0.4 s as
`a-cheap`, because the opening marker stayed in every resent thread). The second drives each `e2e:*`
behaviour through `decide_turn` with the request shapes a real client sends, and — where it is
cheap — through the agent graph itself, with a real `ChatOpenAI` posting to the mock's own app, so
what is asserted is what the system did with the scripted model rather than what the script meant.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from chemclaw.cli import e2e_behaviours as e2e
from chemclaw.cli.delegation_behaviours import DELEGATION_BEHAVIOURS
from chemclaw.cli.mock_llm import (
    Behaviour,
    DecidedTurn,
    MockLlm,
    ToolCall,
    _paced,
    _within_declared,
    already_has_tool_results,
    build_app,
    catalogue,
    decide_turn,
)
from chemclaw.cli.storm_behaviours import BEHAVIOURS as STORM_BEHAVIOURS

# --------------------------------------------------------------------------------------------
# Request shapes


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _called(*names: str) -> dict[str, Any]:
    """An assistant message that called `names`, in the chat-completions shape."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"call_{n}", "type": "function", "function": {"name": n, "arguments": "{}"}}
            for n in names
        ],
    }


def _tool(content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "call_x", "content": content}


def _answer(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _body(*messages: dict[str, Any], system: str = "You are Chemclaw.") -> dict[str, Any]:
    return {"model": "mock", "messages": [{"role": "system", "content": system}, *messages]}


def _decide(mock: MockLlm, payload: dict[str, Any]) -> Behaviour:
    decided = decide_turn(mock, payload)
    assert isinstance(decided, DecidedTurn), decided
    return decided.behaviour


@pytest.fixture(scope="module")
def served() -> MockLlm:
    """The kind cluster's catalogue, validated against the live tool surface once."""
    return MockLlm(catalogue("e2e"))


# --------------------------------------------------------------------------------------------
# Selection: the newest marked message decides


def test_a_later_marker_replaces_the_conversations_opening_one(served: MockLlm) -> None:
    """The kind suite's measurement: `[[f-slow]]` after `[[a-cheap]]` must be `f-slow`."""
    payload = _body(
        _user("[[a-cheap]] opener"),
        _called("find_notes"),
        _tool("{}"),
        _answer("Two notes cover this coupling."),
        _user("[[f-slow]] now slowly"),
    )
    assert served.select(payload).name == "f-slow"


def test_an_unmarked_follow_up_inherits_the_last_marker(served: MockLlm) -> None:
    """What a shared session's queued message and a plan's "Continue" rely on."""
    payload = _body(
        _user("[[a-cheap]] first"),
        _answer("ok"),
        _user("[[f-slow]] second"),
        _answer("ok"),
        _user("bob queued, no marker"),
    )
    assert served.select(payload).name == "f-slow"


def test_within_one_message_the_first_marker_by_position_wins(served: MockLlm) -> None:
    """Catalogue order no longer decides inside a message: `f-slow` precedes `a-cheap` in it."""
    assert served.select(_body(_user("[[f-slow]] then [[a-cheap]]"))).name == "f-slow"


def test_a_marker_outside_the_user_messages_does_not_override_one_inside(served: MockLlm) -> None:
    """A tool result or an assistant message quoting a marker is data, not the chemist's ask."""
    payload = _body(_user("[[a-cheap]] ask"), _called("find_notes"), _tool("see [[f-slow]]"))
    assert served.select(payload).name == "a-cheap"


def test_with_no_marked_user_message_the_whole_request_scan_still_answers(
    served: MockLlm,
) -> None:
    """The fallback is the old rule, so a request marked only elsewhere is served as before."""
    payload = _body(_user("no marker here"), system="instructions naming [[f-slow]]")
    assert served.select(payload).name == "f-slow"
    assert served.select(_body(_user("nothing at all"))).name == STORM_BEHAVIOURS[0].name


def test_the_delegation_caller_and_its_helper_still_select_their_own() -> None:
    """The one catalogue whose order was load-bearing: the caller's second pass carries both."""
    mock = MockLlm(DELEGATION_BEHAVIOURS)
    caller = _body(
        _user("[[d-delegates]] what recurs?"),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_task",
                    "type": "function",
                    "function": {
                        "name": "task",
                        "arguments": '{"description": "[[d-helper-report]]"}',
                    },
                }
            ],
        },
        _tool("the helper's report"),
    )
    helper = _body(_user("[[d-helper-report]] Read the corpus"))
    assert mock.select(caller).name == "d-delegates"
    assert mock.select(helper).name == "d-helper-report"


def test_tool_results_count_only_within_this_turn() -> None:
    """Turn two must call its tools, not inherit turn one's result and answer blind."""
    turn_one_done = _body(_user("[[a-cheap]] one"), _called("find_notes"), _tool("{}"))
    turn_two = _body(
        _user("[[a-cheap]] one"), _called("find_notes"), _tool("{}"), _answer("ok"), _user("two")
    )
    continuation = {"model": "mock", "input": [{"type": "function_call_output", "output": "{}"}]}
    assert already_has_tool_results(turn_one_done)
    assert not already_has_tool_results(turn_two)
    assert already_has_tool_results(continuation)


def test_turn_two_of_a_conversation_calls_its_tool_again(served: MockLlm) -> None:
    """The consequence, end to end through `decide_turn`."""
    payload = _body(
        _user("[[a-cheap]] one"), _called("find_notes"), _tool("{}"), _answer("ok"), _user("two")
    )
    assert [c.tool for c in _decide(served, payload).calls] == ["find_notes"]


# --------------------------------------------------------------------------------------------
# The catalogue


def test_the_e2e_catalogue_extends_the_storms_without_a_collision() -> None:
    """The one deliberate union: the storm's list first, every addition namespaced `e2e:`."""
    served = catalogue("e2e")
    names = [b.name for b in served]
    assert served[: len(STORM_BEHAVIOURS)] == STORM_BEHAVIOURS
    assert len(names) == len(set(names))
    added = names[len(STORM_BEHAVIOURS) :]
    assert added and all(name.startswith("e2e:") for name in added)
    assert not any(b.name.startswith("e2e:") for b in STORM_BEHAVIOURS)
    for behaviour in e2e.E2E_BEHAVIOURS:
        assert "[[" not in behaviour.text, "a marker in an answer would select a behaviour"


def test_the_kind_mock_serves_the_e2e_catalogue() -> None:
    """The browser suite's markers exist only if the cluster's mock is started on this catalogue."""
    from pathlib import Path

    manifest = Path(__file__).parents[1] / "deploy/kind/manifests/mock-llm.yaml"
    assert '"--catalogue", "e2e"' in manifest.read_text()


def test_a_scripted_pass_is_held_to_its_declared_templates() -> None:
    """The LOAD-1 guard over what a script emits at request time."""
    declared = Behaviour(name="s", calls=[ToolCall(tool="find_notes", arguments={"text": "x"})])
    ok = Behaviour(name="s", calls=[ToolCall(tool="find_notes", arguments={"text": "y"})])
    assert _within_declared(declared, ok) is ok
    with pytest.raises(ValueError, match="none of its templates"):
        _within_declared(declared, Behaviour(name="s", calls=[ToolCall("expand_note", {})]))
    with pytest.raises(ValueError, match="does not declare"):
        _within_declared(
            declared, Behaviour(name="s", calls=[ToolCall("find_notes", {"query": "x"})])
        )


# --------------------------------------------------------------------------------------------
# e2e:plan


def test_the_plan_is_proposed_with_a_declaration_and_then_said_to_await_approval(
    served: MockLlm,
) -> None:
    """Turn one: `write_todos` declaring the tool, then prose saying nothing ran."""
    from chemclaw.agent.plan_gate import plan_identity
    from chemclaw.agent.plan_scope import declared_scope

    ask = _user("[[e2e:plan]] compute the ammonia reaction energy")
    first = _decide(served, _body(ask))
    assert [c.tool for c in first.calls] == ["write_todos"]
    steps = first.calls[0].arguments["todos"]
    assert plan_identity(steps) is not None
    assert declared_scope(steps) == {"compute_reaction_energy"}

    second = _decide(served, _body(ask, _called("write_todos"), _tool("Updated todo list")))
    assert second.calls == [] and second.text == e2e.PLAN_PROPOSED


def test_the_turn_after_the_plan_runs_its_step_and_reports_what_happened(
    served: MockLlm,
) -> None:
    """Turn two (the UI's "Go ahead with the approved plan."): the step, then ran or refused."""
    from chemclaw.connectors.calc.specs import ReactionJobSpec

    thread = [
        _user("[[e2e:plan]] compute the ammonia reaction energy"),
        _called("write_todos"),
        _tool("Updated todo list"),
        _answer(e2e.PLAN_PROPOSED),
        _user("Go ahead with the approved plan."),
    ]
    run = _decide(served, _body(*thread))
    assert [c.tool for c in run.calls] == ["compute_reaction_energy"]
    ReactionJobSpec.model_validate(run.calls[0].arguments["params"])

    done = [*thread, _called("compute_reaction_energy"), _tool("dE -9.1 ± 3 kcal/mol")]
    assert _decide(served, _body(*done)).text == e2e.PLAN_RAN
    refused = [
        *thread,
        _called("compute_reaction_energy"),
        _tool("compute_reaction_energy changes stored data …, has not been approved yet"),
    ]
    assert _decide(served, _body(*refused)).text == e2e.PLAN_REFUSED


def test_a_new_plan_marker_proposes_again(served: MockLlm) -> None:
    """A plan made before *this* marker belongs to the earlier ask."""
    payload = _body(
        _user("[[e2e:plan]] one"),
        _called("write_todos"),
        _tool("ok"),
        _answer("proposed"),
        _user("[[e2e:plan]] another one"),
    )
    assert [c.tool for c in _decide(served, payload).calls] == ["write_todos"]


# --------------------------------------------------------------------------------------------
# e2e:remember and e2e:conditions


def _recommends_dcm(answer: str) -> bool:
    """`Chemclaw3_ui` `e2e/kind/07-preferences.spec.ts`'s own judgement, transcribed."""
    text = answer.lower()
    return bool(
        re.search(
            r"\b(use|in|solvent:?)\s+(dry\s+|anhydrous\s+)?(dichloromethane|dcm|ch2cl2)\b", text
        )
    ) and not re.search(
        r"(avoid|not|never|instead of|forbidden|excluded)[^.]{0,60}(dichloromethane|dcm)", text
    )


def _section(*entries: tuple[str, str]) -> str:
    from chemclaw.agent.preferences import Preference, standing_preferences_section

    return standing_preferences_section([Preference(key=k, value=v) for k, v in entries])


def test_remember_stores_the_dcm_prohibition(served: MockLlm) -> None:
    """The stored arguments the UI asserts on, then a confirmation once the tool has answered."""
    ask = _user("[[e2e:remember]] never DCM")
    first = _decide(served, _body(ask))
    assert [(c.tool, c.arguments) for c in first.calls] == [
        ("remember_preference", {"key": e2e.PREFERENCE_KEY, "value": e2e.PREFERENCE_VALUE})
    ]
    assert re.search(r"dichloromethane|DCM", e2e.PREFERENCE_VALUE)
    second = _decide(served, _body(ask, _called("remember_preference"), _tool("Remembered")))
    assert second.calls == [] and second.text


def test_conditions_echo_the_section_and_keep_to_it(served: MockLlm) -> None:
    """The section reached the model, it says so, and the solvent is the first one not excluded."""
    system = "You are Chemclaw.\n\n" + _section((e2e.PREFERENCE_KEY, e2e.PREFERENCE_VALUE))
    answer = _decide(served, _body(_user("[[e2e:conditions]] EDC coupling"), system=system))
    assert answer.calls == []
    assert answer.text.startswith(e2e.PREFERENCES_RECEIVED)
    assert e2e.PREFERENCE_KEY in answer.text
    assert "in DMF" in answer.text
    assert not _recommends_dcm(answer.text), answer.text


def test_without_the_section_the_answer_says_so_and_recommends_what_it_would_have_avoided(
    served: MockLlm,
) -> None:
    """A lost section is visible twice: the answer says none arrived, and it recommends DCM."""
    answer = _decide(served, _body(_user("[[e2e:conditions]] EDC coupling")))
    assert answer.text.startswith(e2e.PREFERENCES_ABSENT)
    assert _recommends_dcm(answer.text), answer.text


def test_two_exclusions_move_the_choice_down_the_list(served: MockLlm) -> None:
    system = _section(("no_dcm", "no dichloromethane"), ("no_dmf", "DMF is prohibited (REACH)"))
    answer = _decide(served, _body(_user("[[e2e:conditions]]"), system=system))
    assert "in acetonitrile" in answer.text
    assert "avoiding dichloromethane (DCM), DMF" in answer.text


# --------------------------------------------------------------------------------------------
# e2e:cite

_SWEEP = (
    "EvidenceSweep(chunks=[EvidenceChunk(content='Similar reaction amide-17 (Tanimoto 0.71)', "
    "source_note_id='reaction-eln-acme.ELN-2024-0117', retriever='reaction-fingerprint'), "
    "EvidenceChunk(content='Similar reaction (Tanimoto 0.66)', "
    "source_note_id='reaction-eln-ord.suzuki-flow-hte-04620', retriever='reaction-fingerprint')])"
)
_NOTES = (
    "NoteSearch(hits=[NoteHit(id='failure-dcm-amide-coupling', title='Amide coupling in DCM')])"
)


def test_cite_searches_the_store_before_it_cites_anything(served: MockLlm) -> None:
    """First pass: the structure search on the default anchor, and the note search."""
    first = _decide(served, _body(_user("[[e2e:cite]] EDC couplings")))
    assert [(c.tool, c.arguments) for c in first.calls] == [
        ("gather_evidence", {"query": e2e.CITE_QUERY, "reaction_smiles": e2e.CITE_ANCHOR}),
        ("find_notes", {"text": e2e.CITE_QUERY}),
    ]
    assert first.text == ""


def test_cite_takes_a_named_record_or_an_anchor_from_the_message(served: MockLlm) -> None:
    named = _decide(served, _body(_user("[[e2e:cite]] reaction-eln-ord.suzuki-flow-hte-04620")))
    assert named.calls[0].tool == "expand_note"
    assert named.calls[0].arguments["note_id"] == "reaction-eln-ord.suzuki-flow-hte-04620"
    anchored = _decide(served, _body(_user("[[e2e:cite]] like CCO.CC(=O)O>>CCOC(C)=O please")))
    assert anchored.calls[0].arguments["reaction_smiles"] == "CCO.CC(=O)O>>CCOC(C)=O"


def test_cite_cites_the_first_record_and_note_that_came_back(served: MockLlm) -> None:
    ask = _user("[[e2e:cite]] EDC couplings")
    answer = _decide(
        served, _body(ask, _called("gather_evidence", "find_notes"), _tool(_SWEEP), _tool(_NOTES))
    )
    assert "reaction-eln-acme.ELN-2024-0117" in answer.text
    assert "failure-dcm-amide-coupling" in answer.text
    assert "suzuki-flow-hte-04620" not in answer.text


def test_cite_prefers_the_record_the_message_named_when_it_came_back(served: MockLlm) -> None:
    ask = _user("[[e2e:cite]] reaction-eln-ord.suzuki-flow-hte-04620")
    answer = _decide(served, _body(ask, _called("expand_note", "find_notes"), _tool(_SWEEP)))
    assert "reaction-eln-ord.suzuki-flow-hte-04620" in answer.text


def test_cite_invents_nothing_when_nothing_came_back(served: MockLlm) -> None:
    """Prose that says "reaction-energy" is not a record, and an empty search cites nothing."""
    ask = _user("[[e2e:cite]] EDC couplings")
    empty = _tool("EvidenceSweep(chunks=[]) the reaction-energy job is unrelated")
    answer = _decide(served, _body(ask, _called("gather_evidence", "find_notes"), empty))
    assert answer.text == e2e.CITE_NOTHING


# --------------------------------------------------------------------------------------------
# e2e:long-job


def test_the_long_job_launches_a_startable_campaign_unique_to_the_ask(served: MockLlm) -> None:
    """A spec the launcher's own precondition accepts, on the objective that waits for a person."""
    from chemclaw.connectors.jobs import job_workflow_id
    from chemclaw.science.bo.objectives import is_measured
    from chemclaw.science.bo.problem import CampaignSpec, require_campaign_startable

    def launched(text: str) -> dict[str, Any]:
        calls = _decide(served, _body(_user(f"[[e2e:long-job]] {text}"))).calls
        assert [c.tool for c in calls] == ["start_optimization_campaign"]
        assert calls[0].arguments["rationale"]
        params: dict[str, Any] = calls[0].arguments["params"]
        return params

    first, again, other = launched("k4-abc start"), launched("k4-abc start"), launched("k4-xyz")
    spec = CampaignSpec.model_validate(first)
    require_campaign_startable(spec)
    assert is_measured(spec.objective_name)
    ids = {job_workflow_id("bo", "start_optimization_campaign", p) for p in (first, again, other)}
    assert len(ids) == 2, "the same ask is one job; a different ask is another"


def test_the_long_job_quotes_the_id_the_launcher_returned(served: MockLlm) -> None:
    from chemclaw.connectors.jobs import job_workflow_id

    job = job_workflow_id("bo", "start_optimization_campaign", e2e.campaign_spec(7))
    ask = _user("[[e2e:long-job]] k4-abc")
    answer = _decide(served, _body(ask, _called("start_optimization_campaign"), _tool(job)))
    assert job in answer.text
    nothing = _decide(served, _body(ask, _called("start_optimization_campaign"), _tool("Error")))
    assert nothing.text == e2e.LONG_JOB_NO_ID


# --------------------------------------------------------------------------------------------
# e2e:slow, over the wire


def test_the_slow_turn_streams_its_text_over_time_rather_than_all_at_once() -> None:
    """Frames are spread over `stream_seconds`, the first one at once. A lower bound only.

    Timed on `_paced`, the generator both encoders draw their text frames from, rather than over
    `httpx.ASGITransport`: that transport hands the client the response body only once the app has
    finished, so every frame would appear to arrive at the same instant.
    """
    slow = next(b for b in e2e.E2E_BEHAVIOURS if b.name == e2e.SLOW)
    assert slow.calls == [] and slow.stream_seconds == e2e.SLOW_STREAM_SECONDS

    async def arrivals() -> tuple[list[float], str]:
        start = time.monotonic()
        stamps: list[float] = []
        text = ""
        async for chunk in _paced(replace(slow, stream_seconds=0.6)):
            stamps.append(time.monotonic() - start)
            text += chunk
        return stamps, text

    stamps, text = asyncio.run(arrivals())
    assert text == slow.text
    assert len(stamps) > 5
    assert stamps[0] < 0.3, "the first frame waits for nothing"
    assert stamps[-1] - stamps[0] >= 0.5


# --------------------------------------------------------------------------------------------
# Through the agent graph: a real `ChatOpenAI` against the mock's own app


def _graph_turn(app: Any, text: str, **build: Any) -> dict[str, Any]:
    """One turn of `build_langgraph_agent` whose model is a `ChatOpenAI` posting to `app`."""
    from langchain_openai import ChatOpenAI

    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            model = ChatOpenAI(
                model="mock",
                base_url="http://mock/v1",
                api_key=SecretStr("unused"),
                http_async_client=client,
                max_retries=0,
                # Streamed, as every turn the front door runs is: the mock's non-streaming body
                # carries prose only, so an unstreamed graph would never see a tool call.
                streaming=True,
            )
            agent = build_langgraph_agent(model, audit_sink=NullAuditSink(), **build)
            result: dict[str, Any] = await agent.ainvoke({"messages": [("user", text)]})
            return result

    return asyncio.run(run())


def _final_text(result: dict[str, Any]) -> str:
    return str(result["messages"][-1].text)


def test_a_preference_stored_on_one_turn_reaches_a_later_conversations_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remember_preference` runs for real, and the next thread's model call carries the section."""
    from chemclaw.agent.preferences import PreferenceStore
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr("chemclaw.agent.preferences._STORE", PreferenceStore())
    monkeypatch.setattr("chemclaw.agent.preferences.require_actor", lambda: "anna")
    app = build_app(MockLlm(e2e.E2E_BEHAVIOURS))

    stored = _graph_turn(app, "[[e2e:remember]] never DCM in my lab")
    assert any(getattr(m, "name", None) == "remember_preference" for m in stored["messages"]), (
        stored["messages"]
    )
    import chemclaw.agent.preferences as module

    remembered = asyncio.run(module._STORE.recall("anna"))
    assert [(p.key, p.value) for p in remembered] == [(e2e.PREFERENCE_KEY, e2e.PREFERENCE_VALUE)]

    answer = _final_text(_graph_turn(app, "[[e2e:conditions]] EDC/HOBt amide coupling"))
    assert answer.startswith(e2e.PREFERENCES_RECEIVED), answer
    assert not _recommends_dcm(answer), answer


def test_a_plan_proposed_through_the_graph_lands_in_the_todo_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`write_todos` is bound by the harness, accepted with its declaration, and kept in state."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "harness_enabled", True)
    app = build_app(MockLlm(e2e.E2E_BEHAVIOURS))
    result = _graph_turn(app, "[[e2e:plan]] compute the ammonia reaction energy")
    assert [t["content"] for t in result.get("todos", [])] == [e2e.PLAN_STEP], result
    assert result["todos"][0]["tools"] == ["compute_reaction_energy"]
    assert _final_text(result) == e2e.PLAN_PROPOSED
    json.dumps(result["todos"])  # what the plan route and the stream serialize
