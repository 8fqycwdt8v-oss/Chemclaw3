"""The result-sink manifest: one validated declaration of where computed results are written.

The third of this system's three attachment seams, and written to their template deliberately —
a folder, a YAML file, `extra="forbid"`, discovered from a path list, enabled by a name list.
`connectors/manifest.py` is the first (capability: work whose result is a value),
`ingest/sources/manifest.py` the second (corpus: records to ingest, evidence to retrieve).

**Why a sink is neither of those, and could not be declared as one.** A connector *produces*;
`cli/validate_connectors.py` refuses a `write_`/`submit_`/`update_`-prefixed endpoint tool
outright, because "the agent-facing surface is read/compute only". A data source *supplies*;
`ingest/sources/README.md` states that "a source cannot acquire a write path by declaring one". A
sink *consumes what this system produced*, which is a third thing, so it gets a third manifest
rather than a hole cut in one of the other two.

**The site's schema is not in here.** Unlike the warehouse ELN binding — where the whole point was
that the site's tables are a manifest block, because a schema nobody can see yet cannot be written
into Python — this seam ships its *own* schema (`schema/result-store/`). What the binding carries
is how to reach the destination and who may write to it, not what the tables are called. A
deployment that must land in pre-existing tables writes a driver, which is what the
`module:callable` is for.
"""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResultSinkManifest(BaseModel):
    """Everything one sink declares: its name, the driver that reaches it, and that driver's config.

    `extra="forbid"` so a misspelled key fails `make sink-validate` in CI rather than silently
    disabling a destination — the stance every manifest in this tree takes, and the one that
    matters most here, because a sink that silently does not publish looks exactly like a sink
    with nothing to publish.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9-]*$",
        description=(
            "The sink's stable key. Must equal its directory name, and it is the enable token in "
            "`CHEMCLAW_RESULT_SINKS` and the value stored in `result_publications.sink` — so "
            "renaming a sink orphans its outbox rows rather than moving them."
        ),
    )
    description: str = Field(
        min_length=1,
        description="What this sink is, for the operator deciding whether to enable it.",
    )
    driver: str = Field(
        min_length=1,
        pattern=r"^[\w.]+:[A-Za-z_]\w*$",
        description=(
            "`module:callable` building a `ResultSink`, resolved the first time a delivery is "
            "attempted rather than at import. The same late binding the data-source seam uses, "
            "buying the same property: a process that never publishes never imports the client."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keyword arguments the driver is built with. Free-form rather than a typed union: the "
            "callable's own signature is the schema, and `make sink-validate` binds these against "
            "it — so a wrong key is a validation failure without a second model to keep in step."
        ),
    )
    required_roles: list[str] = Field(
        default_factory=list,
        description=(
            "Entitlements a turn must hold for its results to reach this sink. Empty means every "
            "authenticated actor. Expressed in the binding rather than in core config, following "
            "the mounted share's precedent: which people a destination is for is a fact about that "
            "destination."
        ),
    )
    tenant_id: str = Field(
        default="",
        description=(
            "What this deployment calls itself in the published record. Defaults to the sink's own "
            "name. Recorded on every publication row so one shared results database can hold "
            "several deployments' output without their provenance merging."
        ),
    )

    @model_validator(mode="after")
    def _config_does_not_shadow_the_name(self) -> Self:
        """Reject a `config:` key that would collide with an argument the registry supplies.

        The builder passes `name=` alongside the config, so a manifest setting it there would raise
        a `TypeError` from deep inside the driver construction naming neither the sink nor the key.
        The same guard `DataSourceManifest` carries, for the same failure.
        """
        for reserved in ("name", "tenant_id"):
            if reserved in self.config:
                raise ValueError(
                    f"result sink {self.name!r} sets {reserved!r} in `config:`, but the registry "
                    f"supplies it; set the top-level `{reserved}:` field instead"
                )
        return self
