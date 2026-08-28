# BACKLOG

The things worth doing next, highest-consequence first. Top = next.

**This is a queue of what is still open, not a log of what was found.** A closed item is **deleted**
from it in the commit that closes it; the commit is the record and `git log` is the history. Do not
strike a row through, do not append "**Done**" under it, and do not add a dated section explaining
that a row above has gone stale. That is exactly how this file reached 4,717 lines and 237 open rows
in twenty-one days, growing about three lines for every line removed — the same failure `DEFERRED.md`
had and D-154 fixed there with this one rule.

**Rows are grouped by what they ask for, not by which review produced them.** A finding's date and
its reviewing pass are provenance, and provenance belongs in
[`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md) — the long-form record of every
row this queue has ever carried. How many rows either file holds is a `grep`, not a sentence —
`grep -c '^- \[ \]' docs/archive/findings-2026-08.md` and the same over this file — and the two
counts do not subtract: promoting a row **restates** it, so a queued row is still open there under
its original wording, and matching the two sets by title matched only 7 of the 30 this queue held
when that was measured. §5 is the first thing here that is not a defect this repository found
in itself, and none of its rows is in the archive at all. The overlap is real and unmeasurable by
`grep`, which is why neither number is a difference. When a queued row needs its full measurement
history, that file has it under the review that found it.

Both counts *were* written here, and both were wrong — 223 against 221, and 41 against 45 — each
printed beside the command that disproves it, which is the whole argument of
`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`. A number nobody re-derives is a claim
about its author's afternoon; `tests/test_backlog_register.py` keeps one from coming back.

**A row must name an anchor in the tree** — a module, a line, a manifest key — so any row can be
checked with one `grep` instead of an argument. A row that cannot name one is not ready to be
queued.

**A row is a claim about the code, and claims go stale.** A 2026-08-17 pass opened every anchor this
file then held and found seventeen rows not workable as written: four described code that a merged
decision had already deleted or fixed, eight were misstated in a way that would have sent someone to
the wrong function, and three carried their own deferral trigger and belonged in `DEFERRED.md`. Two
stated the opposite of what the tree does — one pointed at a `DEFERRED.md` row that does not exist,
and one said a data-subject erasure route was missing while `make user-erase` implements it across
**twelve** tables with a dry run and per-table counts
(`python -c "from chemclaw.agent.leaver import _ERASE; print(len(_ERASE))"` → 12: seven always,
plus the checkpointer's three and the store's two, each skipped when the deployment has not created
it). **Before working a row, check it against `HEAD`**; if it is wrong, the fix is to correct or
delete the row, and that is as much a contribution as the code would have been.

Ten further rows arrived from concurrent reviews while that pass ran and are carried here unedited —
they postdate it and have not been re-verified against `HEAD` by anyone but their author, which is
exactly the state the paragraph above is about.

**And the pass that wrote the two paragraphs above is not exempt from them.** An audit later the
same day found its own numbers stale in the way it was written to catch: it said "nine tables" for
an erasure that clears twelve; it filed a hazard as unpinned that
`tests/test_connector_registry.py:293` had already pinned; and five of the anchors it wrote no
longer resolved to the construct they named by the time the branch was audited, four of them off by
a handful of lines. None of that is neglect — it is a measurement taken hours before the tree moved
under it, which is the failure mode this file has rather than an exception to it. Re-measure on the
way past; a row you had to correct before you could work it is a row that was worth opening.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes the actor as an argument sourced from core's validated front-door
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:126` — the row named a
      field called `actor`, which does not exist), and never reads the header. The real reach is two
      columns on the synchronous MCP path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py:393`. The `unverified:<id>` marking is in place (D-2026-08-13),
      so what is open is that a caller still chooses the string. A bearer on the row above proves
      *core called*, not *which chemist*, so full closure needs an actor assertion bound to the call
      (OBO or a signed memo) — which is the `DEFERRED.md` warehouse row's blocker too.
      **Narrowed 2026-08-27** (`D-2026-08-27-a-bound-that-multiplies-…`): the claim no longer
      travels back out as provenance — `CampaignThread` dropped `opened_by`, because a reader of a
      resumed campaign cannot tell a marked actor from a verified one. Both columns keep the value
      for the audit trail, where that question can be answered. What stays open is unchanged: the
      string is still the caller's to choose.

- [ ] **The per-round campaign record is unbounded and best-effort** — [S], both halves found by
      reviewing `D-2026-08-27-a-bound-that-multiplies-…` after it merged. (a) Each round's row
      snapshots the *cumulative* history, so N rounds store a triangular number of observations:
      measured at 173 B/observation, a 500-round batch-1 campaign holds **22.19 MB** against the
      terminal write's 87.4 kB (254x), and 87.45 MB at batch 4. `retention.py` refuses to prune
      `bo_campaigns` and `bo_suggestions` cascades from it, so nothing reclaims it. The snapshot is
      what makes an interrupted campaign resumable, so the fix is not "store less per row" but
      "record every Nth round", trading a bounded number of lost rounds for an N-fold reduction.
      **Trigger:** the first deployment that runs durable campaigns at all — the capability map
      records that none ever has, which is why this is [S] and not urgent. (b) The write is
      best-effort: `record_suggestion` swallows `_TRANSIENT_WRITE_FAILURES` and returns normally,
      so the activity succeeds on a round that never landed and Temporal never retries it. Making
      the durable path's guarantee unconditional means letting that caller opt out of the swallow,
      which is a change to a contract the inline tool depends on.

## 2 — Answers that are wrong without saying so

- [ ] **The `chem` enumerations and `compute_fukui_at` are served; the merge has landed and what
      remains is a live-lane confirmation** — [S]. `Chemclaw3-mcp#18` merged 2026-08-27 (commit
      `90e7486`): `enumerate_tautomers`, `enumerate_protonation_states`, `enumerate_stereoisomers`,
      `enumerate_bond_cleavages` and their siblings now exist in `servers/chem/.../tools.py`, and
      `compute_fukui_at` (which `connectors/calc/compose.py::ensemble_property` calls) exists in
      `servers/calc/.../tools.py`, so the six templates `D-2026-08-25-the-loop-is-a-composite-not-a-template`
      added can complete. Delete this row once the live lane has actually run one of those templates
      end to end — `make template-validate` still cannot see the difference (`chem` is a bundle this
      repository declares and does not run, so its tools are name-checked and argument-unchecked),
      and `make connector-validate` against a running server is what would; no live-lane transcript
      postdating the merge exists yet.
      **`transform_structure` was the seventh name and is now gone from the manifest** rather than
      implemented: it had no caller, no template, no skill reference and no documented signature in
      either repository, so serving it would have meant inventing its contract.

## 3 — Work that is lost, dropped or invisible

- [ ] **A development report's durable run has no correlation id to stamp** — [S].
      `ReportRequest` and `SectionRequest` (`retrieval/harness.py:38,68`) carry `requested_by` and
      `requested_roles` and no correlation id, so `report_workflow.retrieve_section` and
      `propose_report` stamp an actor and nothing that joins the run to the turn that asked for it —
      the log lines and the PR-gated draft both book an empty one. `ConnectorJobInput.correlation_id`
      (`durable/connector_job.py:142`) is the shape to copy, and `request_development_report` runs
      inside a turn where `get_current_correlation_id()` is bound, so the id exists at the launch.
      Left out of `D-2026-08-27-a-step-runs-under-the-correlation-id-it-was-launched-with`
      deliberately: that ADR fixed the sites that already carried an id, and inventing one here
      would make an unjoined run look joined. `durable/memory_jobs.py:178` is the same shape and is
      *not* this row — a synthesis job is system-triggered, so there is genuinely no turn.

- [ ] **A timed-out parse still runs to completion on the worker thread** — [L]. **The cheap half
      is closed**: `ingest/documents/sync.py::_parse_changed` now bounds its `asyncio.to_thread`
      with the front door's own `attachment_parse_timeout_seconds` and counts the outcome as
      `skipped_timeout` through every rendering a run is read through. What remains is the half
      that was always [L]: `agent/attachments.py:284` shields the future deliberately, so on both
      paths the timeout frees the caller and the slot and never the thread — no parser behind
      `parse_document` offers an interruption hook, so a hostile document still burns a worker to
      completion in the background. The only real fix is a killable subprocess, with pickling and a
      new child-OOM failure mode to classify (~150-250 lines).

## 4 — Operating it

- [ ] **Settle `pytest-xdist` on a real runner** — [S].
      The `check` job is 87% one step: `make lint type cov` was **12m06s of a 13m56s job** on
      `d8c312a`, of which lint is 1s and type 68s (measured), so ~11 min is the suite itself.
      `D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one` took the free
      half — lint and type now run in parallel in `static` — and deliberately left this one open,
      because the evidence for it is a *reading* rather than a measurement.
      **What the reading says**: the suite looks parallel-safe already. `tests/pg.py` suffixes its
      `TEST_SCHEMA` with `os.getpid()` at import time, so an xdist worker gets its own
      Postgres schema with no change at all, and the two files that use Temporal go through
      `start_time_skipping()`, which binds an ephemeral port per environment. `pytest-cov` combines
      across workers natively, so the 84% floor survives.
      **Why it is not done**: "looks safe" is not a number, and the sandbox this was reviewed in ran
      the suite far slower than a GitHub runner does, so a local figure would say nothing about CI.
      The unknowns worth checking are tests that write into the repo tree rather than `tmp_path`,
      and whether four workers on a 4-core runner contend on the single Postgres service container.
      Closing this is one experiment: add `pytest-xdist`, run `-n auto` on a branch, compare the
      job's wall time and its failure set against the serial run on the same commit. If it is not
      a clear win, say so and delete this row.

- [ ] **Two of the four deployables have no chart, so a release changes their bytes and nothing
      else** — [M]. `D-2026-08-26-a-release-is-a-descriptor-and-a-target` deploys `Chemclaw3_ui`
      and each `Chemclaw3-mcp` server with `oc set image` against a Deployment an operator created
      by hand, because neither repository describes itself deployably: the fleet has seven
      `Containerfile`s and a per-server `networkpolicy.yaml`, the UI has a `Dockerfile` and a
      compose file for local work. That is the honest minimum — it changes the image and claims
      nothing else — and it means a release cannot move a port, a probe, a resource limit or an
      env var for either, and cannot create either from nothing. A chart per repository (or one
      chart for the fleet, whose seven servers differ only in name, port and token env) closes it.
      Not written from here, because doing so would be inventing somebody's Service, Route and
      limits; it wants one real namespace to be written against.

- [ ] **Turn the image scan back on, with its contradiction resolved** — [M].
      Carried forward unchanged from the SBOM work and re-confirmed by the 2026-08-26 CI review:
      `image.yml` now emits an SBOM and pins/verifies both binaries it downloads, but there is
      still no scan of the built image. It ran once, found three real classes of problem now fixed
      in `deploy/Containerfile`, and then reported two packages the build's own exhaustive
      filesystem listing says are not present. A gate whose last word contradicts the artifact it
      scanned makes every future red build ambiguous, so it goes back on with its own change rather
      than riding along on someone else's. The SBOM is now `main`-only, so a scan reading it is
      `main`-only too.

- [ ] **`read_corpus` re-reads the entire ELN from `datetime.min` on every call** — [M].
      `durable/memory_jobs.py:82-86` calls `fetch_new_entries(datetime.min.replace(tzinfo=UTC))` on
      every ingest half inside `read_corpus`, so each of the three memory jobs (`build_campaign_notes_activity`,
      `build_playbook_notes_activity`, `build_optimization_notes_activity`) walks the whole record
      from the beginning of time, and `all_reactions()` is called once per activity. On the two
      file-drop exports this costs nothing; against a real warehouse ELN it is a full table scan
      per activity per scheduled run. `ElnAdapter` (`ingest/eln/adapter.py:128`) has exactly two
      methods and neither is a fetch-by-id, so there is no cheaper read to reach for — closing this
      means either a fetch-by-id on the adapter protocol (every source pays) or a derived store of
      mapped `OrdReaction`s.
      **Found while building the protocol condenser and deliberately not fixed there**
      (`D-2026-08-25-the-structure-is-discarded-at-the-note-boundary` records the reasoning): a
      derived store would have answered it as a side effect, and answering a scaling problem as a
      side effect of a retrieval change is how a store nobody decided on gets built. It is also the
      trigger on the `DEFERRED.md` row for reagent/solvent set diffs in the turn-time comparison —
      one change answers both.

- [ ] **The results store has no live target** — [M]. `D-2026-08-25-a-cache-is-not-a-record` ships
      the whole path — `src/chemclaw/publish/`, the canonical schema in `schema/result-store/`, two
      drivers, the outbox (migration 050) and the drain — and it is proven end to end against a
      local Postgres running the shipped DDL (`tests/test_publish_sql.py`). What has not happened is
      an actual deployment pointing at an actual results database: `CHEMCLAW_RESULT_SINKS` is empty
      by default and `src/chemclaw/publish/sinks/postgres/sink.yaml` addresses a host nobody runs.
      Attaching one is configuration (`make sink-schema`, apply, set the variable), so this is a
      deployment action rather than code — but until it happens, no number below has been measured
      against a real corpus. `D-2026-08-26-a-route-is-not-a-shape` is why that matters more than it
      reads: the composite half of the path was inert for a release and no test noticed, because
      every test started at a projector rather than at a hook. A live target is the only thing that
      would have made it obvious.
- [ ] **Nothing has measured how many rows a real corpus produces** — [M]. The volume risk named in
      `D-2026-08-25`: `cached_compute` publishes on every miss, and a conformer search projects one
      record with ~47 conformer rows plus their structures. Before publishing is enabled by default
      anywhere, run `python -m chemclaw.cli.backfill_publications --dry-run` against a populated
      deployment and count rows-per-calculation per `calc_type`. That growth curve is also what
      decides the deliberately open question of whether `property_value` needs partitioning, and on
      what — a partition key chosen before the row count is known would be a guess.
- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. `docs/guides/runbook.md:972-997` (§ xiii, "Restore a
      store") states what this system *requires* of those stores and documents a Postgres restore
      procedure — what does not exist
      anywhere is tooling that performs or **verifies** a restore, and that cannot be built against
      a store this repo does not own. (The former separate "no backup tooling" row is folded in
      here; it was downstream of this one and overcounted the stores.)

- [ ] **The image vulnerability scan is not merged as a gate** — [M]. The runbook's false claim that
      it runs is corrected (2026-08-17) and
      `tests/test_deploy_chart.py::test_every_supply_chain_gate_the_runbook_names_actually_runs`
      keeps it corrected. **State the guarantee, not the implementation:** no supply-chain tool the
      runbook's §(xiv) claims — in the gate table *or* in the prose beside it — may be one that
      `image.yml` does not actually execute. It does not prove the named gate is *blocking*, only
      that something runs it. This row said it "fails if the runbook names a gate nothing runs",
      which was one degree stronger than the assertion then in the tree: the check was a substring
      over the workflow, so a comment naming the tool satisfied it, and only backticked table rows
      were read at all. Both holes were found and closed the same day — which is the argument for
      naming the guarantee rather than the mechanism, since the mechanism changed under this row
      within hours of it being written. The gate itself is still absent: `trivy`
      appears nowhere in
      `.github/workflows/image.yml`, which already builds `chemclaw:ci` locally on every PR, so the
      step needs no registry. Held for a stated reason — per D-2026-08-01 the candidate scan
      reported `setuptools` 70.3.0 and `msgpack` 1.1.2 while an exhaustive `find / -xdev` in the
      same build listed neither, and a gate whose last word contradicts the artifact it scanned
      makes every red build ambiguous. Re-check that against a current trivy before merging.

- [ ] **The note reindex prunes a shared index against one pod's disk** — [M], and it is what the
      singleton row became once the audit ran
      (`D-2026-08-27-what-a-second-background-worker-would-race-on`). The two suspects that row
      named are both safe — every Schedule carries SKIP overlap, which Temporal enforces
      server-side, and a lost ELN-cursor update was measured to move the mark *backwards*, so the
      corpus is re-ingested rather than skipped. One worker was never single either: the worker
      runs eight activities at once by default, so `replicas: 1` only ever excluded pod-local
      state.
      The real blocker is `retrieval/vector_index.py::reindex_notes`, which calls `retire_absent`
      over the notes on *this pod's* disk while `note_index` is shared — and that disk is an
      emptyDir each pod's sidecar refreshes on its own schedule. So a merged note reaches pod A,
      a run there indexes it, the next run lands on B and retires it, alternating; the existing
      guards refuse an *empty* scan, not a *lagging* one. Closing it means keying the prune on the
      commit the index was built from, so a pod whose checkout predates it declines to prune — or
      pinning the reindex to one pod. That is the single change gating `replicas > 1`.

- [ ] **The knowledge graph coming *in* has no signal, only the graph going *out*** — [S].
      `ChemclawKnowledgeNotesLost` alerts on a note that failed to reach the PR-gate. Nothing covers
      the other direction: `deploy/knowledge-sync.sh`'s `loop` catches a failed refresh so a dead
      remote cannot kill the pod (correct), and the pod then serves a frozen corpus indefinitely
      while logging one WARNING per interval into a stream nobody tails. On an expired push
      credential — the exact cause `templates/prometheusrule.yaml` names for the notes alert — the
      graph silently stops moving and every answer keeps citing it.
      **The deploy half shipped**: the script stamps a heartbeat on each successful refresh and the
      sidecar has an `exec` liveness probe reading its age, so a wedged loop becomes a restarting
      container instead of a quiet one (`tests/test_deploy_chart.py`). That is a degraded
      substitute and says so — a container restart is not a metric, it needs kube-state-metrics to
      alert on, and those series are not in the user-workload Prometheus that evaluates our rules.
      **What is left is in `src/`**: a `chemclaw_knowledge_sync_age_seconds` gauge bound through
      `Metrics.bind_gauge_family` on the process that *reads* the tree — it already resolves
      `settings.knowledge_path`, so the age of the newest note there is one `stat()` — plus its
      rule, which then works on any cluster because it reads a first-party series. The sidecar's
      heartbeat and that gauge answer the same question from the two sides of one volume; ship the
      gauge and the probe becomes belt-and-braces rather than the only signal.

- [ ] **The background worker is a singleton with no PDB, and the PDB is not the fix** — [M].
      `poddisruptionbudget.yaml` covers the front door alone and argues that correctly in the
      template: `minAvailable: 1` over a one-replica Deployment makes the pod un-evictable and
      blocks every node drain forever, which is worse than no policy. So the row is not "add a PDB".
      It is that core's background worker cannot safely run two replicas — the schedules, the
      re-index and the sync jobs assume one holder — so a node drain ends whatever it was running
      and Temporal re-delivers only after the activity's start-to-close timeout elapses.
      What it needs is a distributed checkout lock so a second replica is safe, at which point a
      `maxUnavailable: 1` PDB becomes meaningful. Until then the honest state is one replica, a
      derived grace period long enough to drain (`chemclaw.workerGracePeriod`, shipped), and this
      row. Raised by the 2026-08-27 deployment-monitoring review, which checked the PDB's argument
      and found it sound; the singleton underneath it is the defect.

---

## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **The `default` profile carries eleven names it could narrow, worth 5,787 tokens** — [M], and
      it is what the eighteen-tools row became once measured
      (`D-2026-08-27-eighteen-names-for-a-primitive-set`). **The probe half is closed**: seventeen
      probes landed — seventeen, not eighteen, because `transform_structure` was deleted rather
      than implemented — and the grandfathered list is gone rather than left holding an empty set.
      The redundant-pair question is answered too: the two bond-strength names are two
      capabilities, since the job's cleavage list is mandatory and so it cannot answer from a
      SMILES at all.

      **The ceiling was deliberately not lowered**, because nothing was reduced: 28,114 tokens
      before and after, against a ceiling since raised to 29,500 by three unrelated merges. What
      the measurement found is where a reduction actually lives — a `default` allow-list is worth
      **-5,787 tokens (-21%)** — plus two facts that make it more than a one-line edit: the saving
      is flat in the six `enumerate_*` endpoint tools, which an offline floor cannot see at all,
      and the skills listing does not move (3,034 tokens in every arm), because `ensemble-workflows`
      stays listed after every tool it routes to is gone. So this needs the profile allow-list *and*
      the skill gate. The two single-job wrapper templates (681 tokens) are the other candidate, and
      deleting a named protocol the shipped skill routes to is its own decision.

- [ ] **A tool schema is 38% developer rationale, and it ships on every turn** — [M], and it is
      what `§ 5`'s deferral row turned into once measured. `science/bo/problem.py`'s nested models
      carry design arguments in their class docstrings — *"One `objectives` field rather than a lead
      objective plus a sidecar list (W3)"* — and Pydantic turns a class docstring into the schema
      `description`, so `convert_to_openai_tool` ships them. Measured 2026-08-25 on the `default`
      profile: `start_optimization_campaign` is 8,063 chars of schema, 4,392 of it description and
      **3,047 of that elaboration past the first paragraph**; `propose_knowledge_note` 4,259/2,262/663.
      Those two are 25% of the profile's 12,536-token tool budget between them, and both are already
      in `tests/test_context_floor.py::KNOWN_OVERSIZED`.

      **Not a blanket cut.** Some elaboration is genuinely the caller's — when to supply categorical
      descriptors changes what the model should send — so this is per-paragraph judgment: rationale
      moves to a `#` comment, guidance stays in the docstring. **And it does not ship until the live
      lane can show every probe still reaching its tool**, because a cheaper prompt that stops
      finding tools is a regression with a good-looking metric. Blocked on the live-lane row in § 1.

- [ ] **Half the probe corpus tests one tool** — [S]. `gather_evidence` is in `expects_tools` for
      124 of 261 probes (re-counted 2026-08-27; the corpus has grown by 29 probes since the
      2026-08-25 figures of 116/232); `find_notes` 95; `expand_note` 60; bucket C is 53 probes; the
      tail is thin. Two consequences worth separating: the corpus mostly measures one retrieval path,
      and ChemToolAgent's finding — that tool augmentation **does not consistently beat the base
      LLM**, and hurts on general chemistry questions — cannot be reproduced here. Bucket C scores
      restraint but never runs the same question tool-free for comparison. `evals/ab.py::compare_tool_utility`
      is already written and already registered as `plan_execute_utility`; an A/B arm over bucket A
      is mostly wiring.

      **Blocked on a working model credential** — see "This environment's `API-KEY` comes and goes"
      below in this section, not §4 (which has no credential row) — and the mock cannot stand in:
      `cli.mock_llm` emits scripted tool calls without *choosing* them in response to a question, so
      both arms of any comparison would measure the script. Measured 2026-08-25 through the real
      lane: expected-tool-reached 0/3.

- [ ] **No external benchmark has ever been run** — [M]. `make eval` gates 23 metric values over 15
      case files (re-counted 2026-08-27; one has been added since the 2026-08-25 figure of 14), a
      **7-document** retrieval corpus and a **39-note** knowledge graph, with the science half
      resting on one solubility value, one BO regret replay and two mass balances. It is honest and
      it is not comparable to anything. ChemRAG-Bench (1,932 expert-curated chemistry QA pairs) is the
      best first target because it scores the retrieval half — where this system's science actually
      lives — and it runs against an OpenAI-compatible endpoint, which is exactly the seam
      `agent/llm_provider.py` already has. ChemBench and AstaBench are the follow-ups. A number
      somebody else can also produce is the only kind that survives an argument with a chemist.

      **Blocked on a working model credential** — see "This environment's `API-KEY` comes and goes"
      below in this section, not §4 — and the mock cannot stand in: `cli.mock_llm` emits scripted
      tool calls without *choosing* them in response to a question, so both arms of any comparison
      would measure the script. Measured 2026-08-25 through the real lane: expected-tool-reached 0/3.

- [ ] **`deep-research` has no index behind it** — [M]. `agent/research_tools.py::gather_evidence`
      sweeps the knowledge graph, the ELN, the mounted document share and the fingerprint store —
      every one internal. `skills/deep-research/SKILL.md` describes a capability whose corpus is
      whatever notes exist (39 on this checkout). `Chemclaw3-mcp/MODULES.md` files `litsearch`
      (Europe PMC / OpenAlex / Crossref bulk, built at image time, no egress) as *proposed*, and says
      in as many words that it "gives Chemclaw3's existing `deep-research` skill a real index".
      ChemRAG measured **+17.4% average relative gain** from a chemistry corpus and — the design input
      that matters — that corpus choice is task-dependent: reaction prediction wants literature,
      nomenclature wants structured databases. A process chemist asking "has anyone run this coupling
      on a deactivated aryl chloride" currently gets whatever those 39 notes happen to say.

- [ ] **`pyexec` is merged in the fleet and unreachable from any deployment here** — **[M], not
      [S], and the sizing changed when somebody looked.** `Chemclaw3-mcp` #12 shipped
      `servers/pyexec` and `D-2026-08-25-a-sandbox-is-a-server-not-a-verb` records the decision, but
      `grep -rn pyexec` in this tree finds only that ADR and `tasks/todo.md`: no entry under
      `connectors:` in `deploy/helm/chemclaw/values.yaml:161`, so no `url`, no
      `networkPolicy.egressDestinations` host, and nothing telling an operator to provide
      `CHEMCLAW_PYEXEC_TOKEN`. The seam working as designed is why it is easy to miss — **zero core
      edits also means zero core changes to remind anybody.**

      **The obvious fix is wrong, and this is the part worth reading before starting.** Copying
      `chem`/`safety` means adding a manifest stub under `src/chemclaw/connectors/pyexec/`. But
      `registry.enabled()` is *"discovery is enablement until you say otherwise"* — an empty
      `connectors_enabled` loads every discovered bundle — so shipping that stub would:

      1. put `run_python` on the agent surface of **every fresh checkout**, by default;
      2. make the front door dial `127.0.0.1:8899` — a hard boot failure only for a deployment that
         has separately set `connectors_required`/`CHEMCLAW_CONNECTORS_REQUIRED=true`, since the
         chart itself sets neither and the setting's own default is `False` (corrected 2026-08-27:
         the row previously claimed the chart ships `connectors_required=true`, which is not true of
         `deploy/helm/` or `deploy/jenkins/` today — an operator has to opt into fail-fast
         separately for this consequence to bite);
      3. trip `tests/test_probe_coverage.py` (no probe names `run_python`) and raise the context
         floor.

      Turning a code-execution tool on by default is still a decision, not a wiring change, and (1)
      and (3) alone are enough to make it one. **So this needs an ADR about the default
      before it needs a diff**, and the branch point is whether `CHEMCLAW_CONNECTORS_ENABLED` stops
      meaning "empty loads everything" — which is a chart-wide behavioural change with its own
      blast radius. Whichever way it goes, the change also owes a `run_python` probe, an
      `egressPorts` entry for 8899 (the egress rule restricts by port independently of the peer
      list, so a destination with no matching port still drops), and the token obligation in the
      comment `chem` already models.

- [ ] **This environment's `API-KEY` comes and goes, and three rows are blocked exactly while it is
      down** — [S], and it is operational rather than code. Measured 2026-08-25:
      `anthropic.AuthenticationError: 401` with and without the session's `ANTHROPIC_BASE_URL`
      cleared. **Re-measured 2026-08-27: the same variable answers** (a haiku call returned 200; the
      day's verifier-margin run spent ~120 calls through it), so present-and-rejected is a *state*
      of this environment rather than a fact about it, and the worse case remains the stale one —
      it reads as a defect rather than as a missing credential.
      `tests/test_prompt_caching.py` probes reachability and skips with a reason naming which case
      it is, so the suite is honest about it. The *live* half of the eval plan (the bucket-C control
      arm, any external benchmark, grading any probe on the model's judgement) needs the working
      state and nothing else — probe first (`printenv 'API-KEY'`, one cheap call), then run the
      measurement in the same session, because tomorrow's state is not evidence about today's.

- [ ] **Memory records; it does not change what the next turn does** — [L], and it needs an ADR
      before it needs code. Six tiers exist and all six are *read on request*:
      `memory/campaign.py`, `interaction.py`, `failure.py`, `playbook.py`, `progression.py`,
      `observations.py`, surfaced by `recall_observations`, `find_past_jobs` and `record_failure`.
      Nothing in that set changes the agent's behaviour on the next turn unless a human writes a
      `SKILL.md`: `skills/playbook-distillation/SKILL.md` is the distillation *judgment*, and the
      PR-gate is where a distilled playbook becomes knowledge — but the loop is manual end to end and
      nobody has measured how often it closes. The 2026 work (SkillRL, SkillForge and the
      self-evolving surveys) is specifically about abstracting recurring trajectories into reusable
      procedure automatically. **The PR-gate is the right control for that, not an argument against
      it** — a proposed skill is exactly the shape the gate already carries. What is owed first is a
      measurement rather than a mechanism: over the sessions on disk, how many recurring trajectories
      *are* there, and would a distilled one have changed a later answer? A generator built before
      that number is a routing hypothesis nobody measured, which is the mistake
      `D-2026-08-15` already made once here.

      **Attempted 2026-08-25, and the corpus to measure does not exist.** Against a live Postgres:
      `session_messages` 12 (all from that day's own probe run), `session_turns` 0, `observations` 0,
      `note_proposals` 0, `audit_events` 3. The five notes under `knowledge/playbook/` are committed
      examples, not distillations of anything. So this row is blocked on **deployment history**
      rather than on effort — nobody can count recurring trajectories in a database that has never
      served a user. Its trigger is therefore a deployment with real sessions in it, and until then
      building the generator would be building against an imagined corpus, which is the row's own
      objection.

      **The measurement itself is no longer owed — the corpus is.**
      `D-2026-08-27-count-the-trajectories-before-building-the-distiller` defines the recurring
      trajectory, ships `make trajectory-census` (`chemclaw.cli.trajectory_census`), and states the
      greenlight numbers (≥5 recurring classes across ≥3 sessions, ≥1 would-have-helped multi-tool
      class); the command prints the verdict itself. Run 2026-08-27: 0 sessions, 0 turns, not
      greenlit. The day a deployment has sessions, this row is one command to check.

### The upstream-capability register — what our pinned dependencies now ship that we build ourselves

*Re-derived 2026-08-25, and re-derive it whenever a dependency is bumped.* `make upstream-check` and
`tests/test_upstream_surface.py` guard the *shapes* this repository borrows — the coupling that
breaks on a bump. Nothing guarded its **decisions** against upstream shipping the thing, which is
why the Temporal LangGraph plugin sat five weeks old and reached no list here. This is prose rather
than a test, deliberately: what is being watched is judgement, and a test cannot hold one.

Pinned at the time of writing: `temporalio` 1.31.0 · `langchain` 1.3.15 · `langgraph` 1.2.11 ·
`langchain-core` 1.5.5 · `deepagents` 0.7.6.

| Upstream ships | We | Standing |
| --- | --- | --- |
| `temporalio.contrib.langgraph.LangGraphPlugin` — graph nodes as activities, durable `interrupt()` | run two durability layers | **declined**, `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use` — we use no `interrupt()`; the human gate is already a Temporal workflow |
| `langchain.agents.middleware.ContextEditingMiddleware` / `ClearToolUsesEdit` | use it, on its own trigger since 2026-08-25 | **adopted** |
| `SummarizationMiddleware` | construct it switched off (`disabled_summarizer`) | **declined** — a summary is new model prose over content `agent/framing.py` marked untrusted, and the envelope does not survive it |
| `ModelCallLimitMiddleware` | subclass our own cap | **reverted**, `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` — measured, a cap of 2 ran 4 model calls |
| `ToolErrorMiddleware`, `ToolRetryMiddleware` | neither | **declined** — both trigger on raised exceptions and MCP tools never raise |
| `HumanInTheLoopMiddleware` | our own plan gate | **declined for plan approval**, `D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question` — an async `when` cannot be awaited (fails closed, silently), a new user message discards a pending interrupt and corrupts the thread, a mismatched resume bypasses the gate, and retention prunes an unresolved interrupt; **not** declined for per-call approval of an irreversible action, which is a different, still-open question. Restart condition is monitored by `tests/test_upstream_surface.py::test_the_interrupt_on_predicate_is_still_synchronous`: upstream shipping an async `when` |
| `deepagents.SkillsMiddleware` | use it, narrowed at the backend | **adopted**, with the narrowing on the backend because deepagents publishes skill *paths* into the prompt |
| `deepagents` `execute` filesystem verb | withhold it | **declined**, and answered elsewhere — `D-2026-08-25-a-sandbox-is-a-server-not-a-verb` puts the capability in the fleet instead |
| LangSmith tracing | first-party OTel + OpenInference | **declined** — proprietary, no OSS self-host, and its core value is prompt/response content in a third party |

**How to use this.** On a dependency bump, read the release notes against the middle column and ask
one question per row: *does upstream now do this, and better?* A row that changes answer needs an
ADR, not an edit here. A capability upstream ships that this table does not mention is the gap this
register exists to catch — add the row in the same pull request that notices it.

---

## The turn-time comparison cannot diff what the ELN gives structured

On a prose-only ELN the *mined* `optimization-campaign` note produces excellent condition deltas —
`solvent DMF → 2-MeTHF`, `reagent cesium carbonate → potassium carbonate` — because
`memory.progression.changes_between` reads the species set of each role off `OrdReaction.inputs`.
The **turn-time** comparison cannot: `agent.condense._changes` diffs `ProcessConditions` plus the
solvent its prose reader extracted, and `reaction_records` keeps `reaction_id, body,
compound_smiles, project, performed_at, conditions, source` — the component list survives only as
prose inside `body`. So on the one schema where the components are the *most* reliable thing the
source provides, the artifact a chemist is answered with in the turn is the one that cannot use them.

Measured (`D-2026-08-26-silence-is-not-a-successful-run`, four runs): the mined note named all three
swaps; the turn-time table rendered `—` in every "Changed vs previous" cell.

Nothing is wrong today — the campaign note is retrievable, `experiment-progression` already starts
from it, and the two artifacts together answer the question. What is unresolved is whether the
deterministic delta should be available without a mining pass. The cheap shape is a column on
`reaction_records` carrying the per-role canonical species sets (a projection, not the charge list,
so it stays a serving copy rather than a second record); the expensive one is handing `Protocol` a
component list, which `agent.condense` deliberately does not have because a share document has none.
Wants its own ADR and a measurement of what the extra column costs on a real corpus.

## Everything else

223 open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md)
(`grep -c '^- \[ \]'` on that file), grouped by the review that found them, with their full
measurements. That set **overlaps** this queue rather than extending it — promotion restates a row,
so a queued row is still open there under its original wording, and the header's "~185 further"
was a subtraction nobody could reproduce. They are open, not abandoned — promote one into the queue
above when it becomes the next thing worth doing, and delete it from here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Temporal broker, real
cluster, a real Databricks workspace — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.

## `turn_cost_ratio` scores a fixture, not the system

`data/evals/cases/autonomy-turn-cost.md` carries literal turn records, so the metric returns
0.9845458333333333 whatever changes in the agent — the 32% static-prefix growth that
`tests/test_context_floor.py` caught would leave its `baseline.json` row untouched. The metric's
arithmetic is right and tested; what is missing is a case fed from real recorded `TurnCost` rows.

Blocked on the same thing the memory-distillation row is: a deployment with turns in it. This
system has 12 session messages and 0 recorded turns, so there is nothing to build the case from
yet. Trigger: the first live lane run that persists a session's worth of turns.

## Recover the flow-Suzuki screen, or decide it stays out

`Chemclaw3_mock` seeds 10,011 ORD records and **5,760 of them — 57% — cannot be ingested at all**.
Every refusal is the Perera flow-Suzuki set (*Science* 2018, 359, 429), whose second coupling
partner the source spreadsheet publishes only as its own shorthand (`2a, Boronic Acid`).
`ord_adapter._smiles` refuses rather than inventing a structure, which is right and is pinned by
`test_ord_compound_with_no_resolvable_identifier_is_still_refused` — but that docstring's own words
are "57% of a real corpus lost, including the yield data on components that *were* resolvable",
and the widening it documents (INCHI, then NAME through `resolve_compound_name`) moved the number
from 5,761 refused to 5,760.

The open question is whether a reaction with one structure-less participant is worth keeping as
*evidence*: its yield, ligand, base and halide are all real, and questions like "which base wins on
this halide" need none of the missing structure. The two candidate shapes are (a) a `Component`
that may carry a name instead of SMILES, with the reaction excluded from every fingerprint index,
and (b) a separate lower-tier record type that retrieval can cite but similarity cannot reach.
Both change what a `Component` is, so this wants its own ADR and its own measurement of what a
partially-structured reaction does to retrieval — not a patch to `_smiles`. Measured and declared
by `make live-data`; see `D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk`.

## Carry the rejection ledger to the site that covers every source

`ingest_rejections` ships (`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask`): the
119.43% well now lands in the ledger with its reason, `gather_evidence` answers the question that
used to return "I have no such record", and the shape is unmistakably a rejection rather than a
result. Three refusal sites in `ingest/eln/ord_adapter.py` write to it.

What is not covered is every *other* source. `ingest/eln/json_adapter.py::map_to_ord` — the adapter
the live 119.43% record actually arrives through — plus `sync.py`'s future-timestamp refusal and
`ingest.py`'s `IngestError` still refuse into a log line only. The one site that would cover every
adapter and every source at once is `durable/eln_sync.py`, which already holds `IngestSummary.rejected`
(id, reason, timestamp) in an async activity needing no pre-flight: one call, and the per-adapter
writers become redundant rather than multiplied.

## The PR-gate costs 1.81 s per proposed note, and a backfill is one note per record

Measured over the ORD backfill: 103 records per 3.1 minutes, steady, with the cost in the PR-gate's
git branch-and-commit cycle rather than in mapping (the whole 10,011-record corpus maps in 0.3 s).
That is a little over two hours for the mock's 4,251 ingestible records and 4,251 branches in the
note repository. A real deployment's first sync is a decade of records, where this is days and a
repository nobody can list. Nothing is broken — every proposal genuinely is a reviewable unit — but
a backfill and an incremental sync arguably want different submission shapes (one branch per batch,
or a bulk proposal a reviewer expands). Found by the 2026-08-18 corpus-fidelity pass.

## The labelling client is the one MCP leg with no identity or trace on the wire

`core/mcp_session.open_session` grew a `request_hook` seam so a caller can stamp the outbound
request, and `connectors/calc/remote.py` uses it: that leg now carries the W3C `traceparent`, the
correlation id, the actor and the session, plus the origin-strip guard that removes them again if a
redirect leaves the endpoint's origin. `ingest/labels/labeller.py:216` is the only other
`open_session` caller and still sends `Authorization` alone, so a labelling drain — hours long,
inside a durable activity — is invisible to the trace and unjoinable to the audit trail.

It is not one line. `turn_identity_hook` lives in `connectors/identity.py` and sits on top of both
`agent.turn_flags` (for the dry-run flag) and `connectors.manifest`, and neither
`ingest -> connectors` nor `ingest -> agent` is an edge `tests/test_layering.py` permits. So closing
it means deciding where identity stamping for a **non-connector** MCP client belongs: the labelling
server is an endpoint this system dials, not a connector bundle, and the hook it needs is a strict
subset of the connector one (no `ConnectorAuth`, no dry-run flag). The likely shape is a
core-level `trace_and_identity_headers()` that `connectors/identity.py` composes rather than owns —
which is a small change once the question is answered and a layering exception if it is not.
Found by the 2026-08-27 logging and monitoring review.

## Two producers bind a template step's ambient identity, and only one of them is needed

`durable/interceptor.py` binds the actor, the roles, the session and the correlation id around
*every* activity on every worker, reading them one level into a nested `identity` field — which is
exactly the shape `durable/template_activities.py`'s `ToolStepInput`, `AgentStepInput` and
`JobStepInput` use. Measured against those real models, `activity_context` returns the same four
values `template_activities._acting_as:161` binds, over a scope that strictly contains the
bracket's. So on a worker the bracket is redundant in full.

It is still there, and deleting it is not a tidy-up: with the bracket neutered, four tests fail, and
two of them — `test_an_expensive_job_step_is_refused_for_an_unentitled_requester` and
`test_an_entitled_requester_passes_the_same_gate` in `tests/test_template_job_step.py` — are the
proof that a template step cannot run a tool its requester could not run. They invoke
`authorize_job_step` directly, where no interceptor runs, so collapsing the two producers means
moving a security control's proof onto a worker harness. That is the whole of the work and the whole
of the risk; decide it deliberately rather than by deletion. The two cannot drift while both stand,
because both read `StepIdentity`'s own fields.

The same question does **not** apply to `connectors/calc/activities.py::_acting_for`: the
interceptor skips plain string arguments by design, so it binds nothing there and that bracket is
the only producer on the calc job path.

Found resolving the merge of #256's branch with #258.
