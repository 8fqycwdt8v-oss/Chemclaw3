"""QM job timeouts, the mock-HPC spine, and the real Nextflow launcher (plan 1.2–1.4, F5).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class HpcSettings(BaseSettings):
    """QM job timeouts, the mock-HPC spine, and the real Nextflow launcher (plan 1.2–1.4, F5).

    Grouped because the qm_* and hpc_* knobs describe one execution path — the QM activities and
    the HPC backend they dispatch to — and the poll/heartbeat relationship between them is
    validated here, in the section that owns it.
    """

    # QM job timeouts and mock-HPC timing (plan steps 1.2–1.4). Times are in seconds. The
    # "mock_*" values only shape the simulated HPC job's duration so the durable path is
    # observable; they vanish when a real backend lands.
    qm_activity_timeout_seconds: float = Field(default=30.0, gt=0)
    # Heartbeat timeout for the long-running poll: if a worker dies, Temporal waits at most this
    # long before retrying the activity on another worker.
    qm_poll_heartbeat_timeout_seconds: float = Field(default=10.0, gt=0)
    # How often the poll loop heartbeats / re-checks the (mock) scheduler. Must be positive — a
    # zero interval would make the poll loop never advance.
    hpc_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # Simulated submission latency and total run time of the mock HPC job.
    hpc_mock_submit_seconds: float = Field(default=1.0, gt=0)
    hpc_mock_run_seconds: float = Field(default=6.0, gt=0)
    # The real HPC execution path (plan F5): `hpc_launch_interface` selects the backend the QM
    # activities dispatch to — `"mock"` (default, the simulated SLURM spine kept for CI/local,
    # needs no cluster) or `"nextflow"` (the Seqera Platform/Tower REST launcher, ADR D-A5a).
    # The `hpc_api_*` values address and authenticate that launcher (the token arrives via the
    # HPC bridge / a mounted secret, F4-T6); `hpc_pipeline_name`/`_version` name the pipeline to
    # run; `hpc_artifact_store_url` is where a finished run's QM output blob is fetched from.
    # All empty in dev. `hpc_launch_interface` and `hpc_pipeline_version` both enter the
    # calculation cache key (`connectors.qm.cache.calc_version`), so a pipeline bump is a cache
    # miss not a stale hit (D-011/D-033) and a mock-produced energy can never be served to a
    # deployment pointed at a real cluster. `_hpc_launch_config` therefore refuses an empty
    # version under `nextflow` — see it for why that is a cache rule and not a connectivity one.
    hpc_launch_interface: Literal["mock", "nextflow"] = "mock"
    hpc_api_base_url: str = ""
    hpc_api_token: str = ""
    hpc_pipeline_name: str = ""
    hpc_pipeline_version: str = ""
    hpc_artifact_store_url: str = ""
    # Real-run budgets for the `nextflow` poll (the mock uses `hpc_mock_run_seconds`). A real
    # QM/DFT run takes far longer than the mock: `hpc_run_timeout_seconds` is the poll
    # activity's single-attempt `start_to_close` cap (default 24h — heartbeating does NOT extend
    # it, so it must cover the whole run — and it must in turn fit *inside*
    # `connector_job_timeout_seconds`, the ceiling on the workflow that runs it, or raising this
    # one alone changes nothing at all; `Settings._the_job_ceiling_covers_the_poll_it_bounds`
    # refuses that pairing at startup); `hpc_run_heartbeat_timeout_seconds` is the poll's
    # heartbeat timeout, set comfortably above one poll's HTTP round-trip +
    # `hpc_poll_interval_seconds` so a slow launcher does not trip a false dead-worker timeout;
    # `hpc_http_timeout_seconds` bounds each launcher or artifact HTTP call (a dedicated knob,
    # not the Entra-token timeout).
    hpc_run_timeout_seconds: float = Field(default=86400.0, gt=0)
    hpc_run_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    hpc_http_timeout_seconds: float = Field(default=30.0, gt=0)
    # How many *consecutive* failed launcher polls (HTTP 5xx, transport blips) the poll activity
    # tolerates before failing its attempt. A transient blip during an up-to-24h run must not
    # burn the activity's shared retry budget — the loop just polls again next interval — while
    # a persistently broken launcher still surfaces within roughly this many poll intervals.
    hpc_poll_max_consecutive_errors: int = Field(default=30, ge=1)
    # Bearer token for the artifact store when it lives on a different origin than the launcher:
    # the launcher token must never be sent to a third host (F4 three-secret model). Empty means
    # the artifact fetch is unauthenticated — unless the store shares the launcher's origin, in
    # which case the launcher token still applies.
    hpc_artifact_store_token: str = ""
    # Persist a finished QM result in the shared calculation store (D-158). On by default, which
    # is the *un*usual choice for a new flag here and deliberate: D-011 already says every result
    # is persisted once and never recomputed, and `qm` was the one capability not doing it — so
    # this is the bundle complying with an existing rule, not a new opt-in behaviour. The write is
    # an idempotent upsert keyed by content, so re-running a job cannot corrupt anything.
    #
    # The switch exists for the deployment that runs the `qm` worker without a reachable Postgres:
    # there, every job would log a failed persist. Turning it off restores exactly the old
    # behaviour — the result still reaches the session and the PR-gated note, just without the
    # durable cache entry or the note's `calc_refs`.
    qm_persist_to_calc_store: bool = True

    @model_validator(mode="after")
    def _poll_faster_than_heartbeat(self) -> Self:
        """The poll loop must beat faster than Temporal's heartbeat timeout.

        Otherwise every `poll_hpc_status` activity is declared dead between two heartbeats and
        retried in a loop — a mis-set interval must fail at startup. The `nextflow` backend
        heartbeats on the same interval but against its own `hpc_run_heartbeat_timeout_seconds`,
        so that pair is checked when selected.
        """
        if self.hpc_poll_interval_seconds >= self.qm_poll_heartbeat_timeout_seconds:
            raise ValueError(
                "hpc_poll_interval_seconds must be smaller than qm_poll_heartbeat_timeout_seconds"
            )
        return self

    @property
    def hpc_submit_timeout_seconds(self) -> float:
        """How long the launch activity gets: its own HTTP bound, twice, or the generic budget.

        **Derived rather than validated, because the alternative was a second knob to get wrong.**
        `submit_to_hpc` is bounded by `qm_activity_timeout_seconds` and the POST inside it by
        `hpc_http_timeout_seconds`, and at the shipped defaults both are 30 s — so a slow launcher
        races its own start-to-close. Measured: start-to-close cancelling a launch in flight lost
        the run id, Temporal retried, and the cluster ended up with **two** runs, one of which
        nothing will ever poll, cancel or bill to a job.

        A validator demanding `2 * http < activity` would have caught it and refused the shipped
        chart at boot, making every `nextflow` deployment tune a pair of unrelated knobs to say
        something the code already knows. The relation is not a policy an operator holds an opinion
        about; it is arithmetic. So the activity's bound follows its own HTTP bound: doubled, so the
        POST can time out *and* the activity still has room to report that it did.

        Raising `hpc_http_timeout_seconds` for a sluggish launcher therefore needs no second edit,
        and the generic short-activity budget still applies to every other QM activity.
        """
        return max(self.qm_activity_timeout_seconds, self.hpc_http_timeout_seconds * 2 + 5.0)

    @property
    def hpc_effective_heartbeat_timeout_seconds(self) -> float:
        """The poll's heartbeat timeout, floored by the real gap between two of its heartbeats.

        Derived for the same reason as `hpc_submit_timeout_seconds`. `_poll_nextflow` beats once at
        the top of each loop and then makes an HTTP call bounded by `hpc_http_timeout_seconds`
        before sleeping `hpc_poll_interval_seconds`, so the true interval between beats is their
        sum — while `_poll_faster_than_heartbeat` compares only the sleep. Measured as accepted at
        startup: `hpc_http_timeout_seconds=300` against the shipped 120 s heartbeat timeout leaves
        302 s between beats, so Temporal declares a healthy worker dead, retries the poll elsewhere
        while the first is still polling, and burns the attempt budget on a run that is fine.

        A floor rather than a refusal, because the configured value is a real preference (how fast a
        genuinely dead worker is noticed) and only becomes wrong when it drops below what the loop
        can physically achieve. Slack of one whole interval, so a single slow round trip does not
        sit exactly on the boundary.
        """
        floor = self.hpc_http_timeout_seconds + self.hpc_poll_interval_seconds * 2
        return max(self.hpc_run_heartbeat_timeout_seconds, floor)

    @model_validator(mode="after")
    def _hpc_launch_config(self) -> Self:
        """`nextflow` needs the launcher endpoint, pipeline, version, and artifact store set.

        Checked at startup (mirroring `_llm_provider_config`) so a half-configured backend fails
        here with a clear message rather than as an opaque httpx protocol error five retried
        activity attempts deep in the first QM job. The `mock` dev path needs none.

        **`hpc_pipeline_version` is required here, and it is a cache rule rather than a connectivity
        one.** The other three are needed to reach the cluster at all; this one is needed to tell
        one cluster result from another. An empty version renders as the `unversioned` slug
        (`connectors.qm.cache.version_slug`), so two genuinely different pipelines would key their
        DFT energies identically and the second would be served the first's number. Refusing the
        empty value is the mechanism; the shipped Helm values pinning `1.0.0` was only a default,
        and a default protects the deployments that did not need protecting.
        """
        if self.hpc_launch_interface == "nextflow":
            required = (
                ("hpc_api_base_url", self.hpc_api_base_url),
                ("hpc_pipeline_name", self.hpc_pipeline_name),
                ("hpc_pipeline_version", self.hpc_pipeline_version),
                ("hpc_artifact_store_url", self.hpc_artifact_store_url),
                # **The credential belongs in this list, and its absence was the one silent hole
                # left in it.** `_auth_headers()` returns `{}` for an empty token rather than
                # refusing, so a secret that failed to mount produced green pods, green probes and a
                # first DFT job that died five retried attempts deep on `launch failed: 401` — which
                # is verbatim the outcome this validator's docstring says it exists to prevent.
                # Measured as ACCEPTED at startup before this line. It was excluded because the
                # token was said to arrive "via the HPC bridge"; that bridge was deleted with
                # workload-identity federation (D-2026-08-15), so a mounted secret is the only path
                # and nothing else checks it.
                ("hpc_api_token", self.hpc_api_token),
            )
            missing = [name for name, value in required if not value]
            if missing:
                raise ValueError(
                    f"hpc_launch_interface='nextflow' requires {', '.join(missing)} to be set"
                )
        return self
