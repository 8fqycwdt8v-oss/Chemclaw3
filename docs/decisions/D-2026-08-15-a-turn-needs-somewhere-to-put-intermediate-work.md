# D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work — the agent gets a scratchpad, and durable memories behind an actor-keyed namespace

**Status:** accepted · **Date:** 2026-08-15 · Reverses the `FilesystemMiddleware` declination in `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` and the `BaseStore` rejection in `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives`, on grounds each ADR did not have. Keeps both of their load-bearing objections and answers them.

## Context

Layer 1 could not do a hard research task, and the reason was structural rather than promptable.

The only backend a turn could reach was the narrowed skills tree: read-only, one verb. So every
tool result landed in the context window, and `agent/compaction.py` reclaimed it from there —
`ClearToolUsesEdit` replaces an older payload with a flat placeholder. A turn that pulled six
sources therefore could not *hold* six sources: the earliest were gone before the answer was
written. No instruction fixes that, and none was tried, because the shape of the failure is a
missing place to put things rather than a missing sentence in a prompt.

Two merged ADRs stood in the way, and both were right when written.

**`D-2026-08-11` declined `create_deep_agent` and with it `FilesystemMiddleware`**, because its
default stack "always registers `FilesystemMiddleware` — a write/edit/glob/grep surface, plus
shell", every name of which "would then have to be answered for by `available_tool_names`, gated by
`tool_role_gates` and justified in the safety rubric — a general filesystem acquired as a side
effect of wanting to read a `SKILL.md`."

**`D-2026-08-10` rejected `BaseStore`** on four grounds. The decisive one was not about capability:
`store` has no actor column, so `tests/test_leaver.py`'s derived right-to-erasure check "would pass,
report nothing missing, and a departing person's memories would remain. *A safety net that returns
a false green is worse than no safety net, because it is trusted.*" A second objection was that "a
store write passes through none of the six tool middlewares including `audit._recording`."

## Decision

Take the filesystem, and answer both objections rather than waive them.

**Three routes over one `CompositeBackend`** (`agent/scratchpad.py`): `/scratch/…` to a
`StateBackend` (graph state, per-thread, no disk); `/skills/…` to the existing
`NarrowedSkillsBackend`, unchanged; `/memories/…` to a `StoreBackend` over `AsyncPostgresStore`,
and only when a deployment enables it *and* the turn has an actor.

**The 2026-08-11 objection is paid, not dodged.** Every filesystem verb now *is* answered for:
`chemclaw_agent.skill_tool_names` went from one name to six, read off the middleware rather than
spelled out, so `make prose-validate`, `make skill-validate`, the template validator and the
profile check all see them. What made the cost worth paying is that 0.7.5 supplies three narrowings
the 2026-08-11 tree did not have — a `tools=` allow-list, `FilesystemPermission` rules, and
`StateBackend`, which is not a filesystem at all. **`execute` and `delete` are withheld**: the first
because deepagents ships one concrete sandbox (LangSmith, declined here on egress grounds) and
`LocalShellBackend` is documented as unrestricted, the second on D-2026-08-12's argument, which GxP's
retirement does not touch — a turn that cannot rewrite a `SKILL.md` but can remove it still decides
what judgment the next turn can load. The hand-written `skill_read_tool` is deleted; upstream's
`read_file` reads through the same narrowed backend, and `tests/test_skill_backend.py` now proves
the refusal against *upstream's* tool, which is the arrangement that actually ships.

**The false-green objection is answered by putting the actor in the namespace.** Memories live under
`("memories", stable_hash(actor))`; `store.prefix` holds the dotted namespace, so erasure is a
prefix match — the shape every other table in `agent/leaver.py` already uses — and
`memory_prefix()` is called by both the writer and the sweep so the two cannot drift. One digest per
*spelling* of an id, because this database holds `alice-oid` and `unverified:alice-oid` for the same
chemist. **No actor means no route at all**, rather than a shared prefix: a memory nobody can erase
and everybody can read is worse than no memory, and the CLI, template steps and the eval harness all
run without ambient identity.

**The audit objection answers itself under this design, and a test enforces that rather than
describing it.** A direct `store.aput` would bypass the audit row, the authorization gate, the
dry-run refusal and the repeat guard — all four, silently. Here the only path to the store is
`write_file`/`edit_file`, which are *tools*, so they cross the same `wrap_tool_call` chain as
everything else. `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly`
walks the AST of every module under `src/chemclaw` and fails on a store write that is not a tool
call.

**Not `create_deep_agent`, yet.** The middleware is composed by hand onto the existing
`create_agent`, because `create_deep_agent` returns a `RunnableBinding` (it ends
`.with_config({"recursion_limit": 9_999, …})`) whose `.bound` must be taken before anything can
call `aget_state`, it does not install `TodoListMiddleware` for Anthropic models, its
`_apply_custom_middleware` splices by `.name` — so `ReloadingSkillsMiddleware` would be appended
beside upstream's rather than replacing it — and it auto-inserts a general-purpose subagent holding
every tool with none of this repository's middleware. Each is answerable; none is answerable in the
change that introduces the backend. The consequence recorded here is that `permissions=` is public
API only through `create_deep_agent`, so `filesystem_permissions()` is written and **not yet
enforced by upstream** — the write refusal on `/skills/` currently comes from
`NarrowedSkillsBackend`, which is where D-2026-08-10 put it and where it is load-bearing anyway.

## Consequences

- A research turn can hold intermediate work: write to `/scratch/`, launch a calculation, re-read.
  `FilesystemMiddleware` additionally evicts oversized tool results to the backend, which is
  strictly better than compaction's placeholder — the evidence stays *readable at a path* instead of
  being dropped, repairing the one thing D-025 knowingly lost.
- `agent_memory_enabled` ships **off**. The default is about data, not confidence: enabling it
  creates `store`/`store_vectors` and starts writing agent-authored files that outlive a session.
  The scratchpad — the half that makes a multi-source turn possible — is on unconditionally.
- **Layer 4 is unchanged.** The store is a working surface, not knowledge. A conclusion worth
  keeping still goes through `propose_knowledge_note` and a human, and nothing under `/memories/` is
  evidence a citation can resolve to.
- **Honest limit: the erasure path is written and not exercised.** `tests/test_leaver.py` needs
  Postgres and this environment has no Docker, so the twelve tests that would prove a departing
  actor's memories are actually deleted **skip**. The statements, the prefix derivation and the
  two-spelling handling are unit-tested; the DELETE is not. That is the first thing to run where a
  database exists, and it is exactly the class of claim this repository has been burned by before.
- Age-based pruning of `store` is **not** wired into `durable/retention.py`. `AsyncPostgresStore`
  has native TTL support, so the mechanism exists; whether a memory should expire on a clock is a
  product question nobody has asked yet.
