"""Delivery channels: where a message may leave for a person.

The counterpart of `publish.py`, which says where a computed *record* goes.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class DeliverySettings(BaseSettings):
    """Where a digest, a report or an escalation may be delivered, and whether any may be."""

    # `PATH`-style list defaulting to the channels shipped in the package; discovering a channel
    # enables nothing.
    delivery_channels_dir: str = Field(
        default_factory=lambda: _shipped("deliver", "channels"),
        description="`PATH`-style list of directories holding `channel.yaml` folders.",
    )
    # Empty by default: this is the switch for outbound delivery. Unlike connectors, discovery is
    # not enablement, because a channel sends something out of the building.
    delivery_channels: str = Field(
        default="",
        description="Comma-separated channel names to enable. Empty means deliver nothing.",
    )

    # Budget for one outbound send across all enabled channels, which `registry.deliver` walks
    # serially; a too-small budget expires mid-walk and the retry re-sends to channels that already
    # took the message. A single channel is bounded by its own `timeout_seconds`.
    delivery_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Wall clock for one outbound send across every enabled channel, serially.",
    )

    @property
    def delivery_channels_dirs(self) -> list[str]:
        """The discovery path, split and stripped."""
        return [part.strip() for part in self.delivery_channels_dir.split(":") if part.strip()]

    @property
    def delivery_channel_list(self) -> list[str]:
        """The enabled channel names, in the order the operator named them."""
        return [part.strip() for part in self.delivery_channels.split(",") if part.strip()]
