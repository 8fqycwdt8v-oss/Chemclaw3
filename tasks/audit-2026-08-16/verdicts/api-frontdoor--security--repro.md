# Verdicts — API front door, security (reproduction lens)

Scope: only `critical`/`high` findings in
`tasks/audit-2026-08-16/findings/round1/api-frontdoor--security.md`.
That file has **one** in-scope finding (high). The other three are medium/low and were not examined.

Working tree checked against `HEAD` (`e319cdc`) before starting: no source file in this slice is
modified, so nothing below is an artifact of another agent's mutation experiment.

---

## Any authenticated user can enumerate and read every durable job's full result

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I did not run the reporter's `/tmp/audit/repro_jobs.py` and did not use its patch seam. I wrote my
own end-to-end reproduction that deliberately avoids the two pieces of scaffolding the finding
leaned on — `monkeypatch` of `chemclaw.api.app.job_status` / `search_job_records` — so the data
travels the real path:

- Alice's finished job written to the **real** `job_records` table via
  `chemclaw.durable.job_record_store.PostgresJobRecordSink` (Postgres up, `make up` containers
  running, migrations applied through `tests/pg.migrated_db_or_skip`, isolated test schema).
- `settings.entra_required = True`, real `entra_audience`/`entra_issuer`, and a **genuinely signed
  RS256 token** decoded by the real `chemclaw.api.auth.validate_token`. The only indirection is
  `auth._signing_key` returning a local public key — that is the module's own designed test seam
  ("The signing-key lookup is a single indirection (`_signing_key`) so tests validate real tokens
  against a local key without network"), and signature/audience/issuer/`exp` are all still verified.
- The caller is `mallory-oid-9999`, an identity that appears nowhere in the row.
- `GET /jobs/{id}` was **not** stubbed: it went through `routes.jobs.get_job` →
  `chemclaw.api.app.job_status` → `durable_tools.job_status` → `temporal_client.connect()` → live
  Temporal `handle.describe()` → `RPCStatusCode.NOT_FOUND` → `_recorded_status` → `lookup_job_record`
  → Postgres. (The presence of `rationale` in the response is the proof it took the record branch;
  that field is empty on the live-Temporal branch.)

Command: `uv run pytest tests/test_zzaudit_repro_jobs.py -s -q` (scratch file, since deleted).
Printed:

```
NO TOKEN GET /jobs -> 401
LIST status 200
LIST body [{'job_id': 'bo-campaign-auditalice1', 'connector': 'bo', 'job': 'campaign',
            'rationale': 'Alice: optimise the CC-7781 coupling before the Feb gate',
            'summary': 'BO campaign for internal candidate CC-7781',
            'note_id': '', 'completed_at': '2026-08-17T05:12:31.975216Z'}]
DETAIL status 200
DETAIL body {'job_id': 'bo-campaign-auditalice1', 'status': 'completed',
             'summary': 'BO campaign for internal candidate CC-7781',
             'result': {'compound': 'CC-7781 (unpublished)', 'best_yield': 0.94,
                        'conditions': {'T': 55, 'solvent': '2-MeTHF'}},
             'rationale': 'Alice: optimise the CC-7781 coupling before the Feb gate'}
CANCEL(non-reviewer) status 403 {...operator action...}
```

The `401` on the credential-less call establishes that the gate is genuinely enforcing; the `200`s
are therefore a real authenticated-but-unscoped read, not a dev-mode artifact.

Citations checked against current source, all real and current:
- `src/chemclaw/api/routes/jobs.py:18` = `async def list_jobs`, `:38` = `async def get_job`,
  `:55` = `async def cancel_durable_job`.
- `src/chemclaw/agent/durable_tools.py:80` = `result: dict[str, Any] = Field(default_factory=dict)`.
- `search_job_records(text, connector, limit)` (`durable/job_record.py:146`) and
  `job_status(job_id)` (`durable_tools.py:249`) take no actor argument — confirmed by reading both
  signatures. `JobRecord.requested_by` exists (`durable/job_record.py:59`) and the search projection
  `_SEARCH` (`durable/job_record_store.py`) neither selects nor filters on it.

### Why

Every element of the claim holds on my own reproduction: the trigger is reachable with nothing but a
valid tenant token and no role, `GET /jobs` returns another principal's row including their
free-text `rationale`, and `GET /jobs/{id}` returns the entire structured `result` blob. Nothing
upstream prevents it — `require_principal` authenticates and spends a rate budget, and that is the
only gate on either route.

Three things I would add or correct, none of which change the verdict:

1. **"Cross-tenant" is the wrong word.** `validate_token` pins `aud` and `iss`, so a token from
   another Entra tenant is rejected. The exposure is cross-*user* / cross-*team* inside one tenant.
   The body of the finding ("another group's optimisation output") describes it correctly; only the
   summary sentence overreaches.

2. **The proposed fix, applied where the finding proposes it, closes nothing.** The same two reads
   are exposed on the agent surface to the same population: `find_past_jobs`
   (`durable_tools.py:322`) is explicitly unscoped and `get_durable_job_status`
   (`durable_tools.py:214`) calls the identical `job_status(job_id)`. Neither has a
   `tool_role_gates` entry, and the shipped defaults are `CHEMCLAW_TOOL_ROLE_GATES={}` with
   `CHEMCLAW_TOOL_AUTHZ_DEFAULT=allow` (`.env.example:746-747`,
   `core/config/entra.py:59-60`), so any authenticated chemist reaches the same payload by asking
   the chatbot. Scoping only `routes/jobs.py` would leave the data one prompt away. The scoping
   argument belongs at `search_job_records` / `job_status`, i.e. below both surfaces.

3. **The codebase already knows.** `_report_id`'s docstring (`durable_tools.py:108-130`) states in
   as many words that "`job_status()` … applies no actor check", and treats that as a fixed
   constraint it must design a workflow id around rather than as something to fix. That is a claim
   about intent, not a refutation — but it does mean this is a standing access-model decision, and
   fixing it is a cross-cutting change, not a two-line route patch. I kept the severity at **high**
   anyway: `get_job` makes no scoping claim of any kind, and a connector job's `result` is the whole
   evaluation history of a campaign, which is not a "summary for cross-project reuse" by any
   reading.
