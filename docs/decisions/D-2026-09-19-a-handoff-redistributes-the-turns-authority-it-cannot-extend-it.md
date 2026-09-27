# D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it — peer-to-peer handoff, and the invariant that replaces attenuation-of-the-caller

## Status

Accepted, 2026-09-19. **Supersedes the topology half of
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`** — its invariant 1 and its choice of a
supervisor over a swarm. That ADR's invariants 2, 3 and 4 are carried unchanged and are load-bearing
here; its ship-disabled-until-measured requirement is honoured rather than waived.

Peer-to-peer handoff ships **off** (`CHEMCLAW_AGENT_PEER_ROSTER=""`). Nothing in this record is
evidence that handing over pays.

## Context

Delegation in this tree has always been hierarchical. `task` invokes a compiled helper inline and
the caller regains control, because a tool call is a tool call — there is no arrangement of
`agent/subagents.py` in which a helper answers the chemist. Three merged records declined the other
topology, and a review of them found each reason either stale, narrower than it read, or true of
something else:

- `D-2026-08-10` chose the supervisor "for one reason that outranks routing latency: one routing
  node means every delegation decision is visible in the trace, and here the trace *is* the
  regulated record." The regulated-record clause expired with
  `D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks`, and the
  visibility clause turns out not to require a routing node at all — see the Decision.
- `D-2026-08-13` declined `langgraph-swarm` on **scope**: both packages solve runtime routing and
  that design had no runtime routing decision. True of that design; not of this one.
- `tasks/review-2026-08-12-langgraph-native.md` declined both packages on a hard technical ground:
  `langgraph_supervisor/supervisor.py:431` builds on `create_react_agent`, which is
  `@deprecated(LangGraphDeprecatedSinceV10)`. **That finding stands and is strengthened here.** On
  LangGraph 1.x the native primitive is `create_agent` plus `Command`; `langgraph-swarm` is the
  *non*-native path. Adopting the package would have been the hand-rolled-looking choice.

So the package question and the topology question are separate, and only the second was open.

## Decision

A turn may compile a **turn graph**: a `StateGraph` whose nodes are several compiled
`build_langgraph_agent` graphs. A peer hands control to another by calling a generated
`transfer_to_<peer>` tool returning `Command(goto=…, graph=Command.PARENT)`. `active_agent` is
checkpointed, so a later turn on that thread resumes with whoever held the conversation — which is
the property that distinguishes this from a supervisor that re-routes from scratch each turn.

### The invariant that replaces "attenuation of its caller"

`D-2026-08-10` invariant 1 forbids a handoff that adds a tool the **caller** does not hold. Read
literally that forbids this feature outright, since a peer worth handing to holds something its
counterpart does not. What replaces it is a bound one frame further out:

> Every peer's surface is `root_surface ∩ peer_profile.tool_names`, where `root_surface` is what
> the agent that **opened the turn** binds. No peer holds a name the root did not.

This is **stronger** than the pairwise rule, not a relaxation of it. A chain of pairwise narrowings
promises `C ⊆ B ⊆ A` and says nothing about a fourth hop that re-widens back toward `A` after two
narrowings — a shape a mesh can reach and a tree cannot. Bounding every element against the root
bounds a chain of any length by its first frame. `turn_graph._peer_surface` takes `root` as an
argument and never the handing agent, so the property is arithmetic: there is no code path that
*adds* a name, which is why no review has to check that nobody added one.

The chemist authorised the root surface when the turn opened. A handoff redistributes it.
Authorisation itself is untouched: the per-call gate reads the **actor's** entitlements, and
`agent/profiles.py`'s standing rule — a profile "attenuates, it never authorizes" — is exactly why
redistributing a profile-shaped surface cannot escalate anything.

### The traceability objection is answered, not dismissed

`D-2026-08-10`'s stated reason for a supervisor was that one routing node makes every delegation
decision visible. A handoff here is an **ordinary tool call**, so it crosses `@wrap_tool_call` like
everything else: it is an audit row, it passes the authorization gate, it is refused under dry-run,
and it is counted by `repeat_guard`. The deciding agent's own `reason` is recorded in that row and
carried on `HandoffEvent`. Tool calls *are* the trace, so the routing node was one way to get
visibility rather than the only one.

### What each side gets

- A **peer** keeps the acting tools the root held, because the chemist reads it directly. A
  **helper** does not, because it works on a brief the chemist never saw. That difference is the
  whole reason `helper_profile`'s subtraction is not applied to a peer, and it is also why a helper
  may not hand over: `_subagents` passes no `handoffs=`, so there is no set to subtract from and no
  name anybody can forget — unlike `SPEAKS_TO_THE_CHEMIST`, which works only while somebody
  remembers the name.
- Every peer is compiled by `build_langgraph_agent`, so each carries the whole middleware chain.
  The constraint that outlives every subagent decision here — deepagents assembles a bare
  `SubAgent` from `spec["middleware"]` alone, so anything not compiled by that function runs
  ungoverned and silently — applies unchanged and is satisfied by construction.
- `D-2026-08-10` invariant 3 gets a producer on a handoff path for the first time:
  `build_langgraph_agent(peer=…)` reaches `make_audit_middleware(agent=…)`, the **build-time**
  argument `D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail` established, never a
  contextvar. Empty stops meaning "the agent the chemist talks to" once a mesh exists, because then
  several do; under the shipped configuration no mesh is built and the convention is unchanged.

### It ships off

`D-2026-08-10` requires hand-off accuracy measured against the single-agent baseline before a team
is turned on. **That measurement still does not exist** — `evals/delegation.py` has never run
against a model, and this work does not create it. An empty roster also leaves
`tests/test_context_floor.py`'s prefix untouched, since a first-party tool schema is charged
against `CEILINGS` with no allowance to absorb it. `build_turn_graph` returns `None` and callers
run the same object they ran before, which is a stronger claim than "equivalent".

## What was measured rather than assumed

Five things, each of which a reading of the documentation gets wrong. The probe scripts are not
kept — `tasks/` holds no Python and a script whose finding has been asserted is a second copy of
the assertion. What each one measured is below, and what holds it is `tests/test_turn_graph.py`.

1. **`Command(goto=…, graph=Command.PARENT)` from a tool inside `create_agent` does reach an outer
   `StateGraph` node.** The topology rests on this and it holds.
2. **It terminates the inner agent without merging its state.** Returning only the `ToolMessage` —
   the obvious implementation — produced a three-message turn, `Human → Tool → AI`, with an
   **orphan `ToolMessage`** whose `tool_call_id` matches nothing: the `AIMessage` carrying the call
   never reached the parent. An OpenAI-compatible endpoint rejects that thread on the *next*
   request, so the failure surfaces in whichever agent is holding the conversation by then.
   Carrying `state["messages"]` up with the command gives `Human → AI(tool_calls) → Tool → AI`.
   Wrapping the agent in a function instead of adding it as a node does not help.
3. **Peers share the turn's caps.** `model_calls` measured **2** over two peers rather than 1 each,
   so a mesh cannot buy a fresh allowance by handing over — which would have made `loop_cap` and
   `spend_cap` advisory.
4. **`checkpointer=False` and `checkpointer=None` on a peer are observationally identical** — same
   channels checkpointed, same restore, same message count — so `False` is taken, on
   `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer`'s measurement of what silent
   inheritance costs.
5. **The stream's attribution inverts under a wrapper, and the obvious marker for fixing it is
   wrong in the worse direction.** `api/graph_stream` attributed by `bool(namespace)`; a peer is a
   node, so every event it produces arrives one frame down and would be marked `"subagent"` — and
   the runner builds the answer from *unattributed* tokens, so every turn would answer empty and be
   classified `empty_answer`, with the plan withheld. The first fix asked whether the graph declared
   an `active_agent` channel; **every** compiled agent declares it (`ChemclawState` does) and a
   plain single agent measured `root_depth == 1`, which would have broken every existing deployment
   rather than the new feature. The marker is therefore a stamp the builder writes.

Four further defects were found by the tests rather than by the probes, and every one of them
would have shipped silently:

- **The handoff counter wrote a delta into a channel defined against absolute totals.**
  `TurnTotal` folds `base + (value - base)`, so a constant `1` contributes 0 on every hop after the
  first: a two-hop turn counted **1** and `agent_max_handoffs` was unreachable. That channel's own
  docstring says a delta "would be read as a walk backwards and contribute 0", which is exactly
  what happened. The multi-hop test is what caught it.
- **`HandoffEvent`'s `from`/`to` pair could not be serialised as named.** `from` is a keyword, so
  the field needs an alias, and `sse_frame` dumps with `model_dump_json()` — which does not apply a
  serialization alias without `by_alias=True`. The frame went out carrying `from_`. Adding
  `by_alias=True` would fix one event by changing how sixteen others serialise, so the fields are
  named `from_agent`/`to_agent` and need no alias.
- **The event's first producer read the wrong source, and was wrong twice over.** It scanned a
  completed node's `tool_calls` for a transfer, which is structured data rather than the
  `ToolMessage`'s prose and looked like the careful choice. But a handoff carries its agent's whole
  message list into the parent — it must, per defect 2 above — so every later update replays every
  earlier hop: measured, a **two-hop turn announced seven handoffs**. And the peer's name is not
  recoverable from the tool's name, because a tool name cannot carry `-`:
  `transfer_to_evidence_peer` reads back as `evidence_peer` for a profile called `evidence-peer`,
  so a surface would have printed a name no profile has. Both vanish when the tool raises the
  event itself: once per call, carrying the name it closed over. `record_handoff` therefore returns
  — the function `D-2026-08-26` deleted for having no caller — **with its caller in the same
  commit**, which is the condition that ADR set.
- **And the branch for it was first written in the wrong function**, where `HandoffSignal` fell
  through `_signal_event`'s `isinstance` chain to the unguarded `NoteRecordedEvent` tail and raised
  `AttributeError: 'HandoffSignal' object has no attribute 'note_id'`. That is precisely what the
  comment on `SkillLoadedSignal` warns happens to a new member of that union, in the function it
  warns about — a standing warning does not fire on its own.

## Consequences

- **`langgraph-swarm` is not adopted, and this makes that permanent rather than pending.** The
  deprecated-constructor finding is unchanged, and the mechanism it wraps is four functions here.
- `HandoffEvent` returns to the union **with its producer in the same commit**, which is what
  `tests/test_event_producers.py` requires and what did not happen the last two times. That is a
  coordinated change with `Chemclaw3_ui`, whose `eventContract.test.ts` currently pins the event's
  *absence*: until both land, that repository's suite is red on this.
- `active_agent` is the first deliberately **checkpointed** field on `ChemclawState`, against three
  paragraphs in that module arguing a checkpointed field is how a session gets bricked. The
  distinction is what the field means: `model_calls` describes work this turn did, so carrying it
  across turns is a miscount; `active_agent` describes who the chemist is talking to, so *not*
  carrying it is the bug. It carries no authority — an unknown name routes to the root, and every
  peer was intersected against the root before compilation.
- **What this settles about delegation: nothing.** It is a capability, not evidence. The open row
  in `docs/planning/BACKLOG.md` is unchanged and now has a second arm to measure.

## What keeps it true

`tests/test_turn_graph.py` holds the feature; the named ones below are the assertions that would
not be obvious to re-derive.

**The invariant, as arithmetic rather than as a number:**

- `test_a_peer_cannot_reach_a_tool_the_root_does_not_hold` — one line of set algebra.
- `test_the_second_hop_is_bounded_by_the_root_not_by_the_first` — the same inequality over every
  peer a compiled mesh binds, which is what makes a *chain* bounded rather than each link.
- `test_a_profile_that_narrows_nothing_narrows_to_nothing_as_a_peer` — `tool_names is None` is
  "does not narrow" for a session profile and nothing for a peer.
- `test_the_root_surface_is_both_halves_or_a_peer_loses_every_connector` — the union that would
  otherwise fail in the safe direction and go unnoticed.

**That turning it on changes nothing else:**

- `test_no_roster_builds_no_turn_graph` and `test_a_roster_that_survives_to_one_peer_builds_no_mesh`
  — the shipped default, and the case where a mesh would be a wrapper with no second node.
- `test_the_root_peer_binds_what_it_would_have_bound_alone` — the root's own surface is unmoved by
  the mesh, compared against a separately compiled single agent rather than argued.
- `test_a_helper_holds_no_handoff_tool` — driven on a compiled graph, not asserted about source.
- `tests/test_context_floor.py` — re-run and unmoved, which is the prefix claim above.

**The mechanism, which is where every defect was:**

- `test_a_turn_hands_twice_and_the_thread_stays_well_formed` — the multi-hop scenario, asserting
  that every tool call has its answer and that the turn's answer is the last peer's. This is the
  test that caught the delta-into-`TurnTotal` counter.
- `test_a_two_hop_turn_announces_each_handoff_exactly_once` — the duplicate-emission and
  name-mangling defects, as a multiset over `(from, to)` so a repeat is named rather than counted.
- `test_a_peers_answer_reaches_the_chemist_unattributed` and
  `test_a_single_agent_is_not_mistaken_for_a_mesh` — the stream regression, in both directions;
  the second is the one that pins the marker defect that would have broken every deployment.
- `test_a_bouncing_turn_hits_the_cap_and_still_answers` — the chain bound on a real mesh, holding
  the count and the turn still answering together.
- `tests/test_langgraph_stream.py::test_every_per_turn_counter_survives_a_mid_turn_resume` —
  membership of `_CARRIED_CHANNELS` derived from the `TurnTotal` channels rather than listed, so
  the next per-turn counter cannot be left out of a resume the way `handoffs` first was.

**Elsewhere:**

- `tests/test_state_channels.py` — every channel `ChemclawState` declares, now probed with a value
  of its own type: the derivation defaulted everything non-bool to `7`, so `active_agent: str` was
  visited rather than covered, and an unrecognised type now raises instead of guessing.
- `tests/test_event_producers.py::test_every_declared_turn_event_has_a_producer` — the union member
  and its emitter arrive together, which is the condition this event failed twice before.
