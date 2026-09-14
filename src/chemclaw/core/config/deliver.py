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

    # **What one outbound send may spend, and it is not one activity's worth.**
    # `deliver_message_activity` was given `activity_timeout_seconds` (30 s) by copying the shape of
    # `durable/notify.py`'s session push-back, which is one small database insert. This is not that:
    # `registry.deliver` walks the enabled channels **serially**, and the shipped webhook channel's
    # own `timeout_seconds` is 10 s — so three webhook channels can reach 30 s on their own, at
    # which point the activity's whole budget is spent inside the last one. A `start_to_close`
    # expiry is *retryable*, and `activity_max_attempts` is 5, so the retry re-POSTs to every
    # channel that already took the message: duplicate tickets manufactured by a budget that was
    # 300 s a commit earlier, when the only caller was the digest.
    #
    # 300 s rather than a number derived from the channel list, because the derivation would have to
    # read each channel's own `config:` — a per-driver key this model does not know and should not
    # learn. What it buys is room for the serial walk; what bounds a *single* channel stays the
    # channel's own setting, which is where an operator who adds a slow one will look.
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
