"""The MAF conversational agent: model, skills, capabilities, compaction, harness.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    """The MAF conversational agent: model, skills, capabilities, compaction, harness.

    Grouped because everything here shapes how `build_agent` assembles one agent — which model
    orchestrates, which skills and MCP capability servers attach, how the conversation context
    is compacted, and whether the autonomous plan/execute harness (Phase F1) wraps it.
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
    framing_envelope_secret: str = ""

    # Which framework assembles the conversation layer. `maf` is the Microsoft Agent Framework
    # this system was built on; `langgraph` selects the LangGraph rebuild (a typed `StateGraph`,
    # `interrupt()` for every human gate, a Postgres checkpointer for turn state, and the
    # supervisor/specialist team). Both engines advertise the same tools, skills and audit
    # middleware and emit the same `api/events.py` stream — that event contract is the
    # conformance boundary between them, which is what makes running the eval suite twice a
    # measurement rather than an argument.
    #
    # **Defaults to `langgraph` since M13 Step 0.** It defaulted to `maf` while the rebuild landed
    # phase by phase, so an unfinished engine was never the one a deployment got; that condition
    # ended when both engines carried the whole suite — 4233/37 on MAF, 4223/47 flipped, zero
    # failures either way, the difference being tests that name a MAF-only subject.
    #
    # **What this default does not assert.** M12 left three probes unrun because each needs a live
    # model credential: the D-123 concurrency run, plan→approve→execute end to end, and team
    # routing accuracy. This value says the graph engine passes everything measurable offline. It
    # does not say those three passed, and the switch stays here until they do — a deployment that
    # must have the old engine still has `CHEMCLAW_AGENT_ENGINE=maf`. The switch and the MAF branch
    # are deleted together once the rebuild is proven live (no dead path kept "for later").
    agent_engine: Literal["maf", "langgraph"] = "langgraph"

    # Route turns through a supervisor and five specialists rather than one agent (M9,
    # `docs/decisions/D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md`).
    #
    # **Off by default, and this is not caution about the code.** A single agent holding sixty
    # tools chooses badly among them and pays for the whole surface in every prompt, which is the
    # case for a team; but a supervisor that mis-routes is *worse* than the agent it replaces, and
    # no unit test can establish which of those a deployment gets. M12 measures routing accuracy
    # and per-specialist token cost against the single-agent baseline, and that measurement — not
    # this flag — is what decides whether the default changes. LangGraph only.
    agent_teams_enabled: bool = False

    # MAF agent (plan step 1.5). `agent_model` is the orchestration model name
    # (ENV-overridable); the provider's API key is read by the chat client from its own env var
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
    # Conversation context management (MAF compaction). The agent keeps a session thread and
    # composes tool calls that return large payloads (evidence sweeps, full ELN recipes), so a
    # long chat would grow unbounded. Compaction runs only when the included context exceeds
    # `agent_context_token_budget` (measured with a char/4 estimator — no external tokenizer),
    # then reclaims tokens cheapest-first: collapse stale tool-result dumps to a short trace
    # (keeping the newest `agent_keep_last_tool_groups` verbatim), then drop older conversation
    # turns beyond `agent_keep_last_conversation_groups`. System instructions/skills are always
    # kept. No LLM summarizer — deterministic and credential-free.
    agent_context_token_budget: int = Field(default=100_000, ge=1)
    agent_keep_last_tool_groups: int = Field(default=2, ge=0)
    agent_keep_last_conversation_groups: int = Field(default=12, ge=1)
    # Apply that same policy to the *stored* history, not only to the model's context (D-151).
    # MAF's after-run compaction cannot reach `PostgresHistoryProvider` — it reads a session-state
    # slot the durable provider deliberately never writes — so under `session_store="postgres"` the
    # rows accumulated forever and every turn re-read all of them. This runs the identical strategy
    # against the table after a turn is stored.
    #
    # Off by default, matching `retention_enabled`: a `DELETE` on conversation history is a policy a
    # deployment states, never one it inherits on upgrade. Deliberately reuses the budget above
    # rather than taking its own — durable deletion is strictly more destructive than context
    # exclusion, so it must never be the more aggressive of the two.
    agent_durable_compaction_enabled: bool = False
    # Short sessions never pay the extra read: a pass is only worth doing once there is something to
    # reclaim, and this is far below where the token budget starts excluding anything.
    agent_durable_compaction_min_rows: int = Field(default=200, ge=1)

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

    # MAF Agent Harness (plan Phase F1) — the autonomous plan/execute backbone (the
    # Claude-Code-like experience). When `harness_enabled`, `build_agent` wires MAF's
    # `create_harness_agent` (todo list + plan/execute mode + a bounded completion loop) over
    # the *same* tools/skills/audit/ compaction as the classic agent, with MAF's generic
    # batteries (file memory/access, web search, shell) OFF — capability comes from our MCP
    # servers and tools, not the harness's built-ins. Off by default so today's classic
    # single-turn agent stays the safe fallback against the harness's `[Experimental]` API.
    # `harness_autonomy` picks the starting mode: `plan_only` (default, the pharma-safe one)
    # starts in plan mode and presents a plan for human approval before any execution — the
    # pre-execution GxP gate — and only loops once approval switches it to execute; `execute`
    # starts looping through the todo list immediately. `harness_max_loop_iterations` caps the
    # loop so a stuck plan aborts instead of spinning (the runaway guard).
    harness_enabled: bool = False
    harness_autonomy: Literal["plan_only", "execute"] = "plan_only"
    harness_max_loop_iterations: int = Field(default=25, ge=1)

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

        `FileSkillsSource` takes a list of directories; keeping the config a single delimited
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
