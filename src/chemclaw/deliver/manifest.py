"""The delivery-channel manifest: one validated declaration of where a message may leave.

The fourth attachment seam, written to the template the other three share — a folder, a YAML file,
`extra="forbid"`, discovered from a path list, enabled by a name list. `connectors/manifest.py` is
the first (a capability *produces*), `ingest/sources/manifest.py` the second (a source *supplies*),
`publish/manifest.py` the third (a sink *consumes what this system produced*).

**Why a channel is none of those.** A sink takes a typed scientific record to a database; nobody
reads it. A channel takes a *message to a person* — a digest, a report, an escalation — and the
difference is not the transport but the audience. `durable/digest.py` says so in as many words:
"no new delivery mechanism, no email integration, no second notification system", which was right
while the product was a chat window and is the reason a project leader could not be reached on a
Monday morning. Nothing here changes what a sink is; it adds the seam that was missing beside it.

**A channel carries no evidence and answers no question.** It is write-only and outbound: nothing
in this tree reads from a channel, and a driver that offered to would be an ingest source declaring
its way into a write path, which `ingest/sources/README.md` already forbids in the other direction.
"""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryChannelManifest(BaseModel):
    """Everything one channel declares: its name, the driver that reaches it, and its config.

    `extra="forbid"` so a misspelled key fails `make channel-validate` in CI rather than silently
    disabling a destination — the same stance every manifest here takes, and the one that matters
    most for this seam, because a channel that silently delivers nothing looks exactly like a
    channel with nothing to deliver.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9-]*$",
        description=(
            "The channel's stable key. Must equal its directory name, and it is the enable token "
            "in `CHEMCLAW_DELIVERY_CHANNELS`."
        ),
    )
    description: str = Field(
        min_length=1,
        description="What this channel is, for the operator deciding whether to enable it.",
    )
    driver: str = Field(
        min_length=1,
        pattern=r"^[\w.]+:[A-Za-z_]\w*$",
        description=(
            "`module:callable` building a `DeliveryDriver`, resolved the first time a message is "
            "delivered rather than at import — the late binding the source and sink seams use, "
            "buying the same property: a process that never delivers never imports the client."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keyword arguments the driver is built with. Free-form rather than a typed union: the "
            "callable's own signature is the schema, and `make channel-validate` binds these "
            "against it, so a wrong key fails without a second model to keep in step."
        ),
    )

    @model_validator(mode="after")
    def _config_does_not_shadow_the_name(self) -> Self:
        """Reject a `config:` key the registry itself supplies.

        The builder passes `name=` alongside the config, so a manifest setting it there raises a
        `TypeError` from inside the driver construction naming neither the channel nor the key —
        the same guard `ResultSinkManifest` and `DataSourceManifest` carry, for the same failure.
        """
        if "name" in self.config:
            raise ValueError(
                f"delivery channel {self.name!r} sets 'name' in `config:`, but the registry "
                "supplies it; set the top-level `name:` field instead"
            )
        return self
