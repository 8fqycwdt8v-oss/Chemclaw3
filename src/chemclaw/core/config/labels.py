"""Where the reaction labeller lives, and how hard the enrichment drain may push on it.

The first three fields address a server, the rest bound the drains. What a source already knows
about its rows is `labels:` in its own `datasource.yaml`. There is no `labels_enabled`:
`CHEMCLAW_DATA_SOURCES` plus a declared `labels:` block already answers it.
"""

from pydantic import BaseModel, Field


class LabelSettings(BaseModel):
    """The labelling server's address and the enrichment drain's bounds."""

    # Where the reaction labeller answers (port per `Chemclaw3-mcp`'s MODULES.md). Its manifest is
    # not on `connectors_dirs`: these are internal primitives, not tools for the agent's prompt.
    rxnlabel_server_url: str = "http://127.0.0.1:8865/mcp"
    # Environment variable holding the server's bearer, read per request so rotation needs no
    # restart. A missing value refuses the call.
    rxnlabel_server_token_env: str = "CHEMCLAW_RXNLABEL_TOKEN"
    # Bound on one labelling batch; below the activity's bound so the client read timeout trips
    # first (otherwise `mcp.client.streamable_http` swallows it and the caller hangs).
    rxnlabel_server_timeout_seconds: float = Field(default=120.0, gt=0)

    # Reactions per drain call. The upper bound is the server's `MAX_BATCH = 500`; above it every
    # batch is refused, so it fails at startup instead.
    label_batch_size: int = Field(default=200, ge=1, le=500)
    # Batches per run before `continue_as_new`, bounding event history. Derived from
    # `schedule_run_timeout_seconds`: `_a_bounded_run_fits_the_ceiling_that_kills_it` refuses a
    # count whose iterations cannot finish inside the run timeout.
    label_sync_max_iterations: int = Field(default=90, ge=1)
    # The drain activity's start-to-close; per drain because each is paced by a different
    # downstream.
    label_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    # A batch has no natural progress point, so liveness is time-based.
    label_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # --- the corpus drain, which fills the record phase the labelling drain then completes ---
    # Warehouse rows per page; one relation, one query, no bind-limited `IN (...)` lists. Capped by
    # the binding's own `fetch_limit` where lower.
    corpus_page_size: int = Field(default=1_000, ge=1)
    # Pages per run before `continue_as_new`. Derived from `schedule_run_timeout_seconds`:
    # `_a_bounded_run_fits_the_ceiling_that_kills_it` refuses a count that cannot finish in one run
    # (a killed release-mode drain keeps no cursor).
    corpus_sync_max_iterations: int = Field(default=90, ge=1)
    # Start-to-close and heartbeat bound of the corpus drain activity (a query plus fingerprints).
    corpus_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    corpus_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # Corpus drain cadence; daily because a corpus changes when a vendor ships a release.
    corpus_sync_schedule_minutes: int = Field(default=1440, gt=0)

    # Labelling drain cadence; hourly, matching the ELN sync it follows.
    label_sync_schedule_minutes: int = Field(default=60, gt=0)
