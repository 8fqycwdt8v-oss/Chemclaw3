"""Where the reaction labeller lives, and how hard the enrichment drain may push on it.

Two groups, and the split is the house rule *config says which and where; a manifest says what*:
the first three fields address a server, the rest bound a drain. What a given source already knows
about its own rows is **not** here — that is `labels:` in the source's own `datasource.yaml`
(`chemclaw.science.labels.policy`), because it describes the internals of one attached thing.

There is deliberately no `labels_enabled`. `CHEMCLAW_DATA_SOURCES` plus a declared `labels:` block
already answers "is anything being labelled here", and a second flag could only restate that or
contradict it — the same argument `sources.py` makes about `CHEMCLAW_DATA_SOURCES` being its own
enable switch, and the shape every conditional Schedule in `durable/schedules.py` already has.
"""

from pydantic import BaseModel, Field


class LabelSettings(BaseModel):
    """The labelling server's address and the enrichment drain's bounds."""

    # Where the reaction labeller answers. Plain configuration read by one client module, and its
    # manifest is deliberately **not** on `connectors_dirs` — the same call `calculators.py` makes
    # for the calculation server, and for the same reason: these are internal primitives, and
    # mounting the manifest would put them in the agent's prompt as tools to choose between.
    # 8865 is the port `Chemclaw3-mcp`'s MODULES.md allocates to `rxnlabel`, and not a tranche-1
    # number: 8850-8856 are all claimed by that catalogue's own proposals, and taking a proposed
    # server's port silently is exactly what its `test_the_catalogue_claims_no_port_a_server_
    # contradicts` exists to prevent.
    rxnlabel_server_url: str = "http://127.0.0.1:8865/mcp"
    # The environment variable holding the bearer the server enforces on `/mcp`. Named rather than
    # carried, and read per request, so a rotation needs no restart. A missing value is a refused
    # call, not an open one.
    rxnlabel_server_token_env: str = "CHEMCLAW_RXNLABEL_TOKEN"
    # How long one batch of labelling may take. Well above a connector's 30 s because a batch is
    # `label_batch_size` reactions through an atom-mapping transformer, and well below the
    # activity's own bound — the client's read timeout must be the one that trips, or
    # `mcp.client.streamable_http` swallows httpx's at debug level and the caller waits forever
    # (the measured hang `core/mcp_session.py` documents).
    rxnlabel_server_timeout_seconds: float = Field(default=120.0, gt=0)

    # How many reactions one drain attempt sends in a single call. Bounds the remote payload and
    # the window it has to fit in; the `document_reembed_batch_size` twin. The failure it prevents
    # is one over-large attempt that can never complete against a multi-million-row backlog.
    #
    # **The upper bound is the server's, not a taste.** `rxnlabel` refuses a request above
    # `MAX_BATCH = 500` — a worded `ValueError`, so it arrives classified as bad data rather than
    # as an outage to retry — which means an operator raising this past 500 gets *every* drain
    # attempt refused, forever, with the corpus never labelled. A limit worth writing down is worth
    # failing on at startup rather than on the first batch of a nightly run
    # (`core/config/__init__.py` makes the same argument for the settings its startup validator
    # guards). Stated as a bound on the field rather than as a validator there, because the
    # relationship is to a constant in another repository rather than to another field here.
    label_batch_size: int = Field(default=200, ge=1, le=500)
    # How many batches one workflow run drains before `continue_as_new`. Event history is bounded,
    # and a 13M-row corpus at 200 a batch is 65,000 batches — far past what one run may hold.
    label_sync_max_iterations: int = Field(default=100, ge=1)
    # The drain activity's start-to-close. Its own field rather than a shared one because each
    # drain in this tree is paced by a different downstream (`eln_sync_timeout_seconds`,
    # `document_sync_timeout_seconds`), and one number for all of them would be wrong for each.
    label_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    # A batch is minutes of remote work with no natural progress point, so liveness is time-based.
    # Without it a dead worker goes undetected until the whole start-to-close lapses.
    label_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # --- the corpus drain, which fills the record phase the labelling drain then completes ---
    # Rows read per page out of the warehouse. Higher than the ELN's batch because there are no
    # child-table `IN (...)` lists to blow a bind limit — one relation, one query — and a corpus is
    # millions of rows rather than a decade of one site's runs. Bounded by the binding's own
    # `fetch_limit` where that is lower, so a site can cap it without a redeploy of this.
    corpus_page_size: int = Field(default=1_000, ge=1)
    # How many pages one workflow run drains before `continue_as_new`.
    corpus_sync_max_iterations: int = Field(default=100, ge=1)
    # The drain activity's start-to-close and its heartbeat bound. A page is a warehouse query plus
    # a fingerprint per distinct structure — minutes, with no natural progress point.
    corpus_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    corpus_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # How often the corpus drain runs. Daily rather than hourly: a corpus release changes when a
    # vendor ships one, not continuously, so an hourly re-walk would read a warehouse to learn
    # nothing. A new release is picked up within a day, or immediately by an operator running the
    # workflow by hand.
    corpus_sync_schedule_minutes: int = Field(default=1440, gt=0)

    # How often the labelling drain runs. Hourly, matching the ELN sync it follows: a reaction
    # ingested now is searchable by structure immediately and by facet within the hour.
    label_sync_schedule_minutes: int = Field(default=60, gt=0)
