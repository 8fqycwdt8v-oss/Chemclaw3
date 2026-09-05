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

import logging
from typing import Self

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from chemclaw.core.config.agent import AgentSettings
from chemclaw.core.config.bo import BoSettings
from chemclaw.core.config.calculators import CalculatorSettings
from chemclaw.core.config.connectors import ConnectorSettings
from chemclaw.core.config.deliver import DeliverySettings
from chemclaw.core.config.eln import ElnSettings
from chemclaw.core.config.entra import EntraSettings
from chemclaw.core.config.evals import EvalSettings
from chemclaw.core.config.fingerprints import FingerprintSettings
from chemclaw.core.config.kg import KgSettings
from chemclaw.core.config.labels import LabelSettings
from chemclaw.core.config.llm import LlmSettings
from chemclaw.core.config.memory import MemorySettings
from chemclaw.core.config.observability import ObservabilitySettings
from chemclaw.core.config.publish import PublishSettings
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
from chemclaw.core.netguard import arm_from_settings as arm_egress_guard

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
    "KgSettings",
    "LabelSettings",
    "LlmSettings",
    "MemorySettings",
    "ObservabilitySettings",
    "DeliverySettings",
    "PublishSettings",
    "ReportSettings",
    "RetrievalSettings",
    "ServiceSettings",
    "Settings",
    "SourcesSettings",
    "StoreSettings",
    "TemporalSettings",
    "settings",
]


_TLS_SSLMODES = {"require", "verify-ca", "verify-full"}
# `""` is in here for the callers that build a URL and ask whether its host is local — a value with
# no host at all (`/var/chemclaw/outbox`) reads as local, which is what `publish/drivers/http.py`,
# `deliver/driver.py` and `cli/validate_channels.py` want. **`require_pg_tls` deliberately does not
# use that member**, and it is worth saying here rather than only there: for a Postgres DSN an empty
# host does not mean local, it means libpq will resolve one from a `service=` file or from `PGHOST`,
# neither of which the parse can see. See `_dial_is_offline` below.
PG_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

# `durable/connector_job.py::_FINISH_STEPS`, restated because it cannot be imported.
#
# `ConnectorJobWorkflow` is bounded by `wrapper_execution_timeout()` — its child's whole ceiling
# plus one activity's wall clock for each of the four things the wrapper still owes after that
# child returns (the durable record, the results offer, the note PR-gate, the session push-back).
# That number is what a template `job` step is actually bounded by, and
# `_the_template_run_ceiling_covers_one_step` below has to clear it.
#
# It is a literal here and a literal there because `chemclaw.core` imports no sibling
# (`tests/test_layering.py`, `core/README.md`), so this module cannot call the function that owns
# the arithmetic. A restatement nothing checks would be the duplication moved rather than removed,
# so `tests/test_template_job_step.py` asserts the two constants agree *and* that the full
# identity `wrapper_execution_timeout() == connector_job_timeout_seconds + activity_timeout_seconds
# * _WRAPPER_FINISH_STEPS` still holds — a fifth post-child step turns that red instead of leaving
# this validator clearing a bound 30 s short.
_WRAPPER_FINISH_STEPS = 4


def _pg_dial(dsn: str, name: str) -> tuple[str, str]:
    """The host libpq will dial and the sslmode it will use, read with libpq's own parser.

    **One parser, because a second spelling is a second answer.** This pair used to be hand-rolled
    (`urlsplit` + `parse_qs` + a `dsn.split()` scan) while `core/db.py::_redact` round-tripped the
    same strings through `conninfo_to_dict` for the reason it states — "every form psycopg accepts
    is covered … not just the userinfo case a URL split can see". The two disagreed, and both
    measured disagreements were in the direction that admits plaintext:

    - libpq *connects* to `hostaddr` when it is set and reads `host` only as the name to verify
      against, so `host=localhost hostaddr=10.0.0.5` took the loopback exemption while the socket
      went over the network. `hostaddr or host` is therefore what a TLS decision is about.
    - libpq resolves a repeated URL query parameter to its **last** occurrence and `parse_qs` to
      its **first**, so `?sslmode=require&sslmode=disable` was checked as `require` and connected
      as `disable`.

    A DSN libpq cannot parse raises here rather than defaulting: `""` is a member of
    `PG_LOOPBACK_HOSTS`, so *any* "could not tell" answer would exempt the connection, and nothing
    can say what libpq would do with a string libpq will not read. It is refused as the
    misconfiguration it is, naming the setting rather than the DSN — the value carries a password.
    """
    # Imported inside the function, not at module scope. `core/config` is reached by
    # `ingest/sources/registry`, and `tests/test_datasource_isolation.py` asserts that asking which
    # sources to ingest imports **no** third-party driver — a manifest is two strings and should
    # cost two strings. Reading a DSN the way libpq reads it is worth a driver import; doing it at
    # import time would put psycopg behind every caller of `settings`.
    from psycopg import ProgrammingError, conninfo

    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except ProgrammingError as exc:
        # **The exception's message is deliberately not interpolated.** libpq quotes the offending
        # token, and for two realistic single-character slips — a typo'd scheme, a stray leading
        # space — that token is the whole DSN, userinfo included. This raise happens during
        # `import chemclaw.core.config`, which is *before* `configure_logging()` installs
        # `SecretRedactingFilter`, so a credential printed here reaches the container log with
        # nothing able to scrub it. The docstring above already promised this ("naming the setting
        # rather than the DSN — the value carries a password") and the first version of this code
        # broke that promise in the same breath.
        #
        # The type is named because it is the one part of libpq's answer that carries no input, and
        # it distinguishes "unparseable" from the other ways this can fail. `__cause__` keeps the
        # original for a debugger; it is the traceback rather than the message, and a traceback is
        # not what ships to a log aggregator from a startup refusal.
        raise ValueError(
            f"{name} is not a connection string libpq can parse "
            f"({type(exc).__name__}), so nothing can say whether it would connect with TLS. "
            "Refused under entra_required=true rather than guessed at; the same DSN would fail at "
            "connect. The value is not repeated here because it carries a password."
        ) from exc
    return str(parts.get("hostaddr") or parts.get("host") or "").lower(), str(
        parts.get("sslmode") or "prefer"
    ).lower()


def _dial_is_offline(host: str) -> bool:
    """Whether every host libpq would dial is a Unix-domain socket rather than a network address.

    libpq reads a `host` beginning with `/` as a socket *directory* and one beginning with `@` as an
    abstract-namespace socket, and `sslmode` does not apply to either — there is no network to
    encrypt, and libpq ignores the parameter outright on that transport. So a TLS guard has nothing
    to require here, and requiring it anyway was a strict regression: the hand-rolled parse this
    replaced could not see `host=` inside a URL query, so `postgresql:///db?host=/var/run/postgresql`
    started, and the rewrite refused the whole class with the only passing spelling being
    `sslmode=require` on a transport that ignores it — a lie written into a DSN to satisfy a guard.
    A `pgbouncer` sidecar or a local cluster over a mounted socket is the deployment that means.

    **Every element**, because `host` may be a comma-separated list libpq tries in order: one socket
    directory beside one network host is a network connection, and `startswith` on the joined string
    would answer otherwise.
    """
    return all(part.strip().startswith(("/", "@")) for part in host.split(","))


def require_pg_tls(dsn: str, name: str) -> None:
    """Refuse a non-loopback Postgres DSN whose sslmode leaves plaintext or an unverified peer.

    libpq's default is `prefer`: it tries TLS, silently falls back to cleartext when the server does
    not offer it, and verifies no certificate even when it does negotiate. The full conversation
    transcript, the turn checkpoints and the audit trail all cross this connection, so under the
    enforced posture a non-loopback DSN must state `sslmode=require`/`verify-ca`/`verify-full`
    (`verify-full` recommended, with `sslrootcert=`). Loopback dev and a Unix socket are exempt.

    **An empty host is not the loopback exemption, and it used to take it.** `PG_LOOPBACK_HOSTS`
    contains `""` for callers that ask the same question about a URL they built themselves, and
    reading a DSN through that member is the fail-open `_pg_dial`'s docstring says it closes:
    `conninfo_to_dict` is `PQconninfoParse`, which reads the *string* and applies neither libpq's
    environment defaults (`PGHOST`) nor a `service=` file, so `service=chemclaw` and
    `dbname=c user=u` both parse cleanly to no host and were exempted while libpq dialled a remote
    server at `prefer`. Refused rather than resolved — resolving means a second implementation of
    libpq's precedence rules, which is the "a second spelling is a second answer" error this guard
    exists to have stopped making. Both escapes are one honest line: name the host, or state the
    sslmode.
    """
    host, sslmode = _pg_dial(dsn, name)
    if sslmode in _TLS_SSLMODES or _dial_is_offline(host):
        return
    if not host:
        raise ValueError(
            f"entra_required=true with a {name} that names no host and no sslmode: libpq resolves "
            "the host from a service file or from PGHOST, neither of which this check can read, so "
            "nothing here can say whether the connection would leave the pod — and it carries the "
            "conversation transcripts, turn checkpoints and the audit trail. Name the host in the "
            "DSN (a Unix socket directory such as host=/var/run/postgresql is exempt), or state "
            "sslmode=verify-full&sslrootcert=<ca> so the answer does not depend on the host."
        )
    if host in PG_LOOPBACK_HOSTS:
        return
    raise ValueError(
        f"entra_required=true with a non-loopback {name} and sslmode={sslmode!r}: libpq's "
        "default permits a silent plaintext fallback and verifies no certificate, and this "
        "connection carries the conversation transcripts, turn checkpoints and the audit trail. "
        "Add "
        "sslmode=verify-full&sslrootcert=<ca> to the DSN (or sslmode=require on a trusted net)."
    )


class Settings(
    ObservabilitySettings,
    TemporalSettings,
    StoreSettings,
    CalculatorSettings,
    BoSettings,
    LlmSettings,
    AgentSettings,
    ServiceSettings,
    EntraSettings,
    KgSettings,
    EvalSettings,
    FingerprintSettings,
    LabelSettings,
    ElnSettings,
    SourcesSettings,
    ConnectorSettings,
    MemorySettings,
    RetrievalSettings,
    ReportSettings,
    DeliverySettings,
    PublishSettings,
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

    @property
    def note_reindex_effective(self) -> bool:
        """Whether the note reindex schedule runs — derived from the source list unless overridden.

        Lives on the composed class because the answer spans two mixins: the flag is
        `RetrievalSettings`' and the source list is `SourcesSettings`'. The field's own comment
        carries the why; this is only the join.
        """
        if self.note_reindex_enabled is not None:
            return self.note_reindex_enabled
        return bool(NOTE_INDEX_SOURCES & set(self.data_source_list))

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
        - **A stated autonomy that nothing enforces.** `harness_autonomy="plan_only"` with
          `harness_enabled=False` is `gate_applies` returning False — the approval-first posture
          named in one setting and attached by neither. Both are the *code defaults*, so this is
          refused only under `entra_required`, which is the deployment that believes it is in the
          enforced posture: the shipped chart sets the harness on, and the image run directly,
          `docker compose` and any non-Helm deployment did not. The opt-out is
          `harness_autonomy=execute`, in the same vocabulary as the thing being decided rather
          than in a security knob that means something else; with the harness off it changes no
          behaviour, which is what makes it a statement rather than a switch.
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
          the pool landed: `pg_pool_max_size` bounds one *pool*, and the deployment total is that
          times every pool the fleet opens. Nothing computed it, so the shipped chart ran all
          of its pods on the default 16 and the fleet ceiling was ~272 against the
          `max_connections=100` D-119 measured against. Then the computation itself counted
          *processes*, which is the same error one level in: a front-door process holds three pools
          and opens 48 connections where `1 × 16` said 16. Same self-disabling convention:
          undeclared means inert.
        - **A fleet dispatching more concurrent calculations than the backend will serve.** The
          third instance of the same shape, one subject over again: the activity cap bounds one
          worker process and `servers/calc` is a single shared pod, so scaling the `calc` worker
          multiplied CPU-bound load on it with nothing named for the product. Same self-disabling
          convention: undeclared means inert. The *durable* half only — the bundle's own server
          pods dispatch there from a tool call with no per-process cap, which no product can see
          and `chemclaw_calc_requests_in_flight` can.
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

        Every rule in this method **raises**, and that is what keeps the one rule that does not out
        of it: `_a_durable_deployment_is_told_its_envelopes_will_orphan` below warns instead, for
        the reason its own docstring measures. A method whose name promises refusal and whose body
        sometimes logs is a method a reader stops trusting.
        """
        # Only when the operator *set* it. At its default the trigger is clamped instead, because a
        # default is this repository's opinion and a budget is the deployment's: a small-context
        # site setting `CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET=20000` and nothing else would otherwise
        # fail to construct `Settings()` at all, citing a variable it never heard of. That made a
        # 30,000-token floor out of a field whose entire purpose is to sit *below* the budget.
        if "agent_tool_result_clear_trigger" not in self.model_fields_set:
            self.agent_tool_result_clear_trigger = min(
                self.agent_tool_result_clear_trigger, self.agent_context_token_budget
            )
        elif self.agent_tool_result_clear_trigger > self.agent_context_token_budget:
            raise ValueError(
                "agent_tool_result_clear_trigger must not exceed agent_context_token_budget: the "
                "lossless tool-result edit exists to run *before* the destructive conversation "
                "window, and setting it above the budget silently restores the single-threshold "
                "behaviour it was split off from — a misconfiguration that looks like it took "
                "effect (agent/compaction.py::context_compaction_middleware)."
            )
        unattached = not self.harness_enabled and self.harness_autonomy == "plan_only"
        if self.entra_required and unattached:
            raise ValueError(
                "harness_autonomy='plan_only' with harness_enabled=False enforces nothing: "
                "`plan_gate.gate_applies` is `harness_enabled and autonomy == 'plan_only'`, so "
                "the approval gate D-167/DARK-1 exists for is not attached at all and a turn can "
                "start state-changing work with nothing to approve it (report_measurement, which "
                "writes the calibration ledger, plus compute_xtb_energy, watch_for and "
                "remember_preference are allowed by both authorize_tool and authorize_trigger for "
                "an authenticated user holding no app role). Refused only under "
                "entra_required=true, because that is the deployment that believes it is in the "
                "enforced posture. Set CHEMCLAW_HARNESS_ENABLED=true (what the shipped chart "
                "does), or CHEMCLAW_HARNESS_AUTONOMY=execute to state that this deployment's "
                "turns are deliberately unsupervised."
            )
        temporal_insecure = not (
            self.temporal_tls_cert
            or self.temporal_tls_ca
            or self.temporal_api_key.get_secret_value()
        )
        temporal_host = self.temporal_address.rsplit(":", 1)[0].strip("[]").lower()
        temporal_loopback = temporal_host in {"localhost", "127.0.0.1", "::1", ""}
        if self.entra_required and temporal_insecure and not temporal_loopback:
            raise ValueError(
                "entra_required=true with a non-loopback temporal_address "
                f"({self.temporal_address!r}) and no temporal_tls_cert / temporal_tls_ca / "
                "temporal_api_key opens a plaintext, unauthenticated gRPC channel to the broker — "
                "and identity rides *inside* the workflow payload (ConnectorJobInput.requested_by, "
                "StepIdentity), so anyone who can reach the broker can start any workflow as any "
                "actor. mTLS is what restricts broker write access, which the template authorize "
                "path relies on (D-2026-08-28). Set temporal_tls_ca (+ cert/key) or "
                "temporal_api_key, or bind a loopback address for local dev. Refused only under "
                "entra_required=true, the deployment that believes it is in the enforced posture."
            )
        if self.entra_required:
            require_pg_tls(self.postgres_dsn, "postgres_dsn")
            if self.postgres_migration_dsn:
                require_pg_tls(self.postgres_migration_dsn, "postgres_migration_dsn")
            # The third one, and the one the refusal message above literally describes: the session
            # layer's own database holds `session_messages`, the LangGraph checkpoints, the plan
            # approvals, the turn-cost rows and the effect ledger. Empty is not "unchecked" — it
            # means "use `postgres_dsn`", which the first line already checked.
            if self.session_store_dsn:
                require_pg_tls(self.session_store_dsn, "session_store_dsn")
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
            # **Pools, not processes.** `pg_pool_max_size` bounds one pool and a process holds one
            # per distinct `(dsn, libpq options)` key plus any foreign pool it registers, so a
            # front door holds three (stores, `/readyz`'s own statement timeout, the checkpointer's
            # autocommit pool) and opens 3 × `pg_pool_max_size`. Multiplying by processes said
            # `1 × 16 = 16` for a process measured at 48.
            opened = self.pg_fleet_pools * self.pg_pool_max_size
            if opened > self.pg_fleet_max_connections:
                raise ValueError(
                    f"this deployment may open {opened} Postgres connections "
                    f"({self.pg_fleet_pools} pool(s) × "
                    f"{self.pg_pool_max_size} per pool) against a declared server ceiling of "
                    f"{self.pg_fleet_max_connections}. A process holds one pool per distinct DSN "
                    "and statement timeout, plus the checkpointer's — the front door holds three. "
                    "Lower pg_pool_max_size or the number of pooled processes, or raise "
                    "pg_fleet_max_connections if the server's max_connections can serve it."
                )
        if self.calc_backend_max_concurrent_requests:
            # Three factors, not two: a solvent screen fans out inside one activity under
            # `asyncio.Semaphore(calc_screen_max_parallel)` and each branch holds its own
            # `calc_session` for the whole of its chain, so an activity is not one backend session.
            # Measured over five solvents with the knob at 1/4/8: 1/4/6 concurrent sessions inside
            # a single activity. `connectors/calc/compose.py` has exactly two concurrency sites,
            # both bounded by this one knob and neither nested inside the other, so one extra
            # factor is the whole correction.
            dispatched = (
                self.calc_fleet_worker_processes
                * self.worker_max_concurrent_activities
                * self.calc_screen_max_parallel
            )
            if dispatched > self.calc_backend_max_concurrent_requests:
                raise ValueError(
                    f"this deployment may dispatch {dispatched} concurrent calculations "
                    f"({self.calc_fleet_worker_processes} calc worker process(es) × "
                    f"{self.worker_max_concurrent_activities} activities each × "
                    f"{self.calc_screen_max_parallel} media in flight per screen) against a "
                    f"calculation backend declaring "
                    f"{self.calc_backend_max_concurrent_requests}. That backend pins "
                    "OMP_NUM_THREADS=1 and is CPU-bound, so the surplus arrives as thrashing, then "
                    "as heartbeat timeouts, then as retries onto the same pod. Lower "
                    "worker_max_concurrent_activities, calc_screen_max_parallel or the calc worker "
                    "replica count, or raise calc_backend_max_concurrent_requests if that server "
                    "can serve it."
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
        writes_note_index = self.note_reindex_effective or bool(
            NOTE_INDEX_SOURCES & set(self.data_source_list)
        )
        # Inert wherever the note vectors do not live in that column, exactly as
        # `require_schema_vector_width()` is for the document one: an external store's deployment
        # may legitimately run a 768-wide model, and refusing it over a column nothing writes would
        # be this check inventing a constraint instead of reporting one.
        pgvector_notes = self.vector_store_provider == "pgvector"
        if writes_note_index and pgvector_notes and self.embedding_dim != SCHEMA_VECTOR_DIM:
            raise ValueError(
                f"embedding_dim={self.embedding_dim} disagrees with the note_index vector column "
                f"({SCHEMA_VECTOR_DIM}, infra/sql/012_note_index.sql); pgvector would reject "
                "every write. Change both together, drop 'vector' from data_sources, or move the "
                "vectors out of Postgres with vector_store_provider."
            )
        return self

    @model_validator(mode="after")
    def _a_durable_deployment_is_told_its_envelopes_will_orphan(self) -> Self:
        """`session_store="postgres"` with no `framing_envelope_secret`: warned about, not refused.

        The condition. `agent/framing.py::_envelope_nonce` falls back to `secrets.token_hex(8)`
        per *process* when the secret is empty, and the agent instructions say only an envelope
        carrying **exactly** the current tag marks retrieved content as data. A durable session
        outlives a process, so a replayed thread carries envelopes written under a previous
        process's nonce: they no longer match, and the ELN text, note bodies and uploaded
        attachments inside them are presented to the model as ordinary prose. The prompt-injection
        marking switches itself off for precisely the oldest material it exists to cover
        (`D-2026-08-06-an-envelope-that-only-survives-its-own-process`, which shipped the setting
        and left the pairing unchecked).

        **Why this one warns where every rule in `_guards_that_the_comments_already_demand`
        raises**, which is the decision rather than an omission
        (`D-2026-08-27-a-warning-is-the-shape-a-guard-takes-when-raising-would-break-a-deployment`):

        - What a raise would cost is measured, not estimated: `deploy/helm/chemclaw/values.yaml`
          ships `CHEMCLAW_SESSION_STORE: "postgres"` and lists `framingEnvelopeSecret` under
          `secrets.optionalKeys`, so the flagged pairing **is the shipped default release**. A
          `ValueError` here is not "some deployments" — it is every pod of every existing release,
          front door and workers alike, failing to construct `Settings()` on `helm upgrade`, over a
          condition the operator did not change and this repository documented as the default.
          Scoping the raise to `entra_required` — the escape the harness guard above takes — buys
          nothing, because the same file sets `CHEMCLAW_ENTRA_REQUIRED: "true"`.
        - What the lapse costs is real and bounded. It is a *marking*, not a gate: no verdict is
          computed from an envelope's presence, so nothing here is a degraded check clearing the
          gate it guards (`D-2026-08-08-a-degraded-check-must-not-clear-the-gate`). The defang half
          of the mechanism — the half that closes forgery, and the half D-2026-08-06 measured a
          real bypass in — runs at framing time and is untouched by a rotated nonce. Every tool
          call an injected instruction could reach still passes `authorize_tool`, the plan gate and
          the audit trail, so a payload that lands cannot exceed the caller's own entitlements.
        - Failing the fleet to start is itself the larger harm, and the surprising one: a
          deployment that answers nothing is worse than one whose oldest retrieved content is read
          unmarked. That is the same trade D-2026-08-08 took for `usage_tokens` — instrument the
          defect, decline to make an upgrade a full outage.

        The one thing this cannot buy is a fix. Persisting the nonce beside the session is the root
        cause's answer and a schema decision; the two cheap alternatives (a fixed public tag,
        deriving from another credential) were both rejected by D-2026-08-06 and stay rejected.

        Emitted with the standard library's logger rather than `core/logging.py::log_event`,
        matching `core/logging.py::_warn_about_sensitive_data`, which announces what a
        configuration now means the same way. Not a preference: `core/logging.py` does `from
        chemclaw.core.config import settings` at import, so importing it from here — during the
        `Settings()` at the bottom of this module, before that name is bound — is a circular
        import. That also fixes *when* the line lands: at import, ahead of `configure_logging()`,
        so it reaches stderr through `logging.lastResort` and not through `JsonFormatter`. An
        unconfigured process still prints it, which is the half that decides whether a deployment
        sees anything, and `tests/test_config.py` pins it with a subprocess rather than trusting
        it.
        """
        if self.session_store == "postgres" and not self.framing_envelope_secret.get_secret_value():
            logging.getLogger(__name__).warning(
                "CHEMCLAW_FRAMING_ENVELOPE_SECRET is unset while CHEMCLAW_SESSION_STORE=postgres: "
                "the prompt-injection envelope tag (agent/framing.py) falls back to a random "
                "per-process nonce, so every restart and every extra replica frames retrieved "
                "content under a tag the next process does not recognise. The agent instructions "
                "say only an envelope carrying exactly the current tag marks retrieved content as "
                "data, so a replayed thread's older ELN text, note bodies and uploaded "
                "attachments reach the model as ordinary prose and the injection marking lapses "
                "for the oldest content it exists to cover. Set CHEMCLAW_FRAMING_ENVELOPE_SECRET "
                "to a stable per-deployment value (Helm: "
                "secrets.optionalKeys.framingEnvelopeSecret). Warned rather than refused because "
                "the shipped chart is this configuration, so refusing would fail every existing "
                "release on upgrade "
                "(D-2026-08-27-a-warning-is-the-shape-a-guard-takes-when-raising-would-break-a-"
                "deployment)."
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
        """The same rule again, on the template run and the longest step it has to contain.

        `templates/registry.py` starts `TemplateWorkflow` with `template_run_timeout_seconds` as an
        execution timeout. A run ceiling at or below the longest step's budget kills the procedure
        inside that step — with a bare `WorkflowExecutionTimedOut` naming neither setting, and with
        the per-step timeout that was *meant* to fire made unreachable, so `agent_step_retry`'s
        attempts become a number that can never be spent.

        **A step's budget is not one number, and this rule read the wrong one for the kind of step
        that costs the most.** `template_step_timeout_seconds` (900 s) is the `start_to_close` of an
        `agent` or a `tool` step, both of which are activities. A `job` step is not an activity: it
        starts `ConnectorJobWorkflow` as a child under `wrapper_execution_timeout()`
        (`durable/template_job.py`), which is `connector_job_timeout_seconds` plus the wrapper's
        four post-child steps — 18,120 s against a run ceiling of 7,200 s when this was measured.
        So a CREST search well inside its own budget ended the whole run as a silent `TIMED_OUT`:
        an execution timeout is not delivered to workflow code, so `TemplateWorkflow`'s `except
        BaseException -> _notify_failure` never ran, the chemist was told nothing on the session
        stream, and the connector child was terminated with its parent before it could write its
        own `job_records` failure row. Seven of the nine shipped templates have a `job` step.

        Only `connector_job_timeout_seconds` can move to close that: its own floor is the CREST
        search plus the activity's overhead (`_the_job_ceiling_covers_the_activity_it_bounds`), so
        raising the run ceiling is the one direction available.

        Strictly greater rather than at least, because equality is the defect. Only one step is
        required rather than N: how many steps a template has is a property of a YAML file this
        object cannot see, so the honest machine-checkable floor is "a single step fits", and the
        setting's own comment carries the sizing advice for a longer procedure.
        """
        # The max over the budgets a *step* can carry, the shape
        # `_the_job_ceiling_covers_the_activity_it_bounds` already uses one level down: naming one
        # step kind is how this rule came to be checking 900 s against an 18,120 s bound. A new
        # step kind with its own ceiling gets covered by being added here.
        job_step = (
            self.connector_job_timeout_seconds
            + self.activity_timeout_seconds * _WRAPPER_FINISH_STEPS
        )
        longest, budget = max(
            (
                (self.template_step_timeout_seconds, "template_step_timeout_seconds"),
                (
                    job_step,
                    "connector_job_timeout_seconds + activity_timeout_seconds x "
                    f"{_WRAPPER_FINISH_STEPS}, the ceiling a `job` step carries",
                ),
            )
        )
        if self.template_run_timeout_seconds <= longest:
            raise ValueError(
                f"template_run_timeout_seconds={self.template_run_timeout_seconds} does not cover "
                f"the step it bounds: one step may take {longest}s ({budget}), so the run would "
                "time out inside that step — as a bare TIMED_OUT that reaches neither the chemist "
                "nor the job record, because a workflow execution timeout is not delivered to "
                f"workflow code. Raise the run ceiling above {longest}, or lower that budget."
            )
        return self

    @model_validator(mode="after")
    def _the_job_ceiling_covers_the_activity_it_bounds(self) -> Self:
        """A parent ceiling no larger than its child's longest activity is not a ceiling.

        `ConnectorJobWorkflow` gives its child `connector_job_timeout_seconds` as an **execution**
        timeout (`durable/connector_job.py`), and the longest thing inside any child is
        `run_xtb_calculation` — a CREST conformer search budgeted by `xtb_job_timeout_seconds`
        (`connectors/calc/workflows.py`). Set the ceiling at or below that and two things break,
        neither of which says so: the activity's `BAD_DATA_RETRY` is dead, because a single attempt
        already exhausts the parent's whole budget, so `activity_max_attempts` is a number that can
        never be reached; and an operator who raises `xtb_job_timeout_seconds` for a large molecule
        observes no change whatsoever and gets a bare `WorkflowExecutionTimedOut` naming neither
        setting.

        This is the same rule that used to be written against the DFT poll's 24 h budget. The tier
        it guarded is gone (`D-2026-08-26-semiempirical-is-the-whole-tier`) and the rule is not: the
        ceiling still has to cover the longest activity under it, and that activity is now a CREST
        search. The allowance is one short activity's worth of the child's own overhead, and it is
        strictly greater rather than at least, because equality is the defect.

        Scoped to `Settings` and not to either section because it is one of the rules here that
        spans two: the ceiling is a connector-wide deployment choice and the search budget is the
        calculators' own, and neither section can see the other.
        """
        # The **max over every activity budget a bundle child can spend**, not the one activity
        # this rule was first written against. Naming `xtb_job_timeout_seconds` alone is how the
        # `results` bundle's republish walk got past it: that walk scans two never-pruned tables
        # and was handed `connector_job_timeout_seconds` itself, which the ceiling equals rather
        # than exceeds. A new long activity gets covered by being added here.
        longest, budget = max(
            (
                (self.xtb_job_timeout_seconds, "xtb_job_timeout_seconds"),
                (self.result_republish_timeout_seconds, "result_republish_timeout_seconds"),
            )
        )
        needed = longest + self.activity_timeout_seconds
        if self.connector_job_timeout_seconds <= needed:
            raise ValueError(
                f"connector_job_timeout_seconds={self.connector_job_timeout_seconds} does not "
                f"cover the job it bounds: one attempt at the longest activity may "
                f"take {longest}s ({budget}) and the child's "
                f"own overhead up to {self.activity_timeout_seconds}s more "
                f"(activity_timeout_seconds). Raise connector_job_timeout_seconds above {needed}, "
                f"or lower {budget} — raising the activity budget alone changes "
                "nothing, because the parent's ceiling fires first."
            )
        return self

    @model_validator(mode="after")
    def _the_heartbeat_fits_inside_the_budget_it_reports_within(self) -> Self:
        """A heartbeat timeout outside the budget it sits under is a control that does nothing.

        `background_activity_heartbeat_timeout_seconds` is the heartbeat timeout for core's long
        background activities — the note reindex, the retention sweep, the result-publish drain and
        the artifact eviction sweep, which said "three" here until eviction became the fourth — and
        its own comment asserts as fact that it sits "far below every start-to-close budget it sits
        under". The `min()` below is over the budgets rather than over a count, so the guard stayed
        correct while the sentence went stale; eviction is budgeted by `retention_timeout_seconds`,
        which is already in it. Nothing checked that, and both directions of getting it wrong are
        silent:

        - **Above the budget it guards.** `CHEMCLAW_RESULT_PUBLISH_TIMEOUT_SECONDS=30` with one
          configured sink gives the drain a 30 s start-to-close under a 60 s heartbeat timeout, so
          the heartbeat can never fire first and the failure detection it exists for is inert.
        - **Large enough that the beat outlives the budget.** `durable/heartbeat.py::beating`
          derives its interval as `timeout / 4`, so a 3600 s heartbeat timeout beats every 900 s —
          longer than the 600 s `retention_timeout_seconds` — and the activity therefore sends *no*
          beat at all before its start-to-close expires. The sweep fails on a timeout that has
          nothing to do with the sweep.

        One rule covers both, because both are the same inequality: the heartbeat timeout must be
        strictly below the shortest budget it sits under. `result_publish_timeout_seconds` is taken
        alone rather than times the sink count, since one sink is the smallest that budget can be.
        """
        shortest, budget = min(
            (
                (self.retention_timeout_seconds, "retention_timeout_seconds"),
                (self.note_reindex_timeout_seconds, "note_reindex_timeout_seconds"),
                (self.result_publish_timeout_seconds, "result_publish_timeout_seconds"),
            )
        )
        if self.background_activity_heartbeat_timeout_seconds >= shortest:
            raise ValueError(
                "background_activity_heartbeat_timeout_seconds="
                f"{self.background_activity_heartbeat_timeout_seconds} does not fit inside the "
                f"budget it reports within: the shortest is {shortest}s ({budget}). At or above "
                "it the heartbeat can never fire first, so the dead-worker detection it exists "
                f"for is inert. Lower it below {shortest}, or raise {budget}."
            )
        return self

    @model_validator(mode="after")
    def _the_activity_budget_covers_the_search_it_awaits(self) -> Self:
        """The same rule one level down: the activity must outlive its longest single client call.

        Inside the xTB activity the longest await is the sampler's —
        `calc_sampling_timeout_seconds`, deliberately matched to the server's own CREST ceiling so
        the *server* bounds a search. The two budgets shipped equal (14400 s each), and equality is
        the defect here exactly as it is for the parent above: a search that ran to its client
        bound exhausted the activity's `start_to_close` at the same instant, so Temporal killed the
        activity as a bare timeout instead of letting the client's own timeout surface as an error,
        and `activity_max_attempts` was a number that could never be reached. The margin is the
        activity's other work around the search — key probe, embed, cache write — bounded by
        `activity_timeout_seconds`, mirroring the parent ceiling's allowance.
        """
        needed = self.calc_sampling_timeout_seconds + self.activity_timeout_seconds
        if self.xtb_job_timeout_seconds <= needed:
            raise ValueError(
                f"xtb_job_timeout_seconds={self.xtb_job_timeout_seconds} does not cover the "
                f"search it awaits: one sampling call may take "
                f"{self.calc_sampling_timeout_seconds}s (calc_sampling_timeout_seconds) and the "
                f"activity's own overhead up to {self.activity_timeout_seconds}s more "
                f"(activity_timeout_seconds). Raise xtb_job_timeout_seconds above {needed}, or "
                "lower calc_sampling_timeout_seconds together with the server's own CREST "
                "ceiling — shortening the client bound alone only abandons work the server "
                "finishes anyway."
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

# The in-process egress guard, armed beside the LangSmith pin and for the same reason: this module
# is the one import every entrypoint makes, so arming here makes the guard a property of the system
# rather than of a launcher. The allowlist is derived from the destinations this deployment dials
# (the LLM gateway, Postgres, Temporal, the connector endpoints, the IdP), so a host outside it —
# a dependency fetching model weights, a usage ping, a DNS licence check — is refused. It is defence
# in depth behind the NetworkPolicy for the "only LLM traffic leaves" invariant and cannot cover a
# child process or a compiled extension's own syscalls; `chemclaw.core.netguard` documents both.
arm_egress_guard(settings)
