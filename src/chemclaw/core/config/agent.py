"""The conversational agent: model, skills, capabilities, compaction, harness.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

import os
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

# The two postures the plan/execute harness can start in, named once so every model that accepts
# the value rejects the same set.
#
# It lived inline on `AgentSettings.harness_autonomy` while that was the only place it was written
# down, and `AgentProfile.harness_autonomy` was declared `str | None` — so the environment variable
# was validated and the profile file was not. A profile spelling it `plan-only` loaded silently and
# `gate_applies` came back False, which does not merely fail to add the gate: it *removes* the one
# the profile would otherwise have inherited from this default, while `TodoListMiddleware` keeps
# running. `GET /plan` then answers `approved=false` while state-changing tools execute — the
# harness looks plan-gated and is not. A constraint spelled in one of the two places it is needed
# is a constraint the other place can contradict.
HarnessAutonomy = Literal["plan_only", "execute"]


class AgentSettings(BaseSettings):
    """The conversational agent: model, skills, capabilities, compaction, harness.

    Grouped because everything here shapes how `build_langgraph_agent` compiles one turn's graph —
    which model orchestrates, which skills and MCP capability servers attach, how the conversation
    context is compacted, and whether the autonomous plan/execute harness (Phase F1) wraps it.
    """

    # Suffix for the `<retrieved-note-...>` envelope that marks untrusted retrieved content as
    # data rather than instructions (`agent/framing.py`). Empty (the default) makes it a random
    # per-*process* value, which is right for dev and tests and is what every deployment has had.
    #
    # Set it for any deployment with durable sessions. `session_store="postgres"` history outlives
    # the process that wrote it and is replayed by other replicas, and the agent instructions say
    # only an envelope with *exactly* the current tag marks retrieved data — so envelopes written
    # by a previous process are read as ordinary content, and the injection mitigation silently
    # lapses for the oldest material. A deployment-wide value keeps one tag across every pod and
    # restart. Hashed before use, so the secret never appears in a prompt or a stored session row.
    # A `SecretStr`, for the reason the credentials carry one
    # (`D-2026-08-26-a-credential-is-a-type-not-a-convention`). It is not a credential to any
    # external system, which is how it was missed once already — it is the HMAC key the envelope
    # tag is derived from, and anyone who learns it can close the envelope from inside.
    framing_envelope_secret: SecretStr = SecretStr("")

    # The agent (plan step 1.5). The orchestration model name is `llm_model` — there is no second
    # model setting here, because `agent_model` was one: a vendor model id in git, read only by the
    # deleted Anthropic branch and by an `or` tail behind `llm_model`, which is always set
    # (`D-2026-09-04-a-gateway-is-the-only-provider`). `skills_dir` is where the agent discovers
    # SKILL.md files — one or more directories, delimited by the OS path separator (like PATH),
    # so an admin can add a second (e.g. team-private) skills directory without code changes.
    # Read it through the `skills_dirs` property, never raw.
    skills_dir: str = "skills"
    # Which discovered skills are actually advertised — discovery is not enablement. Empty (the
    # default) means every skill found under `skills_dir` is active, i.e. today's behavior. A
    # non-empty pathsep list narrows to exactly those names, so a deployment can ship the whole
    # skills tree and turn on the subset it has validated, without deleting folders. This only
    # *attenuates*: it cannot advertise a skill that no directory provides, and the role gates
    # below still apply on top. `make skill-validate` reports a name here that no dir provides.
    skills_enabled: str = ""
    # Role-scoped skill visibility (plan step 6.2): map a skill name to the Entra app-roles
    # allowed to see it. A skill not listed is ungated (advertised to everyone); a listed skill
    # is hidden from a caller (the turn's ambient identity) holding none of its roles. Empty
    # default = every skill visible (today's behavior). ENV override is JSON, e.g.
    # CHEMCLAW_SKILL_ROLE_GATES='{"deep-research": ["process-chemist"]}'.
    skill_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    # Conversation context management (`agent/compaction.py`). The agent keeps a session thread and
    # composes tool calls that return large payloads (evidence sweeps, full ELN recipes), so a
    # long chat would grow unbounded. Compaction runs only when the whole *request* exceeds
    # `agent_context_token_budget` (measured with a char/4 estimator — no external tokenizer),
    # then reclaims tokens cheapest-first: replace stale tool results with a short placeholder
    # (keeping the newest `agent_keep_last_tool_groups` verbatim), then cut older conversation back
    # to the same budget on a group boundary — with the caveat below, which is the whole of what
    # `agent_keep_last_conversation_groups` does to that sentence. System instructions/skills are
    # always kept — they are not in the message list at all. No LLM summarizer — deterministic and
    # credential-free, which is also what keeps a summarizer from becoming an injection surface
    # over retrieved evidence (D-025).
    #
    # **These three had no reader at all between M13 and the ADR that restored them**, because the
    # policy lived in the framework the rebuild removed while the settings, this comment and a
    # sentence in the system prompt stayed behind describing it.
    # `chemclaw_context_compactions_total` is what a deployment now checks instead of re-reading
    # this paragraph.
    #
    # `agent_keep_last_tool_groups` counts the newest *tool results* kept verbatim, not tool-call
    # groups: the strategy behind it is upstream's `ClearToolUsesEdit` and that is what it counts.
    # The name is D-025's and stays, because it is ENV-visible and renaming it would cost every
    # deployment that sets it to buy a more accurate word.
    #
    # `agent_keep_last_conversation_groups` is an **extra cut a deployment may ask for, and it is
    # off by default** — which is a reversal of what this paragraph said for as long as the setting
    # existed, and the reversal was measured rather than argued. The window takes
    # `max(by_tokens, by_groups)`, i.e. the *more* aggressive of "what fits the budget" and
    # "everything older than the newest N groups", so N is a ceiling on what survives and the
    # budget only ever tightens it further. At N=12 that ceiling bound first on every ordinary
    # thread: the crossover is `budget / N` = 8,333 tokens per group, about 33 kB of text per turn,
    # and the lossless edit above runs first precisely to push older groups far below it. Measured
    # over 2,000 prose groups at the shipped defaults, the window cut a 329,900-token thread to
    # **1,944 tokens — 2% of the 100,000 budget it is documented as cutting to** — and sweeping the
    # budget from 10k to 300k changed that number not at all. The sentence that used to stand here,
    # "raising N no longer raises what a request can cost, it only drops more", was wrong in both
    # halves and by 50x: at a fixed budget, N=12 retains 1,944 tokens and N=600 retains 97,800.
    #
    # So the default is now `0`, which the window reads as "no group floor" and which makes
    # `agent_context_token_budget` the control it is named as. **The regression that put the
    # `max()` there is untouched**: a count of groups cannot bound anything, because what a group
    # costs is whatever was said in it, and the count-only version left a 300k-token thread at 180k
    # against this 100k budget. The token arm still runs and still bounds — measured, 20 groups of
    # 60 kB cut to 90,090 with the floor off, not to 180,180 (90,366 was quoted here from a
    # different fixture; the reproducible figure is the one `tests/test_compaction.py`
    # builds). Setting N above 0 re-arms the extra
    # cut for a deployment that wants the model to see fewer *turns* than the budget would allow;
    # the instrument for wanting it to see fewer *tokens* is the budget.
    #
    # The one thing that is never dropped is the newest group, because an empty message list is
    # rejected by the provider (`agent/compaction.py`).
    #
    # **This is a budget on the whole *request*, not on the thread, and that changed deliberately.**
    # `context_budget.effective_trigger` subtracts this request's own prefix — the system message,
    # the skills listing and every bound tool schema — from the number below before the edits see
    # it, whether or not `llm_context_window_tokens` is declared. D-2026-08-28 charged the prefix
    # only under a declared window and no deployment declares one, so the ~43,000 tokens that leave
    # on every model call were budgeted against nothing: measured 2026-09-04, a thread the policy
    # cut to its 90,030-token budget left as a 137,301-token request at a 128k model.
    #
    # What that costs an existing deployment is the prefix, exactly: at the 100,000 below and a
    # 43,175-token prefix the thread gets **56,825** estimated tokens where it used to get 100,000,
    # so a session that never compacted may now compact, and one that compacted may compact
    # earlier. That is the intended trade — the alternative is a bound that does not bound — but it
    # is a behavioural change and not a no-op.
    #
    # **A value at or below the prefix is not a budget.** `effective_trigger` floors at 1, which
    # means "reduce on every model call", and it says so once at WARNING rather than returning it
    # silently (`context_budget._note_floored_trigger`). The clear trigger below is in that state at
    # its shipped default; see the paragraph there.
    agent_context_token_budget: int = Field(default=100_000, ge=1)
    agent_keep_last_tool_groups: int = Field(default=2, ge=0)
    agent_keep_last_conversation_groups: int = Field(default=0, ge=0)
    # `agent_tool_result_clear_trigger` is the *lossless* edit's own threshold, and splitting it
    # off is the whole point of this field. `context_compaction_middleware` composes two edits:
    # upstream's `ClearToolUsesEdit`, which replaces a re-fetchable tool result with a placeholder
    # and leaves the `tool_use` record so the model can fetch it again, and the first-party
    # conversation window, which *deletes* older groups. Both used to read
    # `agent_context_token_budget`, so nothing reduced until 100k and then the cheap edit and the
    # destructive one fired in the same breath.
    #
    # They are different instruments and want different thresholds. Clearing costs nothing and
    # loses nothing, so it should run early and often; every token it reclaims early is a
    # conversation group the window never has to reach for. Anthropic's own composition separates
    # them by an order of magnitude for this reason (30k against 180k in the cookbook's research
    # agent), and the default here is the same shape against this repository's 100k budget.
    #
    # Above the budget it would be pointless — the window would already have fired — so the
    # validator in `Settings` refuses that rather than letting a deployment set a number that
    # silently means "unchanged".
    #
    # **73,500 is a request budget, and it is the old 30,000 re-expressed in the new unit rather
    # than a retuning.** When this field meant *thread* spend, 30,000 was the band above — an
    # order of magnitude below the budget, so clearing runs early and often. Charging the prefix
    # made 30,000 mean something else entirely: the `default` prefix measures ~43,175, so
    # `effective_trigger` subtracted it, floored at 1, and the lossless edit cleared every
    # reclaimable tool result on **every model call**, keeping only the newest batch. That is not
    # a tuning anybody chose; it is what a thread number reads as once the unit changes underneath
    # it.
    #
    # The replacement is derived, not invented: `tests/test_context_floor.py`'s ratchet **ceiling**
    # (43,500 — the bound, deliberately, rather than today's measurement, so this number does not
    # move every time a tool schema does) plus the 30,000 of thread the old default intended.
    # Anything above the prefix restores the band; this one restores it to the same *thread*
    # allowance the setting has always had, which is why it is a translation rather than a new
    # decision about how much evidence the model keeps.
    #
    # The floor it used to hit is still reachable — a deployment that lowers this below its own
    # prefix gets it — so it stays loud rather than silent: one WARNING per process
    # (`context.trigger_floored`) naming both numbers and the remedy, since the condition is static
    # and a rate would carry nothing a line does not. `tests/test_compaction.py` asserts both the
    # floor and this default's clearance above the ratchet ceiling, so the day a tool surface grows
    # past it, that test says so instead of the behaviour changing quietly.
    agent_tool_result_clear_trigger: int = Field(default=73_500, ge=1)
    # **What the two numbers above are denominated in, which used to be left unsaid and was wrong.**
    # Both are counted with `count_tokens_approximately` — chars/4 — and that estimator is content
    # dependent in one direction. Measured against a real BPE tokenizer on this repository's own
    # payloads: the static prefix is 1.04x, tool schemas 1.00x, a markdown note 1.01x — and a
    # connector JSON result is **0.45x**, an xyz geometry 0.47x. So the estimate is good for prose
    # and schemas and roughly half of the truth for exactly the payload class these two triggers
    # exist to reclaim, which put a thread the policy believed was at 100,000 tokens at ~224,000
    # billed ones.
    #
    # No constant corrects that, because the error is a property of the *content* and runs in both
    # directions. What does correct it is the number the provider returns: `input_tokens` on every
    # response is the billed size of the request this system just estimated, so the ratio between
    # them is measurable at the one place that holds both (`agent/context_budget.py`). The budget is
    # therefore read as a **billed**-token budget and converted into the estimator's unit by that
    # measured ratio — which is 1.0, and so changes nothing, until enough calls have been observed.
    #
    # `min_calls` is the sample floor before the ratio is believed: one unusual first turn must not
    # move a budget. The factor only ever *tightens* the trigger (it is clamped at 1.0 below), so
    # the worst a mismeasurement can do is compact earlier than needed — never send a request the
    # policy thinks is smaller than it is, which is the failure being closed.
    #
    # **The sample floor and the prefix subtraction do not interact, and that is worth stating
    # because it looks as though they must.** For the first `min_calls` model calls of a process
    # the ratio reads 1.0, so the *thread* half of both triggers is uncalibrated — but the prefix
    # comes off *before* the division and is therefore exact from the first call. Whether a trigger
    # floors at all is likewise all but ratio-free: the condition is `configured - prefix < ratio`,
    # so the two answers can only differ inside a band as wide as the ratio itself, which the clamp
    # caps at 4 tokens (swept, at a 43,175-token prefix only 43,176..43,179 flip). So a pod restart
    # no longer means "the largest single component of the request is unbudgeted until call 21"; it
    # means the thread's unit conversion warms up, over a smaller thread than before. Measured, the
    # warm-up swing in the thread allowance shrinks with it: 100,000 -> 45,454 tokens between an
    # uncalibrated and a 2.2x-calibrated process before, 56,825 -> 25,829 after.
    agent_context_calibration_enabled: bool = True
    agent_context_calibration_min_calls: int = Field(default=20, ge=1)
    # Ceiling on the factor, so a pathological sample cannot collapse the budget. 4.0 is well above
    # the 2.2x the worst measured payload class produces and still bounds the arithmetic.
    agent_context_calibration_max_factor: float = Field(default=4.0, ge=1.0)
    # **Ceiling on what one tool result may put in front of the model**, and the one bound that was
    # missing entirely. `connector_max_request_bytes` caps what this system *sends* a server;
    # nothing capped what a server — or an in-process tool — sends back. Both context edits have a
    # carve-out for the newest results (`agent_keep_last_tool_groups`) and for the newest
    # conversation group, so a single large result is by construction the thing neither can touch:
    # two results at 200,000 characters each measured 100,077 estimated tokens (one over the
    # budget), ~224,000 billed, with both edits running and reclaiming nothing.
    #
    # 60,000 rather than a new opinion: it is the number this repository already chose for
    # `gather_evidence_max_chars`, its largest deliberate evidence payload. A result over it is cut
    # head-and-tail with a notice naming the tool and the characters removed — never silently, and
    # never in the middle of a sentence a chemist might quote. 0 disables the cap, which restores
    # the unbounded behaviour and is a decision a deployment has to make on purpose.
    #
    # It does not replace a per-tool ceiling (`document_read_max_chars`,
    # `calc_find_max_result_chars` and the rest); it is the floor under all of them, applied at the
    # one place every tool result passes.
    agent_max_tool_result_chars: int = Field(default=60_000, ge=0)
    # Durable working memory for the agent's scratchpad (`agent/scratchpad.py`). Off by default,
    # and the default is about *data* rather than about the code being unproven: enabling it
    # creates the `store`/`store_vectors` tables and starts writing files a turn authored to a
    # place that outlives the session. A deployment should decide that, not inherit it.
    #
    # With it off, a turn still gets `/scratch/` — the graph-state scratchpad that makes a
    # multi-source research turn possible — and simply has no `/memories/` route. The two are
    # separate capabilities and only the durable half needs a decision.
    #
    # It is also inert without an actor: no ambient identity means no namespace, and a memory
    # written under a shared prefix would be one nobody can erase and everybody can read
    # (`agent/scratchpad.memory_namespace`).
    agent_memory_enabled: bool = False
    # **How much of the chemist's own conversation stays quotable** — the ambient
    # `core/turn_text.py` binds and `agent/protocol_design_tools.require_quotes_are_verbatim`
    # checks a `basis="stated"` slot against.
    #
    # It carried exactly the message that started the turn in flight, while
    # `structure_experiment_request` tells the model to call it "first … while correcting it is
    # still cheap" — iteratively, across turns. Measured: a chemist who wrote "24 wells, no DMF, by
    # Friday please." on turn 1 and "ok go ahead" on turn 3 had the intake refused, because
    # `'24 wells'` is not in "ok go ahead". So an honest `stated` was unrepresentable on the
    # ordinary path, and the remedy the refusal prescribed recorded a real chemist constraint as a
    # model inference.
    #
    # **Two currencies, because either alone is unbounded in the other** — the
    # `agent_keep_last_conversation_groups` lesson, and the reason `protocol_digest_*` is a pair.
    # The front door accepts `service_max_message_chars` (100,000) in one message, so a turn count
    # bounds no memory at all: 20 turns is up to 2 MB held in a contextvar for the turn's whole
    # duration and scanned on every `stated` slot.
    #
    # `agent_stated_quote_turns` counts the chemist's *earlier* messages; the turn in flight is
    # always quotable and is never counted here, so `0` is exactly the behaviour this widening
    # replaced. `agent_stated_quote_chars` bounds the whole window, that message included — and it
    # cannot take that message away, because a bound that could would make a configuration
    # silently stricter than the narrow version it replaced.
    agent_stated_quote_turns: int = Field(default=20, ge=0)
    agent_stated_quote_chars: int = Field(default=20_000, ge=1)
    # Local testing CLI (`agents.cli`). The CLI is a developer affordance for driving the agent
    # from a terminal; the production ingress is Teams/Copilot with native Entra-ID SSO
    # (architektur.md §7), not this. Because Entra enforcement defaults off in dev
    # (`entra_required=False`), the CLI can only run in explicit `--admin` mode, which bypasses
    # auth for testing and attributes the audit trail to this actor. It is a config value (not a
    # hardcoded string) so a deployment can label its test runs — e.g. a machine name — rather
    # than a generic "admin".
    cli_admin_actor: str = "admin@localhost"

    # The roles `--admin` holds. **Empty by default, and deliberately its own setting**: it used to
    # be derived as the union of every role named in `skill_role_gates`, which is a *visibility*
    # map — it decides which skills a chemist is shown — while `authorize_tool` and
    # `authorize_trigger` read `tool_role_gates` and `entra_privileged_role_set`. Those are
    # unrelated maps, and the coupling was the role *name*.
    #
    # Measured on the shipped chart the derivation is harmless (36 tools allowed, 6 denied, 0 of 5
    # expensive actions). Add one skill gate whose role name an operator also put in
    # `entra_privileged_roles` — the runbook's own remedy for a refused job, and the literal example
    # in this file's `skill_role_gates` docstring — and the unauthenticated terminal CLI holds that
    # role: 42 of 42 tools allowed, every expensive action allowed. Neither config edit mentions the
    # CLI, and `uv sync` puts the `chemclaw` console script in the image, so `oc exec` reaches it.
    #
    # Empty means `--admin` bypasses *authentication* only. A deployment that genuinely wants a
    # full-access local seam sets this explicitly, which is a decision someone made rather than a
    # consequence of naming two unrelated things the same way.
    cli_admin_roles: list[str] = Field(default_factory=list)

    # The agent harness (plan Phase F1) — the autonomous plan/execute backbone (the
    # Claude-Code-like experience). When `harness_enabled`, `build_langgraph_agent` attaches
    # `TodoListMiddleware` and the plan gate (a todo list + plan/execute approval + a counted
    # completion cap) over the *same* tools/skills/audit/compaction as the single-turn agent, with
    # every generic battery (file memory/access, web search, shell) OFF — capability comes from our
    # MCP servers and tools, not from the harness. Off by default so the single-turn agent stays
    # the safe fallback.
    # `harness_autonomy` picks the starting mode: `plan_only` (default, the pharma-safe one)
    # starts in plan mode and presents a plan for human approval before any execution — the
    # pre-execution approval gate — and only loops once approval switches it to execute; `execute`
    # starts looping through the todo list immediately. `harness_max_loop_iterations` caps the
    # loop so a stuck plan aborts instead of spinning (the runaway guard).
    harness_enabled: bool = False
    harness_autonomy: HarnessAutonomy = "plan_only"
    harness_max_loop_iterations: int = Field(default=25, ge=1)

    # What one turn may **bill** before the runaway guard stops it, counting every dimension the
    # provider reports (input, output and cache) across every model call of the turn, the
    # subagent's included.
    #
    # **The cap above counts the wrong thing to be the only cap.** It counts model *calls*, and a
    # call is not a unit of cost: the same 25 iterations bill a few thousand tokens on a prose
    # turn and millions on one that fans out wide over large tool results against a long context.
    # Nothing else closes that, and it is easy to believe something does. `api/budget.py` meters
    # tokens — and its `check()` runs *before* a turn against usage already booked while
    # `record()` books the turn *after* it ended, so a single turn's runaway is precisely what it
    # cannot see. Its own docstring carries the belief that leaves the hole: "A single agent turn
    # is already iteration-capped, so one turn cannot loop forever." One turn cannot *loop*
    # forever; one turn can *spend* without a bound.
    #
    # **0 means no cap**, the convention `budget.py::_over` and `llm_context_window_tokens`
    # already use, and it is the shipped default for the reason a wrong number here is worse than
    # no number: the cap ends the turn, and a turn ended early on a corpus this setting was never
    # sized against loses a chemist's work. A deployment sets it from what its own
    # `turn_costs.total_tokens` rows actually show, which is the number `chemclaw.evals` and the
    # cost ledger exist to give it. The iteration cap stays on regardless, so switching this off
    # is not switching the runaway guard off.
    #
    # Billed rather than estimated tokens, because this is a *cost* ceiling and the estimator is
    # measured at 0.45x on exactly the payload class a runaway turn is made of
    # (`agent/context_budget.py`). No conversion is needed here and none is done: the number the
    # provider reports is the number this compares.
    agent_max_turn_billed_tokens: int = Field(default=0, ge=0)

    # Supersteps one model call costs, for deriving the graph's own step ceiling below.
    #
    # **Why the graph needs a ceiling at all.** `create_agent` bakes `recursion_limit=9999`, and
    # `create_deep_agent` bakes a second onto the graph it returns; a config passed at invoke time
    # displaces both (measured — a looping model reports "Recursion limit of 158"), and
    # nothing here ever chose otherwise, so a turn's real bound was thousands of model calls — and
    # it fails by raising `GraphRecursionError`, which discards whatever the turn had produced.
    # That is the opposite of the position `agent.loop_cap` takes deliberately: end the run, let the
    # partial answer out, mark it. The loop cap above is the graceful stop — on every profile, the
    # classic agent included, since the harness gate on it expired with the second engine — and
    # this is the backstop under it, sized so the cap always fires first.
    #
    # **A superstep is not a model call, which is why this is a multiplier.** One model/tool round
    # trip is several graph nodes — the model node, the tools node, and one per hook-bearing
    # middleware. Measured by binary search on the minimal limit that completes N calls: `2N + 1`
    # on a bare agent and with the harness off, `4N + 3` with the harness on. **Approximate on
    # purpose**: the constant is the middleware *count*, so adding a middleware moves it, and a
    # number with no headroom turns "we added a middleware" into "long turns started failing". 6 is
    # the measured 4 with headroom, and still bounds a runaway to ~25 model calls at the
    # default cap rather than ~2,500.
    #
    # **That sentence about headroom is not decoration — it was tested by accident.** M14 briefly
    # put the runaway cap on `ModelCallLimitMiddleware`, which also declares `after_model`; that one
    # extra node took the real cost to `5N + 3`, and the ceiling's constant was `+ 1` at the time,
    # which granted exactly 7 where a one-iteration turn then needed 8. The multiplier's headroom
    # absorbed it at every cap except the smallest. The cap swap is reverted, so the cost is `4N +
    # 3`
    # again — but the constant stays at 3 rather than going back to 1, because the whole lesson is
    # that a ceiling sized to the exact requirement fails the first time anyone adds a node.
    # Re-measure the multiplier and the constant together, never one alone.
    #
    # An earlier draft of this reasoning used 1.83, taken from counting streamed `updates` events.
    # Those are node updates, not supersteps; a ceiling derived from it would sit *below* what a
    # healthy 25-iteration turn needs and would have truncated good turns.
    agent_supersteps_per_model_call: int = Field(default=6, ge=2)

    # How many tool calls from one assistant message may run at once. `ToolNode` gathers every
    # call in the batch with no bound of its own, so before this existed a 40-call fan-out ran 40
    # tool bodies, 40 audit rows and up to 40 plan-gate reads concurrently — against a Postgres
    # pool of 16. Applied as LangGraph's `max_concurrency` in `agent.state.turn_config`, so it
    # bounds a superstep's parallel work (subagent fan-outs included) rather than only tools.
    # 8 matches the front door's own admission width (`service_max_concurrent_turns`) and the
    # worker's activity slots, which are both sized to that pool; 0 removes the bound.
    agent_max_parallel_tool_calls: int = Field(default=8, ge=0)

    # How many of one reply's unparseable tool calls are promoted onto `tool_calls` and refused
    # individually (`agent/model_calls.PromoteInvalidToolCalls`); the rest are counted and named
    # for the operator without becoming calls. 0 removes the bound.
    #
    # **This restores a ceiling that was deleted on a false premise.**
    # `D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure` removed
    # `agent_max_reported_lost_calls` saying `agent_max_parallel_tool_calls` "bounds how many calls
    # a reply may hold". It does not — it is LangGraph's `max_concurrency`, which bounds how many
    # run *at once*. Measured with nothing in between: one reply carrying 1000 unparseable calls
    # produced **1000 audit rows, 1000 `tool_failed` events and 268 kB of `ToolMessage`s** fed back
    # into the model's own context, in 5.8 s, with nothing refusing or truncating — and a model
    # steered by injected content is exactly what emits a wide fan-out of malformed calls.
    #
    # Nothing is *lost* past the bound, which is the property the old setting also kept:
    # `chemclaw_invalid_tool_calls_total` counts every call, the WARNING names the remainder, and
    # the model still learns what it did wrong from the calls that were promoted — every one of
    # them carries the same sentence. 20 is the old setting's value, kept so a deployment that
    # tuned it reads the same number.
    agent_max_promoted_invalid_calls: int = Field(default=20, ge=0)

    # How many times one turn may call a tool with the *identical* arguments before the call is
    # refused (`agent.repeat_guard`). The loop cap above bounds the harness's iterations and says
    # nothing about this: a live run called `find_past_jobs` 7-8 times in a single turn, with
    # `load_skill` x6 and `find_notes` x5 beside it, and the only symptom was a median turn of
    # 128-142 s against 16.9 s on the archived run. Two, not one, because a genuine re-check is a
    # real pattern — a job polled after a wait, a note re-read after a write — and seven is not.
    # Raise it for a deployment whose tools are cheap and whose answers move; 1 disables repeats
    # entirely.
    max_identical_tool_calls: int = Field(default=2, ge=1)

    # Where profiles are discovered (`agents.profile_discovery`): one or more directories,
    # OS-path-separator delimited like `PATH` and like `skills_dir`. A profile selects *across*
    # capabilities, so a shared tree is its common home; a profile genuinely about one
    # capability lives in that connector's bundle instead and is found there.
    profiles_dir: str = "data/profiles"

    # Where deterministic step templates are discovered (`data/templates/`). A template fixes the
    # order of a procedure and runs it as a durable workflow, where a profile configures an agent
    # and leaves the order to the model. `src/chemclaw/templates/README.md` says which one a task
    # wants.
    templates_dir: str = "data/templates"
    # Which discovered templates are enabled; empty (the default) means every one found.
    templates_enabled: str = ""
    # Per-step wall clock for a template run. Generous because an `agent` step is a model turn and
    # a `tool` step may be a real calculation, but bounded so one wedged step cannot pin a run.
    template_step_timeout_seconds: float = Field(default=900.0, gt=0)
    # How long a step may go without saying anything before Temporal declares its worker dead.
    #
    # A `start_to_close` timeout alone cannot tell a step that is working from a worker that was
    # killed: both look like silence, and the whole 900 s above has to elapse before the attempt is
    # retried. `run_tool_step` and `run_agent_step` therefore beat while they wait
    # (`durable/heartbeat.beating`, the same idiom the document and ELN syncs use), and this is the
    # timeout that both the workflow's `heartbeat_timeout` and the beat interval derive from — one
    # number, so the two can never drift. Sized like `eln_sync_heartbeat_timeout_seconds` rather
    # than like the step budget: it measures worker liveness, not the work.
    template_step_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # Whole-run wall clock for one template execution, the ceiling the per-step budget cannot give.
    #
    # Without it an N-step template's only bound was `template_step_timeout_seconds` × N — a number
    # nothing declares, that changes when an author adds a step, and that no operator can read off
    # any setting. `ConnectorJobWorkflow` gives its children `connector_job_timeout_seconds` for
    # exactly this reason (`durable/connector_job.py`), and a template is core's own sequencer of
    # the same kind of work. Eight steps at the default step budget; a longer procedure raises this
    # deliberately rather than inheriting an unbounded run. The cross-field validator in
    # `core/config/__init__.py` refuses a run ceiling that cannot contain a single step.
    template_run_timeout_seconds: float = Field(default=7200.0, gt=0)

    @property
    def templates_dirs(self) -> list[str]:
        """The template dirs, split on the OS path separator (like `PATH`), blanks dropped."""
        return [d for d in self.templates_dir.split(os.pathsep) if d]

    @property
    def templates_enabled_list(self) -> list[str]:
        """The explicitly enabled template names; empty means "every discovered template"."""
        return [t for t in self.templates_enabled.split(os.pathsep) if t]

    @property
    def profiles_dirs(self) -> list[str]:
        """The profile directories, split on the OS path separator (like `PATH`), blanks dropped."""
        return [d for d in self.profiles_dir.split(os.pathsep) if d]

    @property
    def skills_dirs(self) -> list[str]:
        """The skills directories, split on the OS path separator (like PATH), empties dropped.

        The skills backend takes a list of directories; keeping the config a single delimited
        string (rather than a JSON list) means an admin sets `CHEMCLAW_SKILLS_DIR=skills:/opt/
        team-skills` the same way they set `PATH`, no JSON quoting.
        """
        return [d for d in self.skills_dir.split(os.pathsep) if d]

    @property
    def skills_enabled_list(self) -> list[str]:
        """The explicitly enabled skill names; empty means "every discovered skill" (the default).

        A bare-key set, so it uses the delimited-string idiom (like `skills_dir`/`data_sources`)
        rather than JSON — these are names, not config-carrying objects.
        """
        return [s for s in self.skills_enabled.split(os.pathsep) if s]

    @property
    def agent_recursion_limit(self) -> int:
        """The graph step ceiling one turn runs under (`agent.state.turn_config`).

        Derived from `harness_max_loop_iterations` rather than set directly, because the two are one
        decision: the cap is what a deployment says a turn may cost, and a step ceiling that did not
        follow it would either fire first — discarding an answer the cap would have let out — or
        never fire at all, which is what an inherited 9999 would do.

        `+ 8` is a *bound with margin*, not the measured cost, and the distinction is the whole
        lesson of this docstring. Both halves of the formula have been wrong, at different times,
        for opposite reasons — and each time only the smallest cap noticed.

        **The history, because the procedure matters more than the number.** The constant was `+ 1`
        until M14 moved the runaway cap onto `ModelCallLimitMiddleware`, which declares
        `after_model` as well as `before_model`: a one-iteration turn then needed 8 where the
        formula granted 7,
        and died with `GraphRecursionError` — the failure this ceiling exists to *avoid*, since it
        discards the partial answer the cap would have let out. It became `+ 3`, and stayed right
        until `create_deep_agent` brought `SubAgentMiddleware`, `SummarizationMiddleware` and
        `PatchToolCallsMiddleware`, at which point a one-iteration turn needed 14 against the 9 the
        formula granted.

        **Then two branches each measured a graph the other did not have, and neither number
        survived the merge.** `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`
        reverted the cap to a first-party `before_model` hook and measured `4*N + 3`; the
        `create_deep_agent` swap measured `6*N + 8`. Merged, the graph has main's cheaper cap *and*
        the swap's extra middleware, so it is neither.

        **Re-measured on the merged graph by binary search rather than by counting nodes**
        (2026-08-15): the minimal working `recursion_limit` for N tool calls is 12, 17, 22, 32, 47
        for N = 1, 2, 3, 5, 8 — an exact fit to **`5*N + 7`**. The multiplier fell from 6 to 5
        because a `before_model` hook costs one superstep per model call less than a middleware
        declaring both hooks; the constant rose from 3 to 7 because the harness brought fixed
        overhead. Five points rather than one precisely because a single point cannot tell a changed
        multiplier from a changed constant — which is how it went stale the first time. Re-measure
        both together, never one alone.

        **So why `6 * N + 8` and not `5 * N + 7`.** The formula grants
        `agent_supersteps_per_model_call` (6) per call against a true cost of 5, plus 8 against a
        true 7 — a margin of `N + 1`
        supersteps that widens with the cap and is never below 2. That is deliberate: a ceiling that
        fits exactly is one node away from being wrong, and every stale-constant incident above was
        a graph gaining a node nobody re-measured for. The setting stays the knob a deployment can
        raise; this constant is the floor under it.

        At the shipped defaults this is `25 * 6 + 8 = 158` against the 132 a 25-iteration harness
        turn actually needs — so the cap fires first, which is the intent. The ceiling should never
        be what stops a harness turn; it is what stops a turn that has no cap, because the loop cap
        is attached only when the harness is on.
        """
        return self.harness_max_loop_iterations * self.agent_supersteps_per_model_call + 8
