# D-2026-08-10-basestore-is-not-where-this-systems-memory-lives — BaseStore is not adopted; the memory package emits notes, not rows

**Status:** accepted
**Context:** phase M11 of the LangGraph rebuild
(`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`)

## Decision

**LangGraph's `BaseStore` is not adopted.** Nothing under `src/chemclaw/memory/` is migrated to it,
and neither are `user_preferences`, `subscriptions` or `observations`. M11 closes having built
nothing, which is the outcome the investigation earned.

This does not forbid `BaseStore` forever. It records that the phase's premise did not survive
contact with what `chemclaw/memory/` actually is, and states what would have to be true — and what
would have to be built alongside it — before adopting it becomes the right call.

## Why the phase was planned

The migration plan said: "Map `chemclaw/memory/` onto LangGraph's `BaseStore` (namespaced, semantic
search) over the pgvector instance already deployed. Cross-session recall stops being
chemclaw-specific plumbing and becomes a store the graph reads natively."

That sentence contains the error. It assumes `chemclaw/memory/` is *plumbing for cross-session
recall*. It is not.

## What `chemclaw/memory/` actually is

Fourteen modules, of which **one** touches a database. `chains.py`, `campaign.py`, `playbook.py`,
`optimization.py`, `progression.py`, `similarity.py`, `ids.py`, `supersede.py`, `failure.py`,
`interaction.py` and `jobs.py` are pure functions that read reactions and *emit Markdown notes*.
Their output goes to Git through the PR-gate, where a human merges it. The package's own README
says so in one line: "Nothing here writes to the graph directly."

A key-value store has nothing to hold here. There is no cross-session recall being hand-plumbed;
there is a note synthesiser whose product is a pull request.

The one module that does persist rows is `observations.py`, and it exists *because* its contents
are explicitly not knowledge (D-161). That is the opposite of a reason to move it somewhere with
fewer guarantees.

## The four things `BaseStore` cannot express

Each of these is enforced somewhere today, and each would be lost or would have to be rebuilt.

**1. The PR-gate.** `propose_note` → git branch → human merge is D-005, and it is the GxP line the
whole architecture is arranged around. `BaseStore` has `put`. There is no analogue of "this write
is a *proposal*", so any memory routed through it stops being proposable.

**2. Bi-temporal retirement.** `valid_from`/`valid_to`, `Note.is_current`, `supersede_updates`,
`close_refuted_note`. `supersede.py` states the rule: a superseded note "gets `valid_to` set … never
deleted — it stays in Git and remains reachable by id." `BaseStore` offers `put` (overwrite in
place) and `delete` (destroy). Both are what the GxP line forbids.

**3. Derived, tamper-resistant support.** `Observation.support` is `len(evidence_note_ids)`, and the
self-confirming-loop guard is a **database CHECK** — `observations_evidence_is_merged_notes` refuses
evidence naming an `observation-` id. In an opaque `jsonb` value that becomes a field the agent
writes, and the constraint is unimplementable.

**4. The audit trail.** This is the sharpest one and it is easy to miss. Chemclaw's authorization,
dry-run refusal, repeat guard and GxP audit record all key on **tool names** — they are
`wrap_tool_call` middleware. A `BaseStore` handed to `create_agent` is reachable from any node and
any middleware, and *a store write is not a tool call*. It would pass through none of the six
wrappers, including `audit._recording`. A memory surface the audit trail cannot see is not a memory
surface this system can have.

## What it would duplicate

**A second pgvector surface, worse than the one that exists.** `note_index` is 1536-dimensional
`hnsw vector_cosine_ops` *plus* a GIN `tsvector`, fused by reciprocal rank fusion, with an
`embedding_key` column that heals itself when the embedding model changes and a nightly rebuild.
`store_vectors` would have no lexical half, no fusion, and no `embedding_key` — which is precisely
the defect `039_note_index_embedding_key.sql` was written to close, where a model swap left every
stored vector byte-identical and a query scored its exact match at cosine 0.0000.

**A second embedding entry point that blocks the loop.** `IndexConfig.embed` accepts a sync
callable, so it would take `core.embeddings.embed_texts` — and call it from inside the store's own
batch task, bypassing the `asyncio.to_thread(embed_texts, …)` offload that six call sites in this
tree standardised on. Under `openai_compatible` that is a network call on the turn's event loop.

**A second migration ledger and a hole in the grant matrix.** `store_migrations` sits beside
`schema_migrations`, unseen by `tests/test_schema_inventory.py`. Worse,
`tests/test_database_privileges.py` derives the expected grants *from SQL literals in `src/`* and
fails in both directions; a table whose SQL lives in site-packages would either be ungranted (an
outage on first use) or need a hand-written exception the derivation cannot check.

## The finding that decided it: the GDPR check would go green while erasure failed

M6 hit this trap with the checkpointer and caught it. Here it is worse.

`agent/leaver.py::_ERASE` erases a departing person's rows, and `tests/test_leaver.py` guards it
with a **derived** check: every column in `information_schema.columns` whose name is in
`{actor, owner, holder, requested_by, decided_by, opened_by}` must be accounted for. That is what
makes the sweep hard to forget.

`store`'s columns are `prefix, key, value, created_at, updated_at, expires_at, ttl_minutes`. **None
of them is an actor column.** So the derived test would pass, report nothing missing, and a
departing person's memories would remain. The checkpoint tables at least had `thread_id` to join
through; this has a string convention and nothing else.

Scoping erasure would mean `WHERE prefix LIKE 'memories.' || actor || '%'` — an escaping-sensitive
match over a convention, replacing an FK-shaped join. And the namespace design would have to put
the actor at position 0 *before the first row is written*, because a namespace like
`("memories", project, oid)` cannot be prefix-matched by actor at all.

A safety net that returns a false green is worse than no safety net, because it is trusted.

## What was actually missing, and what it would cost

One thing: **a cross-session scratchpad the agent does not have today** — an arbitrary durable memo
outside the PR-gate and the two typed stores. That absence is real. It is also deliberate: it is
what D-005 exists to make hard.

So `BaseStore` is not rejected on capability. It is rejected because adopting it *for the work M11
named* would migrate things that must not move, and adopting it for the one thing genuinely absent
is a **product decision about whether agents may write ungated durable memory** — not a migration
step, and not one to take as a side effect of porting a framework.

If that decision is ever taken, this ADR is the list of what must ship with it: the actor at
namespace position 0, `store` and `store_vectors` in `_ERASE` via `leaver._existing_tables` (never a
`to_regclass` guard — Postgres resolves the relation at parse time), a **table-level** erasure
allow-list because column derivation cannot see this one, rows in the grants file, `IndexConfig.embed`
wired through `core.embeddings` with the `to_thread` offload preserved, and an answer for how a
store write reaches the audit trail.

## Consequences

- M11 closes with no code. `tasks/todo.md` records the reasoning; this ADR is the record.
- The memory layers are unchanged: episodic campaigns and optimization series, semantic playbooks,
  interactions and failure modes — all Markdown behind the PR-gate — plus ungated observations,
  preferences and subscriptions in their own tables with their own policy.
- `create_agent(store=…)` stays unset in `agent/langgraph_agent.py`. That is now a decision with a
  reason rather than an omission.
- The plan's claim that this phase would delete "chemclaw-specific plumbing" is withdrawn. There was
  no plumbing to delete; there was a synthesiser and three typed stores, each carrying policy.

## What this does not supersede

`D-019` (memory layers add no new infrastructure, only note types), `D-161` (the human gate moves
from every observation to promotion), `D-005` (the PR-gate) and `D-078` (notes are retired, never
deleted) all stand. This ADR is the reason none of them had to be reopened.
