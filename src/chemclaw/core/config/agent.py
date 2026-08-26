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

    # The agent (plan step 1.5). `agent_model` is the orchestration model name
    # (ENV-overridable); the provider's API key is read by the chat model from its own env var
    # (e.g. ANTHROPIC_API_KEY), not stored here. `skills_dir` is where the agent discovers
    # SKILL.md files — one or more directories, delimited by the OS path separator (like PATH),
    # so an admin can add a second (e.g. team-private) skills directory without code changes.
    # Read it through the `skills_dirs` property, never raw.
    agent_model: str = "claude-sonnet-5"
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
    # long chat would grow unbounded. Compaction runs only when the included context exceeds
    # `agent_context_token_budget` (measured with a char/4 estimator — no external tokenizer),
    # then reclaims tokens cheapest-first: replace stale tool results with a short placeholder
    # (keeping the newest `agent_keep_last_tool_groups` verbatim), then cut older conversation back
    # to the same budget on a group boundary. System instructions/skills are always
    # kept — they are not in the message list at all. No LLM summarizer — deterministic and
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
    # `agent_keep_last_conversation_groups` is a **floor on the cut, not the rule**, for that same
    # reason. The conversation window cuts to `agent_context_token_budget` — a count of groups
    # cannot bound anything, because what a group costs is whatever was said in it, and the
    # count-only version left a 300k-token thread at 180k against this 100k budget. So groups older
    # than the newest N always go and the budget may drop more; raising N no longer raises what a
    # request can cost, it only drops more. The one thing that is never dropped is the newest group,
    # because an empty message list is rejected by the provider (`agent/compaction.py`).
    agent_context_token_budget: int = Field(default=100_000, ge=1)
    agent_keep_last_tool_groups: int = Field(default=2, ge=0)
    agent_keep_last_conversation_groups: int = Field(default=12, ge=1)
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
    agent_tool_result_clear_trigger: int = Field(default=30_000, ge=1)
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

    # Supersteps one model call costs, for deriving the graph's own step ceiling below.
    #
    # **Why the graph needs a ceiling at all.** `create_agent` bakes `recursion_limit=9999`, and
    # `create_deep_agent` bakes a second onto the graph it returns; a config passed at invoke time
    # displaces both (measured — a looping model reports "Recursion limit of 158"), and
    # nothing here ever chose otherwise, so a turn's real bound was thousands of model calls — and
    # it fails by raising `GraphRecursionError`, which discards whatever the turn had produced.
    # That is the opposite of the position `agent.loop_cap` takes deliberately: end the run, let the
    # partial answer out, mark it. The loop cap above is the graceful stop; this is the backstop
    # under it, and it also bounds the classic agent, which has no loop cap at all.
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
