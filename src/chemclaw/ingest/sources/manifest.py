"""The data-source manifest: one validated declaration of where a body of evidence comes from.

The counterpart of `connectors/manifest.py`, for the other thing this system attaches to the
outside world. A connector contributes *capability* (work whose result is a value); a data source
contributes *corpus* (records to ingest, evidence to retrieve). They are different enough to
deserve different manifests and similar enough that a second idiom would be indefensible — so this
file mirrors that one deliberately: a folder plus a YAML file, `extra="forbid"`, discovered from
disk, enabled by a config token.

**Why a manifest replaced a dict of factories.** `ingest/sources/registry.py` used to hold
`DATA_SOURCES: dict[str, Callable[[], DataSource]]`, so every source's adapter was constructed by
a lambda in that module — which meant the module imported every adapter at module scope. Asking
for the *retrieve* sources, which under the default config yields exactly one source (`graph`),
loaded all five ELN *ingest* modules and 836 modules in total. Nothing was wrong with any one of
those imports; the coupling was in the registry's shape, and it grows with every source added.
That matters here more than it would elsewhere: a data source's dependency is a *driver* — a
database client, a vendor SDK — and a warehouse ELN connector would otherwise have put its driver
in the chat pod, which will never ingest anything.

A manifest fixes it by making the one fact you need in order to *skip* a source available as
data: `ingest:` and `retrieve:` say which halves exist without importing either. So
`active_retrieve_sources()` can filter first and import second, and a source's driver is loaded
only in the process that uses that half.

**Halves are `module:callable` strings, resolved late.** The same late-binding the connector seam
uses for `params_model` — and the same hazard, which `connectors/calc/specs.py` was split out to
avoid: a name in YAML is an import that no reader of the importing module can see. The rule
that falls out is worth stating, because it is the discipline of this seam: *a half's callable may
import whatever it needs, because only the processes that use that half will ever resolve it.*
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
        description=(
            "The source's stable key. It is also the enable token in `CHEMCLAW_DATA_SOURCES` and "
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
        """Reject a source declaring neither half — nothing could ever use it.

        The manifest-level twin of `SourceSpec.__post_init__`. Both exist because the two can be
        reached independently: a manifest is validated before anything is built, and `SourceSpec`
        is also constructed directly in tests.
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

        Caught here rather than at import time so the failure names the manifest and the field. A
        bare `JsonExportAdapter` would otherwise surface as an opaque `ValueError: not enough
        values to unpack` from the resolver, in whichever process happened to need that half first.
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
        """Reject a `config:` block carrying a `name` key — the folder already decides that.

        `_build_retrieve_half` passes `name=<this manifest's name>` on top of `**config`, so a
        `name` in `config` is a duplicate keyword and the source cannot be built at all. Refusing
        it here is what keeps the validator honest: `make datasource-validate` builds the kwargs as
        a *dict*, where a second `name` silently overwrites the first, so the manifest validated
        green and then the process died at startup with `got multiple values for keyword argument
        'name'`. A validator that passes what startup refuses is worse than no validator, because it
        is the thing an operator trusts before deploying (D-2026-08-08-a-source-is-named-by-...).
        """
        if "name" in self.config:
            raise ValueError(
                f"data source {self.name!r} sets `name` in its `config:` block; a source's name is "
                "its folder name (the token `CHEMCLAW_DATA_SOURCES` enables) and is passed to the "
                "retrieve half automatically — remove the key"
            )
        return self
