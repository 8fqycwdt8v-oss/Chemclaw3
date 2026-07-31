# D-157 — A durable record of every connector job: what ran, with what data, and why

**Status:** accepted · **Context:** a review of what survives a finished Bayesian-optimization
campaign. Five defects, one cause. The BO connector is where they were found and the *seam* is
where they are fixed, because every one of them is true of every durable job this system runs.

## What was actually stored

A multi-round campaign is durable **while it runs**: `BoCampaignWorkflow` carries the observation
history as workflow state, each propose/evaluate is an activity, and a worker restart resumes it
exactly where it stopped. That part was right and is unchanged.

Once it *finished*, this is what remained:

| where | what it held |
|---|---|
| Temporal workflow result | `CampaignResult` — the best point **and every observation**. Expires with the namespace's retention window. |
| `calculation_results` | Per-*evaluation* calculator results, and only for a calculator-backed objective. Keyed by calculation input, so not walkable back to a campaign. |
| `bo-candidate` note | The best point. Opt-in twice over, and only after a human merges the PR. |
| `audit_events` | That a tool was called, with a truncated argument preview. |

So:

1. **The campaign's own data expired.** Nothing in this repository sets namespace retention; the
   deployment inherits the server's. When it lapses, the intermediate observations — the expensive
   part — are gone, and no other store has them.
2. **A later session could not find a past run at all.** The job id lives in the transcript of the
   conversation that launched it. `get_durable_job_status` needs that id, and asked Temporal, which
   by then may have forgotten it. There was no listing tool of any kind.
3. **Nothing recorded why any job was ever run.** Not BO, not QM, not any connector. `Note` has no
   intent field, result notes are output-neutral by design (D-005), and an audit row says a tool was
   called, not what question it was meant to answer.
4. **The recommendation note was not self-contained.** "1.2 mol% Pd" means one thing when the space
   went to 5 mol% and another when 1.2 was the ceiling; the note carried the point and not the space.
5. **A campaign could finish and leave nothing behind.** `publish_to_graph` existed *twice*: on the
   manifest (the deployment's intent, `true`) and on `CampaignSpec` (model-authored, default
   `False`). With the second unset the campaign published nothing — and per (1), the result then
   expired.

The common cause: **the connector-job seam kept no record of a run.** Temporal is an execution
engine, and we were using its history as an archive.

## Decision

**Core's `ConnectorJobWorkflow` writes one durable record per finished connector job**, and a
launch must state its reason.

- `infra/sql/023_job_records.sql` — one row per finished run: `payload` (the launch arguments, so a
  campaign's whole decision space), `result` (the entire `ConnectorJobResult.data`, so its whole
  evaluation history), `rationale`, the actor, the session, the correlation id, and the note the run
  proposed. Self-contained: reading it back reconstructs the run with no Temporal, no session and no
  graph.
- `ConnectorJobInput.rationale`, required and non-blank, supplied by the generated launcher as an
  argument beside the params. A template `job` step passes its declared `purpose` — a template
  already says why each of its steps exists.
- `get_durable_job_status` falls back to the record when Temporal does not know an id, and
  `find_past_jobs` searches past runs by the words their reason used.
- `note_with_run_provenance` stamps the reason and the run id onto **any** connector's note before
  the PR-gate, so the merged markdown answers "why was this done".
- `CampaignSpec.publish_to_graph` is deleted; the manifest flag is the single decision. The BO note
  gains the decision space and the objective direction.

### Why in the wrapper, and not in the BO bundle

The same argument as the PR-gate, the actor stamp and the session push-back: an obligation that
must hold for *every* capability belongs to the one wrapper they all run inside. `audit.py`'s own
history is the evidence — the durable audit sink was constructed at one call site, the deployed
service passed nothing, and the compliance record was empty in production while every document
called it the trail. "Each connector remembers" is the discipline that fails silently, so the
record and the note footer are written where no connector can forget them.

### Why the rationale is not in `payload`

`payload` is hashed into the workflow id, which is the idempotency key (D-011). A reason folded in
there would make "the same campaign, explained differently" two separate expensive runs. It rides
beside the arguments instead — required at the boundary the model calls, rejected when blank, in
the same reject-if-absent shape as `require_actor`.

### Why the record is best-effort, and why it is logged at error

The science is finished by the time it runs. Failing a completed job on a database write would mark
it failed, and `ALLOW_DUPLICATE_FAILED_ONLY` would then let a re-ask *re-execute* the campaign —
the worst outcome available. So it is bounded-retry-then-continue, like the note publish. Unlike the
note publish it logs at error level: a failed proposal can be re-proposed from data that still
exists, while a failed record loses data nothing else holds.

## Consequences

- **Every durable job now costs one row.** Bounded by runs, not by data volume; the two indexes are
  the two orders anything reads it in.
- **`job_records` is not pruned**, and `durable/retention.py` says so out loud. Ageing these rows out
  would restore, one retention window later, precisely the failure this removes. Disposal needs the
  same archive-then-record design `audit_events` needs, in that ADR.
- **Under `session_store="memory"` the record is a null sink.** The searches answer "no history"
  rather than raising — the honest answer for a deployment with no database, and identical to what
  an empty table returns.
- **Every generated job tool's signature changed.** In-repo callers are the launcher tests and the
  template path; both are updated. A tool that takes a mandatory free-text argument is a real cost
  to the model's call budget, accepted deliberately: it is the whole feature.

## What this deliberately does not do

- **Failed runs are not recorded.** A child failure propagates and never reaches the write. The
  attempt is in the audit trail; a *failure* record is a second design — which status, written from
  where, whether a later success supersedes it — and it is in BACKLOG rather than smuggled in here.
- **`request_development_report` gets no record.** It does not run through `ConnectorJobWorkflow`
  (D-115), so it would need its own write site. Its gap is also much smaller: a report's artifact is
  a PR-gated note whose headings say what it is about. BACKLOG.
- **Nothing sets Temporal's namespace retention.** That is a deployment decision and remains one;
  this ADR removes the *dependence* on it rather than choosing a number.
- **The search is `ILIKE`, not a `tsvector`.** One row per durable run is thousands of rows, and a
  reason is a sentence rather than a document. A search index would be machinery to maintain for a
  scan the database does in milliseconds. Revisit when a deployment's table says otherwise.
