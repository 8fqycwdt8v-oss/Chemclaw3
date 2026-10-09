"""The fence that stops a turn whose session claim is no longer its own.

A turn holds its session through a leased claim. A process that stalls past the lease (a
blocked event loop, a stopped container) can wake after another replica took the session over and
resumed the thread; without a fence both would drive one checkpoint. The turn's route builds a
`TurnFence` over its claim, the heartbeat `lose()`s it when a refresh shows the claim gone, and the
tool chain `hold()`s it immediately before a call that is not known to be repeatable. Once lost it
is lost for good: `lose()` cancels the turn's pump, so the turn ends through its ordinary teardown,
which books and settles nothing for it.

Invariant: a call that passes `hold()` had a live claim when it was checked. The check-to-effect
window is one round trip; an effect already issued (a request on the wire, a job started) cannot be
recalled, and a claim that lapses between the check and the effect is not covered.
"""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar


class TurnFence:
    """Ownership of one running turn's session claim, checkable and losable."""

    def __init__(self, owns: Callable[[], Awaitable[bool]], cancel: Callable[[], object]) -> None:
        """`owns` asks the claim store; `cancel` ends the turn (cancels its pump task)."""
        self._owns = owns
        self._cancel = cancel
        self.lost = False

    def lose(self) -> None:
        """Mark the claim gone and end the turn; idempotent."""
        if not self.lost:
            self.lost = True
            self._cancel()

    async def hold(self) -> bool:
        """Whether the claim is still this turn's, now. A store that cannot answer is a no."""
        if self.lost:
            return False
        try:
            owned = await self._owns()
        except Exception:
            owned = False
        if not owned:
            self.lose()
        return owned


_current: ContextVar[TurnFence | None] = ContextVar("chemclaw_turn_fence", default=None)


def set_turn_fence(fence: TurnFence | None) -> object:
    """Make `fence` the running turn's; returns a token for `reset_turn_fence`."""
    return _current.set(fence)


def reset_turn_fence(token: object) -> None:
    """Undo `set_turn_fence` at turn teardown."""
    _current.reset(token)  # type: ignore[arg-type]


def current_turn_fence() -> TurnFence | None:
    """The running turn's fence, or `None` off a fenced path (a CLI, a template step)."""
    return _current.get()
