# Verdicts — `sweep-authz.md`, lens: does it actually reproduce?

Scope: the two findings marked **critical** and **high**. Medium/low ignored.

Method note: none of the reporter's scripts under `/tmp/audit/` were run or read. Every number
below comes from scripts I wrote from the source, under `/tmp/verif/`. Docker/Postgres/Temporal
were already up (`docker ps` → `infra-postgres-1 (healthy)`, `infra-temporal-1`).

---

## Any authenticated principal can read every other principal's durable job — inputs, results, molecule structures

- **Verdict**: OVERSTATED
- **Severity I would assign**: high

### What I did

**1. Route dependency tree, re-walked myself** (`/tmp/verif/f1b.py`, resolving each
`route.dependant` recursively rather than reading a dump):

```
['GET'] /jobs             handler=list_jobs             deps=['require_principal']
['GET'] /jobs/{job_id}    handler=get_job               deps=['require_principal']
['DELETE'] /jobs/{job_id} handler=cancel_durable_job    deps=['require_principal']
```

The two GETs carry no resource-level dependency. (`DELETE` is gated in the body by `_is_reviewer`,
`api/routes/jobs.py:75` — real, and not part of this finding.)

**2. Live end-to-end against Postgres** (`/tmp/verif/f1.py`): migrated the real DB, wrote a
`job_records` row through the production sink `PostgresJobRecordSink.record` with
`requested_by="alice-oid"`, `session_id="sess-alice"`, then drove the real `create_app()` through
`TestClient` with `require_principal` overridden to `Principal(oid="mallory-oid")` — no roles, no
session, no relation to alice:

```
wrote row requested_by=alice-oid
GET /jobs -> 200
  our row visible to mallory: True
   {'job_id': 'qm-compute_dft_energy-VERIF1', 'connector': 'qm', 'job': 'compute_dft_energy',
    'rationale': 'VERIF: undisclosed lead series, pre-filing', 'summary': 'lead 42 barrier 18.3 kcal/mol'}
GET /jobs/qm-compute_dft_energy-VERIF1 -> 200
   {'job_id': 'qm-compute_dft_energy-VERIF1', 'status': 'completed',
    'summary': 'lead 42 barrier 18.3 kcal/mol',
    'result': {'geometry': 'SECRET COORDS', 'energy_hartree': -1234.5678},
    'rationale': 'VERIF: undisclosed lead series, pre-filing'}
```

The detail read went through the *real* path — `job_status` called `connect()`, Temporal answered
`NOT_FOUND` for the id, and `_recorded_status` served the row. Not a stubbed store.

Overriding `require_principal` is not scaffolding that props the finding up: I read
`api/auth.py:209-237` and `_principal_from_claims`, and a valid tenant token with **no** `roles`
claim yields exactly that `Principal`. No role is required to authenticate.

**3. The agent-tool half** (same script, ambient identity `mallory-oid` / role `chemist`):

```
find_past_jobs        in READ_ONLY_TOOLS: True | in DEFAULT_WRITE_TOOL_GATES: False | authorize_tool: ALLOWED
get_durable_job_status in READ_ONLY_TOOLS: True | in DEFAULT_WRITE_TOOL_GATES: False | authorize_tool: ALLOWED
```

with the shipped `tool_authz_default="allow"` and `tool_role_gates={}`.

**4. Is `requested_by` consulted anywhere on a read?** `grep -rn "requested_by" src/chemclaw` — the
only readers of the `job_records` column are `job_record_store.read_job_record` (decoding it into
the model) and `agent/leaver.py:225` (the erasure inventory). No read path branches on it.
That part of the finding is exactly right.

### Why

The mechanism, the trigger and the transcript all reproduce independently, on the arguments stated.
The symbols are real; the line numbers are off by a few in three places and worth correcting before
anyone greps for them (`job_status` is at `durable_tools.py:249`, not `:247`; `read_job_record` is
at `job_record_store.py:103`, not `:100`; `READ_ONLY_TOOLS` at `authz.py:131`). Not disqualifying.

What does not hold, and why the verdict is OVERSTATED rather than CONFIRMED:

1. **"This also invalidates the stated defence of the report path" is false.** I checked who writes
   the table: `grep -rn "record_job\b" src/chemclaw` returns exactly one production caller,
   `durable/connector_job.py:399`. `durable/report_workflow.py` contains **zero** references to
   `job_record`/`JobRecord`. So a development report never lands a `job_records` row, and neither
   `GET /jobs` nor `find_past_jobs` can publish a report id. The `_report_id` mitigation rests on
   id-unguessability, and its premise is untouched by this finding. That matters for severity,
   because the report is the only durable output whose content is derived from an *entitlement-gated*
   corpus (the AD-group-gated share). Nothing in this exposure crosses an entitlement boundary: a
   connector job's `payload` is what the launching chemist typed and its `result` is computed
   physics.

2. **Half of the exposure is an argued design position, not an oversight, and the finding's framing
   flattens that.** `durable_tools._framed_free_text` exists *because* `find_past_jobs` returns
   other people's text — it wraps a stranger's rationale as untrusted input on that ground. The
   cross-principal listing is deliberate and defended. What is genuinely un-argued is the second
   step: the full `result` + `payload` blob on the detail read. The reporter identifies this
   correctly in the fix section, but the headline claims the whole surface as a defect.

3. **The reach is insider-only within one deployment.** An authenticated principal of the tenant,
   with zero roles. That is a real confidentiality failure and I would fix it; it is not the
   anonymous or privilege-escalating shape "critical" should be reserved for here, especially given
   (1): no gated corpus, no role boundary, no write.

Net: high. The fix as written (scope the detail read on `requested_by`, put the check in
`_recorded_status` *and* carry the launching actor onto `ConnectorJobResult` so the live-Temporal
path applies the identical rule) is the right shape, and its point 3 — that gating only the record
path would mean "the gate exists only after Temporal history expires" — is the sharpest thing in
the report. Its point 4 (multiple legitimate requesters per shared run, because `job_workflow_id`
excludes the requester) is correct and I confirmed the id derivation at `connectors/jobs.py:254-261`.

---

## `entra_expensive_actions` is inert: an operator-gated connector job launches for any authenticated user

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

`/tmp/verif/f2.py`, written from `connectors/jobs.py::prepare_job_launch` and
`authz.expensive_actions()`, with the settings an operator following the knob's own documented
purpose would set (`CHEMCLAW_ENTRA_REQUIRED=true`, `ENTRA_PRIVILEGED_ROLES=chem-admin`,
`ENTRA_EXPENSIVE_ACTIONS=compute_reaction_energy`), ambient identity `bob-oid` holding only
`chemist`:

```
entra_required: True
privileged set: frozenset({'chem-admin'})
expensive_action_set: frozenset({'compute_reaction_energy'})
expensive_actions(): ['compute_dft_energy', 'compute_interaction_energy', 'compute_reaction_energy',
                      'request_development_report', 'sample_conformers', 'start_optimization_campaign']
manifest expensive flag: False
authorize_trigger(compute_reaction_energy): REFUSED — user bob-oid lacks a privileged role for compute_reaction_energy
prepare_job_launch(compute_reaction_energy): LAUNCH ALLOWED, payload keys: ['kind', 'level', 'products', 'reactants']
authorize_tool(compute_reaction_energy): ALLOWED
tool_authz_default: allow | tool_role_gates: {}
```

My numbers match the reporter's line for line. The `JobSpec` came from the live registry, not a
fixture: `src/chemclaw/connectors/calc/connector.yaml:99` declares `compute_reaction_energy` and
does not set `expensive` (the only `expensive: true` entries in that file are at `:180`
`sample_conformers` and `:201` `compute_interaction_energy`).

Source, verbatim, `src/chemclaw/connectors/jobs.py:302-303`:

```python
    if job.expensive:
        authorize_trigger(job.name)
```

`authorize_trigger` (`authz.py:357`) already begins `if action not in expensive_actions(): return`,
so the caller-side condition is a strictly narrower duplicate of the callee's own check — which is
exactly how the two diverged.

**Callers of `authorize_trigger` in production**, all of them
(`grep -rn "authorize_trigger" src/chemclaw --include=*.py`, excluding comments/docstrings): two.
`durable_tools.py:183` (`request_development_report`, unconditional — and already in
`CORE_EXPENSIVE_ACTIONS`) and `connectors/jobs.py:303` (conditional). So the operator's list gates
**nothing that the manifest derivation or `CORE_EXPENSIVE_ACTIONS` does not already gate**. Inert
is the correct word.

Template path: confirmed shared —
`durable/template_activities.py:186` calls `prepare_job_launch(connector, job, step.arguments)`, so
a template step naming the operator-listed job is equally ungated.

Config validator: confirmed at `core/config/entra.py:166`, which raises
`"entra_expensive_actions needs entra_privileged_roles: naming a gated action with no privileged
role refuses it to every user"` — a startup refusal protecting a deny-all case for a knob that
denies no one.

### Why — plus what the reporter missed, which makes it worse

It reproduces exactly, on the shipped manifests, with no fixture. The gate agrees the call should
be refused; the only production launcher never asks it; no other gate compensates (`authorize_tool`
allows it under the shipped `allow` default, and the name is in neither `tool_role_gates` nor
`DEFAULT_WRITE_TOOL_GATES`).

**Addition: the two tests that look like they cover this knob are vacuous.** Both
`tests/test_connector_jobs.py:410` and `tests/test_template_job_step.py:268` set
`entra_expensive_actions` to the job name *and* declare the job `expensive: true`
(`_EXPENSIVE_BUNDLE`, `tests/test_template_job_step.py:232`; `JobSpec(... expensive=True)` in the
other), so the refusal they assert comes from the manifest flag. I proved it by emptying the
setting in place and re-running:

```
$ # entra_expensive_actions -> "" in test_template_job_step.py, then:
$ uv run pytest tests/test_template_job_step.py -k expensive -q
.                                                                        [100%]
1 passed, 13 deselected in 2.94s
```

(file restored afterwards). The suite therefore reads as covering the operator knob while covering
only the derived one — which is why the divergence survived. The reporter's proposed regression
test (a non-`expensive` job named in `entra_expensive_actions`, asserting `prepare_job_launch`
raises) is exactly the missing case, and their fix — delete the `if job.expensive:` condition — is
correct and side-effect-free, since `authorize_trigger` returns immediately for any action outside
`expensive_actions()`.

Severity stays **high** rather than critical: the default chart leaves `entra_expensive_actions`
empty, so nothing regresses out of the box, and the manifest-derived half of the gate works. The
harm is a security control that an operator configures, that config-validates, and that silently
enforces nothing.
