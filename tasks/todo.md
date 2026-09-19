# Peer-to-peer handoff (swarm topology) — plan

Decision taken by the user after a Step-1 report found no peer handoff and three merged
decisions declining it. This supersedes the topology half of
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`; it does **not** supersede its
four invariants, three of which are carried unchanged and one of which is restated.

## The design in one paragraph

A turn compiles an outer `StateGraph` — the *turn graph* — whose nodes are compiled
`build_langgraph_agent` graphs, one per peer. A peer hands control to another by calling a
generated `transfer_to_<peer>` tool that returns
`Command(goto=<node>, graph=Command.PARENT, update={...})`. The turn graph holds `active_agent`
in a checkpointed channel, so the next turn on that thread resumes with whoever was last active
— which is the swarm pattern's defining property and the reason an outer graph is required at
all (`Command.PARENT` needs a parent).

## The invariant that replaces "attenuation of its caller"

`D-2026-08-10` invariant 1 reads: *a subagent's surface is an attenuation of its caller's, never
a widening*, and *a handoff that would add a tool the caller does not hold is a build-time error*.
Read literally that forbids peer handoff outright — a peer worth handing to holds something its
counterpart does not.

What replaces it, and what must be true by arithmetic rather than by review:

> **Every peer's surface is an attenuation of the turn's ROOT surface.**
> `peer_surface = root_surface ∩ peer_profile.tool_names`. No peer holds a name the root does
> not. A handoff *redistributes* authority the chemist's turn already opened with; it cannot
> extend it. The chain is therefore bounded by its first frame however long it gets, which is
> the property "attenuation of the immediate caller" was reaching for and which a chain of
> pairwise narrowings does not actually give you.

Invariants 2 (`require_actor` reject-if-absent), 3 (the trail names the agent beside the human)
and 4 (skills do not inherit) are carried unchanged. Invariant 3 gets a real producer here for
the first time on a handoff path — `make_audit_middleware(agent=...)`, the build-time argument
`D-2026-09-06` established, never a contextvar.

## Ships off by default

`CHEMCLAW_AGENT_PEER_ROSTER=""`. Three reasons and they are independent: `D-2026-08-10` requires
a team to ship disabled until hand-off accuracy is measured; `evals/delegation.py` has still
never run against a model; and an empty roster leaves `tests/test_context_floor.py`'s prefix
untouched for every existing deployment, since the handoff tools are only bound when a roster
names peers.

## Steps

- [ ] 1. Install deps, confirm the suite is green on `main` before any change (baseline).
- [ ] 2. MEASURE the four things this design assumes, on a real compiled graph, before writing
      the feature. Each is a separate probe under `tasks/probes/` and each has a recorded number:
  - [ ] 2a. Does `Command(goto=..., graph=Command.PARENT)` returned from a tool inside a
        `create_deep_agent` graph actually reach an outer `StateGraph` node? (The whole design
        rests on this. If `create_agent`'s `ToolNode` swallows or rewrites the `Command`, the
        topology changes.)
  - [ ] 2b. What do the `UntrackedValue` channels (`model_calls`, `billed_tokens`) do across the
        peer boundary? A per-turn cap that resets per peer is a cap that does not exist.
  - [ ] 2c. What does the stream namespace look like with a wrapper frame, exactly?
  - [ ] 2d. Does a peer compiled with `checkpointer=None` inherit the turn graph's saver, and is
        `active_agent` restored on the next turn?
- [ ] 3. `agent/handoff.py` — the handoff tool factory. One tool per reachable peer, schema kept
      minimal because every token lands in `CEILINGS` directly.
- [ ] 4. `agent/state.py` — `active_agent` (checkpointed, `LastValue`-shaped with a reducer that
      survives two writers) and `handoffs` (`TurnTotal`, per-turn, bounds ping-pong).
      Extend `tests/test_state_channels.py`'s `_PROBE_VALUE`/`kind` derivation for a `str` channel.
- [ ] 5. `agent/turn_graph.py` — the outer graph builder. Peers compiled by
      `build_langgraph_agent` (never a bare dict — `governed_roster`'s argument applies here
      verbatim), root surface computed once, each peer intersected against it.
- [ ] 6. `api/graph_stream.py` — root depth. Replace `bool(namespace)` with a depth test read off
      the graph object, not passed by a call site. Without this the main agent streams as
      `"subagent"`, the answer goes empty and the plan is suppressed.
- [ ] 7. `api/events.py` — `HandoffEvent` back, **with its producer in the same commit**
      (`tests/test_event_producers.py`), the contract fixture regenerated, `app.js` case restored.
- [ ] 8. Config: `agent_peer_roster`, `agent_max_handoffs`. Off by default.
- [ ] 9. Tests, including at least one **multi-hop** (A→B→C) scenario driven on a compiled graph,
      plus the root-bound inequality asserted as arithmetic rather than a number.
- [ ] 10. ADR superseding D-2026-08-10's topology half; `docs/decisions/README.md` row;
      BACKLOG.md rows for what this does *not* settle (whether delegation pays — still unmeasured).
- [ ] 11. `make lint type test` green, reporting what it skipped.
- [ ] 12. Companion PR in `Chemclaw3_ui` — its `eventContract.test.ts` currently pins the
      *absence* of `handoff`, so this repo's change breaks that repo until both land.

## Review

(filled in at the end)

## Review

Shipped. `CHEMCLAW_AGENT_PEER_ROSTER` is empty by default, so no deployment's behaviour changes
until somebody opts in.

### What the steps actually produced

- `agent/handoff.py` — the `transfer_to_<peer>` tool factory, the per-peer menu text derived from
  what that peer's graph binds, and the chain cap.
- `agent/turn_graph.py` — the outer `StateGraph`, the root-bounded surface arithmetic, and
  `build_turn_agent`, which is the single entry point the runner now takes.
- `agent/state.py` — `active_agent` (checkpointed, `LastPeer`) and `handoffs` (`TurnTotal`).
- `api/graph_stream.py` — `root_depth`, replacing a `bool(namespace)` attribution that inverts
  under any wrapper graph.
- `api/events.py` + `api/static/app.js` — `HandoffEvent` back, with its producer.
- `tests/test_turn_graph.py` — 18 tests, including the multi-hop scenario.
- `Chemclaw3_ui` — the mirror, on its own branch and PR.

### Where the plan was wrong

**The probes were the most valuable step and the plan under-sold them as step 2 of 12.** Four of
the five design assumptions were wrong or incomplete, and every one would have shipped silently:

1. `Command(graph=PARENT)` works — but it terminates the inner agent *without merging its state*,
   so the obvious implementation leaves an orphan `ToolMessage` and the *next* request is rejected.
2. `checkpointer=False`/`None` are identical here, so the plan's "measure it" was right and the
   worry behind it was not.
3. The stream attribution inverts under a wrapper — this was step 6 on the plan and it was the
   difference between a working feature and one that answers every turn empty.
4. **The fix for (3) was itself wrong first.** Deriving "is this a mesh?" from an `active_agent`
   channel is true of *every* compiled agent in this tree, so a single agent measured depth 1 —
   which breaks every existing deployment rather than the new feature. Caught by measuring, not by
   review.

Two more were caught by the tests rather than the probes, which is the argument for writing the
multi-hop test before believing the mechanism: the chain counter wrote a delta into a channel
defined against absolute totals (two hops counted 1, so the cap was unreachable), and
`HandoffEvent`'s `from`/`to` could not serialise as named because `sse_frame` omits `by_alias`.

### What was dropped from the plan

The probe scripts. `tasks/` holds no Python anywhere in this repo's history, and every finding is
now either asserted in `tests/test_turn_graph.py` or written into the ADR with its numbers — a
script whose finding is asserted is a second copy of the assertion.

Three functions written and deleted before commit for the same reason: `is_handoff`,
`peers_reachable_from` and `peer_instructions` each had no caller in `src/`, and a function kept
alive by a test that calls it directly is the shape this repo deletes on sight.

### What this does not settle

Whether handing over pays. `evals/delegation.py` has still never run against a model; the new
`BACKLOG.md` row is a fourth arm on that comparison, not a substitute for it.
