# D-149 — The durable history compacts itself, because MAF's after-run compaction cannot reach it

**Status:** accepted · **Context:** REV-4. `chemclaw/agent/chemclaw_agent._build_compaction` passes an
`after_strategy` whose docstring promises to "shrink the persisted history so the next turn starts
smaller". Under `session_store="postgres"` — the production default — it does nothing whatsoever.

### Why it cannot be fixed where it looks broken

`CompactionProvider.after_run` reads `session.state[history_source_id]["messages"]`: the slot
`InMemoryHistoryProvider` keeps its thread in. `PostgresHistoryProvider` deliberately keeps nothing
there — that is the entire point of it — so the lookup finds nothing and the strategy returns having
touched nothing.

This is not a wiring bug. Making MAF's `after_run` work would mean reintroducing the in-process
thread the durable provider exists to abolish, and it still would not help: `after_run` only sets
`_excluded` flags, so nothing would be removed from Postgres either way.

The consequence was O(all turns) per turn. `_SELECT_WITH_ID` has no `LIMIT`, so every model call was
preceded by a full read and deserialisation of the entire session, and the stored rows grew for the
session's life. Nothing bounded it: retention prunes by *age*, is off by default, and an age window
does not cap one long-running conversation at all.

### Decision

**`save_messages` applies the same strategy to the table**, after storing the turn.

- **Inline, not a scheduled sweep.** This is where MAF intends after-run compaction, it stays inside
  the two primitives the provider already overrides, and the per-session turn claim (D-121) already
  guarantees one writer per session — so there is no lock to invent, no worker to add, and no
  Temporal replay surface. A sweep would also have to read every session's whole history to compute
  groups, which amplifies the exact problem being fixed.
- **Second transaction, best-effort.** The append commits on its own first; the compaction pass runs
  after and its failure is logged and swallowed, exactly as `_persist_repair` does. Storing the turn
  is the critical path; disposing of old rows is not. This keeps `save_messages`'s existing contract
  byte-for-byte on the append.
- **One policy, reused.** `compaction_strategy()` is now public and has three consumers. Durable
  deletion is strictly *more destructive* than context exclusion, so a second, tighter budget here
  would silently destroy context the model was still entitled to. One budget means the durable pass
  converges on what `before_run` would have produced anyway.
- **A watermark protects the turn just written.** The composed strategy's fallback can exclude
  *every* message when one payload is oversized — a turn that deleted the rows it had just stored
  would lose the conversation it was recording.
- **Off by default** (`agent_durable_compaction_enabled`), matching `retention_enabled`. A `DELETE`
  on conversation history is a policy a deployment states, never one it inherits on upgrade.

### The translation, and why it needs its own module

`chemclaw/agent/history_compaction.py` exists because a MAF strategy and a SQL table disagree about what
compaction is. A strategy **annotates and inserts**; storage **deletes and rewrites**. Three things
follow, each a place the naive version is wrong:

1. **Rows are tracked by object identity, not position** — the strategy inserts, so indices shift.
2. **An inserted summary is anchored onto a row.** `ToolResultCompactionStrategy` inserts a summary
   with no row of its own, back-linked by `SUMMARY_OF_MESSAGE_IDS_KEY`. Resolving those to row ids
   and taking the **minimum** puts the summary exactly where the group it replaced sat in `ORDER BY
   id`. Verified against the real strategy: a collapsed group resolves to its call row and its
   result row, and the summary anchors on the call row. **This is what avoids a schema change** —
   `session_messages` cannot express "insert between rows 113 and 115", and never has to, because a
   summary only ever replaces a group that is being deleted anyway. A summary whose group survives
   is dropped rather than written, since it would duplicate history.
3. **Annotations are stripped before anything is persisted.** `_group`/`_excluded`/
   `_exclude_reason` round-trip through JSONB and `annotate_message_groups` *trusts* an
   already-annotated prefix, so persisting them would make the next pass group against stale spans.

Deletion goes through `droppable_rows` (D-145), so a tool group is removed whole or not at all —
which is what keeps compaction from creating the stranded `tool_result` that has no self-heal path.

### What this does not change

`get_messages` is untouched: no `LIMIT`, and repair-on-read still heals genuinely broken sessions.
Compaction never reads a partial history, so it sidesteps the corruption class D-143 documented
rather than accepting it. `tests/test_durable_compaction_gap.py` keeps pinning both facts, retargeted
from "here is an open gap" to "here is what the fix deliberately did not do".

### Measured

Sixty turns of user + tool-call + 800-byte result + answer, at a small budget. Uncompacted the table
holds 240 rows and grows by exactly 4 per turn (`[40, 80, 120, 160, 200, 240]`). Compacted it sits in
a band — 14 → 23 → 22 → 18 — bounded by the window rather than by the turn count.

The band is worth stating: it does not settle on one number, because a collapsed group leaves a
summary row that is itself evicted a few turns later, so the total breathes. The test asserts
boundedness rather than a monotone plateau for that reason — an earlier before/after ratio caught a
local peak and would have flaked.

### Residual, accepted

A turn that compacts and is then cancelled leaves old rows pruned for a turn that "did not happen".
Those rows were policy-droppable regardless of the turn's outcome, and `rollback_to` only touches
rows above the watermark that compaction is forbidden to delete — so the rollback stays exact for
the turn's own rows. Stated rather than engineered around.
