# D-2026-10-04-an-upload-is-session-state-not-pod-state — uploads live in Postgres beside the session, not in the pod that took them

**Status:** accepted · **Date:** 2026-10-04

## Context

A chemist's uploaded file (`POST /sessions/{id}/attachments`) was parsed and kept in
`agent/attachments.STORE`, a dict in the memory of the front-door process that received the upload.
The two tools that read it (`list_attachments`, `read_attachment`) looked it up by the turn's session
id in *their own* process's dict. Everything else a later turn needs is already readable from any
replica — the ownership row (`session_owners`) rehydrates the session, the checkpointer holds the
thread, `session_turns` is the turn claim — so the attachment store was the one piece of
conversation state pinned to a pod. The Route's `disable_cookies: "false"` annotation said so in as
many words: "the way to remove this is to give attachments a durable home".

Route affinity was never enough, because not every caller comes through the Route. The companion
UI's BFF reaches the backend at `chemclaw-service:8080`, the ClusterIP Service, where there is no
cookie to stick to; `Chemclaw3_ui`'s `docs/operations.md` §4 documents exactly this gap.

## What was measured

Two separate Python processes, each running `create_app()` with `CHEMCLAW_SESSION_STORE=postgres` on
one migrated schema — two replicas with nothing between them but the database:

| step | replica | answer |
|---|---|---|
| `POST /sessions`, `POST /sessions/{id}/attachments` (`runs.csv`) | A | 200, 200 |
| a session-scoped `GET` on the same session | B | **200** — the session rehydrates |
| `list_attachments()` with the session bound | B | `[]`, verdict **"COMPLETE: every file uploaded to this conversation is listed"** |
| `read_attachment("runs.csv")` | B | **"no attachment named 'runs.csv' in this conversation"** |

So the failure is not "the file is unavailable"; it is a false statement, with a verdict field
vouching for it, about a file the chemist uploaded to that conversation. With N replicas behind a
Service, a turn lands on the uploading pod with probability 1/N.

## Options

1. **Keep affinity; make the BFF sticky too.** Route by session in the BFF, or put a sticky Service
   (`sessionAffinity: ClientIP`) in front. Every hop that balances would have to know, ClientIP
   affinity collapses to one pod behind the BFF (one client IP), and a pod restart or a scale-down
   still loses every file it held. It moves the defect rather than removing it.
2. **The calculation artifact store** (`artifact_blobs`/`calculation_artifacts`, D-124). It is the
   existing durable byte store and was the first candidate. It does not fit, on three measurable
   axes: it is keyed by a `CalculationKey` and addressed by content hash, so two chemists uploading
   the same file share one blob, which is a cross-session link this system would then have to
   refuse to erase (the `tool_result_blobs` anti-join in `agent/leaver._ERASE` is the cost of that
   shape, written once already); its eviction (`durable/artifact_eviction.py`) is ordered by
   `compute_seconds` — the cost of regenerating a by-product — which an upload does not have and
   cannot be regenerated from; and it has no session or actor column, so neither the session gate,
   a session delete nor an erasure can reach a row. Adding those would be a second table anyway.
3. **An object store** (S3/ODF bucket). New infrastructure, a credential, an egress or in-cluster
   dependency, and a second place erasure and retention would have to reach — for files whose parsed
   text is bounded at `attachment_store_max_bytes` per session and is already what every read needs.
4. **A session-scoped Postgres table, shaped as `session_exhibits` is (115).** Same database as the
   transcript (`session_store_dsn or postgres_dsn`), so a session delete is one transaction; no
   foreign key to `session_owners`, so the three disposals are code's and the retention sweep holds
   the ownership row back while one remains; and the per-session rule the in-memory store already
   enforced, unchanged.

## Decision

**Option 4.** Migration `120_session_attachments.sql` adds `session_attachments`, and
`agent/attachments.py` grows the store shape `exhibits/store.py` has: an `AttachmentStore` protocol,
`InMemoryAttachmentStore` (what a deployment without durable sessions runs) and
`PostgresAttachmentStore`, chosen by `default_attachment_store()` on `settings.session_store`.

- **The parsed text is stored, not the uploaded bytes.** It is what every read needs; storing bytes
  would keep a second, larger copy nothing reads, and re-parsing on read would put an
  untrusted-document parse on the turn path.
- **The per-session bounds are unchanged and written once** (`_uploads_to_drop`): past
  `attachment_max_per_session` files or `attachment_store_max_bytes` bytes the oldest are dropped,
  never the upload just made. In Postgres a dropped upload keeps its row with `body` NULL, so the
  "was uploaded and then dropped, NOT never sent" sentence is answered by whichever replica serves
  the next turn. The bound is serialized per session by an advisory transaction lock; without it,
  measured, concurrent uploads left the session over its cap in five runs of five.
- **`attachment_store_max_bytes` keeps both meanings it can still have.** It is the per-session byte
  bound in both stores; only the in-memory store also reads it as the cross-session budget, which
  bounded a pod's memory, and nothing the durable store holds is resident.
- **Authorization is unchanged, because it was never the store's.** The upload route is behind
  `resolve_session` (participants only, 404 to anybody else) and the tools read only the turn's bound
  session; the store is never handed a client's claim. `uploaded_by` records the principal so an
  erasure reaches a member's upload in a session somebody else owns, as `session_messages.actor`
  does for their words.
- **Disposal follows the conversation.** `delete_session` deletes the session's rows; `erase_actor`
  deletes by session for the leaver's own sessions and by `uploaded_by` elsewhere; the retention
  sweep prunes on the **conversation's window** (`retention_session_messages_days`), dated by
  `created_at`. A window of its own is declined: it could only equal the conversation's (no effect),
  exceed it (files no remaining transcript refers to) or fall short (a transcript referring to a file
  it can no longer read).

Route affinity stays, for a different reason that this ADR does not change: a *running* turn's event
pump is per process, so `GET …/turn/stream` and `POST …/turn/stop` answer 404 on a sibling pod. The
annotation's comment now says that instead.

## Declined

- **Affinity alone (option 1)** is declined as the fix: it cannot cover a caller that is not a
  browser on the Route, and it loses every file on a pod restart.
  Revisit when: never as the fix for uploads; the turn-stream half is a separate question.
- **The calc artifact store (option 2)** is declined for uploads.
  Revisit when: that store gains a session and actor scope of its own, which would make it a general
  session byte store rather than a calculation cache.
- **An object store (option 3)** is declined.
  Revisit when: a deployment needs to keep the *original bytes* of an upload (a signed CoA, an
  instrument file), which this design deliberately does not, or `session_attachments` shows up as a
  material share of the session database in `pg_total_relation_size`.
- **A retention window of its own** is declined.
  Revisit when: a deployment states a need to keep uploads longer or shorter than the conversation
  they were handed to.

## What keeps it true

- `tests/test_attachments.py::test_an_upload_taken_by_one_replica_is_read_by_another` — uploads
  through the front door, asserts nothing went to this process's memory, then reads the file from a
  second interpreter on the same database.
- `tests/test_attachments.py::test_a_stranger_cannot_upload_into_or_read_from_somebody_elses_session`
- `tests/test_attachments.py::test_concurrent_uploads_to_one_session_do_not_overrun_its_bound`
- `tests/test_attachments.py::test_an_erasure_takes_the_leavers_uploads_in_their_sessions_and_in_others`
- `tests/test_attachments.py::test_the_retention_sweep_ages_out_uploads_on_the_conversations_window`
- `tests/test_retention.py` and `tests/test_leaver.py` hold `session_attachments` in the session-scoped
  sets that decide disposal.
