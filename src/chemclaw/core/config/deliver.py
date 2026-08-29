"""Delivery channels — where a message may leave for a person (F7).

One domain section of the composed ChemClaw `Settings`. The counterpart of `publish.py`: that one
says where a computed *record* goes, this one says where a *message* goes.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class DeliverySettings(BaseSettings):
    """Where a digest, a report or an escalation may be delivered, and whether any may be."""

    # Same `PATH`-style list every other seam uses, defaulting to what ships inside the installed
    # package (D-148) so a fresh checkout discovers the two first-party channels without
    # configuration — discovering them, and enabling nothing.
    delivery_channels_dir: str = Field(
        default_factory=lambda: _shipped("deliver", "channels"),
        description="`PATH`-style list of directories holding `channel.yaml` folders.",
    )
    # **Empty by default, and this is the knob that turns outbound delivery on at all.** Unlike the
    # connector registry, discovery is deliberately *not* enablement here: a discovered connector
    # serves a tool, and a discovered channel sends something out of the building.
    delivery_channels: str = Field(
        default="",
        description="Comma-separated channel names to enable. Empty means deliver nothing.",
    )

    @property
    def delivery_channels_dirs(self) -> list[str]:
        """The discovery path, split and stripped."""
        return [part.strip() for part in self.delivery_channels_dir.split(":") if part.strip()]

    @property
    def delivery_channel_list(self) -> list[str]:
        """The enabled channel names, in the order the operator named them."""
        return [part.strip() for part in self.delivery_channels.split(",") if part.strip()]
