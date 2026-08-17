# cli / evals / templates — security and hardening

Slice: `src/chemclaw/cli/`, `src/chemclaw/evals/`, `src/chemclaw/templates/`.
Lens: what untrusted input reaches, what fails open, what leaks.

Five findings. Scripts run under `/tmp/audit/` against the live venv; output quoted verbatim.

---

## A template run's idempotency key omits the requester and their entitlements

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/templates/registry.py:161` (`run_workflow_id`), consumed at `:186-207` (`build_template_tool.launch`)
- **Trigger**: two principals with different entitlements launch the same enabled template with the same declared inputs.

  ```python
  def run_workflow_id(template: Template, inputs: dict[str, Any]) -> str:
      return f"template-{template.name}-{stable_hash([template.name, inputs])}"
  ```

  The identity is resolved *after* the key is built and travels only inside the payload:

  ```python
  workflow_id = run_workflow_id(template, inputs)
  requested_by = require_actor()
  ...
  TemplateRunInput(..., requested_by=requested_by, roles=sorted(get_current_roles()), ...)
  id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
  ...
  except WorkflowAlreadyStartedError:
      return workflow_id
  ```

- **Consequence**: a template run is shared across principals whose entitlements differ, and the run
  executes under whichever principal launched it *first*. This matters because a template's output
  is entitlement-dependent: `TemplateRunInput.roles` is stamped ambient by
  `durable/template_activities.py` before each step, and the shipped `data/templates/hazard-briefing.yaml`
  ends in an `agent` step whose turn can call `gather_evidence` — which reads entitlement-gated
  sources as the requester (`ingest/documents/retriever.py`, "the entitlement gate lives here, and
  it is the whole security model").

  This repository has already diagnosed and fixed this exact flaw one module over. `agent/durable_tools.py:108`
  (`_report_id`) keys a report run on the requester **and** their roles, and states why:

  > "Keyed on the title, the section specs **and the requester's entitlement**. The last part is not
  > idempotency, it is access control, and leaving it out was a cross-user data exposure the moment
  > `retrieve_section` began reading entitlement-gated sources as the requester."

  The template launcher never received that fix. What is reachable *today* is the availability half
  and a silent wrong answer, not yet the disclosure half:
  - Bob asks for `run_hazard_briefing(smiles="CCO")` after Alice already ran it. He gets
    `WorkflowAlreadyStartedError`, the launcher returns Alice's id and returns **before**
    `record_job_started`, so his session gets no `job_started`, no push-back, and no mid-turn resume.
    His request simply did not happen and nothing says so.
  - Mirror case: Bob (no share entitlement) runs it first; Alice's identical request binds to Bob's
    narrower run.

  The *disclosure* half is blocked only by an unrelated defect, which makes the mitigation accidental
  rather than designed: `TemplateWorkflow` returns `TemplateRunResult`, which is not a
  `ConnectorJobResult`, so every reader of a job id (`get_durable_job_status` → `completed_job_status`
  → `envelope_from_result`, and `agent/job_results.await_job_results`) rejects it. That is itself a
  broken promise — `templates/registry._docstring` tells the model "The job id to poll with
  `get_durable_job_status`" — and the obvious repair (make templates return the envelope) opens the
  cross-user read immediately, because `job_status()` applies no actor check and the id is derivable
  offline by anyone who knows the template name and the inputs.

  `run_hazard_briefing` is not in `DEFAULT_WRITE_TOOL_GATES` and is not an expensive action, so under
  the shipped `tool_authz_default="allow"` any authenticated user can call it.

- **Evidence**: `/tmp/audit/repro_template_id.py`

  ```
  alice id: template-hazard-briefing-932aabc518a6cac7
  bob   id: template-hazard-briefing-932aabc518a6cac7
  identical: True
  recomputed by a stranger: template-hazard-briefing-932aabc518a6cac7
  readable through get_durable_job_status: NO -> durable job 'template-hazard-briefing-932aabc518a6cac7'
    completed but did not return the connector job envelope; the id does not belong to a job any
    launcher in this system started
  ```

  (`alice` and `bob` differ only in the ambient roles at call time; the key never sees them.)

- **Fix**: key the run on the requester and the sorted role set, exactly as `_report_id` does:

  ```python
  def run_workflow_id(template: Template, inputs: dict[str, Any], actor: str, roles: Sequence[str]) -> str:
      return f"template-{template.name}-{stable_hash([template.name, inputs, actor, sorted(roles)])}"
  ```

  and compute it after `require_actor()`/`get_current_roles()` in `launch`. Roles are what the corpus
  depends on; the actor is what the run is attributed to. Do this *before* fixing the result envelope,
  not after.

---

## `cli/live_jobs.report()` re-implements DSN redaction and leaks the password for two of the forms psycopg accepts

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/cli/live_jobs.py:377` (`report`)

  ```python
  f"· Postgres `{settings.postgres_dsn.rsplit('@', 1)[-1]}`\n",
  ```

- **Trigger**: run `make live-jobs` (or `python -m chemclaw.cli.live_jobs`) with
  `CHEMCLAW_POSTGRES_DSN` in either the libpq keyword form or a URL carrying the password as a query
  parameter — both are accepted by `psycopg`/`core.db.connection` and both are ordinary deployment
  spellings:
  - `host=db port=5432 user=chemclaw password=s3cr3t dbname=chemclaw`
  - `postgresql://chemclaw@db:5432/chemclaw?password=s3cr3t`

- **Consequence**: the database password is printed to stdout (CI logs) and written to disk by
  `main()`:

  ```python
  destination = args.report or Path(settings.live_probe_transcript_dir) / "durable-smoke.md"
  destination.write_text(text + "\n", encoding="utf-8")
  ```

  The default lands under `tasks/live-test/`, a directory whose artefacts this repository commits —
  `evals/phoenix.py`'s own docstring notes a run file "was only recoverable because it had been
  committed". So the credential goes to a report a reader is meant to keep.

  The correct redactor already exists two packages away and covers all three forms deliberately:
  `core/db.py:66` `_redact`, "Round-trips through libpq's own parser … so every form psycopg accepts
  is covered — URL userinfo, URL query parameter, and the keyword `host=... password=...` form — not
  just the userinfo case a URL split can see." `live_jobs` is exactly the URL split that docstring
  warns about.

- **Evidence**: `/tmp/audit/repro_dsn.py`

  ```
  DSN        : postgresql://chemclaw:s3cr3t@db.internal:5432/chemclaw
    live_jobs: db.internal:5432/chemclaw   LEAKS=False
  DSN        : postgresql://chemclaw@db.internal:5432/chemclaw?password=s3cr3t
    live_jobs: db.internal:5432/chemclaw?password=s3cr3t   LEAKS=True
  DSN        : host=db.internal port=5432 user=chemclaw password=s3cr3t dbname=chemclaw
    live_jobs: host=db.internal port=5432 user=chemclaw password=s3cr3t dbname=chemclaw   LEAKS=True
  ```

  `_redact` returns `user=chemclaw dbname=chemclaw host=db.internal port=5432` for all three.

- **Fix**: `from chemclaw.core.db import _redact` (promote it to a public `redact_dsn`) and use it
  here. One reader for one rule — the same argument this file makes about `_RUN_TEMPERATURE_K`
  living in three copies.

---

## The storm's SQL-injection check is a tautology, and its stated failure mode is wrong

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/cli/live_storm.py:904-916` (`family_h_edges`)

  ```python
  audit_before = await _scalar("select count(*) from audit_events")
  (inj,) = await storm("h-injection", turns=1, concurrency=1)
  audit_after = await _scalar("select count(*) from audit_events")
  ...
      ok=inj.status == 200 and audit_after >= audit_before,
      observed=f"audit_events {audit_before} → {audit_after} (a dropped table reads as 0)",
      detail="the string asks for `DROP TABLE audit_events`; the row count is the answer",
  ```

- **Trigger**: run the H family at all. The behaviour it drives sends
  `"'; DROP TABLE audit_events; -- <script>alert(1)</script> {{7*7}}"` as `find_notes(text=…)`
  (`cli/storm_behaviours.py:265`).

- **Consequence**: the check cannot distinguish a system that resisted the injection from one that
  did not.
  1. `audit_events` is INSERT-only and the injection turn itself writes an audit row for the
     `find_notes` call, so `audit_after >= audit_before` is monotonically true. Only the transport
     status can ever fail this row.
  2. The `observed` string's claim — "a dropped table reads as 0" — is false. `select count(*)` on a
     dropped table raises `UndefinedTable`, and `family_h_edges` has no handler; `run_storm` calls it
     bare at line 1245, so the exception aborts the whole run and takes families **A, E and B** —
     which run after H — with it. The one scenario the check exists for is the one that produces no
     finding at all.

  This is the shape the module's own preamble warns about: "a harness which can silently measure the
  wrong process is worse than none at all."

- **Evidence**: `/tmp/audit/repro_injection_check.py`, against the live `infra-postgres-1`:

  ```
  dropped table RAISES UndefinedTable: relation "audit_events_definitely_dropped" does not exist
  audit_events 12 -> 12; ok=True (the check's condition)
  ```

- **Fix**: assert the table still *exists and is queryable* inside a try/except, and assert the
  injected string round-tripped as data (the same shape the unicode check above it already uses —
  read the argument back out of `session_messages`/`audit_events` and compare):

  ```python
  try:
      audit_after = await _scalar("select count(*) from audit_events")
      observed = f"audit_events {audit_before} → {audit_after}"
      ok = inj.status == 200 and audit_after > audit_before
  except Exception as exc:                      # the table is gone, or unreadable
      ok, observed = False, f"audit_events is no longer queryable: {type(exc).__name__}: {exc}"
  ```

---

## `Probe.id` is unvalidated and is used directly as a filename in two writers

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/evals/probe.py:70` (`Probe.id`), written at
  `/home/user/Chemclaw3/src/chemclaw/evals/live.py:633` (`run_probes.one`) and read at
  `/home/user/Chemclaw3/src/chemclaw/evals/phoenix.py:118` / `cli/live_probes.py:184`.

  ```python
  id: str = Field(min_length=1)          # no pattern
  ...
  (out_dir / f"{probe.id}.json").write_text(...)
  ```

- **Trigger**: a probe YAML under the configured probe directory (`CHEMCLAW_LIVE_PROBE_DIR`,
  `CHEMCLAW_LIVE_M12_PROBE_DIR`, or `--probe-dir`, all operator-supplied paths) declaring
  `id: ../../../../etc/whatever`. `ProbeSet.model_validate` accepts it; `run_probes` writes there.

- **Consequence**: an arbitrary-path write outside the transcript directory, with attacker-influenced
  JSON content, driven by a data file rather than by code. The same id is later joined against
  Phoenix dataset examples, so a `..`-bearing id also silently reads a file the run never wrote.
  Every other declaration in this tree is pattern-anchored for exactly this reason
  (`Template.name`, `TemplateInput.name`, `_Step.id` all carry `^[a-z][a-z0-9_-]*$`); the probe
  corpus is the one that is not.

- **Evidence**: `/tmp/audit/repro_probe_id.py`

  ```
  pydantic accepted id: '../../../../tmp/audit/escaped'
  wrote to: /tmp/audit/escaped.json
  ```

  (`ls -l /tmp/audit/escaped.json` → the file exists, written from `/tmp/audit/transcripts/`.)

- **Fix**: `id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")` on `Probe`, matching the
  template and connector manifests. One line, and it closes both the writer and the reader.

---

## The CLI's audit actor is unauthenticated free text and reaches the durable approval record

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/cli/chat.py:65-88` (`resolve_identity`), `:308-312`
  (`--actor`), `:287` (`plan_approval_store().record(...)`)

  ```python
  parser.add_argument("--actor", default=None, help=f"Audit-trail actor label (default: {settings.cli_admin_actor!r}).")
  ...
  return actor or settings.cli_admin_actor, frozenset(settings.cli_admin_roles)
  ...
  await plan_approval_store().record(_CLI_SESSION_ID, plan_hash, actor, True)
  ```

- **Trigger**: `uv run chemclaw --admin --actor <a-real-entra-oid>` from a checkout holding only the
  runtime Postgres DSN.

- **Consequence**: `set_current_identity(actor, roles)` stamps that string as the ambient identity for
  the whole session, so `audit_events.actor`, `job_records.requested_by` and the `plan_approvals` row
  written by `/approve` all name a principal that took no action. There is no privilege gain — the
  roles come from `settings.cli_admin_roles`, empty by default (verified: `config/agent.py:140`
  `cli_admin_roles: list[str] = Field(default_factory=list)`), and the docstring's "confers identity
  and no entitlement" claim holds. What is lost is non-repudiation, and it is lost precisely where the
  file argues it matters most: the comment at `:282-286` says the approval record "is the artifact of
  the 'agent proposes, human decides' line" and must not "name an identity that took no action" —
  which a free-text flag makes trivially possible. The audit-trail INSERT-only grant
  (`D-2026-08-05-append-only-by-grant-not-by-contract`) is the control that makes the trail
  trustworthy, and it does not constrain *what* is inserted.

  Related and smaller: `_CLI_SESSION_ID = "cli"` is a fixed thread id justified by "the CLI is
  single-user admin by construction". Under `session_store=postgres` shared with a real deployment,
  every CLI invocation — including one run by a different operator with a different `--actor` —
  resumes the same checkpointed thread and reads the previous operator's conversation.

- **Fix**: restrict `--actor` to a label that cannot be mistaken for a directory principal — prefix it
  (`cli:<value>`) at the point it is stamped, or drop the flag and use `settings.cli_admin_actor`
  alone. Derive `_CLI_SESSION_ID` from the resolved actor (`f"cli-{actor}"`) so two operators do not
  share a thread.

---

## Checked and found sound

Stated so the absence of a finding is legible:

- **SQL construction** — every query in the slice is parameterized (`cli/explain.py` `_MESSAGES`/
  `_AUDIT`/`_JOBS`, `cli/live_jobs._scalar`, `cli/live_storm._scalar`, `evals/live._SESSION_COST_SQL`).
  No f-string reaches a cursor.
- **Dev servers bind loopback** — `cli/mock_llm.MOCK_HOST` and `cli/connectors_dev.DEV_HOST` are both
  `127.0.0.1` module constants, and `--port` is the only knob; neither can be talked onto `0.0.0.0`.
  `cli/leak_probe` pins `CHEMCLAW_SERVICE_HOST=127.0.0.1` with `setdefault`, so an operator's stricter
  environment wins.
- **Deserialization** — `yaml.safe_load` everywhere (`templates/registry._load`, `evals/live.load_probes`,
  `cli/live_probes._m12_probes`); no `pickle`, no `eval`/`exec`. The only dynamic imports are
  `importlib.import_module` over a fixed module path built from a *discovered directory name*
  (`connectors_dev._local_app`, `validate_templates._resolvable_signatures`), not from request data.
- **`templates/resolve.py`** — `re.sub` with a *callable* replacement, so a step result containing
  `\1` or `\g<0>` cannot be re-interpreted as a backreference; the reference grammar is anchored and
  closed; an unresolved reference raises rather than yielding `None`. This is the one place in the
  slice where tool output is substituted into a prompt and it does not widen anything.
- **`cli/chat.py`'s central claim** — "bypasses authentication, not authorization" — holds:
  `resolve_identity` raises `SystemExit` without `--admin`, and the roles come from
  `cli_admin_roles`, which really is empty by default.
- **`cli/validate_skills._role_gate_problems`** — the fail-open direction it claims to close
  (`skill_role_gates` naming a skill nothing provides) is genuinely checked, in the direction that
  matters, and the accompanying "this is not a privilege escalation" caveat is accurate.
- **`cli/validate_connectors._served_tool_problems`** — asks the running `FastMCP` server what it
  serves rather than trusting the manifest, and reports a renamed `server` object as a violation
  rather than passing vacuously. `_check_classification`, which its docstring leans on, does exist
  (`connectors/manifest.py:187`) and does constrain `state_changing`/`read_only` to subsets of `tools`.
- **`evals/live_judge`** — the judge's reply is bounded (`live_probe_judge_max_tokens`), parsed
  defensively, and an unparseable reply becomes `ungraded` rather than a verdict.
