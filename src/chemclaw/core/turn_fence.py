"""The fence that stops a turn whose session claim is no longer its own.

A turn holds its session through a leased claim. A process that stalls past the lease (a blocked
event loop, a stopped container) can wake after another replica took the session over and
resumed the thread; without a fence both would drive one checkpoint. The turn's route builds a
`TurnFence` over its claim and three things consult it:

- the heartbeat `lose()`s it when a refresh shows the claim gone;
- the tool chain `hold()`s it before a call that is not known to be repeatable, and the model chain
  before a model call;
- the checkpointer takes `Claim` (session, holder) and writes a checkpoint only in a transaction
  that holds a share lock on that claim row (`agent/checkpointer.py`), so no takeover can commit
  between the check and the write.

Lost is lost for good: `lose()` cancels the turn's pump, so the turn ends through its ordinary
teardown, which books and settles nothing for it. Lost is only ever an answer: the store says the
claim is gone, another holder's or lapsed. *Could not ask* and *could not confirm* are not lost:
`hold()` raises `ClaimUnverifiable` (the store failed twice, or the row still names this holder
with less than its margin left because refreshes are failing), the caller refuses the effect it
was about to take, and the turn goes on and is booked as any turn is. A store that failed twice is
not asked again for `quiet_seconds`, so an outage costs one wait, not one per call.

Invariant: a call that passes `hold()` had a claim with a margin of its lease left when it was
checked. The check-to-effect window of a call is one round trip plus that margin; an effect already
issued (a request on the wire, a job started) cannot be recalled. A checkpoint write has no window.
"""

import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import NamedTuple


class Claim(NamedTuple):
    """Which claim a turn holds: the session row and the holder that must still be on it."""

    session_id: str
    holder: str


class ClaimUnverifiable(Exception):
    """The claim store could not be asked twice running; ownership is neither proven nor refuted."""


class TurnFenceLost(Exception):
    """A write found that its turn no longer holds the claim; the turn is ended, not failed."""


class TurnFence:
    """Ownership of one running turn's session claim, checkable and losable."""

    def __init__(
        self,
        claim: Claim,
        owns: Callable[[], Awaitable[bool | None]],
        cancel: Callable[[], object],
        *,
        quiet_seconds: float = 0.0,
    ) -> None:
        """`owns` asks the claim store; `cancel` ends the turn (cancels its pump task).

        `owns` answers `True` (held, with margin), `False` (gone, taken or lapsed) or `None` (still
        this holder's but short of margin: not confirmed).
        """
        self.claim = claim
        self._owns = owns
        self._cancel = cancel
        self._quiet_seconds = quiet_seconds
        self._unreachable_until = 0.0
        self.lost = False

    def lose(self) -> None:
        """Mark the claim gone and end the turn; idempotent."""
        if not self.lost:
            self.lost = True
            self._cancel()

    async def hold(self) -> bool:
        """Whether the claim is still this turn's, now.

        `False` only when the store says it is not (taken over, gone or lapsed): the fence is then
        lost. A store that cannot answer twice, or a claim it cannot confirm, raises
        `ClaimUnverifiable` and loses nothing.
        """
        if self.lost:
            return False
        if time.monotonic() < self._unreachable_until:
            raise ClaimUnverifiable("the claim store did not answer a moment ago")
        for attempt in range(2):
            try:
                owned = await self._owns()
            except Exception as exc:
                if attempt:
                    self._unreachable_until = time.monotonic() + self._quiet_seconds
                    raise ClaimUnverifiable from exc
                continue
            if owned is None:
                raise ClaimUnverifiable("the claim is still this turn's but short of its margin")
            if not owned:
                self.lose()
            return owned
        raise ClaimUnverifiable  # pragma: no cover - the loop returns or raises


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
