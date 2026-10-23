"""A chemist's standing queries over HTTP: see what they watch for, and stop a watch.

`watch_for` saves a watch from a turn; until these routes a person could neither see nor cancel one
without asking the agent. Owner-scoped by construction: every statement is keyed on the caller's
`oid`, so another person's id is a 404, never a 403. Saving stays a tool: what to watch for is what
a conversation decides.
"""

from datetime import datetime

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from chemclaw.agent import subscriptions
from chemclaw.api.deps import CurrentUser
from chemclaw.core.config import settings


class WatchOut(BaseModel):
    """One standing query and its watermark: only news after `last_seen_at` is reported."""

    id: int
    query: str
    note_type: str | None
    last_seen_at: datetime | None


class WatchesOut(BaseModel):
    """The caller's watches, oldest first, and whether this deployment evaluates any of them.

    `digests_enabled` false means nothing runs a watch (`CHEMCLAW_DIGEST_ENABLED`): the rows are
    kept and will report once an operator turns digests on, so a surface must not promise news.
    """

    watches: list[WatchOut]
    digests_enabled: bool


async def list_watches(principal: CurrentUser) -> WatchesOut:
    """The caller's own standing queries."""
    return WatchesOut(
        watches=[
            WatchOut(
                id=watch.id,
                query=watch.query,
                note_type=watch.note_type,
                last_seen_at=watch.last_seen_at,
            )
            for watch in await subscriptions.for_owner(principal.oid)
        ],
        digests_enabled=settings.digest_enabled,
    )


async def stop_watch(watch_id: int, principal: CurrentUser) -> Response:
    """Stop one of the caller's watches. 404 for an id the caller does not hold."""
    if not await subscriptions.remove_by_id(principal.oid, watch_id):
        raise HTTPException(status_code=404, detail="no such watch")
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only."""
    app.get("/watches")(list_watches)
    app.delete("/watches/{watch_id}", status_code=204)(stop_watch)
