"""The storm's behaviour catalogue — what the mock model does, per scenario family.

Kept beside the mock rather than inside it because these are the *test's* content and the mock is
its mechanism: adding a scenario should not mean editing a server. Each behaviour is named, and the
storm selects one by putting `[[name]]` in the turn's message, so a scenario and the behaviour it
asserts against cannot drift apart.

Eight families, and the split is the point — "it held up under load" and "it held up under a model
behaving badly" are different claims, and only one of them was ever testable with a real model:

* **A volume** — cheap, realistic turns, used to find where admission control bends.
* **C shapes** — the same call delivered whole, fragmented, and in parallel.
* **D durable** — real connector jobs, including deliberate idempotency collisions.
* **F adversarial** — what a real model will not reliably do: malformed arguments, an unknown tool,
  an empty function name (the STREAM-1 shape), a 100 KB argument document, forty parallel calls,
  a turn with no prose, and an unbounded tool loop.
* **H edges** — pathological chemistry, semantically impossible arguments, and unicode driven
  through the real tools and the real database.

Families B (tool-path truth) and G (limits) need no behaviour of their own: B is a cross-check over
`audit_events` after the others, and G attacks the front door's own limits rather than the model.
**E (chaos) borrows** — it kills processes around `a-cheap`, `f-slow` and a directly-launched
durable job rather than asking the model for anything new.

Every behaviour here is reached by some check in `cli/live_storm.py`, and
`tests/test_live_storm.py::test_every_declared_behaviour_is_reached_by_some_check` is what makes
that a fact rather than a hope. It was written because the sentence above it was **false when it
was first written**: six behaviours — `a-retrieval`, `d-status`, `f-slow`, `h-bad-smiles`,
`h-injection`, `h-unicode` — were declared and asserted by nothing while the run reported "17/17
checks passed", and after four of them were wired the same claim was made again with two still
dead. Confident prose about coverage is what this repository has learned not to trust, including
its own; a test is the only form of it that stays true.
"""

from __future__ import annotations

import time

from chemclaw.cli.mock_llm import Behaviour, ToolCall

# The reaction the durable family launches. The workflow id is a hash of the payload
# (`connectors.jobs.job_workflow_id`), so many sessions launching *this* simultaneously is the
# idempotency collision the D-011 guarantee is about — and the only honest way to check it is to
# count what the database did, not to read a summary.
#
# **The temperature varies per run, and that is load-bearing rather than decorative.** With a fixed
# payload the *second* storm against one database finds the answer already cached: it launches
# nothing, computes nothing, and satisfies every "at most one run" bound with zero. Measured — a
# storm on 2026-08-04 reported "0 distinct workflow id(s) across 12 turns; calculation_results
# 113 → 113" as a pass. `cli/live_jobs.py` documents this failure at length and designs
# `_RUN_TEMPERATURE_K` against it; the storm inherited the hazard without the fix.
#
# A real physical input rather than a nonce, for the same reason: any temperature in this range is
# a question a chemist could ask, and the answer genuinely changes with it. Constant within the
# process, so all twelve simultaneous launches still derive the identical id — which is the whole
# point of the family.
#
# **The period must outlast the longest run that will use it, and the first version's did not.**
# `% 719` gives 719 distinct temperatures on a one-second grid, so a value recurs every ~12 minutes
# — invisible in a single storm and unmissable in a soak: 6 of 81 rounds failed this family with
# "0 job_records row(s) written", spaced 12 rounds apart at a ~58 s round. Nothing was broken. The
# payload had been computed in an earlier round, `ALLOW_DUPLICATE_FAILED_ONLY` correctly rejoined
# the completed run rather than recomputing it, and no new record was written — D-011 working, read
# as a failure. 100,000 values on a 10-µK grid puts the period at 27.8 hours, past any soak that
# fits in this container, and keeps every value a temperature a chemist could ask about. The same
# modulus is now in all three copies (it had landed in this one only) and `tests/test_run_jitter.py`
# evaluates each expression across a 24-hour window so a fourth copy cannot get a smaller one.
#
# **The base temperature differs per harness and must.** Each grid spans base + [0, 1) K, so two
# copies sharing a base share the whole set — which is what happened when `cli/live_jobs.py` took
# this modulus and kept 298.15, giving two independent harnesses byte-identical payloads and one
# workflow id. This one keeps 298.15; `live_jobs` is 301.15 and `live_storm` 300.0, and
# `tests/test_run_jitter.py` asserts the union is disjoint rather than trusting the arithmetic.
_COLLISION_TEMPERATURE_K = 298.15 + (int(time.time()) % 100_000) / 100_000.0
_COLLISION_PAYLOAD: dict[str, object] = {
    "kind": "reaction",
    "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["N", "N"],
    "level": "quick",
    "temperature_k": _COLLISION_TEMPERATURE_K,
    "symmetry_numbers": {"N#N": 2, "[H][H]": 2, "N": 3},
}

BEHAVIOURS: list[Behaviour] = [
    # ---------------------------------------------------------------- A · volume
    Behaviour(
        name="a-cheap",
        calls=[ToolCall(tool="find_notes", arguments={"text": "suzuki coupling"})],
        text="Two notes cover this coupling; both are cited above.",
        think_seconds=0.4,
    ),
    Behaviour(
        name="a-retrieval",
        calls=[
            ToolCall(tool="find_notes", arguments={"text": "amide coupling"}),
            ToolCall(tool="gather_evidence", arguments={"query": "amide coupling additive"}),
            ToolCall(
                tool="expand_note",
                arguments={"note_id": "failure-dcm-amide-coupling", "hops": 1},
            ),
        ],
        text="The record covers the additive choice and the DCM failure mode.",
        think_seconds=0.4,
    ),
    # ---------------------------------------------------------------- C · streaming shapes
    Behaviour(
        name="c-whole",
        calls=[ToolCall(tool="find_notes", arguments={"text": "buchwald amination"}, fragments=1)],
        text="One call, arguments delivered whole.",
    ),
    Behaviour(
        name="c-fragmented",
        # The hypothesis under test. `ToolCallTrace.feed` treats "name and arguments" as a complete
        # call, and the Responses client puts the name on *every* fragment — so this should either
        # reassemble into one event, or expose N events each carrying a partial document.
        calls=[ToolCall(tool="find_notes", arguments={"text": "buchwald amination"}, fragments=8)],
        text="One call, arguments delivered in eight fragments.",
    ),
    Behaviour(
        name="c-parallel",
        calls=[
            ToolCall(tool="find_notes", arguments={"text": f"probe {i}"}, fragments=3)
            for i in range(6)
        ],
        text="Six interleaved calls, each fragmented.",
    ),
    # ---------------------------------------------------------------- D · durable
    Behaviour(
        name="d-collide",
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": _COLLISION_PAYLOAD,
                    "rationale": "storm: many sessions asking the identical question at once",
                },
            )
        ],
        text="Launched the reaction-energy job.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="d-status",
        calls=[
            ToolCall(tool="find_past_jobs", arguments={"text": "reaction", "connector": "calc"})
        ],
        text="Here is what has run.",
    ),
    # ---------------------------------------------------------------- F · adversarial
    Behaviour(
        name="f-malformed-json",
        # JSON-shaped and **unclosable**, not merely truncated. This is the only argument document
        # that actually reaches `AIMessage.invalid_tool_calls`, which is the field
        # `PromoteInvalidToolCalls` exists to read — so it is the only one that exercises it.
        #
        # It used to be `'{"text": "unterminated'`, and that check could never pass. LangChain runs
        # a streamed call's fragments through `parse_partial_json`, which closes an unterminated
        # string and an unclosed brace, so a truncated document arrives as an ordinary valid call
        # long before anything here sees it. Measured, on the exact two payloads:
        #
        #     '{"text": "unterminated'  -> repaired to {'text': 'unterminated'}
        #     '{"text": }'              -> JSONDecodeError -> invalid_tool_calls
        #
        # `agent/model_calls.py` states this and even corrects an earlier draft of its own docstring
        # for the same confusion. So the old behaviour asserted an outcome the system is documented
        # and measured never to produce, while the reachable case went untested — a permanently red
        # check *and* a blind spot over the middleware written for exactly this.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": }')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-wrong-argument",
        # LOAD-1 itself, reproduced deliberately: `find_notes` takes `text`, not `query`. Every
        # measurement in the 2026-07 load test died here without anyone noticing, so the storm
        # asserts it is *visible* now rather than trusting that it would be.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"query": "benzene"}')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-unknown-tool",
        calls=[ToolCall(tool="tool_that_does_not_exist", arguments={"x": 1})],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-empty-name",
        # The STREAM-1 shape: `String should have at least 1 character` on a tool_use name, which
        # failed 30 of 150 live turns in July and was closed by D-123's `AgentPool`. Nothing has
        # re-exercised it since, because a real model does not emit it on request.
        calls=[ToolCall(tool="", arguments={"text": "x"})],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-huge-arguments",
        calls=[
            ToolCall(
                tool="find_notes",
                arguments={},
                raw_arguments='{"text": "' + ("x" * 100_000) + '"}',
            )
        ],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-call-flood",
        calls=[ToolCall(tool="find_notes", arguments={"text": f"flood {i}"}) for i in range(40)],
        text="Forty calls in one turn.",
        adversarial=True,
    ),
    Behaviour(
        name="f-no-text",
        # The `empty_answer` guard added earlier today: tools ran, nothing was written. Before that
        # guard this turn produced an empty answer and no error at all.
        calls=[ToolCall(tool="find_notes", arguments={"text": "silent"})],
        text="",
    ),
    Behaviour(
        name="f-http-500",
        calls=[],
        text="",
        http_status=500,
        adversarial=True,
    ),
    Behaviour(
        name="f-slow",
        calls=[ToolCall(tool="find_notes", arguments={"text": "slow turn"})],
        text="A deliberately slow turn.",
        think_seconds=8.0,
    ),
    # ---------------------------------------------------------------- H · data edges
    Behaviour(
        name="h-bad-smiles",
        calls=[
            ToolCall(
                tool="gather_evidence",
                arguments={"query": "C1CC", "reaction_smiles": "not>>a>>reaction"},
            )
        ],
        text="",
    ),
    Behaviour(
        name="h-unicode",
        calls=[ToolCall(tool="find_notes", arguments={"text": "咖啡因 · Ω · 🧪 · ünïcødé"})],
        text="Unicode survived the round trip.",
    ),
    Behaviour(
        name="h-impossible-args",
        # Valid JSON, valid types, and impossible to answer: the symmetry-number map names species
        # the equation does not contain. `_checked_symmetry_numbers` refuses exactly this, and it
        # was found the only way such things are found — a chaos payload in `cli/live_jobs.py`
        # inherited the wrong map, the job rejected it correctly, and the lane read as a system
        # fault until someone looked. The missing negative in this family was the shape that is
        # *well-formed and wrong*: a schema check passes it, so the only thing standing between it
        # and a plausible answer is the tool's own domain validation.
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": {
                        "kind": "reaction",
                        # Balanced on purpose, so the symmetry map is the *only* thing wrong with
                        # it. An unbalanced equation would also be refused, and the check would
                        # then pass for a reason other than the one it names.
                        "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
                        "products": ["N", "N"],
                        "level": "quick",
                        "symmetry_numbers": {"c1ccccc1": 12, "CCO": 1},
                    },
                    "rationale": "storm: arguments that parse and cannot be true",
                },
            )
        ],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="h-injection",
        calls=[
            ToolCall(
                tool="find_notes",
                arguments={
                    "text": "'; DROP TABLE audit_events; -- <script>alert(1)</script> {{7*7}}"
                },
            )
        ],
        text="Treated as a search string, which is what it is.",
    ),
]
