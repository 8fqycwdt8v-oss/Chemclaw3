# D-122 — The GxP audit trail defaults to durable, because opting in per call site did not work

**Context.** `PostgresAuditSink`, the tamper-evident hash chain (`chain_hash`, `row_hash`),
`infra/sql/011`, `make audit-verify` and `scripts/verify_audit_chain.py` were all built, tested and
documented as the GxP "who ran what" record. The sink was constructed in exactly **one** place:
`agents/cli.py`, behind `--audit-postgres`. The deployed service's `_default_agent_factory` called
`build_agent(profile=…)` with no `audit_sink`, so `agents/audit.py` installed `NullAuditSink()` and
the compliance record was log-only in the one process chemists actually talk to. The Temporal
template activities had the same gap, independently, in two more call sites.

Nothing failed and no test noticed: `tests/test_audit.py` drives the middleware directly and
`tests/test_audit_store.py` writes to the sink directly, so both pass while the wiring between them
is absent. `audit_events` was simply empty.

**Decision.** The default moves from the call site to the one place that decides.
`agents.audit.default_audit_sink()` returns `PostgresAuditSink` where `session_store="postgres"`
and `NullAuditSink` otherwise, and `make_audit_middleware(sink=None)` resolves it.

The polarity is the whole point. Opting *in* to a compliance control, once per entry point, means a
forgotten keyword argument silently downgrades it — and there is no failure to notice, because the
downgraded state is "the log still has it". So the durable sink is what a caller gets by default,
log-only is the fallback where no database is configured, and opting *out* requires passing
`NullAuditSink()` explicitly, which is a visible act.

Fixing `service/app.py` alone was the obvious change and was rejected: it would have left the
identical trap set for the template activities and for every entry point added later. The gate is
`session_store="postgres"` for the same reason `_default_owner_store` uses it — that switch is the
deployment's statement that a Postgres exists — with a lazy import so the dev/test path never pulls
psycopg for a store it will not use.

`agents/cli.py --audit-postgres` survives with narrowed meaning: it *forces* the durable sink for an
operator running a terminal session against a database without switching `session_store`.

**Consequences.**

- Three call sites stop being able to get this wrong, and so does the next one.
- Verified counterfactually at the decision line rather than by deleting the function: reverting
  only `sink if sink is not None else default_audit_sink()` back to `NullAuditSink()` fails
  `test_an_omitted_sink_no_longer_silently_means_log_only` and nothing else.

**Verified end to end.** `audit_events: 4 -> 5` on a live turn against the stub model, with
`default_audit_sink()` resolving to `PostgresAuditSink` under the load-test config.

That verification took two attempts, and the first one was wrong in a way worth recording. It
reported that the middleware never fires and warned that `enforce_tool_authz` — the RBAC gate,
registered the same way — might be inert too. The cause was the *test harness*: the stub model sent
`{"query": "benzene"}` while `find_notes` takes `text`, so every call failed argument validation
inside `agent_framework._tools._auto_invoke_function` and returned at the parse-error branch, which
sits before the middleware branch. No tool body ran, so nothing was audited. With the stub
corrected: `PIPELINE.EXECUTE fired n=4`, `exc=None`, and the row lands. RBAC was never affected.

What survives is smaller and is tracked separately in `BACKLOG.md`: a call rejected for bad
arguments is not audited at all (**AUDIT-2**), so the trail cannot answer "what did the agent
attempt and get wrong" — and the load runs' "100 tool calls" were all parse failures, so their
tool-path claim is being re-measured (**LOAD-1**).
