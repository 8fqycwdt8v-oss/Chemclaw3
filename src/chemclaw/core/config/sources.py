"""Settings for the `DataSource` seam: where sources are discovered, and which are active.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import os
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class SourcesSettings(BaseSettings):
    """The generic `DataSource` seam (plan F7): where sources are discovered, and which are active.

    Its own section because the seam is deliberately source-agnostic — adding a source (first live
    one: a warehouse ELN connector) is one `datasource.yaml` folder and one name here, zero core
    edits — so it belongs to neither the ELN section nor the retrieval section alone.

    Two tokens, exactly mirroring the connector seam: a *discovery* path and an *enablement* list.
    Discovery is not enablement (D-018) — the repo ships every source, a deployment runs the subset
    it has validated.
    """

    # OS-pathsep list of directories holding `datasource.yaml` folders; read via
    # `data_sources_dirs`. Earlier directories win a name collision, so a mounted folder can
    # override a shipped source.
    data_sources_dir: str = Field(default_factory=lambda: _shipped("ingest", "sources"))

    # Comma list of enabled sources. `graph` is the knowledge-graph retriever (retrieve-only);
    # `eln-json`/`eln-ord` are the ELN adapters (ingest-only). An undeclared name is a startup
    # error.
    data_sources: str = "graph,eln-json"

    # Where the build baked vendored reference datasets: local by construction, reviewed like any
    # pinned dependency, read from disk at runtime.
    vendored_dataset_dir: str = "data/vendored"
    #: Where the `commitments-json` source reads a portfolio extract from; a setting so a container
    #: whose WORKDIR is not the repo root does not silently read an empty portfolio.
    commitment_export_dir: str = "data/commitments"
    # Check `records.csv` against its manifest checksum on load, so shipped data is provably what
    # was reviewed.
    vendored_dataset_verify: bool = True

    # --- Mounted document shares ---
    # Mount point, scope and readers are the binding in the source's `datasource.yaml`; only the
    # machinery's bounds are config.
    #
    # Candidate documents one activity attempt considers; the workflow loops with the crawl cursor,
    # so a share of any size makes progress. Every bound here is constrained because 0 never
    # advances.
    document_sync_batch_size: int = Field(default=500, ge=1)
    document_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    document_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # Chunks per run before `continue_as_new`. Derived from `schedule_run_timeout_seconds`
    # (`_a_bounded_run_fits_the_ceiling_that_kills_it`); a third of its siblings' because each
    # iteration dispatches three activities.
    document_sync_max_iterations: int = Field(default=30, ge=1)
    # Stale chunks one re-embedding pass refreshes; paced by the embedding endpoint, not the share.
    document_reembed_batch_size: int = Field(default=500, ge=1)
    # Crawl cadence in minutes; six hours, since a share changes over days and a crawl scans every
    # path.
    document_sync_schedule_minutes: int = 360
    # Ceiling on a zip-container document's expanded size (`.docx`/`.xlsx`/`.pptx`), also for
    # uploads. It bounds decompression work, not memory (`document_parse_memory_bytes` does that),
    # and is read from the central directory, so a refusal costs no decompression.
    document_max_expanded_bytes: int = 64 * 1024 * 1024
    # What one parse may allocate, in bytes, enforced by the kernel (`RLIMIT_DATA`, set by
    # `ingest/documents/isolate.py` in the child). Archive-size ceilings cannot predict parse memory
    # (string width, shared-string fan-out, DOM overhead), so the bound is in the unit that kills
    # the pod. Derived from the pod's memory: `tests/test_deploy_chart.py` holds the inequality. A
    # refusal names this ceiling; on the share path it lands in `skipped_unreadable`.
    # `tests/test_parse_isolation.py` holds behaviour on named fixtures.
    document_parse_memory_bytes: int = 160 * 1024 * 1024
    # Ceiling on one whole-document read (`ShareDocumentRetriever.read_document`), in characters of
    # indexed text, since that is what reaches the model. Larger than any protocol a turn condenses:
    # the digest names its own "too large" refusal.
    document_read_max_chars: int = Field(default=200_000, ge=1_000)

    @property
    def vendored_dataset_path(self) -> Path:
        """Where the vendored reference dataset was baked into the image.

        Relative to the CWD unless absolute; read-only at runtime, so there is nothing to reconcile.
        """
        return Path(self.vendored_dataset_dir)

    @property
    def data_sources_dirs(self) -> list[str]:
        """The data-source directories, split on the OS path separator (like `PATH`)."""
        return [d for d in self.data_sources_dir.split(os.pathsep) if d]

    @property
    def data_source_list(self) -> list[str]:
        """The active data-source keys, parsed from the comma list (order kept, blanks dropped)."""
        return [s.strip() for s in self.data_sources.split(",") if s.strip()]

    @model_validator(mode="after")
    def _distinct_source_names(self) -> Self:
        """Reject a name listed twice — each name is also a per-source `sync_cursors` cursor key.

        A duplicate would make one source advance the other's cursor and skip entries.
        """
        names = self.data_source_list
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(
                f"data source names must be unique in data_sources; duplicated: {duplicated}"
            )
        return self

    # Bound on one source's commitment mirror pass, a whole-snapshot read.
    commitment_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # Heartbeat timeout for the mirror pass; a fifth of the budget, like the ELN sync's.
    commitment_sync_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)

    # Mirror cadence in minutes (daily); also runnable on demand.
    commitment_sync_schedule_minutes: int = Field(default=1440, gt=0)
