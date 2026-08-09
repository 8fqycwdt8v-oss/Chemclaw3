# D-2026-08-08-the-inventory-that-vouched-for-itself — seven claims, re-measured, and the two that became tests

**Status:** accepted

## Context

This is lane T10 of the 2026-08-08 review campaign, and it runs last on purpose. Its findings are
seven *claims* — sentences in `CLAUDE.md`, `ARCHITECTURE.md`, a README table and four docstrings —
that a review measured and found false. Every other lane had by then edited the code those claims
are about. So the lane's first rule was not "correct the prose" but **re-measure every claim
against the tree as it now is**, because a lane may already have made a false sentence true, and
correcting a sentence that has become correct is a new false record rather than a fix.

That rule paid twice. Of the seven, **one was already true** and one was **half true**, and in both
cases the right action was to leave the corrected part alone.

The repository's own cautionary tale is the reason: a solvent-domination fix asserted by two
docstrings, an ADR and a closed backlog row, where the measured similarity was unchanged to the
fourth decimal. A lane about false claims that writes a false claim is that story again.

## What was measured, and what each measurement said

**1. "Every result is persisted once — never recomputed" (`CLAUDE.md`, `ARCHITECTURE.md`) — still
misleading.** Against the live store, eight concurrent `cached_compute` calls on one fresh key:

```
concurrent on a FRESH key: computes = 8  was_cached = [False × 8]
after the write landed:    computes = 0  was_cached = [True × 4]
```

`cached_compute` is an unguarded check-then-act. The gap is a `DEFERRED.md` row with its own
trigger, and in-flight dedup is deliberately *not* implemented here — but neither of the two files
`CLAUDE.md` tells a reader to internalize qualified the claim. Both now state the condition under
which it holds ("a *persisted* result is never recomputed") and carry the measurement.

**2. "the only place a finished job's result is collected" — false, and the divergence reached a
chemist.** Three sites collect one: `get_durable_job_status`, `chemclaw.agent.job_results`, and the
in-turn wait in `chemclaw.connectors.jobs`, which is a different subsystem and cannot route through
an agent tool. The first two shared `completed_job_status`; the third called
`ConnectorJobResult.model_validate` itself. On one identical bad result:

```
path A  ValueError: durable job 'job-1' completed but did not return the connector job
        envelope; the id does not belong to a job any launcher in this system started
path C  ValidationError: 2 validation errors for ConnectorJobResult
```

That is not an internal detail. A pydantic `ValidationError` **is** a `ValueError`, and `ValueError`
is precisely the family `connectors/server.py::_sanitize_tool_errors` passes through untouched as
"a deliberately-worded, caller-safe message" — so path C relayed pydantic's field dump to the model
verbatim while path A said which id was foreign and why.

**3. `infra/sql/README.md`'s Migration column was wrong for four of twenty-seven rows — and it was
the one column nothing checked.** This is the finding with the most durable value, because the
README's own prose vouches for it: "an inventory nobody verifies is read, believed, and wrong".
`tests/test_schema_inventory.py` compared the *set of table names*, in both directions, and said so
honestly — "it checks the set of tables, not the prose in the other columns". But **Migration is not
prose.** Which files touch a table is a fact the files state. Measured against the shipped set:

```
bo_suggestions      row says ['031']        files say ['031', '037']
calculation_results row says ['001','024']  files say ['001', '019', '024']
note_proposals      row says ['027']        files say ['027', '036']
session_messages    row says ['008','022']  files say ['008', '022', '026']
```

Every one is a later `ALTER TABLE` adding a column the row never mentioned, and every one is
confirmed against the live database (41 applied migrations): `bo_suggestions.job_id` (037),
`calculation_results.compute_seconds` (019), `note_proposals.dependencies` (036),
`session_messages.correlation_id` (026). The original review counted five; `document_chunks` was
fixed in the meantime by lane T6, which is exactly the outcome re-measuring exists to find.

**4. "`--admin` bypasses auth" — already true, except where a human reads it.** The two docstrings
the review named were corrected earlier in this campaign, and `cli_admin_roles` now exists.
Measured on the shipped config: `entra_required=False`, `skill_role_gates={}`, `cli_admin_roles=[]`
→ resolved roles `[]`. What was left was the `--help` string, which still said "bypasses Entra
auth; advertises all skills" — the one sentence a person actually reads at a terminal, and the one
that conflates "nothing is gated" with "this identity is privileged".

**5. "No durable state lives here", and an id an LLM cannot reproduce.** `agent/durable_tools.py`
defines the workflow ids and the reuse policy for three workflows, which is *defining* durable
identity rather than storing it — not a lesser thing. And `_report_id`, which an earlier lane had
already strengthened to fold in `requested_by` and sorted `requested_roles`, was still byte-exact
over the text a model writes:

```
base: report-b4cb729964c8d393
  sections swapped      same job? False
  title re-cased        same job? False
  heading re-cased      same job? False
  query trailing space  same job? False
```

The advertised idempotency ("re-requesting the same title and sections returns the existing job")
therefore held only for a byte-identical request from a caller that reorders and re-cases freely.
Each near-miss starts a fresh unbounded multi-section research run — the cost
`CORE_EXPENSIVE_ACTIONS` gates this tool to avoid.

**6. `known_documents` answers "any chunk", not "all chunks" — already true, and "any" is right.**
Both backends agree with each other and now with their docstrings, which lane T6 rewrote. Measured
on one document with one of five chunks moved to a new key, in-memory and against live Postgres:

```
in-memory known(KEY-B) = {'doc-…'}   stale under KEY-B = 4
postgres  known(KEY-B) = {'doc-…'}   stale under KEY-B = 4
```

That is correct, because this gate answers "must the crawl re-read and re-embed this file" and the
remaining four are the per-chunk drain's job — `stale_chunks` found exactly those four, and
`DocumentSyncWorkflow` drains before it crawls. The docstring was the bug and it is already fixed;
what remained was a `BACKLOG.md` row still describing the docstring as wrong.

**7. Two docstrings promised per-request identity; identity was frozen at the MCP handshake.**
`CallerLogMiddleware` binds the caller contextvars in `dispatch`, an ASGI task — but a tool body
runs in the session-manager task created by `initialize`. Over the real streamable-HTTP transport,
handshaking with alice's headers and calling the tool with bob's on the same `mcp-session-id`:

```
before:  tool body observed ('alice-oid', 'sess-alice', 'corr-alice')
```

Both docstrings asserted this was impossible — "each request runs in its own task context, so a
ContextVar set here is already invisible to the next one … no test can fail without it", and "so
one request's identity cannot leak into the next". The consequence is not a cross-user leak (two
independent MCP sessions showed no bleed) but a mis-attribution: the middleware's log line for that
call prints bob, because it reads the headers directly, while a durable row stamped by the same
call recorded alice. The two artifacts this feature exists to reconcile disagreed with each other.

## Decision

**Where a claim can be made true, make it true; where it cannot, state the condition it holds
under; and where it can be made *enforced*, prefer the test.** Concretely:

1. **Qualify, do not soften.** `CLAUDE.md` and `ARCHITECTURE.md` now say that a *persisted* result
   is never recomputed and that concurrent misses on one key each compute, with the numbers and a
   pointer to the `DEFERRED.md` row. In-flight dedup stays deferred; the claim stops overstating.

2. **One decode for a finished job.** `chemclaw.durable.connector_job.envelope_from_result` is the
   single place raw becomes an envelope or an error, and all three collectors call it. The module
   docstring stops claiming one collector and names the three, saying what *is* single. Two tests:
   one asserting the two paths produce the identical exception type and sentence, one structural —
   `ConnectorJobResult.model_validate` may appear in `connector_job.py` and nowhere else — because
   the defect was a second copy rather than a wrong one, and a fourth collector added tomorrow
   would reintroduce it silently.

3. **The Migration column is checked, not just corrected.** `tests/test_schema_inventory.py` gains
   a rule derived from the SQL: strip line comments, split on `;`, and credit a migration with a
   table when a statement *acts* on it. Matching the construct rather than the bare identifier is
   load-bearing — `observations` is both a table and a column of `bo_suggestions`, so "the name
   appears in the file" credits 031 with a table it only mentions as a column, which a candidate
   rule did on exactly one file out of forty-two.

   And a second test **refuses to pass over a statement shape it does not understand.** Without it,
   teaching the schema a construct the rule does not list would silently stop crediting that
   migration, and the column check would go stale in the way it exists to prevent. Proved by
   introducing `GRANT SELECT ON note_index TO chemclaw_app`, which the guard named and refused.

   The module docstring is rewritten: **Written by** and **Disposal** stay unverified because they
   are judgements, and it now says which column is a fact and why that distinction is the whole
   design.

4. **The `--help` line says what `--admin` does**: bypasses authentication only, authorization
   still applies, roles come from `CHEMCLAW_CLI_ADMIN_ROLES` and are empty by default.

5. **`_report_id` canonicalises the half a model writes, and only that half.** Title, headings and
   queries are whitespace-collapsed and casefolded, and the section list is sorted; every case in
   the table above now rejoins the run. `requested_by`, `requested_roles` and `memory_layer` stay
   byte-exact, and that line is deliberate: those three became part of the id as *access control*
   (a chemist without the share role must not join an entitled run), and folding two spellings of a
   principal or a role together is exactly the cross-user merge the key exists to prevent. A test
   pins each side — that a rephrased request rejoins, and that a re-cased actor or role does not.

   What this costs is stated rather than hidden: two requests differing only in casing or section
   order share one run, so the first requester's rendering wins. The second is not misled —
   `get_durable_job_status` reports the run's own summary, which names the title actually drafted —
   and a PR-gated draft is edited by a human before it becomes knowledge. The alternative is a
   second unbounded research run per rephrasing.

6. **"any chunk" is the correct predicate and now says so**, with the measurement and the reason
   (the drain is per-chunk and runs first) in the docstring, and the `BACKLOG.md` row corrected to
   stop describing a docstring that has been fixed. Per-document completeness is *not* adopted: it
   would re-embed a whole file to fix one chunk.

7. **A tool reads the caller of the call it is serving.** The serving request is reachable —
   `request_ctx` is set per JSON-RPC message and carries the ASGI request — so this is fixed rather
   than documented away. `_bind_caller_per_tool_call` binds and resets around each tool call, at
   the same `_tool_manager.call_tool` interception `_sanitize_tool_errors` already owns, as a
   separate function because they are separate concerns. With no request context (stdio, a direct
   call in a test) it falls through to what the middleware bound, which is today's behaviour.

   ```
   after:  tool body observed ('bob-oid', 'sess-bob', 'corr-bob')
   ```

   Both docstrings are corrected to say what is now true and to record what was false, and the
   regression test drives the real transport. Re-applying the mutation (dropping the call) returns
   `('alice-oid', 'sess-alice', 'corr-alice')` and fails the test by name.

## Consequences

- **Two claims are now defended by tests rather than by good intentions** — the Migration column
  and per-call connector attribution. A sentence a test defends cannot go stale; the other five
  corrections are still prose and can.
- **`_await_briefly` takes the workflow id.** Two call sites and one existing test line changed
  mechanically. The id is passed explicitly rather than read off `handle.id` because the handle is
  typed `Any`, so an attribute guess would not be checked and a parameter is.
- **`_report_id` changes value for every request.** Reports in flight at deploy time keep their old
  ids (Temporal owns them); a re-ask after the deploy starts one new run and then rejoins it. A
  one-off cost, on a tool whose runs are days apart.
- **The canonicalisation is a policy about free text, not about identity.** Anyone extending
  `_canonical` to `requested_by` or the roles would undo D-2026-08-08-identity-must-travel-with-the
  -work's fix; the test that pins the entitlement half exists to make that loud.
- **What this lane did not fix**, each a `BACKLOG.md` row with a trigger: in-flight dedup in the
  calculation store (a `DEFERRED.md` row with its own trigger, deliberately untouched);
  per-document completeness in `known_documents`; and migration 041's `DROP CONSTRAINT`, which
  `tests/test_migrations_are_additive.py` refuses — a red test this lane inherited rather than
  caused, and whose resolution (is replacing a primary key destructive, or is the guard's pattern
  too coarse to tell a constraint from a column?) belongs to the lane that wrote the migration.
