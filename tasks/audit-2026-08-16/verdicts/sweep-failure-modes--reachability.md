# Verdicts — `sweep-failure-modes.md`, reachability & consequence lens

Scope: the two **high** findings. The `medium`/`low` three are out of scope and untouched.

---

## An evidence source that fails is reported to the model as "nothing on file"

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)

- **What I did**

  1. **Reproduced the headline trigger through the real production path, with no monkeypatching of
     internals** — the only change is one environment variable pointing at a closed port:

     ```
     CHEMCLAW_POSTGRES_DSN="postgresql://nobody@127.0.0.1:59999/nope" uv run python -c "
     import asyncio; import chemclaw.agent.research_tools as rt
     print(asyncio.run(rt.gather_evidence(
         query='have we nitrated toluene before',
         reaction_smiles='Cc1ccccc1.O[N+](=O)[O-]>>Cc1ccccc1[N+](=O)[O-]')))"
     ```

     printed:

     ```
     File ".../core/db.py", line 133, in connect
       raise ConnectionError(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc
     ConnectionError: Postgres unreachable at user=nobody dbname=nope host=127.0.0.1 port=59999:
       connection failed: ... Connection refused
     TOOL RETURNED: [] len 0
     ```

     The raise is real production code (`core/db.py:133`), the swallow is `fanout._sweep:100-103`,
     and the caller `FingerprintReactionRetriever.retrieve` only catches `FingerprintError`
     (`retrievers.py:242`), so a `ConnectionError` walks straight into the sweep's bare `except`.
     This is the *structural prior-art* leg — literally "have we run this reaction before?".

  2. **Proved the two branch events are byte-identical** (`/tmp/v_fanout.py`, patching
     `fanout.stream_writer_or_none` to a collector; a dead source and a healthy-empty source):

     ```
     ERROR evidence source 'graph' failed; the sweep continues without it
     ConnectionError: postgres: connection refused
     TOOL RETURNED: [] <class 'list'> 0
     BRANCH EVENTS: [{'evidence_source': 'graph', 'chunks': 0},
                     {'evidence_source': 'documents', 'chunks': 0}]
     ```

  3. **Printed the tool description the model actually receives** from `registered_tools()`:

     > *"Results are merged and de-duplicated. **Empty is a valid answer (nothing on file), never
     > invented.**"*

  4. Checked what stands upstream: `verifier_enabled=False` (`core/config/llm.py:117`),
     `answer_shape_gate_enabled=False` (`:139`), and `grep -rn "VERIFIER\|ANSWER_SHAPE"
     deploy/helm/chemclaw/values.yaml` returned **nothing**, so the shipped chart leaves both off.
     `grep -n "evidence_source" deploy/helm/chemclaw/templates/prometheusrule.yaml` returned
     **nothing** — there is no alert on `chemclaw_evidence_source_failures_total` either.
     `grep -rn "capability_degraded" src/` shows the pre-turn announcement covers connectors and
     Temporal only (`api/runner.py:265`, `_durable_subsystem_reachable`); nothing probes or
     announces a retrieval source.

- **Why**

  Every leg of the finding holds under attack.

  *Reachability*: I could not find anything upstream that prevents it, and I produced it from the
  outermost configuration knob rather than by calling a private function. `gather_evidence` is a
  first-class model tool (`data/profiles/evidence.yaml:23`, and it is `expects_tools` on ~8 eval
  probes), so the path is HTTP request → turn → tool call → `sweep_sources` → dead store. Two of
  the three swallow sites do not even need `_sweep`: `ingest/documents/retriever.py:172-174` and
  `ingest/eln/warehouse/retriever.py:126-130` each end in a bare `except Exception: return []`
  whose own comment names the reachable case ("*a rate-limited or briefly unreachable embedding
  endpoint*"), so on those sources the empty-means-nothing outcome is the designed behaviour.

  *Consequence*: not a worse-sounding paraphrase. `[]` really is what the model gets, the tool
  contract really does define `[]` as "nothing on file", and the only live surface signal really is
  identical to a healthy miss — I printed both.

  One correction that does **not** change the verdict: the default `graph` source is filesystem-
  backed and is quite hard to make raise (I tried — an absent directory returns `0`, a corrupt note
  is skipped, so the finding's "Postgres unreachable" phrasing is wrong *for that one source*). It
  is right for the fingerprint leg, for the document share and for the warehouse — which is where I
  reproduced it, and which are the sources a real deployment adds.

  What the reporter missed, and it makes it slightly worse: `chemclaw_evidence_source_failures_total`
  has no alert rule at all in the shipped `prometheusrule.yaml`, so the server-side signal the
  finding calls "server-side only" is in practice a series nobody is paged on.

---

## A transient git outage is filed as per-entry bad data, and the ELN cursor advances past the lost entries

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)

- **What I did**

  1. **Reproduced `sync_entries`** with five well-formed entries and a submitter raising the real
     `GitSubmitError` (`/tmp/v_eln.py`):

     ```
     INFO eln sync: ingested=0 rejected=5 skipped_existing=0 awaiting_merge=0
     ingested : []
     rejected : [('rxn-1', 'git push origin note/reaction-rxn-1 failed: ...'), ... x5]
     NEXT CURSOR -> 2026-01-05 00:00:00+00:00      (since was 2025-12-01)
     rxn fingerprint rows: ['rxn-1', 'rxn-2', 'rxn-3', 'rxn-4', 'rxn-5']
     mol fingerprint rows: ['CC(=O)O', 'CCO', 'CCOC(C)=O']
     next run fetch floor: 2026-01-04 00:00:00+00:00
     entries next run re-fetches: ['rxn-4', 'rxn-5']
     ```

  2. **Reproduced it on the real durable path**, not just the pure function — the actual
     `ElnSyncWorkflow` on Temporal's server, with only the stores/submitter/cursor-IO swapped
     (`/tmp/v_eln_durable_probe.py`, run as a pytest under `tests/`, then removed):

     ```
     WORKFLOW ingested: []
     WORKFLOW rejected: [('eln-2026-001', 'git push origin note/reaction-eln-2026-001 failed:'),
                         ('eln-2026-002', ...)]
     PERSISTED CURSORS: {'eln-json': datetime.datetime(2026, 2, 3, 14, 30, tzinfo=TzInfo(0))}
     FINGERPRINT ROWS: ['eln-2026-001', 'eln-2026-002']
     1 passed in 0.65s
     ```

     So the cursor advance is *persisted*, not merely returned. `durable/eln_sync.py:243-249` is
     unconditional on the summary's contents; the activity returns **successfully**, so
     `BAD_DATA_RETRY` never retries it.

  3. **Traced the trigger to the raise sites.** `GitSubmitError(ChemclawError)`
     (`kg/git_submitter.py:112`) is raised for a failed push, for `flock` contention
     (`:101-106`, `LOCK_EX | LOCK_NB` — non-blocking, so contention is an *immediate* error with no
     retry), and by `_require_dedicated_checkout` (`:137`). `kg/pr_gate.py:126-139` records the
     FAILED proposal and **re-raises**, and `ingest/eln/sync.py:216` catches `ChemclawError` into
     the `rejected` bucket. `ingest/eln/ingest.py:51-58` writes both fingerprint stores *before*
     `propose_note`, which the fingerprint rows above confirm.

  4. **Checked the operator signals.** `grep -rn "notes_publish_failures_total" src/` — the counter
     behind the chart's `severity: critical` **`ChemclawKnowledgeNotesLost`** alert
     (`prometheusrule.yaml:104`) is incremented in exactly one place, `durable/publish.py:221`
     inside `publish_note_best_effort`. The ELN sync path never touches it. The only counter this
     path moves is `chemclaw_note_proposals_total{state="failed"}` (`kg/proposal.py:303`), and
     `grep -n "note_proposals_total" prometheusrule.yaml` returned **nothing** — no alert.

- **Why**

  Reachability is not in doubt: the trigger is any transient git failure during a scheduled sync,
  it is produced by a *scheduled* workflow with no human in the loop, nothing retries it, and the
  raise site is reached by the production `default_submitter()`. I confirmed the Helm chart does
  set `CHEMCLAW_NOTE_REPO_DIR` (`templates/config.yaml:25` ← `knowledge.noteRepoPath`), which is
  the one thing that would have made the trigger *permanent* by default rather than transient
  (`note_repo_dir` defaults to `"."`, which `_require_dedicated_checkout` rejects on every call) —
  so I am *not* escalating on that, but a deployment that misses the env var burns its entire ELN
  corpus in one run.

  The consequence is as stated and in two respects worse:

  - **Loss does not need a >1-day outage.** The re-entry window is `eln_sync_overlap_seconds` (86 400
    s, `core/config/eln.py:30`) measured back from the *new* cursor. My run lost 3 of 5 entries after
    a **single** failed batch, because the batch spanned five days. Any batch spanning more than the
    overlap — i.e. every chunk of a backfill drain — loses everything older than
    `newest_entry − 1 day` on one failure.
  - **The one alert for "knowledge is being dropped" is blind to this path** (evidence step 4).
    `ChemclawKnowledgeNotesLost` fires on `publish_note_best_effort` only.

  Two sub-claims are slightly overstated, neither enough to move the verdict or the severity:

  - *"The operator report is wrong about the cause"* — the summary counters do read `rejected=5`,
    but `sync.py:253-265` logs each rejection at WARNING **with the reason verbatim**, and my run
    printed `git push origin note/reaction-rxn-1 failed: fatal: unable to access ...`. The cause is
    in the log; what is wrong is the *bucket* (`IngestSummary.rejected` is the deterministic-bad-data
    bucket, and the cursor treats it as one), and that is the real defect.
  - *"recovery is a human action that is not implemented"* — under `session_store=postgres` (what
    the chart sets, `values.yaml:341`) the FAILED proposal rows carry the full rendered bytes and
    are exposed by `GET /proposals?state=failed` with `content` and `dependencies`
    (`api/routes/proposals.py`). So a manual recovery path exists. There is still no *replay*:
    `decide_proposal` refuses any row not in `OPEN` (`kg/proposal.py:230`), and nothing re-submits.
