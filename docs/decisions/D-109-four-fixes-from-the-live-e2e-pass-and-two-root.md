# D-109 — Four fixes from the live e2e pass, and two root causes that were not what they looked like

**Context.** A nine-stage live pass against the real stack (Postgres+pgvector, Temporal, real
Anthropic calls, real signed tokens) left four open findings. Fixing them changed the diagnosis of
two, and the corrected diagnoses are the part worth recording.

**1. Harness mode failed on every tool call — and the test double is why nobody knew.**
`create_harness_agent` sets `require_per_service_call_history_persistence=True`, whose middleware
replaces the outgoing messages each model call and signals "stop resending the transcript" with a
sentinel `conversation_id` on the finalized response. It also installs `MessageInjectionMiddleware`
unconditionally, which *while streaming* returns a new `ChatResponse` from
`ChatResponse.from_updates()` — and the sentinel, living on the inner response rather than on any
streamed update, does not survive. The function-invocation loop therefore re-sent the whole
transcript while history was independently re-injected, and the duplicate put a `user` block
between a `tool_use` and its `tool_result`, which Anthropic rejects outright. Both autonomy modes,
single and parallel calls, 100%.

Chemclaw sets the flag back to `False` after construction. That breaks the chain at its start —
nothing injects, so no sentinel is needed — at the cost of per-*run* rather than per-model-call
history durability, which is exactly what the classic path has always done and what
`harness_enabled=False` (the default) already gives everyone. The correct fix is upstream and is
recorded in `DEFERRED.md`.

**Decision: treat the test double's class hierarchy as production-relevant.**
`ScriptedChatClient` derived from `FunctionInvocationLayer + BaseChatClient` and its docstring
claimed that mirrored a concrete client. It did not: `BaseChatClient` is deliberately the base
*without* middleware wrapping, and the omitted `ChatMiddlewareLayer` is what consumes
`client_kwargs["middleware"]`. Every harness test ran a pipeline containing **zero** chat
middleware — including the two the harness installs — so three tests passed green against
machinery production never used. Adding the layer reproduces the failure offline with no network.
A fake that diverges from the real type's *layering*, not just its behaviour, tests nothing; the
regression tests now assert the wire invariant (every call followed by its result) over the
messages actually handed to the client.

**2. The suite was destroying live data.** Nine test files wrote to production tables with no
isolation. `test_audit_chain` truncated `audit_events` — the GxP tamper-evident hash chain — then
deliberately corrupted a row and left it that way, so `make audit-verify` failed permanently
afterwards. On the dev database this was not hypothetical: rows 1–3 of the "real" audit trail were
that test's own fixtures, with row 2 still reading `actor='attacker'`. CI never noticed because its
database is a per-run container, which is precisely why a shared database was where it bit.

**Decision: isolate by schema, carried on the DSN, not by a parameter threaded through the stores.**
Every store already resolves its connection from `settings.postgres_dsn`, so redirecting that one
value (to `options=-c search_path=chemclaw_test,public`) isolates all of them with no schema
argument anywhere in product code. `public` stays second because `vector` is installed per
database. The schema name is a constant in `tests/pg.py`, not a `Settings` field: `config.py` is
the operator-facing deployment surface and its parity tests require every field to appear in
`.env.example` — a test-only knob does not belong there.

This surfaced a real product bug (**3**): `chemclaw.db.connect` passed `options=` as a psycopg
keyword, which *overrides* the connection string rather than merging with it — but only when a
statement timeout was set, since `None` is dropped. An operator's `search_path`, `application_name`
or `work_mem` therefore vanished on some call sites and survived on others, non-deterministically.
Now merged, with ours appended last so libpq's last-occurrence-wins keeps our timeout authoritative.

**4. The orphan-`tool_use` rollback protected nothing on the path that ships.** D-091 §2 snapshots
and restores `session.state` on a client disconnect. Under `session_store="postgres"` the messages
are not in `session.state` — `save_messages` has already committed them — so the orphan survived
the rollback meant to discard it, and every later turn on that session replayed it into the same
400. **Decision: enforce the invariant on read, and make the rollback durable as well.**
Read-time repair (`PostgresHistoryProvider.get_messages` drops and deletes unanswered calls) is the
load-bearing half, because the disconnect handler is not the only way a turn dies between writing a
call and writing its result — a `SIGKILL`, an OOM, or a pod eviction runs no Python cleanup at all,
and the harness's per-service-call persistence had been *widening* that window by writing the call
before the tool ran. It also heals sessions already broken in the field. The watermark rollback is
kept alongside it because the two differ: repair removes orphans, whereas the rollback's contract
is that a half-written turn is discarded whole.

The pairing rule lives in `agents/message_pairing.py` with two forms, and the distinction is
load-bearing: `unmatched_call_ids` (by id, order-independent) decides what is safe to *delete from
storage*, where a merely out-of-order pair is intact history; `calls_without_adjacent_results`
(the stricter wire rule) validates what is about to be *sent*. Using the lenient one on the wire
would have missed finding 1 entirely — duplicated history leaves a second, unanswered copy of a
call whose id does appear answered once.

**5. RBAC denial narration — the reported cause was wrong.** The pass attributed inconsistent
narration to tool docstrings ("gated" tools explained themselves, others did not). No tool
docstring mentions gating, permissions, or privileges anywhere. The actual cause: `authorize_tool`
emits three different messages, and under the shipped default only the five
`DEFAULT_WRITE_TOOL_GATES` tools can ever be denied — so the self-explaining "lacks a privileged
role" message was the only one anyone had seen. The deny-default message, "not in the tool
allowlist (deny by default)", is written from the perspective of whoever edits the config, and the
model relayed it as "not currently available… a configuration issue" — which sends a chemist to
report a bug rather than to request access. **Decision: all three refusals share one chemist-facing
shape** (who, which tool, why), the operator's remedy moves to the docstring and runbook, and
`_INSTRUCTIONS` gains a passage on narrating a refusal — mirroring the compaction passage, which
was already the house pattern for honest limitation-reporting. Verified live: all five previously
vague read tools now state it is an access decision and say how to get access.

**5b. The test schema is per-process.** Found by hitting it: the session fixture *drops* its
schema on the way out, so a fixed name means a second pytest run deletes the first run's tables
mid-flight — which is what happened when a single test file was run while the full suite was
going. The schema is now suffixed with the pid, verified by running two suites concurrently
against one database and confirming both pass and neither leaves residue. A hard kill can strand
an orphan schema; it is inert and unmistakably named, which is the right trade against the
alternative of a shared name that is unsafe by construction.

**5c. A converged geometry was not a fixed point.** Unrelated to the four findings above and
folded in only because it blocked this branch's CI: `tests/test_xtb_opt.py::test_a_converged_
structure_is_a_fixed_point` failed identically on pristine `main` (verified in a clean worktree —
same two structure ids), so it was `main`'s failure, not a merge artifact.

The in-process optimizer's loop was bounded only by the step count, so it always ran at least one
leg before testing convergence. Re-optimizing an already-relaxed water therefore moved it 3e-4
Angstrom, and a third pass moved it again. Because a structure id is a hash of the coordinates,
every pass minted a new id — which silently forks the calculation cache and quietly voids the
"compute once, never recompute" guarantee (D-011) for every task keyed on a geometry. The test was
right to call this out; it was pinning a property the code did not have.

The fix seeds the convergence test from the *input* geometry's gradient and makes the loop
`while max_gradient > tolerance and steps < max_steps`. It costs nothing: `evaluate_point` already
computed that gradient for the initial energy and discarded it. An already-minimal structure now
runs zero legs and returns byte-identical. Scoped to the library backend, which is the one
reachable here; whether the `xtb` binary's own ANCopt has the same property is untested, because
the binary is not installed in this environment — flagged rather than guessed at.

**5d. The durable-capability registry was not re-import safe.** The second of two failures
inherited from `main` rather than caused by the merge, and the more interesting one, because it
could only ever fail where Temporal actually runs.

`workflows/registry.py` anticipated the sandbox: its duplicate guard compares the defining
*module* rather than object identity, precisely so that Temporal re-importing a workflow module
is not mistaken for two capabilities claiming one name. Having allowed the re-registration, it
then stored it — and the sandbox's re-import builds a *new* class object for the same definition,
so the registry quietly swapped out the very object `workers/hpc_worker.py` captured at import
time. `HPC_WORKFLOWS == registered_workflows("hpc")` then compared two classes that print
identically and are not the same object: `QMJobWorkflow != QMJobWorkflow`.

The fix is to keep the *first* registration and return the incoming object unchanged, so Temporal
still receives the class it built while the registry keeps the one the workers hold. That is what
the guard's own docstring already implied; only the store was missing it.

Worth recording as a testing lesson rather than a one-line fix: `test_workflow_registry` already
had a re-registration test, and it passed throughout — it counted entries *by name*, which is
invariant under exactly this bug. The assertion that mattered was identity, and it was missing.
The regression test now added builds the second class object by hand, so it reproduces a sandbox
re-import with no Temporal server at all — the failure was otherwise invisible in any environment
where the test server cannot be downloaded, which is every environment this was developed in.

**6. ADR numbers now have an allocation ledger.** This ADR was written as D-092, renumbered to
D-095, then to D-109 — three collisions in one day, each found only when a merge conflicted. The
cause is structural: concurrent branches all append to the end of `DECISIONS.md` and all compute
"highest visible + 1" against their own branch, which by construction cannot see the others.
`ADR-REGISTRY.md` is the ledger — one line per number, so "what is taken?" is a grep against
`origin/main` rather than a scan of a 3,700-line document — and `CLAUDE.md` carries the procedure:
enumerate against `origin/main`, reserve in the *first* commit, and on a collision the branch
merging **second** renumbers (a rule, so neither session waits for the other).

Stated honestly, because a ledger that overpromises is worse than none: **this does not prevent
collisions.** Two branches can still append the same number to the ledger. What changes is the
cost — a one-line conflict a grep finds, instead of a ninety-line conflict inside a prose block
where the number is easy to miss. The collision-proof fix is to drop the global sequence for
date-plus-slug ids; that is a convention change worth making deliberately if this recurs, and it
is recorded as the escalation rather than done unilaterally here.
