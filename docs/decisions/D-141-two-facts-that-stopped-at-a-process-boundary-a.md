# D-141 — Two facts that stopped at a process boundary: a session's profile, and the turn's correlation id

**Status:** accepted · **Context:** REV-14 and REV-11. Both are cases where a fact core knows was
not written down at the edge of the process that knew it — so the next process, or the next hour,
made up a different answer.

### An evicted session silently regained the tools its profile had removed (REV-14)

`_LiveSessions` stores `(session, owner, profile)` and says why in its own docstring: the three "can
never drift", because "the profile decides which agent runs the turn *and* which connectors it gets,
so a session that lost it would silently change agent mid-conversation." The durable
`session_owners` row stored only the owner. So rehydration rebuilt the handle on the **default**
profile, and the code called that graceful:

> the conversation resumes with the full tool surface rather than a narrowed one

That has the direction backwards. A profile is **attenuation only** — `agents/chemclaw_agent.py`
states it twice, "it can only attenuate, never widen" — and `property-lookup` cuts the surface to
four tools, drops every connector but `calc`, and specifically removes the ability to start a
durable job. Coming back with the full surface is not a graceful degradation; it is the control
being switched off. Losing an attenuation is never the safe direction to fail in.

And it never needed a restart, which is how it was framed. The live cache is an LRU with a capacity
and **no TTL**, so on a busy pod session 1001 evicts session 1 while both are in use. A chemist
mid-conversation, having done nothing, regains every tool their profile removed, and nothing
anywhere says so.

**Decision:** persist the profile beside the owner (`infra/sql/021`, a nullable column) and rehydrate
onto it. A column rather than a second table because that row is already "the facts about a session
that must survive the LRU", and the profile is one of them by exactly the argument that put the
owner there. The comment declined this as "a migration in service of a case that degrades
gracefully" — the migration is one `ADD COLUMN IF NOT EXISTS`, and the case does not degrade
gracefully.

`None` has to survive the round trip as `None`: storing `""` for "no profile" would turn every
ordinary session into a request for a profile named empty-string, which `get_profile` rejects. That
is pinned by its own test, because it is the natural way to write this fix.

### The correlation id stopped at the process boundary (REV-11)

`agents.audit` stamps every in-core tool call with a correlation id, and it went no further. Not in
the connector identity headers, so a connector logged under an id of its own with nothing tying the
two records together. Not in `ConnectorJobInput`, so a durable run was an island in the trail. "Show
me everything that happened in this turn" was answerable in core and unanswerable across the four
runtimes a turn actually spans — which is most of what a GxP trail is for.

**Decision:** an `X-Chemclaw-Correlation-Id` header beside the actor, roles and session, and a
`correlation_id` field on `ConnectorJobInput` that becomes a workflow memo beside `requested_by`.

Both follow the shape already established for the actor rather than inventing one. The header is
**advisory, never authorization**, exactly as the module docstring requires of the others: a
connector may join its records to ours on it and must never make an access decision on a header's
word. The job field travels in the *input* because a workflow has no request context — the same
argument that put `requested_by` there — and is set as a **memo** rather than folded into `payload`,
because `payload` is exactly the arguments the model filled in, and metadata the LLM can write is
not metadata.

Absent rather than empty when there is no turn, matching the actor header's rule: off the request
path there genuinely is no correlation, and an empty id in a connector's log reads as one that
exists — the precise confusion this header exists to remove.
