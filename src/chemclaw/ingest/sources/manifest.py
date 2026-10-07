"""The data-source manifest: one validated declaration of where a body of evidence comes from.

The counterpart of `connectors/manifest.py`: a connector contributes capability, a data source
contributes corpus, and both use the same idiom (a folder plus a YAML file, `extra="forbid"`,
discovered from disk, enabled by a config token). Declaring halves as data lets a process skip a
source without importing it, so a driver (database client, vendor SDK) loads only where its half is
used. Halves are `module:callable` strings resolved late; a half's callable may import whatever it
needs, because only processes using that half resolve it.
"""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.science.labels.policy import LabelPolicy


class DataSourceManifest(BaseModel):
    """Everything one data source declares: its name, its halves, and their construction config.

    `extra="forbid"` so a misspelled key fails `make datasource-validate` in CI rather than
    silently disabling a half — the same stance `ConnectorManifest` and `SkillManifest` take.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9-]*$",
        description=(
            "The source's stable key, in the shape `ConnectorManifest`, `ResultSinkManifest` and "
            "`DeliveryChannelManifest` all require and this one alone did not — measured, folders "
            "named `UPPER`, `with space`, `unicode-ïd`, `dot.dot` and `-leading` all loaded and "
            "`make datasource-validate` exited 0. **The constraint is really on the folder**, "
            "because the folder name is authoritative and is the string that arrives here. "
            "Defence in depth rather than a fix for a live escape: the 2026-09 review could not "
            "turn a hostile name into a traversal or a metric-label injection (`core/metrics.py` "
            "escapes at all four exposition sites). But this string is the document-index "
            "partition key, the sweep's delete predicate, the citation label and a "
            "`retrieval_source_weights` key, and three sibling manifests already show the shape. "
            "It is also the enable token in `CHEMCLAW_DATA_SOURCES` and "
            "the string the durable ELN sync records in its workflow history "
            "(`sync_eln_entries(source=name)`), so renaming a source is a history-visible change, "
            "not a cosmetic one."
        ),
    )
    description: str = Field(
        min_length=1,
        description=(
            "What corpus this source carries, for the operator choosing whether to enable it."
        ),
    )
    corpus: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]*$",
        description=(
            "The body of evidence this source reads, when it is one several sources share. "
            "Sources naming the same corpus are fused **once** between them before they meet the "
            "others, so a corpus read by three legs gets one vote rather than three "
            "(`retrieval.hybrid.reciprocal_rank_fusion`). Absent — the ordinary case — a source "
            "is its own corpus and nothing changes. It exists because `graph`, `lexical` and "
            "`vector` are three rankers over one note tree and RRF assumes independent ones: "
            "measured on the shipped corpus their pairwise agreement is 47/55, 44/55 and 41/53, "
            "so the agreement term decides the order and the rank term barely participates."
        ),
    )
    ingest: str | None = Field(
        default=None,
        description=(
            "`module:callable` building the ingest half (an `ElnAdapter`), or absent if this "
            "source cannot be ingested from."
        ),
    )
    retrieve: str | None = Field(
        default=None,
        description=(
            "`module:callable` building the retrieve half (a `SourceRetriever`), or absent if "
            "this source cannot be retrieved from."
        ),
    )
    commitments: str | None = Field(
        default=None,
        description=(
            "`module:callable` building the commitments half (a `CommitmentAdapter`), or absent "
            "if this source holds no committed work. The third half (F4): a source that supplies "
            "*entities* — a programme, an activity, a milestone — rather than a corpus. Mirrored "
            "read-only, like the other two: `ingest/sources/README.md`'s rule that a source "
            "'cannot acquire a write path by declaring one' is unchanged, so mirroring a milestone "
            "in does not confer the ability to move one."
        ),
    )
    labels: LabelPolicy | None = Field(
        default=None,
        description=(
            "What derived reaction labels this source already carries, and which to re-derive "
            "anyway. Absent means this source contributes no reaction rows to the label index — "
            "so a `retrieve`-only document share leaves it out, and `make datasource-validate` "
            "reports a block on a source nothing would ever label. Declaring it is also what "
            "gives the labelling Schedule something to exist for: `durable/schedules.py` asks the "
            "manifests, not a `*_enabled` setting, for the same reason `share_sources()` does."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keyword arguments passed to whichever half is being built. Free-form rather than a "
            "typed union: the callable's own signature is the schema, and "
            "`make datasource-validate` binds these against it, so a wrong key is a validation "
            "failure without a second model to keep in step with the adapter."
        ),
    )

    @model_validator(mode="after")
    def _must_provide_a_half(self) -> Self:
        """Reject a source declaring neither half: nothing could ever use it.

        The manifest-level twin of `SourceSpec.__post_init__`; either can be reached without the
        other.
        """
        if self.ingest is None and self.retrieve is None and self.commitments is None:
            raise ValueError(
                f"data source {self.name!r} declares no `ingest:`, `retrieve:` or `commitments:` "
                "half; a source nothing can read from is not a source"
            )
        return self

    @model_validator(mode="after")
    def _halves_are_module_qualified(self) -> Self:
        """Reject a half that is not `module:callable`.

        Caught here so the failure names the manifest and field instead of an opaque unpacking
        error.
        """
        halves = (
            ("ingest", self.ingest),
            ("retrieve", self.retrieve),
            ("commitments", self.commitments),
        )
        for field, value in halves:
            if value is None:
                continue
            module, _, attribute = value.partition(":")
            if not module or not attribute:
                raise ValueError(
                    f"data source {self.name!r} has {field}: {value!r}; expected "
                    "'module:callable' (e.g. 'chemclaw.ingest.eln.json_adapter:JsonExportAdapter')"
                )
        return self

    @model_validator(mode="after")
    def _config_does_not_shadow_the_name(self) -> Self:
        """Reject a `config:` block carrying a `name` key: the folder already decides that.

        The registry passes `name=` on top of `**config`, so a duplicate would fail at startup with
        "multiple values for keyword argument", while `make datasource-validate` (which builds a
        dict) would pass it.
        """
        if "name" in self.config:
            raise ValueError(
                f"data source {self.name!r} sets `name` in its `config:` block; a source's name is "
                "its folder name (the token `CHEMCLAW_DATA_SOURCES` enables) and is passed to the "
                "retrieve half automatically — remove the key"
            )
        return self
