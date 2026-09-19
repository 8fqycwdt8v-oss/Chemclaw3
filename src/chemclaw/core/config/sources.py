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
    #: Where the `commitments-json` source reads a portfolio extract from.
    #:
    #: A setting rather than a `config:` block in the manifest, matching `eln_export_dir` — the
    #: manifest carried `path: data/commitments` and nothing created that directory, so in a
    #: container whose WORKDIR is not the repo root the adapter silently found nothing,
    #: `mirror_freshness` returned NULL, and `review_commitments` presents that as "nothing was ever
    #: mirrored". A wrong directory reached a project leader as a truthful empty portfolio.
    commitment_export_dir: str = "data/commitments"
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
    # **Derived from `schedule_run_timeout_seconds` rather than chosen**, and it moved from 100
    # to 30 when that arithmetic was first done: `_a_bounded_run_fits_the_ceiling_that_kills_it`
    # refuses a count whose iterations cannot finish inside the `run_timeout` on the very run
    # they bound. It is a third of its siblings' because this loop dispatches three activities per
    # iteration, not one: at 100 that was 270,900 s against 86,400 s, and this drain also keeps no
    # cursor between fires. What the cut costs is one extra `continue_as_new`
    # per 30 iterations and nothing else — the hop carries the drain's position — and what it
    # buys is that a run large enough to use its budget is no longer killed near the end of one.
    document_sync_max_iterations: int = Field(default=30, ge=1)
    # How many stale chunks one re-embedding pass refreshes. Its own bound because the work is
    # unlike the crawl's: no filesystem at all, just a read of stored text, one embedding batch and
    # an update — so it is paced by the embedding endpoint rather than by a network share.
    document_reembed_batch_size: int = Field(default=500, ge=1)
    # Every N minutes. Six hours by default: a file share is not an ELN, its documents change over
    # days, and an unchanged crawl still costs a full `scandir` pass over every path.
    document_sync_schedule_minutes: int = 360
    # The ceiling on a zip-container document's *expanded* size. `.docx`/`.xlsx`/`.pptx` are zips,
    # so a binding's `max_file_bytes` bounds only what the file weighs on the share: a 110 KB
    # workbook whose sheet XML expands 280× is under every limit and costs minutes of CPU to
    # decompress and re-parse. Applies to uploads too, where the ratio matters more — the chat
    # pod's own `attachment_max_bytes` is in megabytes.
    #
    # **What this bounds is work, not memory, and it used to be asked for both**
    # (`D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`). The prose here said
    # 64 MB "leaves two concurrent parses well inside 1 GiB" at "the ~5-6x RSS the parsers cost",
    # and that factor is not a property of the expanded size: measured on this tree over a real
    # memory cgroup, one legal document at this ceiling charged the pod between 1.8 and 16.5 MiB
    # per expanded MiB depending on the format, on how much of the archive is text rather than
    # markup, and on the width CPython stores that text at — a 9× spread on a quantity that was
    # declared as one number. A markup-heavy `.docx` at 79% of this ceiling charged 840 MiB with
    # 470,000 characters of text in it. `document_parse_memory_bytes` is the bound on memory now;
    # this one stays because refusing an archive from its central directory costs no
    # decompression, and a refusal that names the expansion is a better answer than a parse that
    # runs for a minute and then hits its allocation ceiling.
    document_max_expanded_bytes: int = 64 * 1024 * 1024
    # What one parse may *allocate*, in bytes, enforced by the kernel on the process that does it
    # (`ingest/documents/isolate.py` sets `RLIMIT_DATA` in the child before it reads a byte).
    #
    # **The bound is in the unit that kills the pod**, which is the whole argument for it. Every
    # declarative ceiling upstream of here — `attachment_max_bytes`, a binding's `max_file_bytes`,
    # `document_max_expanded_bytes` — bounds a number written in the *archive*, and three separate
    # measurements show none of them predicts what the parse costs: CPython stores a `str` at the
    # width of its widest code point, so one em dash or one emoji anywhere multiplies a whole
    # document-wide join by 2 or 4; a workbook's shared-string table is stored once and referenced
    # N times, so 5.9 MiB of expanded XML produced 96.3 M characters; and `python-docx` builds a
    # full lxml DOM, which is charged against the markup rather than the text. Modelling any of
    # those is a coefficient that a library upgrade invalidates in silence. A ceiling the kernel
    # enforces on the child needs no model of any of them, and covers the format added next year.
    #
    # **Derived downwards from the pod rather than chosen.** `resources.service` limits the front
    # door to 1024 MiB and it holds 523 of them idle with its parse forkserver warm, so two
    # concurrent parses have 501 MiB between them and one parse charges the pod up to 1.4x its own
    # budget (`tests/test_deploy_chart.py::PARSE_MIB_PER_PARSE_BUDGET_MIB`): 501 / (2 x 1.4) is
    # 178.9 MiB. Rounded *down* to 160, which leaves the front-door inequality 53 MiB of the 1024
    # rather than the 1 MiB that rounding alone would — and that inequality is where raising this
    # fails, rather than in an OOMKill.
    #
    # What it costs a caller, by *shape* rather than by threshold. A 50 MiB plain-text document (the
    # shipped share binding's whole `max_file_bytes`) parses, and so does a 10 MiB delimited export;
    # a workbook whose shared-string table is read tens of millions of times, the same workbook with
    # one astral code point in it, and a `.docx` whose every word is its own styled run are the
    # three shapes that reach this ceiling, each from an archive well under every declarative bound
    # above. A refusal names this ceiling; on the share path it lands in `skipped_unreadable`, which
    # the sync already reports.
    #
    # **This comment used to publish six character thresholds and two of them were false**
    # (`D-2026-09-19-a-refusal-that-blames-the-document-is-worse-than-one-that-says-nothing`): it
    # said a 52 M character workbook and a 17.2 M character workbook carrying one astral code
    # point were refused, and re-driven through the shipped path both **parse** — 50.1 M ASCII and
    # 18.1 M astral parse here, 60.2 M and 20.1 M refuse. Worse than stale: a character count is
    # not what this bound measures, so the same count parses or refuses depending on the fixture's
    # shape and on how much of the budget the forkserver's own baseline residency has already
    # spent — the crossing measured 22 M on one box and 20 M on another with no code between them.
    # That is the argument of the paragraph three above, applied to the paragraph that was
    # demonstrating it. What holds the behaviour is `tests/test_parse_isolation.py`, which asserts
    # refusals and parses on named fixtures rather than thresholds on a quantity this bound does
    # not read.
    document_parse_memory_bytes: int = 160 * 1024 * 1024
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

    # How long one source's commitment mirror pass may take (F4). A portfolio export is a snapshot
    # rather than a stream, so this bounds a whole-set read rather than a page — larger than the
    # ELN's per-chunk budget for that reason, and still a ceiling rather than an expectation.
    commitment_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # Dead-worker detection for that pass, the one gap in an otherwise complete sweep: every other
    # long background activity in `durable/` carries a heartbeat timeout and this one carried none,
    # so a worker that died mid-mirror was invisible for the whole 300 s start-to-close and the
    # redelivery that would have salvaged the pass waited it out. A fifth of the budget, the same
    # ratio `eln_sync_heartbeat_timeout_seconds` uses against its own, so `durable/heartbeat.py`
    # derives a 15 s beat — comfortably inside a portfolio export's own latency.
    commitment_sync_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)

    # How often the mirror refreshes, in minutes. It is also runnable on demand. The default is
    # daily: a portfolio tool's dates move on a human cadence, and a tighter loop would spend a
    # vendor's API budget to learn nothing.
    commitment_sync_schedule_minutes: int = Field(default=1440, gt=0)
