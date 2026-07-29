# D-034 — Review hardening: migration ledger, durable audit trail, injection framing, stmt timeout

**Context.** The in-depth review surfaced four hardening gaps in otherwise-green code.

**Migration ledger (`calc.migrate`).** The old runner split files on `;` (fragile against a
`DO $$ … $$` block or a semicolon in a string) and re-ran every statement each time, leaving no
record of what applied. Now each file is sent whole (psycopg simple-query protocol) and tracked
in `schema_migrations` (`infra/sql/000_…`) by filename + SHA-256; an already-applied file that
changes is rejected as drift (`MigrationError`) rather than silently re-run. The runner reuses
`chemclaw.db.connect` (redacted-DSN errors) instead of re-implementing the connect.

**Durable GxP audit trail (`agents.audit` + `agents.audit_store`).** The middleware logged to
stdlib only, with no identity, no correlation, no outcome, no durable store. It is now built
per-conversation (`make_audit_middleware`) stamping a `correlation_id` and an `actor` (the Phase-6
identity seam — `"unknown"` until Entra auth), capturing each call's outcome and a short effect
summary (e.g. the PR ref a `propose_*` returned), and emitting to an optional `AuditSink`.
`PostgresAuditSink` writes the append-only `audit_events` table (`infra/sql/006_…`); the default
stays log-only (`NullAuditSink`), so no DB coupling is forced on lightweight runs. A sink failure
is logged and swallowed — the audit store can never break a tool call. Args may hold user PII;
the char budget bounds what is stored (noted in the field docs). A tamper-evident hash chain is
left for Phase 6.

**Indirect-prompt-injection framing (`agents.framing`).** `expand_note`/`gather_evidence` fed note
bodies verbatim into context; ingested (non-agent-authored) notes bypass the PR-gate, so an
adversarial body was a live vector. Retrieved content is now wrapped in a `<retrieved-note id=…>`
envelope, paired with an agent instruction that envelope contents are evidence to cite, never
commands. Cheap, centralized, marks the trust boundary; full content-provenance stays Phase 6.

**Per-statement DB timeout.** `chemclaw.db.connect` gained an optional `statement_timeout_seconds`
(libpq `statement_timeout`), applied by both stores from `settings.pg_statement_timeout_seconds`,
so a hung query is cancelled rather than burning the whole enclosing activity budget; migrations
opt out (an index build may run long).

**Also:** an absolute `knowledge_dir` is rejected at startup (it would escape the note repo via
`Path` join); the memory-job corpus reader catches only `ChemclawError` (not bare `ValueError`)
and logs each skipped entry. The fingerprint bit-width "dual source of truth" was left as-is: a
width change already fails loudly (SQL `bit(<configured>)` insert vs the column, plus the
definition string), so a runtime assertion would be redundant defensive code.

**Result.** New/updated tests: `test_ids`, `test_config` (absolute `knowledge_dir`), `test_evals`
(A/B epsilon band, `bo_regret` case), `test_audit` (factory, sink, outcome, sink-failure),
`test_framing`, `test_postgres_store` (idempotent tracked migrate). `make lint type` green;
`make test` green (server/pg-backed cases skip offline, run in CI).
