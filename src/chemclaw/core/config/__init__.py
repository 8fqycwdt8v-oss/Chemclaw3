"""Single, typed source of every environment-dependent value in Chemclaw.

Why this exists: the plan forbids magic numbers and second config sources (CLAUDE.md "Config,
never magic numbers"; plan step 0.3). Every URL, DSN, queue name, and timeout that code or
infrastructure needs is declared here once, is type-checked, and is overridable via environment
variables or a local `.env` file. `infra/docker-compose.yml` is wired to the same variable
names, so the app and the dev stack can never drift apart.

Usage:
    from chemclaw.core.config import settings
    client_target = settings.temporal_address

Only fields that are actually consumed (by code or by the compose stack) live here — no
speculative "for later" settings. New phases add their own fields when the first real consumer
lands.

Structure: the flat `Settings` class is composed from one mixin per domain (`class
Settings(ObservabilitySettings, TemporalSettings, ...)`), and each mixin lives in its own module
of this package, named for its section (D-072 drew the section boundaries; the D-156-blessed
split gave each section a file). Each mixin holds its section's fields, validators, and derived
properties, so a reader finds everything about one concern in one file — while the composed
class keeps every attribute flat (`settings.postgres_dsn`) and every env name
unprefixed-by-section (`CHEMCLAW_POSTGRES_DSN`), exactly as before the split. A cross-field
validator lives in the section that owns the relationship; only a rule that spans sections lives
here on the composed class, because no single section can see both sides of it.

House rule for a *collection* field — pick by what the elements are, not by taste:

- **Delimited string** (`skills_dir`, `data_sources`, `data_sources_dir`, `connectors_dir`,
  `skills_enabled`, `entra_expensive_actions`) when the elements are *bare keys* — names resolved
  against a registry, or paths. An admin sets these like `PATH`, with no JSON quoting, and a bare
  key has nothing to validate beyond resolving. Expose the parsed value through a derived
  `*_list`/`*_dirs` property and read that, never the raw string.
- **Plain mapping** (`connector_urls`) for a per-name deployment override of one scalar.

There used to be a third: a **typed JSON list** whose elements each carried their own config, as a
discriminated union of pydantic models — `McpServerSpec`, then `DataSourceSpec`. Both are gone, and
they went the same way rather than by coincidence. Each described a *thing attached to this
deployment* (an MCP server; an ELN drop directory), and each made attaching one an edit to this
file: a new model, a new arm of the union, and a new branch at the single place that built from it.
D-118 replaced the first with `connectors/<name>/connector.yaml` and D-120 the second with
`sources/<name>/datasource.yaml`, so the config token is now a *path* to search and a *name* to
enable, and the thing's own configuration lives with the thing.

The rule that leaves behind, worth stating because it is what keeps this file from growing without
bound: **config says which and where; a manifest says what.** If a proposed field would describe
the internals of one attached thing, it belongs in that thing's manifest, not here.
"""

from typing import Self

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from chemclaw.core.config.agent import AgentSettings
from chemclaw.core.config.bo import BoSettings
from chemclaw.core.config.calculators import CalculatorSettings
from chemclaw.core.config.connectors import ConnectorSettings
from chemclaw.core.config.eln import ElnSettings
from chemclaw.core.config.entra import EntraSettings
from chemclaw.core.config.evals import EvalSettings
from chemclaw.core.config.fingerprints import FingerprintSettings
from chemclaw.core.config.hpc import HpcSettings
from chemclaw.core.config.kg import KgSettings
from chemclaw.core.config.llm import LlmSettings
from chemclaw.core.config.memory import MemorySettings
from chemclaw.core.config.observability import ObservabilitySettings
from chemclaw.core.config.reports import ReportSettings
from chemclaw.core.config.retrieval import (
    NOTE_INDEX_SOURCES,
    SCHEMA_VECTOR_DIM,
    RetrievalSettings,
)
from chemclaw.core.config.service import ServiceSettings
from chemclaw.core.config.sources import SourcesSettings
from chemclaw.core.config.store import StoreSettings
from chemclaw.core.config.temporal import TemporalSettings
from chemclaw.core.egress import pin_langsmith_egress

# The package's public surface, exactly what the single-file module exported: the composed class,
# its singleton, every section mixin (a few are imported directly, e.g. `EvalSettings`), and the
# one shared constant. Explicit because `mypy --strict` disables implicit re-export for names that
# arrive via import — which is all of them, now that the sections live in their own modules.
__all__ = [
    "NOTE_INDEX_SOURCES",
    "SCHEMA_VECTOR_DIM",
    "AgentSettings",
    "BoSettings",
    "CalculatorSettings",
    "ConnectorSettings",
    "ElnSettings",
    "EntraSettings",
    "EvalSettings",
    "FingerprintSettings",
    "HpcSettings",
    "KgSettings",
    "LlmSettings",
    "MemorySettings",
    "ObservabilitySettings",
    "ReportSettings",
    "RetrievalSettings",
    "ServiceSettings",
    "Settings",
    "SourcesSettings",
    "StoreSettings",
    "TemporalSettings",
    "settings",
]


class Settings(
    ObservabilitySettings,
    TemporalSettings,
    StoreSettings,
    HpcSettings,
    CalculatorSettings,
    BoSettings,
    LlmSettings,
    AgentSettings,
    ServiceSettings,
    EntraSettings,
    KgSettings,
    EvalSettings,
    FingerprintSettings,
    ElnSettings,
    SourcesSettings,
    ConnectorSettings,
    MemorySettings,
    RetrievalSettings,
    ReportSettings,
):
    """Environment configuration, loaded from process env then a local `.env`.

    Field names map to `CHEMCLAW_<FIELD>` environment variables (e.g.
    `CHEMCLAW_TEMPORAL_ADDRESS`). Defaults target the local `docker-compose` dev stack so a
    fresh checkout runs without any `.env` present.

    Composed from the per-domain section mixins, one module each in this package; every field
    stays a flat attribute with its original env name, and this `model_config` (prefix,
    `.env`, `extra="forbid"`) governs them all.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    @model_validator(mode="after")
    def _guards_that_the_comments_already_demand(self) -> Self:
        """The combinations whose prose already forbids them, now enforced at startup.

        Each of these was documented in a field comment as "must stay below" / "this must stay 1"
        and enforced by nothing, so a deployment could set it and find out in production. A rule
        worth writing down is worth failing on. (Counted in the list below, not in this sentence —
        a number in prose beside a list is a number that goes stale.)

        - **A tool-result clear trigger above the conversation budget.** The lossless edit was
          split off from the budget precisely so it could fire first; setting it higher means it
          never fires before the window does, which is the behaviour the split removed. The
          inverted setting is the worse of the two possible misconfigurations because it looks
          like it took effect.
        - **`service_uvicorn_workers > 1` silently breaks five per-process guarantees.** Until
          those have a shared story (shared rate limiter, shared budget tracker, shared attachment
          store, shared session LRU, shared metrics scrape), the knob is a foot-gun that offers no
          operator response path. Replicas plus Route affinity remain the supported way to use more
          CPU (entrypoint.sh comment, lines 13-20).
        - **A fleet admitting more concurrent turns than its declared ceiling.** The admission cap
          is per-process by design (SCALE-1), which makes `replicas × uvicorn workers × cap` the
          number the shared LLM endpoint actually sees — and nothing stated it, so raising the
          per-process cap multiplied fleet demand invisibly. Only checked once an operator declares
          the ceiling their endpoint can serve; undeclared, there is nothing to check against.
        - **A fleet opening more Postgres connections than the server will serve.** The same shape
          one subject over, and `core/config/store.py` had stated the multiplication in prose since
          the pool landed: `pg_pool_max_size` bounds one process, and the deployment total is that
          times every process that opens a pool. Nothing computed it, so the shipped chart ran all
          of its pods on the default 16 and the fleet ceiling was ~272 against the
          `max_connections=100` D-119 measured against. Same self-disabling convention: undeclared
          means inert.
        - **Mid-turn resume outliving the turn.** A resume wait longer than the turn deadline can
          never complete; it just burns the turn's remaining time holding an admission permit.
        - **Budgets enabled with every cap at zero.** `0` means unlimited for each cap, so this is
          a guard that guards nothing while reporting itself as on.
        - **`embedding_dim` disagreeing with the `vector(N)` column** in migration 012, *while
          anything writes the note index*. pgvector rejects the insert at write time, so the
          mismatch surfaces as a failed reindex rather than as the configuration error it is.
          Still scoped rather than unconditional, because the embedder is used on its own (the hash
          embedder's unit tests pick a small dim and touch no database) — but the scope was wrong
          (DARK-8): it asked whether the *vector* source was enabled, while `reindex_notes` writes
          the embedding column for **every** note-index-backed source. A `lexical`-only deployment
          with a 768-wide model therefore passed validation and failed every reindex on a pgvector
          dimension error, with nothing pointing at the setting that caused it. The question is
          "does anything in this deployment write `note_index`", and `note_reindex_enabled` is the
          third way that happens — the scheduled rebuild, which needs no retrieve source at all.
        """
        if self.agent_tool_result_clear_trigger > self.agent_context_token_budget:
            raise ValueError(
                "agent_tool_result_clear_trigger must not exceed agent_context_token_budget: the "
                "lossless tool-result edit exists to run *before* the destructive conversation "
                "window, and setting it above the budget silently restores the single-threshold "
                "behaviour it was split off from — a misconfiguration that looks like it took "
                "effect (agent/compaction.py::context_compaction_middleware)."
            )
        if self.service_uvicorn_workers > 1:
            raise ValueError(
                "service_uvicorn_workers>1 silently breaks five per-process guarantees until they "
                "have a shared story: the rate limiter (api/rate_limit.py, N× configured rate), "
                "the budget tracker (api/budget.py, N× budget), the attachment store "
                "(agent/attachments.py STORE, upload on worker A invisible to a turn on worker B), "
                "the session LRU (api/state.py live-session, state/todos drift), and the metrics "
                "registry (core/metrics.py, a scrape hits one worker, counters under-report ~1/N). "
                "Replicas plus Route affinity are the supported way to use more CPU (D-121)."
            )
        if self.service_fleet_max_concurrent_turns:
            admitted = (
                self.service_fleet_replicas
                * self.service_uvicorn_workers
                * self.service_max_concurrent_turns
            )
            if admitted > self.service_fleet_max_concurrent_turns:
                raise ValueError(
                    f"this deployment may admit {admitted} concurrent turns "
                    f"({self.service_fleet_replicas} replicas × {self.service_uvicorn_workers} "
                    f"uvicorn worker(s) × {self.service_max_concurrent_turns} per process) against "
                    f"a declared fleet ceiling of {self.service_fleet_max_concurrent_turns}. Lower "
                    "service_max_concurrent_turns or the replica ceiling, or raise "
                    "service_fleet_max_concurrent_turns if the LLM endpoint can serve it."
                )
        if self.pg_fleet_max_connections:
            opened = self.pg_fleet_pooled_processes * self.pg_pool_max_size
            if opened > self.pg_fleet_max_connections:
                raise ValueError(
                    f"this deployment may open {opened} Postgres connections "
                    f"({self.pg_fleet_pooled_processes} pooled process(es) × "
                    f"{self.pg_pool_max_size} per pool) against a declared server ceiling of "
                    f"{self.pg_fleet_max_connections}. Lower pg_pool_max_size or the number of "
                    "pooled processes, or raise pg_fleet_max_connections if the server's "
                    "max_connections can serve it."
                )
        if self.mid_turn_resume_enabled and (
            self.mid_turn_resume_timeout_seconds >= self.service_turn_timeout_seconds
        ):
            raise ValueError(
                "mid_turn_resume_timeout_seconds must be smaller than service_turn_timeout_seconds"
            )
        if self.budget_enabled and not any(
            (
                self.budget_max_turns_per_session,
                self.budget_max_tokens_per_session,
                self.budget_max_turns_per_user,
                self.budget_max_tokens_per_user,
            )
        ):
            raise ValueError(
                "budget_enabled=true with every cap at 0 (unlimited) guards nothing; set at least "
                "one budget_max_* cap or disable budgets"
            )
        writes_note_index = self.note_reindex_enabled or bool(
            NOTE_INDEX_SOURCES & set(self.data_source_list)
        )
        if writes_note_index and self.embedding_dim != SCHEMA_VECTOR_DIM:
            raise ValueError(
                f"embedding_dim={self.embedding_dim} disagrees with the note_index vector column "
                f"({SCHEMA_VECTOR_DIM}, infra/sql/012_note_index.sql); pgvector would reject "
                "every write. Change both together, or drop 'vector' from data_sources."
            )
        return self

    @model_validator(mode="after")
    def _the_fan_out_ceiling_covers_the_section_it_bounds(self) -> Self:
        """The same rule as below, on the other parent/child pair that has a ceiling.

        A fan-out child's longest single piece of work is one report section, budgeted by
        `report_section_timeout_seconds` (`durable/report_workflow.py`). A ceiling at or under that
        pre-empts a section that was still running and reports it as a timed-out child — the guard
        causing the failure it exists to bound. Strictly greater, because equality is the defect.

        Scoped here rather than to `ReportSettings` only for symmetry with the validator below; both
        settings live in that section, so this one could move if a second cross-section rule ever
        needs the company.
        """
        if self.fan_out_child_timeout_seconds <= self.report_section_timeout_seconds:
            raise ValueError(
                f"fan_out_child_timeout_seconds={self.fan_out_child_timeout_seconds} does not "
                f"cover the section it bounds: one section may take "
                f"{self.report_section_timeout_seconds}s (report_section_timeout_seconds). Raise "
                "the ceiling above it, or lower the section budget."
            )
        return self

    @model_validator(mode="after")
    def _the_template_run_ceiling_covers_one_step(self) -> Self:
        """The same rule again, on the template run and the step it has to contain.

        `templates/registry.py` starts `TemplateWorkflow` with `template_run_timeout_seconds` as an
        execution timeout, and the longest thing inside it is one step at
        `template_step_timeout_seconds`. A run ceiling at or below the step budget kills the
        procedure inside its own first step — with a bare `WorkflowExecutionTimedOut` naming
        neither setting, and with the per-step timeout that was *meant* to fire made unreachable,
        so `agent_step_retry`'s attempts become a number that can never be spent.

        Strictly greater rather than at least, because equality is the defect. Only one step is
        required rather than N: how many steps a template has is a property of a YAML file this
        object cannot see, so the honest machine-checkable floor is "a single step fits", and the
        setting's own comment carries the sizing advice for a longer procedure.
        """
        if self.template_run_timeout_seconds <= self.template_step_timeout_seconds:
            raise ValueError(
                f"template_run_timeout_seconds={self.template_run_timeout_seconds} does not cover "
                f"the step it bounds: one step may take {self.template_step_timeout_seconds}s "
                "(template_step_timeout_seconds), so the run would time out inside its own first "
                "step. Raise the run ceiling above it, or lower the step budget."
            )
        return self

    @model_validator(mode="after")
    def _the_job_ceiling_covers_the_poll_it_bounds(self) -> Self:
        """A parent ceiling no larger than its child's longest activity is not a ceiling.

        `ConnectorJobWorkflow` gives its child `connector_job_timeout_seconds` as an
        **execution** timeout (`durable/connector_job.py`), and the longest thing inside that child
        on the QM path is the HPC poll, whose single-attempt `start_to_close` budget is
        `hpc_run_timeout_seconds` under `nextflow` (`connectors/qm/workflows.py`). Both default to
        86400, so the shipped `nextflow` configuration gives the parent *less* room than the one
        activity it has to contain.

        Two things break, and neither says so. The poll's `BAD_DATA_RETRY` is dead — a single
        attempt already exhausts the parent's whole budget, so `activity_max_attempts` is a number
        that can never be reached. And an operator following `hpc_run_timeout_seconds`'s own
        comment ("it must cover the whole run") raises it, observes no change whatsoever, and gets
        a bare `WorkflowExecutionTimedOut` naming neither setting. A rule that two comments already
        imply and nothing enforces is the shape this file's other guard was written for.

        Checked on the *selected* backend, mirroring the workflow's own branch, so the mock path is
        validated as the mock path rather than against a budget it does not use. The allowance is
        the rest of that workflow: five activities around the poll — prepare, the cache lookup and
        submit before it, parse and persist after — each capped at `qm_activity_timeout_seconds`.
        Strictly greater rather than at least, because equality is the defect.

        Scoped to `Settings` and not to `HpcSettings` because it is the one rule here that spans
        two sections: the ceiling is a connector-wide deployment choice and the poll budget is the
        QM path's, and neither section can see the other.
        """
        if self.hpc_launch_interface == "nextflow":
            poll_budget = self.hpc_run_timeout_seconds
            budget_name = "hpc_run_timeout_seconds"
        else:
            poll_budget = self.hpc_mock_run_seconds + self.qm_activity_timeout_seconds
            budget_name = "hpc_mock_run_seconds + qm_activity_timeout_seconds"
        needed = poll_budget + 5 * self.qm_activity_timeout_seconds
        if self.connector_job_timeout_seconds <= needed:
            raise ValueError(
                f"connector_job_timeout_seconds={self.connector_job_timeout_seconds} does not "
                f"cover the QM job it bounds: the poll alone may take {poll_budget}s "
                f"({budget_name}, hpc_launch_interface={self.hpc_launch_interface!r}) and the five "
                f"activities around it up to {5 * self.qm_activity_timeout_seconds}s more "
                f"(qm_activity_timeout_seconds). Raise connector_job_timeout_seconds above "
                f"{needed}, or lower the poll budget — raising the poll budget alone changes "
                "nothing, because the parent's ceiling fires first."
            )
        return self


settings = Settings()
"""Process-wide configuration singleton. Import this, not the class."""

# The one line in this file that *does* something, and it is here for the reason the rest of the
# file exists: this module is the single import every entrypoint makes (`api/app.py`,
# `cli/chat.py`, `cli/connectors_dev.py`, `connectors/server_entry.py`,
# `durable/background_worker.py`), so a decision applied here is applied by every process without
# each launcher having to remember. `langsmith` turns its own tracing on from ambient environment
# and would otherwise post prompts and completions to a third party that D-2026-08-11 declined;
# `chemclaw.core.egress` documents why the pin needs both the in-process global and the environ
# write, and why it overrides rather than defaults.
pin_langsmith_egress(allowed=settings.langsmith_tracing_allowed)
