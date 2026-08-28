"""The generic `DataSource` seam (plan F7): where sources are discovered, and which are active.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
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

    # Where `datasource.yaml` folders are discovered. An OS-pathsep list (like `PATH` and
    # `connectors_dir`); read through the `data_sources_dirs` property, never raw. Earlier
    # directories win a name collision, so a deployment can mount a folder that overrides a shipped
    # source — which is how a second JSON-ELN drop with its own `export_dir` is configured now
    # that `data_source_specs` is gone (D-120): a manifest with `config: {export_dir: ...}`,
    # not a new pydantic variant plus a new branch in core.
    data_sources_dir: str = Field(default_factory=lambda: _shipped("ingest", "sources"))

    # A comma list of discovered source names. `graph` is the knowledge-graph retriever
    # (retrieve-only); `eln-json`/`eln-ord` re-host the ELN adapters (ingest-only).
    # `active_retrieve_sources()` feeds `gather_evidence`, so the default keeps today's
    # exactly-one-graph-retriever behavior; `active_ingest_sources()` feeds the ELN sync,
    # defaulting to the JSON adapter as before. A name here that no manifest declares is a startup
    # error, not a corpus that silently stops being searched.
    data_sources: str = "graph,eln-json"

    # Where the build baked a vendored reference dataset (STO-14). A *local* path by construction:
    # the corpus is installed into the image at build time, reviewed once in a pull request like
    # any other pinned dependency, and read from disk at runtime. D-089's "no external data
    # sources" is about a runtime dependency on somebody else's service, and there is none here.
    vendored_dataset_dir: str = "data/vendored"
    # Check `records.csv` against the checksum in its manifest on load. On by default: the whole
    # value of vendoring is that the shipped data is provably what was reviewed, and a corpus that
    # silently drifted from its manifest is worth less than none.
    vendored_dataset_verify: bool = True

    # --- Mounted document shares (D-2026-08-06-a-share-is-mounted-not-called) ---
    # Which share is mounted where, what to index and who may read it is the *binding* in that
    # source's `datasource.yaml`; only the machinery's own bounds are config, because they are
    # about this deployment's Temporal and embedding budget rather than about any one share.
    #
    # How many candidate documents one activity attempt may consider. The bound that lets a share
    # of any size make durable forward progress instead of wedging one over-window attempt: the
    # workflow loops with the crawl cursor until the pass reports no more.
    #
    # Every bound in this block is constrained, as its neighbouring blocks constrain theirs. They
    # were declared bare, and each degenerate value is a live failure rather than a merely odd
    # setting: a batch size of 0 makes a pass that considers nothing and never advances its cursor,
    # `document_sync_max_iterations` at 0 continues-as-new after every chunk forever, and a
    # timeout at or below 0 is what the `max(1.0, timeout / 4)` floor in `durable/heartbeat.py`
    # was added to survive — a floor is a workaround for a value the schema should have refused.
    document_sync_batch_size: int = Field(default=500, ge=1)
    document_sync_timeout_seconds: float = Field(default=900.0, gt=0)
    document_sync_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    # How many chunks one workflow run drains before continuing as new. Event history is bounded,
    # and a first full crawl of a TB share is thousands of chunks — far past what one run may hold.
    document_sync_max_iterations: int = Field(default=100, ge=1)
    # How many stale chunks one re-embedding pass refreshes. Its own bound because the work is
    # unlike the crawl's: no filesystem at all, just a read of stored text, one embedding batch and
    # an update — so it is paced by the embedding endpoint rather than by a network share.
    document_reembed_batch_size: int = Field(default=500, ge=1)
    # Every N minutes. Six hours by default: a file share is not an ELN, its documents change over
    # days, and an unchanged crawl still costs a full `scandir` pass over every path.
    document_sync_schedule_minutes: int = 360
    # The ceiling on a zip-container document's *expanded* size. `.docx`/`.xlsx`/`.pptx` are zips,
    # so a binding's `max_file_bytes` bounds only what the file weighs on the share: a 110 KB
    # workbook whose sheet XML expands 280× is under every limit and still exhausts the pod's
    # memory. Applies to uploads too, where the ratio matters more — the chat pod's own
    # `attachment_max_bytes` is in megabytes. The number is reconciled against where the parse runs.
    # 512 MB was "far below what OOMs a pod" for the *worker* (4 GiB limit) and false for the *front
    # door*, which is where `POST /sessions/{id}/attachments` parses and which the chart caps at
    # 1 GiB: measured, a 1.78 MB upload declaring a legal ~150 MB expansion cleared this and both
    # other gates, and two concurrent parses (the parse-slot cap) took the pod over its limit.
    # 64 MB is still far above any real document (a 64 MB *expanded* .docx is enormous) and, at the
    # ~5-6x RSS the parsers cost, leaves two concurrent parses well inside 1 GiB.
    document_max_expanded_bytes: int = 64 * 1024 * 1024
    # The ceiling on one whole-document read (`ShareDocumentRetriever.read_document`), in
    # characters of *indexed text* rather than bytes on disk: what is being bounded is what reaches
    # a model's context, and the chunks are the only copy left by then.
    #
    # Deliberately larger than any single protocol a turn will condense. A whole-document read and
    # a condensable protocol are two different limits, and collapsing them would put the refusal in
    # the wrong place: this ceiling exists so a 400-page report cannot be pulled into the chat pod
    # at all, while "this protocol is too large to digest" is a judgement the digest makes and
    # *names*, with the citation, so a chemist knows which document to open themselves.
    #
    # 200,000 characters is roughly 50k tokens — well past the largest real SOP (the corpus's
    # biggest protocol-shaped fixture is 6.3 kB) and well under what would exhaust a pod reading
    # one row set.
    document_read_max_chars: int = Field(default=200_000, ge=1_000)

    @property
    def vendored_dataset_path(self) -> Path:
        """Where the vendored reference dataset was baked into the image (STO-14).

        A plain path, resolved relative to the process CWD unless set absolutely — unlike
        `knowledge_path` there is no second location to reconcile, because nothing ever writes
        here. The corpus is installed by the build and read-only at runtime.
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

        A duplicate would make one source advance the other's high-water cursor, silently skipping
        entries. Caught at startup, before any sync runs. (Two *different* sources cannot collide
        any more: a name is a folder name, and `_source_dirs` dedupes by it.)
        """
        names = self.data_source_list
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(
                f"data source names must be unique in data_sources; duplicated: {duplicated}"
            )
        return self
