"""Per-session and per-user turn/token budgets — the runaway-cost guard (plan F-budget).

A single agent turn is iteration-capped (`harness_max_loop_iterations`), so one turn cannot loop
forever. But nothing caps the *number* of
turns, so a client — or an automated job→session push-back loop re-waking a session — could keep
posting turns and accumulate unbounded LLM spend. This tracker is the missing ceiling above the
per-turn loop cap: the front door meters each turn's reported token usage and counts turns per
session and per user, and refuses (HTTP 429) a turn that would exceed a configured cap.

**One turn cannot loop forever; one turn can spend without a bound, and this module cannot see it.**
That sentence used to read "so one turn cannot loop forever" with the *therefore* left implicit, and
the implication is false: an iteration is not a unit of cost. `check()` runs before a turn against
usage already booked and `record()` books a turn after it ended, so the single thing neither half
observes is a turn spending while it runs — measured 2026-09-06 at **250,000 tokens in one turn
against a 1,000-token session cap**, refused only on the turn after. That is the "$400 in twenty
minutes" failure this module was written against, arriving through the door it left open, and it is
`agent/spend_cap.py` that closes it: a per-turn ceiling enforced in `before_model` and metered off
the response, configured by `agent_max_turn_billed_tokens`. **It no longer ships at 0.** This
paragraph said it did and concluded in bold that "nothing bounds a single turn's spend" — true of
an earlier default and false of this one, which is
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`. A single turn's spend is bounded now,
loosely: a runaway backstop *derived* from what the loop cap and the context budget already
authorise, never a cost budget, and not a figure this file holds
(`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` and
`D-2026-09-18-a-cap-below-an-ordinary-turn-is-a-guard-that-kills-another`, which corrects the
number that ADR chose). What remains true is the reason this module's own caps are not the answer:
read them as bounding a *sequence* of turns, never one of them.

**The per-user ceiling is durable since
`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`; the per-session one is not, deliberately.**
This paragraph used to say the counters "reset on restart" and call a durable rolling window a
conscious deferral, which was true and is the failure it described: a cap read as "this user may
spend 20,000,000 tokens" in fact meant "per pod, between restarts", and a three-replica deployment
with a nightly roll multiplies it by the replica count and again by the roll. `api/budget_store.py`
now backs the *user* scope with one Postgres row per principal on a rolling window, engaged wherever
`session_store == "postgres"` — the same switch the audit sink and the turn-cost ledger read, rather
than a flag beside it. A session stays in-process because a session dies with the process that holds
it, so a durable counter for one would outlive the thing it meters.

**The two halves are combined with `max()`, not replaced**, and that is what keeps the guard correct
across the fire-and-forget write below: the in-process counter has this pod's just-ended turn
immediately, the durable row has every pod's turns a moment later, and neither is a superset of the
other. `agent/spend_cap.py` reads its own two sources the same way and for the same reason.

Off by default (`budget_enabled`), so a deployment opts in.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

# Strong references to the in-flight durable writes, for the reason `agent/turn_cost.py` holds the
# same set: `create_task` alone keeps no reference, so a write scheduled off a teardown can be
# garbage-collected mid-statement and lose the booking silently.
_PENDING: set[asyncio.Task[None]] = set()


class BudgetExceeded(Exception):
    """A turn is refused because it would exceed a session or user budget (maps to HTTP 429).

    Deliberately not a `ChemclawError`: this is a capacity/policy refusal (like admission control),
    not bad input, so it must never be swallowed by a reject-and-continue boundary.
    """


@dataclass
class _Counter:
    """Turns and metered tokens booked against one scope (a session or a user) in one window."""

    turns: int = 0
    tokens: int = 0
    #: Monotonic start of the window these counts belong to, for the *user* scope only — a session
    #: counter carries one too and nothing reads it, because `_book` only rolls what it is told to.
    started: float = field(default_factory=time.monotonic)


def _rolled(counter: _Counter) -> bool:
    """Whether this counter's window has expired, so its counts no longer bind.

    **The in-process half has to roll on the same clock as the durable row, or `max()` stops being
    a floor and becomes a ratchet.** `check` takes the larger of the two counts; the durable row
    rolls (`budget_store._BOOK` resets it in place past `budget_window_hours`) and before this
    function the in-process counter never did. So a pod that stayed up across a boundary pinned the
    principal at their *lifetime* spend for ever: measured, one tracker that had booked 900 tokens
    still refused against a 500-token cap after the durable row had correctly rolled to (0, 0),
    while a freshly built tracker admitted the same turn. That inverts this feature's own premise —
    a restart became the only thing that handed the allowance *back*, and on three replicas the
    same request was a 429 on one pod and a 200 on the next.
    """
    return time.monotonic() - counter.started >= settings.budget_window_hours * 3600.0


def _over(cap: int, used: int) -> bool:
    """Whether `used` has reached `cap`, treating a cap of 0 as unlimited."""
    return cap > 0 and used >= cap


def _near(cap: int, used: int) -> bool:
    """Whether `used` has reached the warning fraction of `cap` without reaching `cap` itself.

    Both ends are excluded deliberately. A cap of 0 is unlimited, so there is nothing to approach;
    and usage at or past the cap is the *refusal's* business, which says more than a warning would
    and says it to the caller rather than only to a log.
    """
    fraction = settings.budget_warn_fraction
    if cap <= 0 or fraction <= 0:
        return False
    return used >= cap * fraction and used < cap


def _durable() -> bool:
    """Whether the per-user window is backed by Postgres.

    Derived from the store the deployment already chose rather than from a flag of its own — the
    argument `core/config/service.py` records beside `budget_window_hours`.
    """
    return settings.session_store == "postgres"


def _warn(scope: str, identity: str, turns: int, tokens: int, booked: int) -> None:
    """Say so on the one turn that carries a scope's usage across the warning fraction of a cap.

    **`identity` is on the line because it is the only place it can be.** The alert this feeds is
    deliberately unlabelled — a session id or an Entra `oid` on a metric series is unbounded
    cardinality, the argument `033_cost_attribution.sql` makes — so the log is an operator's one
    route from "somebody is near their cap" to "who". The runbook, the alert annotation and
    `core/metrics.py` all said in the present tense that the line carried it, and it carried the
    scope *kind* ("session", "user") instead, which no `grep` can turn into a principal.

    Called from `record`, never from `check`: the front door checks twice per turn (a fast path
    before the admission permit and the binding one after it), so warning from `check` would double
    every count and every log line for a fact that changed once.

    **Edge-triggered, and it has to be, because the band is wide.** `_near` is a predicate over a
    running total with no memory, so warning whenever it holds warns on *every* turn spent between
    the fraction and the cap. Measured at a 1,000-token cap: one crossing produced **ten** warnings
    over ten turns. At the shipped user cap that band is 80%–100% of 20,000,000 tokens — some 130
    turns of WARNING lines, and 130 increments of a counter
    `deploy/helm/chemclaw/templates/prometheusrule.yaml` rules a `for:` clause out of on the
    explicit ground that "a crossing is a **step**, not a rate ... a single crossing never produces
    a repetition". That premise is only true of an edge. `booked` (this turn's own tokens, and one
    turn) is what makes the previous total derivable without holding any state per principal.
    """
    caps = (
        ("turns", turns, turns - 1, _cap(scope, "turns")),
        ("tokens", tokens, tokens - booked, _cap(scope, "tokens")),
    )
    for unit, used, before, cap in caps:
        if not _near(cap, used) or _near(cap, before):
            continue
        record_metric(lambda m: m.increment("chemclaw_budget_warnings_total"))
        logger.warning(
            "%s %s budget %.0f%% spent (%d of %d) for %s — the next turns will be refused at "
            "the cap",
            scope,
            unit,
            100 * used / cap,
            used,
            cap,
            identity,
        )


def _cap(scope: str, unit: str) -> int:
    """The configured cap for one scope and unit, so the warning and the refusal read one source."""
    if scope == "session":
        return (
            settings.budget_max_turns_per_session
            if unit == "turns"
            else settings.budget_max_tokens_per_session
        )
    if unit == "turns":
        return settings.budget_max_turns_per_user
    return settings.budget_max_tokens_per_user


def _book(
    counters: BoundedLru[str, _Counter], key: str, tokens: int, *, rolls: bool = False
) -> _Counter:
    """Add one turn and its (non-negative) tokens to `key`, evicting the LRU past capacity.

    `rolls` starts a fresh window when the existing counter's has expired, and is passed for the
    *user* scope only: that is the scope the durable row windows, and the two halves are combined
    with `max()`, so a half that never rolled would hold the other one up for ever (`_rolled`).
    A session is not windowed deliberately — `budget_max_turns_per_session` bounds one conversation
    rather than a rate, and rolling it would quietly hand a long-running session a second allowance
    that nobody configured.

    The map itself is `chemclaw.core.bounded.BoundedLru` (S2) — the tracker lives for the pod's
    whole lifetime, and without a bound every session/user ever seen would keep a counter (a slow
    memory leak in the long-lived front door). Eviction resets that scope's in-process budget, which
    for a user is now only half the accounting: the durable row survives both the eviction and the
    restart. Capacity is a live config read, so the caps stay ENV-overridable.

    Returns the updated counter so the caller can warn off it without a second lookup.
    """
    counter = counters.get(key)
    if counter is None or (rolls and _rolled(counter)):
        counter = _Counter()
    counter.turns += 1
    counter.tokens += max(tokens, 0)
    counters.put(key, counter)
    return counter


class BudgetTracker:
    """Meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses (pre-turn) a turn that would breach a cap; `record` books a completed turn's
    turn-count and token usage. Neither runs *inside* a turn — see the module docstring for what
    that means and which guard covers it. A lock guards the in-process counters because the ASGI
    server runs turns for different sessions concurrently. `check` and `record` are separate calls,
    so a bounded number of in-flight turns may pass `check` before any of them `record` — an
    overshoot acceptable for a best-effort guard, not an exact accountant. **That bound is a
    property of where `check` is
    called, not of this class**, and it was false until the front door re-checked *after* taking an
    admission permit: checking only at request entry made the overshoot the number of concurrent
    requests instead (measured: 40 turns against a 1-turn cap with 8 permits). It is
    `service_max_concurrent_turns` plus however many turns are running *detached*, which give their
    permit back at the disconnect and keep spending — not the flat `service_max_concurrent_turns`
    this paragraph used to name. See `chemclaw.api.routes.turns.post_message`. Both counter maps
    are LRU-bounded (sessions by `service_max_live_sessions` — a budget counter lives as long as the
    live session it meters can — users by `budget_max_tracked_users`), so the tracker never grows
    unbounded in the long-lived front door.

    **`check` is async and `record` is not, and the asymmetry is forced rather than chosen.**
    `record`'s one production caller is `api/runner._book_turn_spend`, which runs from a
    `finally`-driven teardown where an `await` re-raises a pending cancellation and skips whatever
    follows it (D-130) — the same constraint that makes `agent/turn_cost.record_turn_cost`
    synchronous by contract, and this schedules its durable write exactly as that one does. `check`
    has no such caller: both sites are in `api/routes/turns.py`, one in an `async def` and one in an
    async generator, so it can read the durable row before admitting a turn.
    """

    def __init__(self) -> None:
        """Start with empty, LRU-bounded per-session and per-user counters."""
        self._sessions: BoundedLru[str, _Counter] = BoundedLru(
            lambda: settings.service_max_live_sessions
        )
        self._users: BoundedLru[str, _Counter] = BoundedLru(
            lambda: settings.budget_max_tracked_users
        )
        self._lock = threading.Lock()

    async def check(self, session_id: str, user: str | None) -> None:
        """Raise `BudgetExceeded` if the next turn would exceed a session or user cap.

        No-op when `budget_enabled` is off. Checked against usage *already booked*, so the first
        turn that reaches a cap is the one refused (a cap of 100 allows 100 turns, refuses no. 101).

        The user scope reads the durable window where one is configured and takes the larger of the
        two counts, for the reason the module docstring gives. A database that cannot be reached
        degrades to the in-process count rather than failing the turn: refusing a chemist's question
        because the meter is unreachable would make the guard an outage amplifier, and the
        in-process half still bounds this pod.
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            local = self._users.get(user) if user is not None else None
        self._check_scope(
            session,
            "session",
            settings.budget_max_turns_per_session,
            settings.budget_max_tokens_per_session,
        )
        if user is None:
            return
        # A counter whose own window has expired reads as zero rather than as a floor under the
        # durable row, which is what `_rolled` exists to stop.
        live = local is not None and not _rolled(local)
        turns, tokens = (local.turns, local.tokens) if live and local is not None else (0, 0)
        if _durable():
            stored_turns, stored_tokens = await self._stored(user)
            turns, tokens = max(turns, stored_turns), max(tokens, stored_tokens)
        self._check_scope(
            _Counter(turns=turns, tokens=tokens),
            "user",
            settings.budget_max_turns_per_user,
            settings.budget_max_tokens_per_user,
        )

    @staticmethod
    async def _stored(user: str) -> tuple[int, int]:
        """This principal's durable window, or zeros if the database cannot answer."""
        from chemclaw.api import budget_store

        try:
            return await budget_store.usage(user)
        except Exception:
            degraded(
                logger,
                "budget_window",
                "could not read the durable budget window for %s; bounding on this pod's counters "
                "alone, which a restart or an eviction has reset",
                user,
            )
            return 0, 0

    @staticmethod
    def _check_scope(counter: _Counter | None, scope: str, max_turns: int, max_tokens: int) -> None:
        """Refuse if this scope's booked turns or tokens have reached either cap."""
        if counter is None:
            return
        if _over(max_turns, counter.turns):
            raise BudgetExceeded(f"{scope} turn budget exhausted ({counter.turns} turns)")
        if _over(max_tokens, counter.tokens):
            raise BudgetExceeded(f"{scope} token budget exhausted ({counter.tokens} tokens)")

    def record(self, session_id: str, user: str | None, tokens: int) -> None:
        """Book one completed turn and its metered tokens against the session and the user.

        No-op when `budget_enabled` is off. A failed turn is still booked — it consumed tokens up to
        the failure, so its cost must count toward the next `check`.

        Synchronous by contract (see the class docstring). The in-process counters are booked here
        and now, so the very next `check` in this pod sees this turn even if the durable write has
        not landed; the durable write is scheduled as its own task and warns off what it returns.
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            session = _book(self._sessions, session_id, tokens)
            local = _book(self._users, user, tokens, rolls=True) if user is not None else None
        booked = max(tokens, 0)
        _warn("session", session_id, session.turns, session.tokens, booked)
        if user is None:
            return
        if not _durable():
            if local is not None:
                _warn("user", user, local.turns, local.tokens, booked)
            return
        self._schedule(user, tokens)

    @staticmethod
    def _schedule(user: str, tokens: int) -> None:
        """Book the durable window off the hot path, warning off the totals it returns.

        A window that cannot be written is logged and lost, which is the same trade
        `agent/turn_cost.record_turn_cost` states: failing a turn that already answered, in order to
        meter what it cost, would be the tail wagging the dog. The in-process counter still holds
        this turn, so the loss is cross-pod visibility of one turn rather than of the budget.
        """
        from chemclaw.api import budget_store

        async def _write() -> None:
            try:
                turns, tokens_spent = await budget_store.book(user, tokens)
            except asyncio.CancelledError:
                # **Separately, and before the `Exception` arm, because it is not one.** A
                # fire-and-forget task's dominant loss mode is cancellation — an ASGI shutdown, a
                # rollout, `asyncio.run`'s `_cancel_all_tasks` — and `except Exception` does not
                # catch it. So the loss this module's docstring calls "logged and lost" was, for
                # the case that happens on every deploy, *silently* lost: measured, a `record()`
                # followed by loop shutdown wrote no row and emitted no line at all, while the
                # degradation counter that exists to make exactly this visible stayed flat.
                degraded(
                    logger,
                    "budget_window",
                    "the durable budget booking for %s was cancelled before it landed; this turn "
                    "is counted on this pod only",
                    user,
                )
                raise
            except Exception:
                degraded(
                    logger,
                    "budget_window",
                    "could not book a turn against the durable budget window for %s; this turn is "
                    "counted on this pod only",
                    user,
                )
                return
            _warn("user", user, turns, tokens_spent, max(tokens, 0))

        try:
            task = asyncio.get_running_loop().create_task(_write())
        except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
            logger.debug("no event loop to book the durable budget window for %s", user)
            return
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


class ThreadTooLong(BudgetExceeded):
    """A turn refused because its session's stored conversation is at `session_max_thread_bytes`.

    A `BudgetExceeded` so every handler of a spent session budget answers it the same way, and its
    own class so it is counted apart: the token budget's counter drives an alert whose remedy —
    read `chemclaw_tokens_total`, raise the window — does nothing for a conversation that is simply
    too long to load.
    """


def refused_metric(exc: BudgetExceeded) -> str:
    """The counter a refused turn is booked on, by which budget refused it."""
    if isinstance(exc, ThreadTooLong):
        return "chemclaw_turns_refused_thread_size_total"
    return "chemclaw_turns_refused_budget_total"


async def check_thread_size(session_id: str) -> None:
    """Raise `ThreadTooLong` if this session's stored conversation is at its size ceiling.

    **A session budget in the unit that kills the pod.** Every turn loads the whole thread, so what
    an admitted turn costs the front door grows with the conversation it continues — measured at
    up to 18 bytes of pod per stored byte, and twelve permits on 10 MB threads OOM-killed a 1Gi
    front door (`D-2026-09-24-a-turn-costs-the-thread-it-loads`). The turn caps cannot bound that:
    they count in process, so a restart or another replica starts a thread's count again. This
    reads the stored thread itself, which every replica sees.

    Not behind `budget_enabled`, because it is a memory bound rather than a cost one — a deployment
    that meters no spend still runs twelve permits in one container. Called twice, like
    `BudgetTracker.check`: at request entry, so a spent thread gets a clean 429 before it takes a
    claim or queues for a permit, and after the permit, which is the check that binds and the one
    that guarantees a refused turn never loads what it was refused for.

    A database that cannot answer admits the turn: the load that follows reads the same database,
    so refusing here would add an outage rather than prevent one.
    """
    cap = settings.session_max_thread_bytes
    if not cap:
        return
    from chemclaw.agent.checkpointer import stored_thread_bytes

    try:
        stored = await stored_thread_bytes(session_id)
    except Exception:
        degraded(
            logger,
            "thread_size",
            "could not read the stored size of session %s; admitting its turn unbounded",
            session_id,
        )
        return
    if stored >= cap:
        raise ThreadTooLong(
            f"This conversation has reached its size limit ({stored / 1024**2:.1f} MiB stored, "
            f"against {cap / 1024**2:.1f} MiB). Start a new session to continue — this one's "
            "transcript stays readable."
        )


async def drain_pending(timeout: float = 5.0) -> None:
    """Wait for the in-flight durable bookings, so an orderly shutdown does not drop them.

    Called from the front door's lifespan after the turns have drained. Without it every rollout
    loses the last booking of every in-flight principal — which is not one lost metric but a
    quantum of allowance handed back, and handing allowance back on restart is the defect this
    whole window exists to remove. A bounded wait rather than an unbounded one: the pod is inside
    its termination grace, and a booking is worth waiting a moment for and never worth holding a
    rollout open for.
    """
    if not _PENDING:
        return
    pending = tuple(_PENDING)
    _, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        degraded(
            logger,
            "budget_window",
            "%d durable budget booking(s) did not land within %.1fs of shutdown",
            len(still_running),
            timeout,
        )
