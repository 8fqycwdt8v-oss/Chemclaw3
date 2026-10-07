"""Single, typed source of every environment-dependent value in Chemclaw.

Every URL, DSN, queue name and timeout is declared once, type-checked, and overridable by
environment variable or a local `.env`; `infra/docker-compose.yml` uses the same names. Only
fields with a real consumer live here.

Usage:
    from chemclaw.core.config import settings
    client_target = settings.temporal_address

`Settings` is composed from one mixin per domain, each in its own module, so a concern's fields,
validators and derived properties are in one file while attributes stay flat
(`settings.postgres_dsn`) and env names unprefixed by section (`CHEMCLAW_POSTGRES_DSN`). Only
rules spanning sections live on the composed class here.

Collection fields: a delimited string (like `PATH`) when the elements are bare keys or paths, read
through a derived `*_list`/`*_dirs` property; a plain mapping (`connector_urls`) for a per-name
override of one scalar. Config says which and where; a manifest says what. A field describing the
internals of one attached thing belongs in its manifest (D-118).
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
from chemclaw.core.config.hypotheses import HypothesisSettings
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
from chemclaw.core.netguard_preload import publish_state as publish_preload_state

# Explicit, because `mypy --strict` disables implicit re-export of imported names.
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
# `""` is here for callers that build a URL and ask whether its host is local (a path with no host
# reads as local). `require_pg_tls` deliberately does not use it: in a Postgres DSN an empty host
# means libpq resolves one from `PGHOST` or a `service=` file, which the parse cannot see.
PG_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _pg_dial(dsn: str, name: str) -> tuple[str, str]:
    """The host libpq will dial and the sslmode it will use, read with libpq's own parser.

    libpq's parser, because a hand-rolled one disagrees in the plaintext direction: libpq connects
    to
    `hostaddr` when set (so `hostaddr or host` is what a TLS decision is about), and resolves a
    repeated parameter to its last occurrence. An unparseable DSN raises, naming the setting and not
    the DSN (it carries a password), since any "could not tell" answer would read as loopback.
    """
    # Imported lazily: `ingest/sources/registry` reaches `core/config`, and asking which sources to
    # ingest must import no third-party driver (`tests/test_datasource_isolation.py`).
    from psycopg import ProgrammingError, conninfo

    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except ProgrammingError as exc:
        # Not interpolated: libpq quotes the offending token, which can be the whole DSN with its
        # password,
        # and this runs before the log redaction filter exists. The type name distinguishes the
        # failure;
        # `__cause__` keeps the original for a debugger.
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

    A `host` starting with `/` (socket directory) or `@` (abstract socket) has no network to encrypt
    and libpq ignores `sslmode` there, so a TLS guard has nothing to require. Every element of a
    comma-separated host list must be a socket, since libpq tries them in order.
    """
    return all(part.strip().startswith(("/", "@")) for part in host.split(","))


def pg_endpoint(dsn: str) -> tuple[str, str] | None:
    """The `(host, port)` libpq will dial, or `None` when the string is not parseable.

    `hostaddr or host`, as in `_pg_dial`. `None` lets the caller take the strict branch (treat two
    incomparable DSNs as one server).

    A string comparison, so one server spelled two ways reads as two (`tests/test_config.py` pins
    this). Normalising would reimplement libpq's precedence rules, and asking the server is
    impossible
    at import time with no event loop. The runtime half, `core/db.same_server`, reads
    `system_identifier` off a live connection and may collapse such a split back to one server.

    Imported lazily for the reason `_pg_dial` gives.
    """
    from psycopg import ProgrammingError, conninfo

    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except ProgrammingError:
        return None
    return str(parts.get("hostaddr") or parts.get("host") or ""), str(parts.get("port") or "")


def require_pg_tls(dsn: str, name: str) -> None:
    """Refuse a non-loopback Postgres DSN whose sslmode leaves plaintext or an unverified peer.

    libpq's default `prefer` silently falls back to cleartext and verifies no certificate, and
    transcripts, checkpoints and the audit trail cross this connection. So a non-loopback DSN must
    state `sslmode=require`/`verify-ca`/`verify-full` (`verify-full` recommended, with
    `sslrootcert=`). Loopback dev and a Unix socket are exempt. An empty host is not loopback here
    (libpq may resolve it from `PGHOST` or a `service=` file); name the host or state the sslmode.
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


# One row per iteration-bounded Temporal Schedule: the setting bounding its iterations, the setting
# budgeting one activity, and activities dispatched per iteration.
# `_a_bounded_run_fits_the_ceiling_that_kills_it` multiplies them out; the third column is checked
# against each workflow's loop by `tests/test_config.py`.
_BOUNDED_DRAINS: tuple[tuple[str, str, int], ...] = (
    ("corpus_sync_max_iterations", "corpus_sync_timeout_seconds", 1),
    ("document_sync_max_iterations", "document_sync_timeout_seconds", 3),
    ("label_sync_max_iterations", "label_sync_timeout_seconds", 1),
    ("eln_sync_max_iterations", "eln_sync_timeout_seconds", 3),
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
    HypothesisSettings,
    DeliverySettings,
    PublishSettings,
):
    """Environment configuration, loaded from process env then a local `.env`.

    Field names map to `CHEMCLAW_<FIELD>` environment variables (e.g. `CHEMCLAW_TEMPORAL_ADDRESS`).
    Defaults target the local `docker-compose` stack, so a fresh checkout runs with no `.env`.
    Composed from the per-domain section mixins; this `model_config` (prefix, `.env`,
    `extra="forbid"`) governs them all.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    @property
    def note_reindex_effective(self) -> bool:
        """Whether the note reindex schedule runs: derived from the source list unless overridden.

        On the composed class because the flag and the source list live in different mixins.
        """
        if self.note_reindex_enabled is not None:
            return self.note_reindex_enabled
        return bool(NOTE_INDEX_SOURCES & set(self.data_source_list))

    @property
    def longest_bundle_activity(self) -> tuple[float, str]:
        """The longest activity budget a connector bundle's child can spend, and its setting name.

        One definition for two readers that bound each other:
        `_the_job_ceiling_covers_the_activity_it_bounds` and
        `durable/publish.py::connector_queue_wait_timeout`, which derives a queued job's wait
        headroom from
        it. On the composed class because the max spans `CalculatorSettings` and `PublishSettings`.

        Returns:
            The largest activity budget in seconds and the name of the setting that carries it.
        """
        longest: tuple[float, str] = max(
            (
                (self.xtb_job_timeout_seconds, "xtb_job_timeout_seconds"),
                (self.result_republish_timeout_seconds, "result_republish_timeout_seconds"),
            )
        )
        return longest

    @property
    def longest_fan_out_activity(self) -> tuple[float, str]:
        """The longest activity budget a fan-out child can spend, and its setting name.

        The fan-out twin of `longest_bundle_activity`, read by
        `_the_fan_out_ceiling_covers_the_section_it_bounds` and
        `durable/publish.py::fan_out_queue_wait_timeout`.

        Returns:
            The largest activity budget in seconds and the name of the setting that carries it.
        """
        longest: tuple[float, str] = max(
            (
                (self.report_section_timeout_seconds, "report_section_timeout_seconds"),
                (self.note_write_timeout_seconds, "note_write_timeout_seconds"),
            )
        )
        return longest

    def _fleet_pool_widths(self, *, at_rollout_peak: bool = False) -> tuple[int, int]:
        """`(one-connection pools, pg_pool_max_size pools)` on `postgres_dsn`'s server.

        The decomposition the refusal prints, derived through the same branches as
        `fleet_connections_per_server` so the two always add up. Under a split, the narrow readiness
        pools
        move to the session store, so `narrow` is zero. `at_rollout_peak` matches the total being
        quoted.
        """
        primary, session = self.fleet_connections_per_server(at_rollout_peak=at_rollout_peak)
        pools, replicas = self.pg_fleet_pools, self.service_fleet_replicas
        if at_rollout_peak and self.pg_fleet_pools_at_rollout_peak:
            pools = self.pg_fleet_pools_at_rollout_peak
            replicas = self.service_fleet_replicas_at_rollout_peak or self.service_fleet_replicas
        if session:
            return 0, primary // self.pg_pool_max_size
        narrow = replicas if pools >= 3 * replicas else 0
        return narrow, pools - narrow

    def fleet_connections_per_server(self, *, at_rollout_peak: bool = False) -> tuple[int, int]:
        """`(connections on postgres_dsn's server, connections on the split session store's)`.

        The one place fleet Postgres spend is added up, as a sum: a front door's `/readyz` pool is
        one
        connection wide, so the fleet holds one narrow pool per front-door replica and
        `pg_pool_max_size` for every other pool. With a split `session_store_dsn` each pooled
        process opens
        one more pool and the readiness and checkpointer pools move to the session store; two DSNs
        naming
        one endpoint (or an unparseable one) are summed onto the primary. Both figures are ceilings;
        `tests/test_fleet_pools.py` pins the per-role pool counts.

        Args:
            at_rollout_peak: Charge both generations of every surging Deployment, as an upgrade
                does, instead of the steady state. Uses the chart-rendered peak pool and replica
                counts
                together; undeclared, they fall back to the steady pair.
        """
        # Narrow pools are one per front door, but only if the declared total can contain them
        # (three
        # pools per front door). Otherwise the pair was set by hand and every pool is charged full
        # width,
        # the conservative direction.
        pools, replicas = self.pg_fleet_pools, self.service_fleet_replicas
        if at_rollout_peak and self.pg_fleet_pools_at_rollout_peak:
            pools = self.pg_fleet_pools_at_rollout_peak
            # Both or neither: a half-declared peak pair falls back to the steady replica count.
            replicas = self.service_fleet_replicas_at_rollout_peak or self.service_fleet_replicas
        consistent = pools >= 3 * replicas
        readiness = replicas if consistent else 0
        full = pools - readiness
        one_dsn = full * self.pg_pool_max_size + readiness
        if not self.session_store_dsn or self.session_store_dsn == self.postgres_dsn:
            return one_dsn, 0
        primary = (pools - 2 * readiness) * self.pg_pool_max_size
        here, there = pg_endpoint(self.postgres_dsn), pg_endpoint(self.session_store_dsn)
        if here is None or there is None or here == there:
            return primary + one_dsn, 0
        return primary, one_dsn

    @model_validator(mode="after")
    def _guards_that_the_comments_already_demand(self) -> Self:
        """The combinations whose field comments forbid them, enforced at startup.

        - **A tool-result clear trigger above the conversation budget**: the lossless edit must fire
          before the window.
        - **A stated autonomy nothing enforces**: `harness_autonomy="plan_only"` with the harness
          off,
          refused only under `entra_required`; the opt-out is `harness_autonomy=execute`.
        - **`service_uvicorn_workers > 1`**, which breaks per-process guarantees (rate limiter,
          budget
          tracker, attachment store, session LRU, metrics scrape). Scale with replicas instead.
        - **A fleet admitting more concurrent turns than its declared ceiling** (replicas x workers
          x
          per-process cap).
        - **A fleet opening more Postgres connections than the server serves**, counted by pools,
          not
          processes.
        - **A fleet dispatching more concurrent calculations than the backend serves** (durable half
          only;
          `chemclaw_calc_requests_in_flight` covers the tool path).
        - **A mid-turn resume wait longer than the turn deadline.**
        - **Budgets enabled with every cap at zero** (zero means unlimited).
        - **`embedding_dim` disagreeing with the `vector(N)` column** while anything writes the note
          index, which pgvector would otherwise reject at reindex time.

        Fleet checks are inert until the operator declares the corresponding ceiling. Every rule
        here
        raises; guards that must only warn live in their own validators.
        """
        # Only when the operator set it; at its default the trigger is clamped instead, so a
        # small-context
        # deployment setting only the budget still starts.
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
            # The session layer's own database (transcripts, checkpoints, approvals, cost rows, the
            # effect
            # ledger). Empty means "use `postgres_dsn`", already checked above.
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
        # A waiting message's poll also refreshes its lease (`agent/session_queue`), so a poll at or
        # above
        # the lease lapses every ticket between two asks and every queued message ends as withdrawn.
        if self.service_turn_queue_poll_seconds >= self.service_turn_claim_lease_seconds:
            raise ValueError(
                f"service_turn_queue_poll_seconds ({self.service_turn_queue_poll_seconds:g}) must "
                "be below service_turn_claim_lease_seconds "
                f"({self.service_turn_claim_lease_seconds:g}): a waiting message refreshes its "
                "place in line each time it asks, so at or above the lease every place lapses "
                "between asks and every queued message reads as withdrawn."
            )
        # The same shape for relayed requests to a running turn (`agent/turn_remotes`).
        if self.service_turn_relay_poll_seconds >= self.service_turn_relay_lease_seconds:
            raise ValueError(
                f"service_turn_relay_poll_seconds ({self.service_turn_relay_poll_seconds:g}) must "
                "be below service_turn_relay_lease_seconds "
                f"({self.service_turn_relay_lease_seconds:g}): a request to a turn on another "
                "replica is refreshed each poll, so at or above the lease it lapses between polls."
            )
        # A per-actor fairness cap at or above the cap it divides refuses nothing while reading as
        # protection. Checked here because only this sees the configuration a pod actually runs (env
        # overrides escape the chart test). Zero is the documented off switch.
        if (
            self.service_max_concurrent_turns_per_actor
            and self.service_max_concurrent_turns_per_actor >= self.service_max_concurrent_turns
        ):
            raise ValueError(
                f"service_max_concurrent_turns_per_actor is "
                f"{self.service_max_concurrent_turns_per_actor} against a per-process "
                f"admission cap of {self.service_max_concurrent_turns}, so one actor may "
                "hold every permit and "
                "the guard refuses nothing. Set it strictly below "
                "service_max_concurrent_turns, or to 0 to disable it deliberately."
            )
        # The socket backstop must exceed what this process's own caps can occupy.
        # `--limit-concurrency`
        # (`deploy/entrypoint.sh`) counts every open socket and answers 503 above the app, probes
        # included,
        # so reaching it gets a busy pod killed by the kubelet. Turns and streams are added, not
        # maxed: a
        # turn holds its sender's stream plus up to `service_turn_max_watchers` followers, and
        # waiters are
        # charged per process (`service_max_concurrent_turns` x `service_turn_queue_max`), the bound
        # the
        # turn route enforces.
        per_turn = 1 + self.service_turn_max_watchers
        waiters = self.service_max_concurrent_turns * self.service_turn_queue_max
        occupied = (
            self.service_max_event_streams_total
            + self.service_max_concurrent_turns * per_turn
            + waiters
        )
        if self.service_max_connections < occupied + self.service_connection_headroom:
            raise ValueError(
                f"service_max_connections ({self.service_max_connections}) is below what this "
                f"process's own caps can occupy plus its headroom: "
                f"{self.service_max_event_streams_total} push-back streams "
                f"(service_max_event_streams_total) + {self.service_max_concurrent_turns} turns "
                f"(service_max_concurrent_turns) x {per_turn} sockets each (the sender and "
                f"service_turn_max_watchers) + {waiters} waiting messages "
                "(service_max_concurrent_turns x service_turn_queue_max) + "
                f"{self.service_connection_headroom} reserved "
                f"(service_connection_headroom) = "
                f"{occupied + self.service_connection_headroom}. uvicorn's --limit-concurrency "
                "counts sockets (idle keep-alives included) and answers 503 above the ASGI app, so "
                "at the limit /healthz answers 503 too and the kubelet restarts a pod that is "
                "merely busy — killing every turn in flight on it. Raise "
                "service_max_connections, or lower the stream/turn caps it has to cover."
            )
        # The rollout peak: an upgrade runs both generations, so that is when the fleet holds the
        # most
        # connections. Undeclared, this is the steady figure.
        primary_connections, session_connections = self.fleet_connections_per_server(
            at_rollout_peak=True
        )
        if self.pg_session_fleet_max_connections and not session_connections:
            # Refused: a session-server ceiling with no split session store has no referent.
            # Declaring a ceiling
            # can only add a firing condition to the runtime alert.
            raise ValueError(
                "pg_session_fleet_max_connections declares a ceiling for a split session store "
                "and there is none: session_store_dsn is unset, equal to postgres_dsn, or names "
                "an endpoint that cannot be told apart from postgres_dsn's, so every pool lands on "
                "one server and pg_fleet_max_connections is the only ceiling that means anything. "
                "Unset it, or point session_store_dsn at the second server it is describing — "
                "spelled differently from postgres_dsn, since the two are compared as strings."
            )
        if self.pg_fleet_max_connections and primary_connections > self.pg_fleet_max_connections:
            # Counted by pools, not processes: a front door holds three (stores, `/readyz`'s, the
            # checkpointer's), and the `/readyz` one is a single connection. The breakdown is
            # printed from the
            # same terms as the total, so the operator's arithmetic reaches the refused number.
            narrow, wide = self._fleet_pool_widths(at_rollout_peak=True)
            raise ValueError(
                f"this deployment may open {primary_connections} Postgres connections on "
                f"{'postgres_dsn' if session_connections else 'its Postgres server'} "
                f"({wide} pool(s) of {self.pg_pool_max_size} plus {narrow} of one) against a "
                f"declared ceiling of {self.pg_fleet_max_connections}. A process holds one pool "
                "per distinct DSN and statement timeout, plus the checkpointer's — the front "
                "door holds three, one of them the readiness probe's single connection. Lower "
                "pg_pool_max_size or the number of pooled processes, or raise "
                "pg_fleet_max_connections if the server's max_connections can serve it."
            )
        # Its own `if`: `postgres.maxConnections: 0` (no primary ceiling) must not silence a
        # declared session
        # ceiling.
        if session_connections > self.pg_session_fleet_max_connections > 0:
            raise ValueError(
                f"this deployment may open {session_connections} Postgres connections on the "
                f"session store's own server against a declared ceiling of "
                f"{self.pg_session_fleet_max_connections}. session_store_dsn splits the session "
                "layer onto a second server, and a front door puts three of its four pools there "
                "— the stores' session pool, /readyz's and the checkpointer's. Lower "
                "pg_pool_max_size or the replica count, or raise "
                "pg_session_fleet_max_connections if that server can serve it."
            )
        if self.calc_backend_max_concurrent_requests:
            # Three factors: a solvent screen fans out inside one activity under
            # `calc_screen_max_parallel`, each branch holding its own `calc_session`. Those are the
            # only
            # concurrency sites in `connectors/calc/compose.py`, and they do not nest.
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
        # Each review round adds a model call and a judge call to a turn bounded by
        # `service_turn_timeout_seconds`. Only the judge half has a declared timeout, so refuse when
        # the
        # judge calls alone could fill the deadline.
        if self.answer_review_max_rounds and self.verifier_enabled:
            judging = (self.answer_review_max_rounds + 1) * self.verifier_timeout_seconds
            if judging >= self.service_turn_timeout_seconds:
                raise ValueError(
                    f"answer_review_max_rounds={self.answer_review_max_rounds} grades the answer "
                    f"{self.answer_review_max_rounds + 1} times at "
                    f"verifier_timeout_seconds={self.verifier_timeout_seconds}s, which is "
                    f"{judging}s of judging alone against a "
                    f"service_turn_timeout_seconds of {self.service_turn_timeout_seconds}s — "
                    "before a single revision's model call. Lower answer_review_max_rounds or "
                    "verifier_timeout_seconds, or raise service_turn_timeout_seconds"
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
        # Inert where note vectors do not live in that column: an external store may run any width.
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

        Without the secret, `agent/framing.py::_envelope_nonce` is random per process, so a durable
        session replayed in a later process carries envelopes whose tag no longer matches and its
        retrieved content is no longer marked as data.

        Warned, not raised, because the shipped chart is this configuration and raising would fail
        every pod on upgrade. The loss is bounded: defanging is unaffected, and any tool call an
        injected instruction reaches still passes authorization, the plan gate and audit.

        Uses the standard logger because `core/logging.py` imports this module; at import the line
        goes to stderr via `logging.lastResort` (`tests/test_config.py` pins it).
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
    def _a_split_session_store_is_told_its_second_server_is_unbounded(self) -> Self:
        """A `session_store_dsn` on another server, with no ceiling declared for that server.

        With a split, a large share of the fleet's connections land on the session server, which
        `pg_fleet_max_connections` does not cover, and only the operator knows what it will serve.
        Warned
        rather than refused because the setting is new and an existing split deployment would
        otherwise
        fail on upgrade. The primary's check still raises.
        """
        _, session_connections = self.fleet_connections_per_server()
        if session_connections and not self.pg_session_fleet_max_connections:
            logging.getLogger(__name__).warning(
                "CHEMCLAW_SESSION_STORE_DSN names a different (host, port) from "
                "CHEMCLAW_POSTGRES_DSN and CHEMCLAW_PG_SESSION_FLEET_MAX_CONNECTIONS is "
                "undeclared: %d connection(s) are charged there and nothing checks them. The "
                "split also adds one pool to every pooled process — a front door holds four, not "
                "three — which CHEMCLAW_PG_FLEET_POOLS cannot show, because the DSN arrives in a "
                "Secret the chart never reads. **First check that these really are two servers**: "
                "this compares the two DSN strings, so one box spelled two ways (a short name "
                "against its FQDN, an omitted port, localhost against 127.0.0.1) reads as two "
                "here — and declaring a ceiling for the second one would then hide the real "
                "total from the startup check *and* from "
                "ChemclawFleetAboveItsConnectionCeiling. If it is one server, spell both DSNs the "
                "same way and be checked once. If it is genuinely two, declare the second "
                "server's ceiling (Helm: postgres.sessionStoreMaxConnections).",
                session_connections,
            )
        return self

    @model_validator(mode="after")
    def _the_fan_out_ceiling_covers_the_section_it_bounds(self) -> Self:
        """The same rule as below, on the other parent/child pair that has a ceiling.

        A fan-out child's longest work is one report section (`report_section_timeout_seconds`), and
        its
        queue wait precedes it, so the ceiling must contain wait plus work. The wait is derived to
        fit
        (`durable/publish.py::fan_out_queue_wait_timeout` subtracts this rule's floor from the
        ceiling);
        this rule keeps it strictly positive, reserving one activity's overhead. Strictly greater,
        because
        equality is the defect.
        """
        longest, budget = self.longest_fan_out_activity
        needed = longest + self.activity_timeout_seconds
        if self.fan_out_child_timeout_seconds <= needed:
            raise ValueError(
                f"fan_out_child_timeout_seconds={self.fan_out_child_timeout_seconds} does not "
                f"cover the child it bounds: one attempt at the longest fan-out activity may take "
                f"{longest}s ({budget}) and the child's own overhead up to "
                f"{self.activity_timeout_seconds}s more (activity_timeout_seconds). Raise "
                f"fan_out_child_timeout_seconds above {needed}, or lower {budget} — the child's "
                "queue wait is derived from what is left, so a ceiling that does not clear this "
                "floor leaves no wait to give it."
            )
        return self

    @model_validator(mode="after")
    def _a_bounded_run_fits_the_ceiling_that_kills_it(self) -> Self:
        """A drain that bounds its own run must be able to finish one inside `run_timeout`.

        Four Schedules drain in chunks and `continue_as_new` after `*_max_iterations`
        (`durable/corpus_sync.py`, `document_sync.py`, `label_sync.py`, `eln_sync.py`). A run killed
        at
        `schedule_run_timeout_seconds` is a `TIMED_OUT` no code sees, and the jobs without a cursor
        restart
        from page one, so a run's budgeted work must fit. Queue wait is deliberately not charged: a
        run
        overrunning because nothing claims its activities is what the ceiling exists to kill.
        `_BOUNDED_DRAINS` supplies the activities-per-iteration term.
        """
        for iterations_name, budget_name, per_iteration in _BOUNDED_DRAINS:
            iterations: int = getattr(self, iterations_name)
            budget: float = getattr(self, budget_name)
            # The planning activity every one of these runs before its loop, then the loop.
            needed = budget * (1 + iterations * per_iteration)
            if needed > self.schedule_run_timeout_seconds:
                raise ValueError(
                    f"{iterations_name}={iterations} does not fit the ceiling that kills the run: "
                    f"one planning activity plus {iterations} iteration(s) of {per_iteration} "
                    f"activity/activities at {budget}s ({budget_name}) is {needed}s against "
                    f"schedule_run_timeout_seconds={self.schedule_run_timeout_seconds}. Lower "
                    f"{iterations_name} to at most "
                    f"{int((self.schedule_run_timeout_seconds / budget - 1) // per_iteration)}, or "
                    f"lower {budget_name} — raising the ceiling instead makes a stuck run "
                    "invisible for longer, which is what it exists to bound."
                )
        return self

    def template_step_ceilings(self) -> dict[str, tuple[float, str]]:
        """The longest one step of each kind may take, and the words that name the budget.

        One definition for two readers: the validator below takes the maximum (one step must fit),
        and
        `cli/validate_templates.py` sums over the steps a template file declares. Keyed by the
        manifest's
        `kind` values, so a new kind missing here raises `KeyError` rather than counting as free.

        A `job` step is not an activity: it starts `ConnectorJobWorkflow` under
        `durable/connector_job.wrapper_execution_timeout()`, the child ceiling plus the wrapper's
        post-child headroom (`finish_headroom`). `core` cannot import `durable`, so that sum is
        restated
        here, and `tests/test_template_job_step.py` checks it against the live function.

        Returns:
            `{kind: (seconds, why)}`, where `why` is phrased to be read inside a refusal.
        """
        return {
            "tool": (self.template_step_timeout_seconds, "template_step_timeout_seconds"),
            "agent": (self.template_step_timeout_seconds, "template_step_timeout_seconds"),
            "job": (
                self.connector_job_timeout_seconds
                + (
                    self.activity_queue_wait_seconds * 3
                    + self.template_step_timeout_seconds * 3
                    + self.activity_timeout_seconds * 2
                    + self.job_record_timeout_seconds
                    + self.result_publish_timeout_seconds
                    + self.note_write_timeout_seconds
                    + self.delivery_timeout_seconds
                ),
                "connector_job_timeout_seconds plus what the wrapper's post-child "
                "steps may spend, the ceiling a `job` step carries",
            ),
        }

    @model_validator(mode="after")
    def _the_template_run_ceiling_covers_one_step(self) -> Self:
        """The same rule again, on the template run and the longest step it has to contain.

        `templates/registry.py` runs `TemplateWorkflow` under `template_run_timeout_seconds`; a
        ceiling at
        or below the longest step's budget kills the run inside that step with a bare
        `WorkflowExecutionTimedOut`, so failure notification never runs and per-step retries are
        unreachable. The longest step is usually a `job` step (see `template_step_ceilings`).
        Strictly
        greater, because equality is the defect.

        This checks only that one step fits; the N-step bound needs the YAML and lives in
        `agent/template_surface.run_ceiling_problems` (`make template-validate`,
        `registry.unrunnable_reason`).
        """
        longest, budget = max(self.template_step_ceilings().values(), key=lambda pair: pair[0])
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

        `ConnectorJobWorkflow` gives its child `connector_job_timeout_seconds` as an execution
        timeout
        (`durable/connector_job.py`); at or below the child's longest activity budget, that activity
        can
        never finish and the run ends as a bare `WorkflowExecutionTimedOut`. Strictly greater, by
        one
        activity's overhead.

        It guarantees one full-length attempt, never two: `connector_queue_wait_timeout` is derived
        from
        the same ceiling, so a worst-case attempt (wait plus work) always consumes nearly all of it.
        Retries
        remain spendable by attempts that fail quickly, which is what `BAD_DATA_RETRY`'s transient
        failures do. On the composed class because the ceiling and the budgets live in different
        sections.
        """
        # The max over every activity budget a bundle child can spend, from
        # `longest_bundle_activity`, the
        # same number `durable/publish.py` derives the queue wait from.
        longest, budget = self.longest_bundle_activity
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

        `background_activity_heartbeat_timeout_seconds` covers core's long background activities. It
        must
        be strictly below the shortest start-to-close budget it sits under: above it the heartbeat
        can
        never fire first, and since `durable/heartbeat.py::beating` beats every `timeout / 4`, a
        large one
        sends no beat before the budget expires. `result_publish_timeout_seconds` is taken for one
        sink,
        its smallest value.
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

        The xTB activity's longest await is the sampler's `calc_sampling_timeout_seconds`, matched
        to the
        server's CREST ceiling. The activity budget must exceed it by `activity_timeout_seconds`
        (the key
        probe, embed and cache write around it), so a client timeout surfaces as an error rather
        than a
        bare activity timeout.
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

# Applied here because every entrypoint imports this module. `langsmith` enables tracing from the
# ambient environment and would send prompts to a third party; `chemclaw.core.egress` explains the
# pin.
pin_langsmith_egress(allowed=settings.langsmith_tracing_allowed)

# Armed here for the same reason. The allowlist is derived from the destinations this deployment
# dials (LLM gateway, Postgres, Temporal, connectors, the IdP), as defence in depth behind the
# NetworkPolicy. Child processes and compiled extensions are covered by the `LD_PRELOAD` layer
# (`chemclaw.core.netguard_preload`).
arm_egress_guard(settings)

# Publish whether that preload layer is loaded, unconditionally: it is a fact about the process,
# not a setting. `is_armed()` asks the dynamic linker, since a preload naming a missing path is
# ignored silently.
publish_preload_state()
