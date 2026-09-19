# Ten-wave review session — 2026-09-19

## Why

Two weeks of change (2026-09-05 → 2026-09-19) across the three repositories in the family, measured
rather than estimated:

| Repository | Base | Files | Insertions | Deletions |
| --- | --- | --- | --- | --- |
| `Chemclaw3` | `63b58ea` (2026-09-05) | 1558 | 193 201 | 28 524 |
| `Chemclaw3-mcp` | `f8f74bc` (2026-08-29) | 435 | 52 643 | 1 994 |
| `Chemclaw3_ui` | `919c68a` (2026-09-04) | 188 | 27 341 | 2 515 |

That is too much surface for one review pass to hold in context, which is the whole reason this is
ten waves with a fan-out per wave rather than one long read. Waves 1–5 are scoped to **the change**;
waves 6–10 review the codebase as a whole, on the premise that a change can be individually correct
and still leave the system unshippable.

## Shape of a wave

1. Fan out subagents over disjoint slices of the wave's scope, each with its own context.
2. **Verify every finding myself** before believing it — this repository's own rule (`CLAUDE.md`,
   "Measure it, don't argue it"). A subagent's confident prose is evidence about the subagent.
3. Fix what survives verification. Root cause, not band-aid.
4. `make lint type test` (serial) in each repository touched, Postgres-backed tests included —
   the daemon is started, so a skipped Postgres lane is not acceptable evidence here.
5. Commit on `claude/10-wave-code-review-k93v0d`, push, open the PR, wait for CI, merge, delete.
6. **Post-merge focused review** of exactly what merged, in fresh context, before the next wave
   starts. A fix is where the next defect goes.

## The ten waves

Waves 1–5 — the change:

- **Wave 1 — the agent layer.** `src/chemclaw/agent/` (+13 188/−1 903, 69 files): `turn_graph.py`
  and `handoff.py` (both new), `tool_result_shape.py`, `state.py`, the loop cap, compaction,
  the spend cap, the skill backend, the plan gate. Highest-risk slice: it is where authority lives.
- **Wave 2 — config, API and CLI.** `core/` (+7 107), `api/` (+4 642), `cli/` (+3 780): the budget
  derivation, the event contract, streaming, the front door, identity.
- **Wave 3 — data and durability.** `durable/`, `kg/`, `ingest/`, `memory/`, `retrieval/`,
  `publish/`, `science/`, `protocols/`, `analytical/`, `deliver/`, `operations/`, `evals/`.
- **Wave 4 — the MCP fleet.** `Chemclaw3-mcp`: four new servers (`unitops`, `thermalsafety`,
  `suitability`, `kinetics`), `mcp_server_kit` (+6 605), `calc`, `rxnpredict`, `rxnlabel`.
- **Wave 5 — the frontend.** `Chemclaw3_ui`: `src/components`, `src/state`, `src/hooks`,
  `src/api`, `src/chem`, `server/`, `shared/`, `e2e/`.

Waves 6–10 — the whole system, change or no change:

- **Wave 6 — cross-repo contracts and regression surface.** Does the change hold at the seams the
  three repositories meet on (manifests, ports, identity headers, the turn-event contract, OpenAPI),
  and did it break anything it did not touch?
- **Wave 7 — security and identity, end to end.** Authorization, authentication, the egress
  posture, secret handling, prompt injection, sandboxing, the dependency closure.
- **Wave 8 — production readiness: deployment and operability.** Helm/OpenShift, resource limits,
  probes, observability, migrations, retention, failure modes, runbook truth.
- **Wave 9 — correctness and reliability of the whole tree.** Concurrency, the durable layer,
  error handling, data integrity, scientific correctness inside the semiempirical tier.
- **Wave 10 — claims against code, test quality, and the final gate.** The corpus this family keeps
  is full of prose that was once true; plus whether the tests prove what their names say, dead code,
  and one full green gate per repository with its skip count named.

## Log

(appended per wave)

### Wave 1 — the agent layer (2026-09-19)

Six reviewers over disjoint slices of `src/chemclaw/agent/` (+13 188/−1 903, 69 files). Every finding
below was **re-driven by me** before it was believed; two did not survive that and are recorded as
such, because a reviewer's confidence is evidence about the reviewer.

Fixed, each with the mutation that reds its guard watched failing:

1. **The durable-memory gate failed open on three spellings of one path** (`agent/authz.py`).
   `writes_durable_memory` tested `startswith("/memories/")` on the model's own spelling while
   `FilesystemMiddleware` normalises through `validate_path` before routing. `memories/a.md`,
   `/./memories/a.md` and `memories/sub/b.md` all answered `False` and all routed to Postgres — so
   under an unapproved plan the plan gate short-circuited and on a dry run the refusal never fired.
   Now normalised first, which also closes the exact-root `/memories` case the blanket deny was
   masking.
2. **The checkpointer's schema stamp refused every live session's next ordinary turn** on any deploy
   that adds a per-turn counter (`agent/checkpointer.py`). Five of the six stamped channels are
   `UntrackedValue` — never written to a checkpoint by *any* build — so the refusal pre-empted
   nothing for them and cost the fleet. The stamp is now the restorable channels only
   (`('active_agent',)`), with the excluded half named as `UNTRACKED_CHANNELS` so the partition test
   still reds on an accidental drop.
3. **The delivered truncation notice described an intermediate, not the tool**
   (`agent/tool_result_size.py`, `agent/tool_framing.py`). Framing nests the size cap and re-bounds
   after escaping; both place the notice at the same relative offset, so the second cut deleted the
   first's sentence and re-derived its arithmetic from the 60 kB middle. A 500 000-character
   connector result reached the model as "451 of 60,102 characters removed". The tool's real output
   size now travels on `response_metadata`, and the same flag stops the truncation counter and its
   log row firing twice per cut.
4. **Both turn caps were per-branch inside one superstep, so a `task` fan-out multiplied them**
   (`agent/loop_cap.py`). Measured `1 + W·(cap − 1)`: 25 model calls against a cap of 4 at width 8,
   and 193 against the shipped cap of 25. The count was right and the *bound* was not. A turn-wide
   count now lives on the watch object that already existed for this exact reason, with the cap
   comparison and the channel advance kept as two numbers so the channel is not inflated.
5. **A peer carried the rostered profile's authority, not the root's** (`agent/turn_graph.py`).
   Only `tool_names` was bounded; `harness_enabled`/`harness_autonomy` (which decide whether the plan
   gate is attached at all), `mcp_server_names` and `skill_names` travelled unbounded, and the
   connector half of the surface was never intersected. The derivation is inverted: a peer is built
   **from the root** and brings only `PEER_OWNED_FIELDS`, so a field added to `AgentProfile` is
   root-derived rather than fail-open.
6. **Two peer names minted one handoff tool** (`agent/handoff.py`) — `-` folds to `_`, so
   `property-lookup` and `property_lookup` are two legal profile names, one unreachable node, and two
   functions of one name in the request. Refused at build time beside the existing duplicate-name
   guard.
7. **The resume carry took `max()` over an additive reducer** (`api/graph_stream.py`), seeding a
   resumed turn with 4 of the 25 calls it had spent. It now folds the superstep the way `TurnTotal`
   does, and still cannot walk backwards.
8. **`propose_skill` hand-copied three of `validated_skill`'s four checks** and omitted the
   shipped-name conflict, so a proposal the accept route answers 409 for was recorded as `open` and
   reported as awaiting a decision with no way to accept it.
9. **An unreadable `tools:` key widened a skill's visibility** (`agent/skill_manifest.py`) — a
   missing entry reads as "declares nothing", which `ToolScopedSkills` leaves visible to everyone.
   Now scoped to nothing, keyed on the directory name `skill-validate` pins to the frontmatter.
10. **`review_commitments` defanged 2 of 8 externally-supplied fields** on a stated ground that was
    false: only `kind` and `state` are `Literal`s. The whole row goes through `defanged_payload`.

Deferred with a `BACKLOG.md` row rather than fixed, each for a stated reason: the `/mine` backend
predicate, the superseded-proposal state, the two turn paths that open no watch, and whether a
*restorable* channel addition should still drain sessions (that one is an ADR, because `D-2026-08-13`
chose the name comparison deliberately).

Did **not** survive my re-driving: the claim that `_signal_event`'s unguarded `NoteRecordedEvent`
tail is a live hazard — `mypy --strict` genuinely catches a new unhandled union member, driven with a
probe signal (2 `union-attr` errors). And the notice-arithmetic defect does **not** reproduce without
the `SERVED_BY` stamp, so my first reproduction was a false negative until I gave the request real
connector metadata; worth recording because the same mistake would have dismissed a HIGH.
