# Verdicts — api-frontdoor--security (lens: reachability + consequence)

In-scope findings (critical/high only): **1**. The other three findings in the file are labelled
medium and low and were not examined.

---

## Any authenticated user can enumerate and read every durable job's full result

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Independent repro — deliberately *not* the reporter's scaffolding. No patching of
`chemclaw.api.app.job_status` / `search_job_records`. Alice's record was written to the real
`job_records` table through `PostgresJobRecordSink`, and Mallory's request travelled the real
route → `front_door` → `durable_tools.job_status` → live Temporal (`localhost:7233`, returns
`NOT_FOUND`) → `lookup_job_record`. `entra_required=True`, `session_store="postgres"`, a real
RS256 token validated against a locally generated key, Mallory holding **zero** roles.

```
LIST 200 [{'job_id': 'bo-campaign-verifieralice1', 'connector': 'bo', 'job': 'campaign',
           'rationale': 'Alice: optimise the CC-7781 Suzuki coupling before the Feb gate',
           'summary': 'BO campaign for internal candidate CC-7781', 'note_id': '',
           'completed_at': '2026-08-17T05:14:48.849125Z'}]
GET  200 {'job_id': 'bo-campaign-verifieralice1', 'status': 'completed',
          'summary': 'BO campaign for internal candidate CC-7781',
          'result': {'best': {'value': 0.94, 'candidate': {'T': 55, 'solvent': '2-MeTHF'}},
                     'history': [{'value': 0.31, 'candidate': {'T': 20, 'solvent': 'DMF'}}]},
          'rationale': 'Alice: optimise the CC-7781 Suzuki coupling before the Feb gate'}
MISSING 404 {'detail': 'no such job'}
```

Also read/checked:

- `deploy/helm/chemclaw/values.yaml:341` → `CHEMCLAW_SESSION_STORE: "postgres"` and `:382` →
  `CHEMCLAW_ENTRA_REQUIRED: "true"` — so the enumerating path is live on shipped chart defaults
  (`_records_are_durable()` gates the whole read on `session_store == "postgres"`).
- `src/chemclaw/agent/authz.py:131-151` — `find_past_jobs` and `get_durable_job_status` are both in
  `READ_ONLY_TOOLS`; `tool_authz_default` defaults to `"allow"`
  (`src/chemclaw/core/config/entra.py:60`) and the chart does not override it.
- `src/chemclaw/agent/durable_tools.py:246` — the agent tool is
  `return await job_status(job_id)`, the *same* unscoped function the route calls.
- `grep -n "record_job\|JobRecord" src/chemclaw/durable/report_workflow.py` → **no output**. Only
  `chemclaw/durable/connector_job.py:399` calls `record_job`.
- `src/chemclaw/connectors/qm/workflows.py:170-176`, `calc/workflows.py:70-73`,
  `bo/workflows.py:197-205` — the three real `result` shapes.

### Why

**Reachability: fully confirmed, nothing upstream stands in the way.** No role, no gate, no
validator, no Helm default blocks it. `CurrentUser` is `Depends(require_principal)` and neither
handler reads the value; `search_job_records`/`job_status` take no actor argument, so the scoping
is not merely skipped, it is inexpressible. Reproduced from an HTTP request against the real store.
That half of the finding is exactly right, and the reporter earned it.

**Consequence: three claims do not hold as written, and together they move this off "high".**

1. **"A cross-tenant read" is false.** `validate_token` pins one `entra_audience` and one
   `entra_issuer`; every principal that can reach this route is a user of the same tenant. This is
   a cross-*user* read inside one organisation's authenticated employee population, not a tenant
   boundary crossing. The label is the single word that most inflates the severity.

2. **The route is not the exposure boundary, so the proposed fix closes one of two doors.** The
   finding reads `get_job` as a forgotten scope check ("`get_job`'s docstring makes no scoping
   claim of any kind"). But `get_durable_job_status` is a one-line passthrough to the *same*
   `job_status`, sits in `READ_ONLY_TOOLS`, and is open to every authenticated chemist under the
   shipped `tool_authz_default="allow"` — and its own docstring tells the model to use ids "found
   with `find_past_jobs`", i.e. other people's runs, by design. The design is aware of this: the
   `_report_id` docstring compensates for it explicitly ("`job_status()` — which applies no actor
   check, and which `find_past_jobs` explicitly points people at with other people's job ids"),
   which is why report ids are keyed on actor *and* roles. So this is an org-wide-readable policy
   the codebase has already reasoned about at least once, not an oversight local to one handler.
   Scoping `list_jobs`/`get_job` while chat still serves the identical bytes is a fix that would
   look like a closure and not be one.

3. **The demonstrated payload is the reporter's own stub, and the real payloads are thinner than
   it implies.** `'compound': 'CC-7781 (unpublished)'` is a value the reporter injected on the
   patch seam; nothing in the system puts a field like that in `result`. The three job types that
   actually write a `job_records` row are BO campaign (`CampaignResult` — best point plus the
   numeric observation history), calc reaction energy, and QM DFT (`QMResult`, which does carry
   `molecule_smiles`). And the QM structure is *already in the deliberately-unscoped listing*:
   `qm/workflows.py:171-174` builds `summary` as
   `f"{method}/{basis} on {result.molecule_smiles}: …"`. The chemist's free-text `rationale` is
   likewise in the listing. So the incremental exposure that `get_job` adds over the position
   `list_jobs`'s docstring already defends (and which the finding accepts as the deployment's
   stated position) is mostly the numeric result body — not the compound identity, which the
   accepted-as-deliberate half already discloses.

4. **The one case that would have made this genuinely high is not reachable.** If an
   entitlement-gated result were enumerable here, this would be an AD-group bypass rather than an
   internal over-share. It is not: only `ConnectorJobWorkflow` calls `record_job`, and the
   development report — the one job whose corpus depends on the requester's
   `chemclaw.sharedrive.reader` entitlement — writes **no** `job_records` row and so never appears
   in `GET /jobs`. Its workflow id hashes the actor *and* the role set, so a second principal
   cannot derive it either. `GET /jobs/{id}` on a guessed id of a *running* foreign workflow leaks
   only the word "running", and `completed_job_status` refuses a non-envelope result.

No safety or impurity-limit answer is involved on this path — the exposed content is optimisation
and QM output, not a hazard or specification verdict — so the "what would a chemist be shown" test
does not apply here.

**Net.** Real, reachable, worth fixing; but it is a *policy* decision about whether `job_records`
is an org-wide corpus (which two merged surfaces currently assert it is), not a missing check in
one route. Medium. If it is acted on, the change has to cover `find_past_jobs` /
`get_durable_job_status` in the same commit, or the leak simply moves into the chat window.
