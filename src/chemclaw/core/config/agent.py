"""Settings for the conversational agent: model, skills, capabilities, compaction, harness.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators. Numeric ceilings follow one
convention: 0 means no bound, where the field allows it.
"""

import os
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

# The two postures the plan/execute harness can start in, named once so the environment variable and
# `AgentProfile.harness_autonomy` reject the same set. A misspelling accepted in one place would
# silently drop the plan gate while the todo list keeps running.
HarnessAutonomy = Literal["plan_only", "execute"]


class AgentSettings(BaseSettings):
    """The conversational agent: model, skills, capabilities, compaction, harness.

    Grouped because everything here shapes how `build_langgraph_agent` compiles one turn's graph —
    which model orchestrates, which skills and MCP capability servers attach, how the conversation
    context is compacted, and whether the autonomous plan/execute harness (Phase F1) wraps it.
    """

    # HMAC key for the `<retrieved-note-...>` envelope tag marking retrieved content as data
    # (`agent/framing.py`). Empty means a random per-process value, which breaks the mitigation for
    # history replayed by other processes, so set it wherever sessions are durable. Hashed before
    # use; a `SecretStr` because whoever knows it can close the envelope from inside.
    framing_envelope_secret: SecretStr = SecretStr("")

    # The orchestration model is `llm_model`. `skills_dir` is one or more SKILL.md directories,
    # OS-pathsep-delimited like PATH; read via `skills_dirs`.
    skills_dir: str = "skills"
    # Skills actually advertised, pathsep-delimited; empty means every discovered skill. Only
    # narrows (role gates still apply); `make skill-validate` reports unknown names.
    skills_enabled: str = ""
    # Skill name → Entra app-roles allowed to see it; unlisted skills are visible to everyone. JSON
    # in the env, e.g. CHEMCLAW_SKILL_ROLE_GATES='{"deep-research": ["process-chemist"]}'.
    skill_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    # Context compaction (`agent/compaction.py`), deterministic and LLM-free so it cannot become an
    # injection surface. When a request exceeds `agent_context_token_budget` (billed tokens of the
    # whole request, prefix included, see `agent/context_budget.effective_trigger`), stale tool
    # results are cleared first, keeping the newest `agent_keep_last_tool_groups` results, then
    # older conversation groups are cut on a group boundary; the newest group is never dropped.
    # `agent_keep_last_conversation_groups` is an optional extra cap on groups kept (0 = off, so the
    # token budget governs). `chemclaw_context_compactions_total` shows it working. Derived default:
    # `tests/test_context_floor.PREFIX_BOUND` + `BUDGET_THREAD_ALLOWANCE`, which must fit within
    # the smallest target window (128k) - `llm_max_tokens`; `tests/test_compaction.py`
    # (`BUDGET_THREAD_ALLOWANCE`, `SMALLEST_TARGET_WINDOW`) asserts it.
    agent_context_token_budget: int = Field(default=119_300, ge=1)
    agent_keep_last_tool_groups: int = Field(default=2, ge=0)
    agent_keep_last_conversation_groups: int = Field(default=0, ge=0)
    # Threshold of the lossless edit (`ClearToolUsesEdit`: results become placeholders the model can
    # re-fetch), set well below the destructive window so clearing runs early and often. `Settings`
    # refuses a value above the budget. Below the prefix it floors and logs
    # `context.trigger_floored`. Derived default: `tests/test_context_floor.PREFIX_BOUND` +
    # `tests/test_compaction.CLEAR_TRIGGER_THREAD_ALLOWANCE`, asserted in
    # `tests/test_compaction.py`.
    agent_tool_result_clear_trigger: int = Field(default=111_600, ge=1)
    # The prefix the two thresholds above were derived for, and the most of it they are charged. A
    # deployment binding more bundles than the chart pays the excess in spend rather than thread
    # (logged once as `context.prefix_over_basis`); a declared `llm_context_window_tokens` is still
    # charged the whole prefix. Equal to `tests/test_context_floor.PREFIX_BOUND`
    # (`tests/test_compaction.py` asserts it).
    agent_context_prefix_basis: int = Field(default=83_700, ge=0)
    # Budgets are in billed tokens but counted with a chars/4 estimator whose error depends on
    # content (tool JSON undercounts badly). `agent/context_budget.py` converts by the ratio of the
    # provider's reported `input_tokens` to the estimate, clamped so it only tightens. One sample
    # suffices (`agent_context_calibration_min_calls`): believing a sample can only compact earlier,
    # and the EWMA is bias-corrected. The first call of a process, before any sample, is unbounded
    # by this.
    agent_context_calibration_enabled: bool = True
    agent_context_calibration_min_calls: int = Field(default=1, ge=1)
    # Ceiling on the calibration factor, so a pathological sample cannot collapse the budget; about
    # twice the worst observed ratio.
    agent_context_calibration_max_factor: float = Field(default=4.0, ge=1.0)
    # Ceiling on the characters one model call's tool results put in front of the model, since
    # neither context edit may touch the newest batch. Split evenly across one `AIMessage`'s
    # parallel calls (`agent/tool_result_size.py`), so a fan-out cannot multiply it; a lone call
    # gets all of it. A result over its share is cut head-and-tail with a notice. Applies under
    # every per-tool ceiling. Keep equal to `gather_evidence_max_chars`; the warm arm in
    # `tests/test_compaction.py` checks the thread still holds one maximal batch. 0 disables.
    agent_max_tool_result_chars: int = Field(default=60_000, ge=0)
    # Characters a helper may hand back into its caller's `files` channel. `task` returns the
    # helper's final state, `files` included, and LangGraph rewrites that channel every superstep,
    # so an unbounded write is amplified in checkpoint storage. A total across files, not per file.
    agent_subagent_files_max_chars: int = Field(default=200_000, ge=0)
    # Longest file a turn's own `write_file`/`edit_file` may produce, enforced by
    # `agent/scratchpad.BoundedStateBackend` (`/scratch/`) and `BoundedStoreBackend` (`/memories/`).
    # Refused with the limit named, never truncated. Equal to the helper channel budget, since one
    # larger file would exhaust it.
    agent_scratch_file_max_chars: int = Field(default=200_000, gt=0)
    # `agent_scratch_retention_days`: days a file in a thread's `files` channel survives its last
    # write, expired at the start of each turn (`agent/scratchpad.expire_stale_scratch`); 0 keeps
    # forever. Not gated on `retention_enabled`: it disposes of a working surface, not a record.
    # `agent_memory_enabled`: mounts durable `/memories/` per actor and enables the personal and
    # organisation skills tiers (`personal_skills_available()`, `POST /skills/*`, `propose_skill`).
    # Without an actor there is no namespace, so nothing is written. Off leaves `/scratch/` only.
    agent_scratch_retention_days: int = Field(default=90, ge=0)
    agent_memory_enabled: bool = True
    # Files per actor in the `store` table, evicting the least recently updated. A count, because a
    # memory is written to persist (age is the wrong axis) and a byte rule is not one a chemist can
    # predict.
    agent_memory_max_files: int = Field(default=200, ge=1)
    # Bounds on a chemist's own skills tier, refused at the route (authored judgment is never
    # evicted). The char cap is one skill's body; the row cap bounds prefix spend, since every local
    # skill's name and description ride on every model call (`agent/local_skills.py` has the
    # arithmetic).
    agent_local_skill_max_chars: int = Field(default=16_000, ge=1)
    agent_local_skills_max: int = Field(default=20, ge=1)
    # Behaviour proposals one `GET /proposals` returns, newest first; a response bound, not storage.
    agent_proposals_list_max: int = Field(default=50, ge=1)
    # Organisation skills, refused at the route. Every row is in the prefix of every turn of every
    # chemist, and again for each helper, so this is the bill rather than a worst case.
    # `tests/test_context_floor.py` measures one maximal row.
    agent_org_skills_max: int = Field(default=12, ge=1)
    # Previously activated bodies of one organisation skill kept for revert. Evicted (least recently
    # activated) rather than refused, so a fix can always be published.
    agent_org_skill_versions_max: int = Field(default=20, ge=1)
    # `user_preferences` rows per owner (model-chosen keys), enforced in the writer's transaction,
    # and how many `recall_preferences` returns, enforced in the read.
    preferences_max_per_owner: int = Field(default=200, ge=1)
    preferences_recall_limit: int = Field(default=50, ge=1)
    # Character bounds, because every preference rides on every model call
    # (`agent/preferences.StandingPreferences`). The entry cap applies to the rendered `- key:
    # value` line and is refused at write time in `remember_preference`; the section cap bounds the
    # whole appended section and leaves room for its framing plus one entry.
    preferences_entry_max_chars: int = Field(default=300, ge=40)
    preferences_section_max_chars: int = Field(default=4_000, ge=1_500)
    # How much of the chemist's own conversation a `basis="stated"` slot may quote
    # (`core/turn_text.py`, `agent/protocol_design_tools.require_quotes_are_verbatim`), so
    # constraints stated in earlier turns stay quotable. Turns count earlier messages (the current
    # one is always quotable, so 0 means only it); chars bound the whole window but never cut the
    # current message.
    agent_stated_quote_turns: int = Field(default=20, ge=0)
    agent_stated_quote_chars: int = Field(default=20_000, ge=1)
    # Actor the local testing CLI's `--admin` mode attributes the audit trail to. That mode bypasses
    # authentication; configurable so a deployment can label its test runs.
    cli_admin_actor: str = "admin@localhost"

    # Roles `--admin` holds; empty means it bypasses authentication only. Its own setting: deriving
    # it from `skill_role_gates` would let a skill-visibility edit hand the CLI privileged tool
    # roles.
    cli_admin_roles: list[str] = Field(default_factory=list)

    # The plan/execute harness: `build_langgraph_agent` attaches `TodoListMiddleware` and the plan
    # gate over the same tools, with no generic batteries (file, web, shell). `plan_only` presents a
    # plan for approval before any state-changing work; `execute` loops the todo list immediately
    # but keeps the plan. `harness_max_loop_iterations` is the runaway cap. On by default because
    # the plan gate covers write tools the role gates do not, and `Settings._check` refuses
    # `entra_required` without it under `plan_only`. Profiles override
    # (`plan_gate.harness_enabled_for`).
    harness_enabled: bool = True
    harness_autonomy: HarnessAutonomy = "plan_only"
    harness_max_loop_iterations: int = Field(default=25, ge=1)

    # Bounds on one `write_todos` call: steps, and tools declared per step. These size the
    # `plan_approvals.scope` row and the out-of-scope refusal text. Scope only narrows, so these
    # bound an unpriced write rather than an escalation; refused at argument validation so the model
    # can split the plan.
    plan_max_steps: int = Field(default=64, ge=1)
    plan_max_tools_per_step: int = Field(default=32, ge=1)

    # Billed tokens one turn may spend (input, output and cache, helpers included) before
    # `agent/spend_cap.py` ends it with a partial answer and `spend_cap_reached`. The loop cap
    # counts calls, not cost, and `api/budget.py` meters only between turns. A runaway backstop, not
    # a budget: derived as `harness_max_loop_iterations` calls each at `agent_context_token_budget`,
    # so a lawful turn never hits it (`tests/test_spend_cap.py` holds that). Set lower from
    # `turn_costs` to buy a cost ceiling, knowing it refuses work. 0 means no cap.
    agent_max_turn_billed_tokens: int = Field(default=3_000_000, ge=0)

    # Supersteps one model call costs, for deriving `agent_recursion_limit`. The framework default
    # (9999) would let a turn run thousands of calls and then fail with `GraphRecursionError`,
    # losing the partial answer; the loop cap is the graceful stop and this ceiling sits just above
    # it. A call costs several supersteps (one per hook-bearing middleware), so 6 includes headroom
    # over the measured cost: re-measure multiplier and constant together whenever middleware
    # changes.
    agent_supersteps_per_model_call: int = Field(default=6, ge=2)

    # Tool calls from one assistant message that may run at once, applied as LangGraph's
    # `max_concurrency` (`agent.state.turn_config`), so it bounds every superstep including helper
    # fan-outs. Multiplied, not added, with admission in the thread reservation
    # (`core/executor.py`). 0 removes the bound.
    agent_max_parallel_tool_calls: int = Field(default=8, ge=0)

    # Agent profiles a turn may delegate to via `task` beside the unnamed `general-purpose` helper
    # (`agent/subagents.py`), pathsep-delimited. A helper's surface is its caller's ∩ the profile's
    # − side-effecting tools, so a name can only narrow. These three keep a coherent surface after
    # that subtraction. Empty leaves only the unnamed helper.
    agent_helper_roster: str = "evidence" + os.pathsep + "computation" + os.pathsep + "safety"

    # Profiles run as peers that hand the conversation over with `transfer_to_…`
    # (`agent/turn_graph.py`); empty builds no turn graph. Off by default: a peer keeps acting tools
    # and answers the chemist, so a mis-routing mesh is worse than one agent, and hand-off accuracy
    # has not been demonstrated. Each peer also adds handoff tools to the prefix. The root profile
    # is implicit.
    agent_peer_roster: str = ""

    # Hand-offs per turn before `transfer_to_…` refuses; 0 removes the bound. Per turn, not per
    # thread (`ChemclawState.handoffs` is untracked), so a long conversation is never capped.
    # Refusal leaves the current agent in control.
    agent_max_handoffs: int = 3

    # Tool names a rostered helper's `task` menu entry lists before "and N more". `describe_helper`
    # lists the bound surface, which grows with the sibling fleet, so `task`'s schema needs a bound
    # to stay under the per-tool ceiling in `tests/test_context_floor.py`.
    agent_helper_menu_tools: int = 12

    # Unparseable tool calls from one reply promoted onto `tool_calls` and refused individually
    # (`agent/model_calls.PromoteInvalidToolCalls`); the rest are counted
    # (`chemclaw_invalid_tool_calls_total`) and named in a WARNING. Bounds audit rows and context
    # fed back from one malformed fan-out. 0 removes the bound.
    agent_max_promoted_invalid_calls: int = Field(default=20, ge=0)

    # Audit events `PostgresAuditSink` may hold before shedding the oldest, so a slow database
    # cannot grow the buffer unboundedly (the in-flight batch counts). Every event is already in the
    # stdlib log; `chemclaw_audit_events_shed_total` is distinct from
    # `chemclaw_audit_sink_failures_total`. At ~1.7 kB per event the default is ~80 MB. 0 removes
    # the bound.
    agent_audit_buffer_max_events: int = Field(default=50_000, ge=0)

    # Times one turn may call a tool with identical arguments before refusal (`agent.repeat_guard`).
    # Two allows a genuine re-check (a polled job, a re-read note).
    max_identical_tool_calls: int = Field(default=2, ge=1)

    # Artefacts (`src/chemclaw/exhibits/`, `agent/exhibit_tools.py`): versioned documents beside the
    # chat. Off unbinds the exhibit tools (saving their prefix) and `GET /sessions/{id}/exhibits`
    # answers `enabled: false`, leaving existing artefacts read-only.
    agent_exhibits_enabled: bool = True
    # Size caps every spec write is validated against: bytes of compact JSON, plus rows, structures
    # and points for the list-shaped kinds.
    exhibit_max_spec_bytes: int = Field(default=200_000, ge=1)
    exhibit_max_rows: int = Field(default=2_000, ge=1)
    exhibit_max_structures: int = Field(default=200, ge=1)
    exhibit_max_points: int = Field(default=5_000, ge=1)
    # Atoms in one inline `geometry` XYZ block, a viewer bound; `source` geometries are not counted.
    exhibit_max_atoms: int = Field(default=500, ge=1)
    # The `html` kind: model-written pages rendered only in the UI's sandbox origin, never served as
    # `text/html` here. Off refuses new ones while existing ones still read (`html_enabled` on the
    # listing). The byte cap is the page's UTF-8 source.
    agent_html_artefacts_enabled: bool = True
    exhibit_max_html_bytes: int = Field(default=200_000, ge=1)
    # Bindings (`exhibits/bindings.py`): distinct stored results one spec may bind (each read and
    # parsed on every artefact read), and the size above which parsing moves off the event loop.
    exhibit_max_bound_results: int = Field(default=20, ge=1)
    exhibit_binding_offload_bytes: int = Field(default=65_536, ge=0)
    # Per-process cache of parsed bound results, in stored bytes; blobs are immutable. 0 disables.
    exhibit_binding_cache_bytes: int = Field(default=8_388_608, ge=0)
    # Failing bindings one refused write names before counting the rest.
    exhibit_binding_problems_shown: int = Field(default=5, ge=1)
    # Minimum milliseconds between `exhibit_draft` frames of one tool call; each frame is the whole
    # document.
    exhibit_draft_min_interval_ms: int = Field(default=250, ge=0)
    # Draft rate in bytes per millisecond: after an N-byte frame the next waits N / this, so a large
    # document's draft bandwidth is linear rather than quadratic in its size.
    exhibit_draft_bytes_per_ms: int = Field(default=100, ge=1)
    # Characters a drafted call may spend beyond spec, title and note before the preview treats it
    # as a call the tool would refuse (`api/exhibit_drafts._argument_bound`). Preview only.
    exhibit_draft_argument_slack_chars: int = Field(default=1_024, ge=0)
    # Artefacts per session; the next create is refused (409 `exhibit_limit`) rather than evicting.
    exhibit_max_per_session: int = Field(default=100, ge=1)
    # Revisions per artefact, refused the same way; every revision keeps a whole spec.
    exhibit_max_revisions: int = Field(default=500, ge=1)
    # The tab title and the one-line change note, in characters.
    exhibit_max_title_chars: int = Field(default=200, ge=1)
    exhibit_max_note_chars: int = Field(default=1_000, ge=1)
    # How much of a revision diff the model sees (in `read_exhibit` and edit notes); excess changes
    # are counted and long values cut. `GET …/diff` is always full.
    exhibit_diff_max_changes: int = Field(default=20, ge=1)
    exhibit_diff_max_value_chars: int = Field(default=300, ge=1)
    # Most differing lines aligned line by line; beyond it the span is one hunk, because alignment
    # is cubic on repeated lines.
    exhibit_diff_max_lines: int = Field(default=200, ge=1)
    # Characters of the per-turn artefact note (chemists' edits, referenced artefacts) appended to
    # the turn's message. The listing is request-only and bounded below.
    exhibit_note_max_chars: int = Field(default=12_000, ge=1)
    # Artefacts each model request's listing names, newest first (`exhibit_notes.ExhibitListing`);
    # the listing is prefix, so the character bound is its cost. A session with none pays nothing.
    exhibit_note_max_listed: int = Field(default=20, ge=1)
    exhibit_listing_max_chars: int = Field(default=2_000, ge=200)
    # Artefacts one chemist message may reference (`MessageIn.exhibit_refs`), each copied into the
    # note.
    exhibit_max_refs: int = Field(default=5, ge=0)
    # The most headers one `GET /exhibits` page serves across a caller's sessions, whatever it asks.
    exhibit_max_listing: int = Field(default=200, ge=1)
    # Unchecked figures one revision records.
    exhibit_max_unverified_figures: int = Field(default=50, ge=1)
    # Evidence rows per round trip while grounding an artefact's figures against tool results.
    exhibit_grounding_batch: int = Field(default=32, ge=1)

    # Profile directories (`agents.profile_discovery`), OS-pathsep-delimited. A profile specific to
    # one capability lives in that connector's bundle instead.
    profiles_dir: str = "data/profiles"

    # Directories of deterministic step templates: fixed-order procedures run as durable workflows
    # (`src/chemclaw/templates/README.md` contrasts them with profiles).
    templates_dir: str = "data/templates"
    # Which discovered templates are enabled; empty (the default) means every one found.
    templates_enabled: str = ""
    # Per-step wall clock for a template run (an agent turn or a calculation).
    template_step_timeout_seconds: float = Field(default=900.0, gt=0)
    # Heartbeat timeout for template steps, so a dead worker is noticed before the step budget
    # lapses. `run_tool_step`/`run_agent_step` beat via `durable/heartbeat.beating`, which derives
    # its interval from this.
    template_step_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # Whole-run wall clock for one template execution. A literal, not derived, so raising
    # `connector_job_timeout_seconds` is refused at startup until this is raised too:
    # `_the_template_run_ceiling_covers_one_step` requires it to exceed the longest step's ceiling
    # (`template_step_ceilings`), usually a `job` step's `wrapper_execution_timeout()`.
    template_run_timeout_seconds: float = Field(default=45330.0, gt=0)

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
        """The skills directories, split on the OS path separator (like PATH), empties dropped."""
        return [d for d in self.skills_dir.split(os.pathsep) if d]

    @property
    def helper_roster(self) -> list[str]:
        """The profile names offered as `task` helpers; empty leaves the single unnamed helper."""
        # Stripped, because `refuse_an_unknown_roster` raises at startup and `"evidence:
        # computation"` would otherwise stop the front door.
        return [name.strip() for name in self.agent_helper_roster.split(os.pathsep) if name.strip()]

    @property
    def peer_roster(self) -> list[str]:
        """The profile names run as peers; empty means no turn graph is built (the default).

        Stripped for `helper_roster`'s reason: `refuse_an_unknown_peer_roster` also raises at
        startup.
        """
        return [name.strip() for name in self.agent_peer_roster.split(os.pathsep) if name.strip()]

    @property
    def skills_enabled_list(self) -> list[str]:
        """The explicitly enabled skill names; empty means every discovered skill."""
        return [s for s in self.skills_enabled.split(os.pathsep) if s]

    @property
    def agent_recursion_limit(self) -> int:
        """The graph step ceiling one turn runs under (`agent.state.turn_config`).

        Derived from `harness_max_loop_iterations` so the loop cap always fires first and the
        ceiling only catches a bug. `agent/loop_cap.enforce_loop_cap` allows one wrap-up call past
        the cap, hence `cap + 1` calls. Measured cost is `5 * N + 7` supersteps; the formula grants
        `agent_supersteps_per_model_call * (cap + 1) + 8`, a margin that grows with the cap, because
        an exact fit breaks the first time a middleware is added. Re-measure multiplier and constant
        together.
        """
        return (self.harness_max_loop_iterations + 1) * self.agent_supersteps_per_model_call + 8
