"""The storm's behaviour catalogue — what the mock model does, per scenario family.

The test's content, kept apart from the mock's mechanism. The storm selects a behaviour by putting
`[[name]]` in the turn's message, so a scenario and the behaviour it asserts against cannot drift.

* **A volume** — cheap, realistic turns, used to find where admission control bends.
* **C shapes** — the same call delivered whole, fragmented, and in parallel.
* **D durable** — real connector jobs, including deliberate idempotency collisions.
* **F adversarial** — what a real model will not reliably do: malformed arguments, an unknown tool,
  an empty function name, a 100 KB argument document, forty parallel calls, a turn with no prose,
  and an unbounded tool loop.
* **H edges** — pathological chemistry, semantically impossible arguments, and unicode driven
  through the real tools and the real database.

B (tool-path truth) and G (limits) need no behaviour; E (chaos) reuses `a-cheap`, `f-slow` and a
directly launched job.
`tests/test_live_storm.py::test_every_declared_behaviour_is_reached_by_some_check` holds that every
behaviour here is asserted by some check in `cli/live_storm.py`.
"""

from __future__ import annotations

import time

from chemclaw.cli.mock_llm import Behaviour, ToolCall

# The reaction the durable family launches. The workflow id is a hash of the payload, so many
# sessions launching it at once is an idempotency collision, checked by counting database rows.
#
# The temperature varies per run so a second storm against one database does not find the answer
# cached and pass every "at most one run" bound with zero; it is constant within the process so all
# simultaneous launches share one id. 100,000 values on a 10-µK grid give a 27.8-hour period, longer
# than any soak. Each harness uses a different base (here 298.15, `live_jobs` 301.15, `live_storm`
# 300.0) so their grids are disjoint; `tests/test_run_jitter.py` asserts both properties.
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
        # The Responses client puts the name on every fragment; this checks the shipped path emits
        # one call event, not N carrying partial documents.
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
        # JSON-shaped and unclosable, not merely truncated: LangChain's `parse_partial_json` repairs
        # a truncated document into a valid call, so only an unclosable one reaches
        # `AIMessage.invalid_tool_calls` and exercises `PromoteInvalidToolCalls`.
        #
        #     '{"text": "unterminated'  -> repaired to {'text': 'unterminated'}
        #     '{"text": }'              -> JSONDecodeError -> invalid_tool_calls
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": }')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-cut-off",
        # A stream cut mid-string with `finish_reason: length`. `parse_partial_json` completes it to
        # a valid call; only the finish reason says it was cut, and the call must be refused, not
        # run.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": "suzuki coup')],
        text="",
        finish_reason="length",
        adversarial=True,
    ),
    Behaviour(
        name="f-wrong-argument",
        # A wrong argument name (`find_notes` takes `text`, not `query`); the storm asserts the
        # failure is visible.
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
        # An empty tool name, which a real model does not emit on request.
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
        # Tools ran and nothing was written: the `empty_answer` guard must report it rather than
        # return an empty answer with no error.
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
        # Well-formed and wrong: the symmetry-number map names species the equation does not
        # contain. A schema check passes it, so only `_checked_symmetry_numbers`' domain validation
        # stands between it and a plausible answer.
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": {
                        "kind": "reaction",
                        # Balanced on purpose, so the symmetry map is the only thing wrong with it.
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
        name="h-size-billed",
        # The one behaviour whose bill follows the request (0.5 tokens per character, about twice
        # the chars/4 estimate), so a lane driving it moves the calibration ratio above 1 and
        # exercises the EWMA, `agent_context_calibration_max_factor` and budget tightening. Every
        # other entry bills a constant.
        calls=[ToolCall(tool="find_notes", arguments={"text": "calibration"})],
        text="Billed by size, so the estimator has something to be wrong about.",
        input_tokens=None,
        input_tokens_per_char=0.5,
    ),
    Behaviour(
        name="h-oversize",
        # The endpoint refusing the request outright: the only way a lane produces the 400 that
        # `llm_provider._is_context_length` classifies as `context_length`. Per-behaviour
        # `http_status` injection lands on the generic `error` label, as 401 and 404 would.
        calls=[ToolCall(tool="find_notes", arguments={"text": "over the endpoint's limit"})],
        text="",
        input_tokens=None,
        refuse_over_input_tokens=2_000,
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
