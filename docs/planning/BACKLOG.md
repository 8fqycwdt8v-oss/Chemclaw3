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

- [ ] **A helper spawn costs 20,712 kB of checkpoint rows and nothing yet explains where they
  go** — [M]. The cost is real and measured on a real `AsyncPostgresSaver` with incompressible
  text: one helper writing 2 MB costs **20,712 kB** above a 296 kB baseline (10.4x), and
  `D-2026-09-12-a-helpers-scratch-file-crosses-into-its-callers-state`'s cap reclaims **1,824 kB**,
  8.8%.

  **The explanation this row used to carry is false in both halves, checked rather than argued.**
  It said the rest was "the helper's own `files` and `messages` channels in `checkpoint_blobs`".
  The helper graph is compiled with **no checkpointer** — `agent/langgraph_agent.py` passes none
  and says why, and `tests/test_subagents.py::test_the_helper_graph_is_compiled_without_a_checkpointer`
  now holds it — so there are no helper checkpoints to account for anything. And `messages` is in
  upstream's `_EXCLUDED_STATE_KEYS`, so a helper's thread never crosses into the caller's state at
  all. The consequence for whoever picks this up: **one of the two levers this row used to offer is
  already spent.** "Compiling a helper with no checkpointer" is the shipped configuration, not a
  choice remaining, and looking for that object is a dead end.

  What is left to do is attribute the 91% before bounding it, because the obvious candidate is also
  bounded already: `agent_subagent_files_max_chars` caps the caller's whole `files` channel at
  200,000 characters (`held` makes it a channel bound, not a per-call one), so a 2 MB helper write
  cannot be 2 MB of crossed file. The remaining suspect is the caller's own channels re-serialised
  per checkpoint version — `files` is a `DeltaChannel(snapshot_frequency=50)` — which is a property
  of the caller's thread rather than of delegation, and would mean this row belongs beside the
  checkpointer's write-volume row rather than beside the helper ones. Measure that attribution
  first; the probe is `/tmp`-free and is the one in that ADR's table. The surviving lever, if the
  attribution holds, is a bound on `write_file`'s *content argument* — which would also silently
  truncate a chemist's own scratchpad, and is therefore still a decision rather than an edit.

- [ ] **`max_concurrent_workflow_tasks` is set nowhere, so nothing this repository chose bounds
  workflow-task concurrency** — [M]. `durable/background_worker.py` sets `max_concurrent_activities`
  and stops there, so the workflow-task ceiling is whatever the SDK defaults to. A **child workflow
  is not an activity**, so the activity ceiling does not bound the bundle children core starts at
  all — which is the population `D-2026-09-12-a-ceiling-that-funds-one-attempt-does-not-fund-a-sequence`
  has just finished reasoning about from the *inside* of one child. Measure what a saturated worker
  actually holds before choosing a number: a ceiling set from the SDK's default is the same
  unexamined posture this row is about, one value further on.

- [ ] **The `git` remote is now a destination a deployment must declare, and nothing derives it** —
  [S], what is left of "the egress guard is blind to gRPC and to Temporal" after
  `D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py`. The blindness
  itself is closed: `core/netguard_preload.c` interposes libc's `connect`, `getaddrinfo`, `sendto`
  and `sendmsg` through `LD_PRELOAD`, armed by `deploy/entrypoint.sh` from the allowlist
  `netguard.derive_allowed` returns, and driven against a real gRPC server over a non-loopback route
  it refuses the plain socket, `grpc` and `temporalio` alike — grpc's own C-core reporting
  `connect failed: ... Operation not permitted` — while loopback and an allowlisted address continue
  to work. Two things remain:
  - **`git` is now bounded and nothing derives its host.** A child inherits `LD_PRELOAD`, so
    `kg/git_writer.py`'s `git push` is refused unless the remote is named in
    `CHEMCLAW_EGRESS_ALLOW` — the first time that destination has been bounded at all, and a
    behaviour change for any deployment pushing notes off-box. `git_remote` is the string `"origin"`,
    so resolving it means `git remote get-url` in a subprocess; the entrypoint already runs one
    interpreter to derive the allowlist and is the one place that could afford it.
  - **The IPv4-mapped arm is unmeasured on hosts without `AF_INET6`**, which includes this sandbox:
    `test_an_ipv4_mapped_address_is_not_a_way_around_the_check` skips with the reason in the message.
  Anchors: `core/netguard_preload.c`, `core/netguard_preload.py`, `deploy/entrypoint.sh`,
  `kg/git_writer.py`.

- [ ] **What "network-exposed" means for a process that only makes outbound calls** — [M],
  opened by `D-2026-09-04-a-gateway-is-the-only-provider`, narrowed to this half by
  `D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`.
  `_refuse_unauthenticated_exposure` is still called only from `api/app.py`, so no worker runs it,
  and it cannot simply be hoisted the way its neighbour was: its signal *is* `service_host` being
  non-loopback — a property of a **bind** — and a Temporal worker does not bind a request surface.
  (It does bind `worker_metrics_host`, default `0.0.0.0`: an unauthenticated `/healthz`, `/readyz`
  and `/metrics` surface whose exposition carries counts and capacity only, which is why reusing
  that as the signal would refuse every worker in every deployment for a surface the NetworkPolicy
  is what keeps inside the cluster.) So the question is a design one and it is genuinely open: with
  `entra_required=false` a worker's activities run as the shared dev principal with every
  authorization gate open, exactly as a request would — but nothing is *listening*, so what an
  operator should be refused for is the thing to decide before any code moves. Whatever it turns
  out to be, `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY`'s shape is the precedent to weigh: a posture a
  deployment states beats one inferred from a field that means something else in the process
  reading it.
  **The gateway half of this row is closed** — `core/llm_gateway.refuse_unconfigured_llm_gateway`
  is called from `create_app`, `api/mcp_face.main`, `durable/background_worker.main` and
  `cli/chat.main`, the two connector components are shown unable to reach the gateway, and
  `tests/test_llm_gateway_guard.py` drives the processes. Do not read that as covering this one.

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes the actor as an argument sourced from core's validated front-door
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:164` — the row named a
      field called `actor`, which does not exist, and an anchor that has since drifted four lines),
      and never reads the header. Re-driven 2026-09-12: a forged `X-Chemclaw-Actor` reaches the tool
      body verbatim and lands as `unverified:<id>` in exactly two columns on the synchronous MCP
      path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py::_recorded_provenance` — and in neither `audit_events` (which
      reads `agent/audit.py::get_current_actor`) nor `job_records` (`require_actor()`).
      The `unverified:<id>` marking is in place (D-2026-08-13), so what is open is that a caller
      still chooses the string.
      **Narrower again 2026-09-12, and one docstring asserted the opposite.**
      `_recorded_provenance` said this bundle "declares `auth: mode: none`, so the pod does not even
      authenticate *core*: anything that can open a socket to it can name any chemist it likes" —
      over a manifest that has declared `mode: bearer` with `token_env: CHEMCLAW_BO_MCP_TOKEN` since
      `D-2026-08-20-a-networkpolicy-selects-peers-not-paths`. Driven against the real app, `/mcp`
      answers 401 with no token and 401 with a wrong one. So the forgery is a **token-holder's**.
      The docstring is corrected and
      `tests/test_bo_provenance.py::test_the_threat_model_this_module_states_is_the_one_its_manifest_declares`
      fails whenever the two disagree, in either direction. The prefix stays on its own argument: a
      bearer proves *core called*, not *which chemist*, so full closure still needs an actor
      assertion bound to the call (OBO or a signed memo) — which is the `DEFERRED.md` warehouse
      row's blocker too.
      **Narrowed 2026-08-27** (`D-2026-08-27-a-bound-that-multiplies-…`): the claim no longer
      travels back out as provenance — `CampaignThread` dropped `opened_by`, because a reader of a
      resumed campaign cannot tell a marked actor from a verified one. Both columns keep the value
      for the audit trail, where that question can be answered. What stays open is unchanged: the
      string is still the caller's to choose.

## 2 — Answers that are wrong without saying so

- [ ] **Both published tool-utility results were measured against a control arm that also swaps
      the prompt** — [M], `data/evals/profiles/no-tools.yaml` (`instructions:`),
      `D-2026-09-14-tools-were-never-the-variable`. The benchmark half is corrected: the arms differ
      by 13,895 characters of system prompt, `data/evals/profiles/tools-removed.yaml` is the arm
      that varies only the tools, and with the prompt held fixed the benchmark moves 62 → 58 rather
      than 62 → 74. The *probe* half is not, because it needs a run: `cli/live_probes.py`'s
      `_AB_BASELINE_PROFILE` is that same profile, so
      `D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter`'s 221-probe result is about
      prompt-and-tools together too. What closes it is `make live-ab` and `make live-benchmark`
      re-run with `tools-removed` as the baseline, on a gateway with a balance — this environment's
      credential answers HTTP 400, "credit balance is too low". Nothing in the tree changes to start
      it; the arm is already registered by `infra/live/processes.sh`.

- [ ] **`hybrid` retrieval is measurably worse than the `graph` default, and the fix is not a
      fusion change** — [M], measured 2026-09-14 on the new gold set
      (`D-2026-09-14-one-corpus-one-vote-is-the-right-fix-for-a-different-problem`). Over 20 real
      probe questions and 46 labelled (query, note) pairs against the shipped `knowledge/` corpus
      with all three legs live: round-robin mean gold rank **4.38**, top-5 **27**; RRF **4.54**,
      top-5 **25**. 12 gold notes rank worse and 17 better, and the losses are the notes the
      question is about — `playbook-degassing` 1 → 7, `opt-suzuki-conditions` 3 → 9,
      `report-biaryl-development` 2 → 7.

      **Four remedies are now measured no-ops** and the numbers are here so nobody re-litigates
      them: `retrieval_fusion_k` (0 of 7 queries reordered, at any `k` down to the minimum),
      `retrieval_source_weights` tiering the strong legs *up* (`graph`+`lexical` at 0.5 is inert),
      one-corpus-one-vote (**0 of 46** gold ranks, structurally — grouping the three legs leaves the
      cross-corpus stage a single list, so the final order is the within-corpus fusion), and
      down-weighting the *correlated* leg, which is the one a reader reaches for next
      (`D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number`): mean gold
      rank 4.72 / 4.56 / 4.67 at `vector` weights 1.0 / 0.5 / 0.1, top-3 *falling* 19 → 18. A weight
      divides the **rank** and the rank term is nearly flat at `k=60`, so the crossover is
      **`w < 2.7e-4`** — the dense leg's rank-1 hit fusing as though it were rank 3,729. The dial is
      impractical rather than inert, and `tests/test_hybrid_rrf.py` now asserts both sides of that
      so the distinction cannot decay. The one-corpus-one-vote mechanism shipped anyway, for the
      different case it does fix.

      **The row's second option is also measured now, and it is a trade rather than a win.** RRF
      over `graph`+`lexical` — not running three legs over one corpus — is the *first* configuration
      ever measured to beat the shipped round-robin: mean gold rank **3.69** against 4.69, 20 notes
      up against 4 down, top-3 21 against 20. It loses **3 of 39** gold notes outright, every one of
      them found only by the dense leg and one at baseline rank 3 (checked for a `retrieval_top_k`
      artefact; it is not one). `retrieval_recall` is the gated retrieval metric and rank is the
      diagnostic, so the leg stays.

      **And "correlated" is not "redundant", which this row used to imply.** Under `hash` the dense
      leg is a differently weighted term ranker — token-count hashing with a cosine, against
      BM25-lite and substring — reaching gold notes the other two never return. Measured over eight
      free-text questions it contributes 27 of 98 delivered chunks and reorders 7 of 8.

      What is left is the cause rather than the fusion: an `openai_compatible` embedding provider,
      which makes the dense leg genuinely orthogonal and is still the thing to measure next. It was
      not measured on 2026-09-15 for a stated reason rather than a vague one — this environment
      carries a credential but no embeddings gateway, and the only available `openai_compatible`
      embedder is `cli/mock_llm.py`'s, which would measure the mock. `retrieval_mode` stays `graph`.

      **Run `make retrieval-arms` before quoting any number above**: four sessions have rebuilt this
      measurement from scratch, and the baseline itself moved 4.38 → 4.69 between 2026-09-14 and
      2026-09-15 with no retrieval code changed, because the corpus grew.

## 3 — Work that is lost, dropped or invisible

- [ ] **The model-facing prose guards scan the in-process registry and four bundles, not the
      surface** — [M], found 2026-09-15 in the round-two review.
      `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
      and `::test_no_tool_description_tells_the_model_to_expect_a_review_gate` read
      `registered_tools()` plus `glob("connectors/*/server/tools.py")`. Three classes of text the
      model is sent are outside that: the `description:` on the 14 `workflow:` job entries in the
      connector manifests, which `src/chemclaw/connectors/jobs.py:226` assembles into a tool
      docstring and its own comment calls "the job's model-facing documentation" — and which is
      **all** of the `results` bundle, since it ships no `server/tools.py`; the bundle skills
      (`connectors/{bo,calc,safety}/skills/*/SKILL.md`); and
      `agent/chemclaw_agent.py::_INSTRUCTION_BLOCKS`. Driven: every forbidden string at once in
      `connectors/results/connector.yaml`'s job `description:` left both guards green.

      **The universe and the patterns are one problem, not two, which is why this is a row rather
      than a widening.** Shipped prose in those places names the removed tier and the removed gate
      *in order to say they are gone* — `connectors/calc/connector.yaml:23` "there is no DFT tier",
      `agent/chemclaw_agent.py:240` "never present one as if it were DFT",
      `connectors/safety/skills/safety-screening/SKILL.md:77` "the PR gate … was deleted",
      `skills/deep-research/SKILL.md:95` "propose the next point(s)" — so widening with today's
      patterns reds on correct text. Both directions are already measurable: the review-gate
      pattern also misses `a PR`/`PRs`, "awaits review", "staged behind the knowledge gate" and
      "submits the finding to the review queue". The shape that works is probably sentence-level
      with a negation/past-tense exclusion; measure the false-positive rate over the three classes
      before building it, the way
      `D-2026-09-11-the-debt-was-in-the-claims-not-in-the-code` measured 82.9% and declined.

- [ ] **An agent-recorded note the model could not date reaches no subscriber who has a
      watermark** — [M], found 2026-09-15 in the review of the wave 2/4/7 merge.
      `durable/digest._is_new` reads an absent `valid_from` as *open-ended* — true for as long as
      anyone has known — and therefore as not news, which is correct about the field and wrong
      about the question a digest asks. Measured on the shipped corpus: **32 of 39 notes carry no
      `valid_from`**, across ten types (`compound` 9, `playbook` 5, `campaign` 3, `interaction` 3,
      `job-result` 3, `bo-candidate` 2, `failure-mode` 2, `optimization-campaign` 2, `report` 2,
      `experiment-proposal` 1). Two producers are closed —
      `retrieval.harness.report_note(drafted_on=…)` and
      `durable.job_record.note_with_run_provenance(ran_on=…)`, both cases where validity and
      arrival are the same day by construction. `agent/graph_tools.py:527`
      (`record_knowledge_note`) is not: the model may legitimately not know when a fact became
      true, and defaulting `valid_from` to today would trade a silence for a false claim about
      chemistry. **The real fix is an arrival signal separate from `valid_from`**, which the
      subscription watermark cannot express today: `agent/subscriptions.py:68` bounds
      `last_seen_note_ids` to one day of matches *on purpose* (DARK-7), and an undated-id set
      grows with the corpus instead. The candidate worth measuring is the notes repository's own
      git history — one `git log --diff-filter=A --name-only` over `knowledge/` gives every note's
      add-date in one subprocess, cacheable behind the same corpus fingerprint `load_notes` already
      uses. Measure that scan on a 10k-note corpus before building it.

- [ ] **The delegation A/B has a comparator and no runner** — [L], found 2026-09-15.
      `evals/delegation.py` is a pure comparison over `ArmRun`s, and **nothing constructs one**:
      `grep -rn "ArmRun" src/ tests/ data/ Makefile` finds the module and its own test, nothing
      records `delegated`, and no profile, prompt or runner builds the `no-helper` arm
      (`data/evals/profiles/` holds `no-tools.yaml` alone). The module docstring called this "the
      run half is what needs [a gateway]", which reads as a runner waiting on a credential. What it
      owes: a `no-helper` profile whose system prompt asks the model not to call `task`, a runner
      that records `delegated` per repeat off the turn's own trace, and `MINIMUM_REPEATS` repeats
      per (task, arm) against a real gateway. Until then the comparator's guards
      (`MINIMUM_COMPARED_SHARE`, `partially_delegated`) are tested and unexercised.

- [ ] **A `pending_requests` row whose run was terminated, or lost with its worker, has no
      collector** — [M]. What is left of the row above after
      `D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting`, which closed
      the three windows a *cancellation* could slip through and measured the "racy settle" reading
      false: with every child past its open activity, 0 of 78 settles were lost across six runs, and
      the real losses were the `try` starting below the activity that writes the row, a cancellation
      arriving as `ActivityError(cause=CancelledError)`, and `notify_session_best_effort` swallowing
      exactly that pair. Two cases remain and neither is reached by a parent dying in the ordinary
      way: a child **terminated** rather than cancelled never resumes workflow code at all
      (`tests/test_awaiting.py::test_a_wait_started_as_a_child_settles_when_its_parent_dies` pins
      that for `ParentClosePolicy.TERMINATE`, and an operator can do it to a child directly), and a
      worker lost between the row write and the settle. A `due_at` reaper is the answer and it is a
      decision rather than an edit: a new Temporal Schedule, with its own disposal rule, against a
      table that is in `retention._NOT_PRUNED` on purpose. `durable/awaiting.py`,
      `durable/pending_store.py`.

- [ ] **A warm parse forkserver is ~109 MB the front door's pod was not sized for** — [S],
      measured 2026-09-13. `ingest/documents/isolate.py` starts its server by fork **and exec**, so
      its pages are not copy-on-write with the front door's: driven on this tree, RSS was
      111,140 kB in the front door and 111,188 kB in the forkserver — a second, full resident copy of pypdf,
      python-docx, openpyxl and python-pptx. `deploy/helm/chemclaw/values.yaml`'s `resources.service` is
      unchanged at `requests: 512Mi / limits: 1Gi`, so that is 21% of the request arriving the
      first time anybody uploads a document, and it is *per replica*. Nothing is wrong today; what
      is missing is that the chart was sized before this process existed. The decision is whether
      to raise the request, keep the forkserver cold (it is lazy, so a replica that never parses
      never pays), or both — and it wants a measurement of the Temporal worker too, which now
      starts one as well (`ingest/documents/sync.py`). Anchor: `isolate.parse_context`,
      `deploy/helm/chemclaw/values.yaml`.

- [ ] **Neither net sees one Postgres server that two DSNs spell differently** — [M], found
      2026-09-05 by a fresh-context review of `D-2026-09-05-a-pool-count-is-not-a-connection-count`,
      whose own "what this does not do" says a measured cluster identity is a row and then did not
      write one. Both halves split the fleet with `core/config.pg_endpoint`, a string comparison:
      `Settings.fleet_connections_per_server` at startup and `db._session_store_max_connections`
      for the runtime gauge. So `localhost` against `127.0.0.1` — one server — is charged and
      alerted as two, each inside its own ceiling, and the real total is checked by nothing.
      Measured: a front door's 49 declared connections split 16 primary / 33 session on one
      database. The *released* expression before the split gauge existed would have caught it,
      comparing one sum against one ceiling, so this is a regression at runtime for that
      configuration. `SELECT system_identifier FROM pg_control_system()` answers it exactly (0.24 ms,
      readable by an unprivileged role) and cannot answer it in a validator that runs at import with
      no loop and no pool — so the fix belongs on the gauge, where a pool has already connected, and
      costs the alert its series during a database outage. That trade is the decision.
      Anchors: `core/config/__init__.py::pg_endpoint`, `core/db.py::_session_store_max_connections`.

- [ ] **A front door scaled to zero renders a release in which every pod refuses to start** — [S],
      found 2026-09-05 by a fresh-context chart review. `service_fleet_replicas` is
      `Field(default=1, gt=0)` and `config.yaml` renders `service.replicas` straight into the shared
      ConfigMap, so `--set service.replicas=0` (or `autoscaling.maxReplicas=0`) gives every pod in
      the release a value `Settings` rejects — workers and connector servers included, none of which
      has a front door. `helm template` and `kubeconform` both pass, so `make helm-validate` is
      green. The arithmetic is *right* at zero (measured: `readiness=0`, and the per-server figures
      match a real fleet with the bound relaxed); only the bound refuses it. Deciding whether a
      front-doorless release is legal is the work. Anchors: `core/config/service.py`,
      `deploy/helm/chemclaw/templates/config.yaml`.

- [ ] **`/readyz` cannot bound a Postgres that accepts the socket and stops answering** — [S],
      found 2026-09-05, upstream in origin and recorded here because `api/routes/ops.py` claimed
      otherwise. `asyncio.wait_for` bounds acquisition; on a warm pooled connection psycopg's
      `AsyncConnection.wait()` catches the cancellation, calls `_try_cancel` against the same
      frozen server, then re-waits on the socket with no timeout. Driven with `docker pause`: one
      run answered `200 ready` after 7.6 s for an unreachable database, another never returned.
      Bounded in a deployment by the kubelet — the chart derives `readinessProbe.timeoutSeconds`
      from this budget and ships 5 s with `failureThreshold: 3`, so the pod goes not-ready either
      way — which is why this is a row and not a fix: the correct outcome is reached by the wrong
      route, and a second in-process timeout cannot cancel what the first one could not. Worth
      revisiting if psycopg gains a cancel that respects a deadline. Anchors:
      `api/routes/ops.py::_probe_database`, `core/db.py::connection`.

## 4 — Operating it

- [ ] **A caller cannot tell that a helper's report is derived from untrusted reading**
      — [M], opened by `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`, which
      closed the mechanical half and deliberately left this open rather than silent.
      A helper's report is now defanged, so it can no longer carry a live envelope delimiter into
      its caller's thread. What it still carries is no *provenance*: the caller's model reads a
      `ToolMessage` of ordinary prose, with nothing saying that the helper wrote it after reading
      evidence that arrived enveloped. Every other path marks that — `gather_evidence` frames each
      chunk with its note id, a connector result is framed `connector:tool`, an attachment is framed
      `attachment:<file>` — because the agent instructions tell the model that enveloped spans are
      evidence to weigh and cite.
      **Framing the report is the obvious answer and it is the wrong one**, which is why this is a
      row rather than a patch: an envelope says "evidence to cite", and citing a helper's summary
      credits a source that is this system's own paraphrase. What is wanted is a third marking —
      *derived from untrusted reading, not itself a source* — and this repository has exactly one
      instrument for that today (`defang`, which says nothing) and one prohibition against inventing
      prompt vocabulary nobody measures.
      The cheap first step is a measurement rather than a design: whether a helper that read
      injected evidence actually propagates the instruction into its report. That needs a live
      model, so it belongs with the delegation row above rather than ahead of it.


- [ ] **Measure whether delegation pays, with an instrument the deleted one could not be**
      — [M] (issue #359), opened by `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`. The corpus
      that was supposed to settle this (`data/evals/probes/m12/routing.yaml`, deleted with the
      specialist team) measured **delegation rate** over fifteen one-tool probes. Rate is a mediator
      rather than an outcome, and a one-tool question gives context isolation no mechanism by which
      it could appear — so the instrument could not observe the benefit it was built to detect, and
      its two runs disagree sevenfold (2/15 through the front door with connectors and history;
      14/15 on the compiled agent with neither, one sample per probe) because they measured
      different systems.
      **What to build instead**: outcomes per *task* — probe pass or `score_answer`, billed tokens
      from `turn_costs`, wall clock — over reading-heavy multi-source work (a six-source evidence
      sweep, a twenty-compound property table, a multi-document comparison), through **one**
      harness, on one pinned model, with at least three repeats. The denominator problem disappears
      the moment the unit is a task rather than a delegation.
      **The arms exist as of that ADR**: no helper (the model simply not calling `task`), helper on
      the caller's model, and helper on its own via `CHEMCLAW_MODEL_ROUTES='{"helper": "…"}'`.

      **The instrument and the corpus exist as of 2026-09-13, and "nothing here needs new code" was
      wrong.** That sentence stood here until the work was attempted: `evals/ab.py` is pairwise and
      dimensionless, and this comparison is three-armed and has two cost axes, so
      `chemclaw.evals.delegation.compare_arms` is genuinely new — quality through `ab.py`'s own
      noise floor, billed tokens and wall clock reported beside it rather than folded in, because
      "cheaper but worse" and "better but slower" are different answers that one number hides.
      `data/evals/probes/delegation.yaml` is the corpus, eight reading-heavy multi-source tasks
      chosen so that isolation has a mechanism by which it could appear at all.
      One design fact worth carrying: the `no-helper` arm is **behavioural**, because `task` cannot
      be removed — `SubAgentMiddleware` is required and an empty roster makes upstream re-insert its
      own. So compliance is observed per run, and a baseline that delegated anyway is reported as
      contaminated rather than averaged in.

      **What is left is the run, and it needs a gateway.** Nothing in `src/` dials a vendor
      (`D-2026-09-04-a-gateway-is-the-only-provider`), so the environment's `API-KEY` is a
      credential *for* a gateway rather than one this stack can use — probed 2026-09-13, the key
      answers 200 against the vendor and no gateway is configured. Until the run exists, no claim
      that helpers do or do not pay is evidence about this deployment.

- [ ] **An advisor is the one delegation shape every merged decision already permits**
      — [M], and the design is fully determined rather than open.
      `D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool` settles the objection
      that killed the summarizer three times. Its table is about **thread versus tool**: a model
      call whose output returns as a `ToolMessage` has the framing envelope re-applied on the way
      out, is audited, authorized, dry-run refused, citable per row, withdrawable by taking one name
      out of the registry, and is cleared by `ClearToolUsesEdit` like any other result. A summarizer
      has none of those, which is why it launders an injected instruction into the model's own voice
      and replays it every turn. **An advisor as a tool sits on the permitted side of all seven
      rows** — and Anthropic's own advisor arrives as an `advisor_tool_result` block, which is the
      same answer reached independently.
      It is also already metered, and the trap that used to sit here is **closed**:
      `agent/condense.py` makes an in-tool model call whose usage reaches `agent/spend_cap.py`
      **because it passes no explicit `config`**, an absence
      `tests/test_spend_cap.py::test_no_in_tool_model_call_passes_its_own_callbacks` guards — and
      that scan named `condense.py` until
      `D-2026-08-29-a-guard-that-names-one-file-guards-one-file` made it derive every module that
      defines a registered tool and builds a model. So an advisor is covered wherever it lands,
      with no edit to the test, and the natural mistake it would have made silently — copying
      `verifier.py`'s correct `config=off_stream_metering()` into a tool body — now fails naming
      the file and the line.
      **`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` does not bind it**:
      that ADR declined a *judge* (it cannot reuse `score_answer`, and a failed grading returns the
      ungraded answer). An advisor does not grade and does not gate — it answers a question the
      agent asked, mid-turn, and the agent remains the author of the answer.
      **What it is actually blocked on**: a deployment whose endpoint serves a second, more capable
      model tier — `build_chat_model("advisor")` is the whole mechanism now that
      `AgentProfile.model_route` exists — and evidence that the self-critique gap
      `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` named as real is closed by
      consulting rather than by thinking longer at higher `effort`. Measure the cheaper lever first.

- [ ] **A second roster name is not the change it was before the helper was narrowed**
      — [S], and the recommendation is to leave it closed.
      The case for a second name used to be a read-only reader beside a full-capability helper.
      `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` made the *only* helper
      read-only, so that difference no longer exists: what a second name could still vary is its
      model route and its prompt, and `task` already tells the model to launch several helpers
      concurrently when their tasks are independent, so fan-out needs no partition either.
      A named partition remains a routing hypothesis, and this repository has measured routing twice
      without learning anything transferable. The trigger is unchanged and it is a number, not an
      argument: the row above, showing that helpers pay *and* that a single brief is what limits
      them. Note also what a second name costs on a path that is otherwise free —
      `governed_roster` is the guard, and upstream's `create_sub_agent` builds a declarative
      `SubAgent` from `spec["middleware"]` alone.
      **Revisited 2026-08-29 and confirmed to have no implementable part**, which is recorded here
      so the next reader does not go looking for one: everything a second name would need already
      exists (`AgentProfile.model_route` for its model, `helper_profile` for its surface,
      `governed_roster` for its governance), so what is missing is the reason, and a name added to
      be ready for one is the capability that ships off and stays off —
      `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`, which deleted 1,442 lines of
      exactly this.

- [ ] **A third reducer sits above the compaction group and no prose mentions it** — [S], found
      2026-09-04 while measuring the context-window row. deepagents' `FilesystemMiddleware`
      silently offloads oversized *message content*: probed, a 500,001-character `HumanMessage`
      reached the model as a 1,293-character pointer reading "Message content too large and was
      saved to the filesystem at: /conversation_history/….md". `agent/compaction.py` describes two
      reducers — upstream's `ClearToolUsesEdit` for tool results and this repository's conversation
      window — and this is a third, above both, that none of its prose names.
      Two consequences worth separating before anything is built. It means a single oversized
      group can no longer be the unreducible shape, so `chemclaw_context_unreducible_total`'s
      reading depends on a mechanism nobody here decided on. And an offloaded message becomes a
      *file*, which is the surface
      `D-2026-09-04-a-helpers-file-crosses-back-and-stays` just finished defanging on read — worth
      checking whether the pointer's own path and the offloaded body round-trip through that
      treatment, since the content is a chemist's message rather than a helper's notes.
      One probe, not a measurement pass: what is owed first is the threshold, whether it is
      configurable, and whether it fires on any real turn.

- [ ] **A corpus read is ~40 kB of memory per entry, and only 25 kB of it is boundable** — [M],
      measured 2026-09-14
      (`D-2026-09-14-the-memory-corpus-is-a-memory-bound-not-a-time-bound`), and it replaces the
      "three times per scheduled run" row rather than continuing it. **Two of that row's three
      clauses were stale**: `D-2026-08-25` removed the timer, so there is no scheduled run, and
      `synthesize_memory(kind)` starts one workflow per call.

      What is real, over a 10,000-record ORD drop directory: **396.8 MB** traced peak and 6.9 s
      unbounded, **144.2 MB** at a cap of 1. So a mapped `OrdReaction` is **25.3 kB** resident and
      the adapter's own page of `RawEntry` is **14.4 kB** per entry. At a real deployment's ~500,000
      entries that is **~20 GB in one activity's process**, of which **~7.2 GB** is the page. The
      read does not get slow; the worker is killed.

      `memory_corpus_max_reactions` now bounds the miner's half and marks a capped pass incomplete.
      **It cannot bound the adapter's page**: `OrdJsonAdapter.fetch_new_entries` accepts `limit` and
      ignores it deliberately (an unsorted scan would return an arbitrary subset and advance the
      cursor past what it skipped), so for a drop directory the page *is* the corpus and no argument
      `read_corpus` can pass changes that.

      **What is left is the streaming protocol**, which is what the old row already identified
      without a number: either `ElnAdapter` gains a fetch-by-id or a bounded iterator (every source
      pays), or the three miners stop taking a `list` — they are whole-corpus algorithms today (DRFP
      fingerprinting, O(n²) Tanimoto, NetworkX components), so this is a reformulation rather than a
      refactor. **Trigger**: a deployment whose corpus exceeds `memory_corpus_max_reactions`, which
      the WARNING now names by number. It is still the trigger on the `DEFERRED.md` row for
      reagent/solvent set diffs — one change answers both.

- [ ] **A stalled append-only feed has no first-party signal** — [S]. `corpus_cursors`
      (`infra/sql/072`) records where each feed's drain stopped, and nothing reads `updated_at`:
      `ingest/labels/cursor.py::load_corpus_cursor` selects `after` only. The module declines a lag gauge for a
      stated reason — a keyset position is opaque, so "how far behind" would have to be invented,
      unlike `sync_cursors`' datetime twin which exports `chemclaw_ingest_cursor_lag_seconds`. What
      was offered instead does not hold, and `cursor.py`'s module docstring now says so: `ReactionCorpusWorkflow`
      returns **one** report aggregated over every source at the end of the whole `continue_as_new`
      chain (`durable/corpus_sync.py::ReactionCorpusWorkflow`), not one per pass, and builds it without `has_more` — so
      a feed whose source stopped exporting looks exactly like a feed with nothing new.
      **A record counter cannot close this and it was briefly thought it could.** The corpus drain
      now emits `chemclaw_ingest_records_total{source,outcome}` per data source, which delivers the
      per-source *visibility* half — but a stopped feed and an idle one both produce an empty page
      and so both book `ingested=0, rejected=0`, identically. The discriminator has to be a
      staleness gauge over `corpus_cursors.updated_at` — age since the last *advance*, a real number
      even when the keyset position is opaque.
      **The second is now buildable and was not when this row was
      written**: the cursor was stored on every page, so `updated_at` re-stamped on every fire and
      measured when the feed was last *looked at* rather than when it last moved.
      `D-2026-08-28-a-watermark-that-is-rewritten-has-no-age` gates that write on
      `report.advanced`; what is left here is a reader.
      **Trigger:** the first deployment that runs an `append_only:` source, since no shipped
      binding sets it (`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop`).

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

      **And attaching one opens a data-protection question this repository cannot answer for it**
      (found 2026-08-28, with the erasure sweep). `schema/result-store/001_core.sql` gives the
      external store a `calculation_publication` table with its own `actor` and `session_id`
      columns and an index on `actor`. `agent/leaver.py` reaches a database this system owns; it
      cannot reach that one, and no ADR says who does. The outbox row on this side is now counted
      and named in the erasure report as retained (`_RETAINED_IN_PAYLOAD`), so an operator sees
      that the receipt stays — but the copy downstream is somebody else's sweep, and the first
      deployment to point at a real store inherits the obligation. Settle it with that
      deployment, not before: the answer depends on whose database it is.
- [ ] **`tool_result_blobs` ships its retention window at zero, and nobody chose zero** — [S].
      What this row used to say — *"six tables still say `nothing bounds it`"* — is **false of the
      current code**, and re-checked on 2026-09-15 before cutting it down: the phrase appears three
      times in `durable/retention.py` and all three are meta-comments *about* the historical
      wording (`:287`, `:292`, `:446`), never a register entry. Each of the six now states a
      decision: `molecule_fingerprints` and `reaction_fingerprints` bounded by the corpus times the
      definitions ever written with no runtime DELETE (`:501`, `:505`), `user_preferences` bounded
      by its writer (`:506`), `store` bounded by `BoundedStoreBackend` (`:453`), and `predictions`
      and `measurements` **unbounded and accepted**, each saying why (`:519`, `:523`). The five
      decisions this row was opened to collect have been taken.

      What survives is the one question it called "of a different kind", and it is smaller than the
      row it is left in: `retention_tool_results_days` defaults to **0**
      (`core/config/memory.py:125`), which is the same value as every other window that means "off",
      so a deliberate uniformity is indistinguishable from an unconsidered default. Decide whether
      a tool-result blob has a retention answer of its own, and if it does not, say so in the
      register the way `predictions` does rather than by sharing a zero.
      **Anchors:** `src/chemclaw/core/config/memory.py`, `src/chemclaw/durable/retention.py`.

- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. `docs/guides/runbook.md:972-997` (§ xiii, "Restore a
      store") states what this system *requires* of those stores and documents a Postgres restore
      procedure — what does not exist
      anywhere is tooling that performs or **verifies** a restore, and that cannot be built against
      a store this repo does not own. (The former separate "no backup tooling" row is folded in
      here; it was downstream of this one and overcounted the stores.)

- [ ] **Nothing audits the `github-actions` closure for advisories** — [S], the accepted risk
      `.github/dependabot.yml` now names out loud
      (`D-2026-09-14-a-gate-for-one-ecosystem-is-not-a-gate-for-the-file`). Actions are pinned by
      commit, which bounds *what runs* and says nothing about whether it is vulnerable, and
      `make ci` reads that ecosystem nowhere.
      **Not built yet because the set is empty, and that is the argument rather than the excuse**:
      measured 2026-09-14 against OSV's `GitHub Actions` ecosystem, all four actions this
      repository uses carry zero advisories at any version, so a gate merged today would be a
      control that ships green forever with nothing behind it —
      `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`.
      The data source is named so the next person does not have to find it: OSV's
      `/v1/querybatch` takes `{"package": {"name": "owner/repo", "ecosystem": "GitHub Actions"},
      "version": ...}`, and the version is the `# vX.Y.Z` comment
      `test_every_action_is_pinned_to_a_commit_not_a_tag` already requires beside every pin — which
      is a *claim* about the SHA rather than a resolution of it, and any gate built on it should
      say so. Trigger: the first advisory that lands on an action in `.github/workflows/`.
      Anchors: `Makefile::ci`, `.github/dependabot.yml`,
      `tests/test_deploy_chart.py::test_every_declared_ecosystem_is_audited_or_accepted`.

- [ ] **Two pods sharing one note index re-embed the whole corpus on every alternating pass** —
      [M], measured 2026-09-14 while closing the prune half
      (`D-2026-09-14-a-prune-needs-the-corpus-two-pods-disagree-about`), and it is the *larger* of
      the two defects that row described as one. `note_file_fingerprints` is `mtime_ns:size`, and
      two clones of one commit carry different mtimes — git sets a file's mtime when it writes it —
      so a note that has not changed reads as changed to whichever pod did not index it last.
      Driven over two real clones against one index: pod B's pass re-embedded 2 of 2 notes it had
      already seen, and pod A's next pass re-embedded 3 of 3. That is one endpoint call per note per
      pass, for ever, which is precisely what `D-2026-08-02-embed-only-what-changed` exists to
      prevent — it prevents it for one pod and for no more than one.
      The fix is a content-derived fingerprint (a hash of the file's bytes), and it supersedes that
      ADR's stat-only argument rather than extending it: a hash costs one read per note per scan
      where a `stat` costs none, which is the trade D-2026-08-02 declined when the alternative was
      an embedding call. It is now the cheaper side of the same trade. Anchors:
      `kg/graph.py::note_file_fingerprints`, `retrieval/vector_index.py::_needs_embedding`.
      Until it lands, `workers.background.replicas` stays 1 — the retirement half no longer gates
      it, this half does.
- [ ] **A second background worker would diverge on its corpus view, not on its writes** — [M].
      `poddisruptionbudget.yaml` covers the front door alone and argues that correctly in the
      template: `minAvailable: 1` over a one-replica Deployment makes the pod un-evictable and
      blocks every node drain forever, which is worse than no policy. That half stands
      (`deploy/helm/chemclaw/templates/poddisruptionbudget.yaml`).

      **The prescription this row used to carry is spent, checked 2026-09-15.** It said "what it
      needs is a distributed checkout lock so a second replica is safe" — and that lock shipped:
      `kg/git_writer.py:574`'s `_cluster_lock` is a Postgres advisory lock serialising submissions
      to one remote across pods, `values.yaml:177-192` says in as many words that this reason is
      closed, and `tests/test_datapath_review_db.py::test_the_submit_lock_names_the_thing_it_holds_a_connection_for`
      holds it. Somebody working the row as written would build a lock that exists. Of the three
      races it named, two are likewise closed by the chart's own note: the periodic jobs are
      Temporal Schedules under `SKIP`, the ELN cursor has exactly one writer, retention re-checks
      every predicate inside its own `DELETE`, and the result outbox claims rows with
      `FOR UPDATE SKIP LOCKED`.

      **What is actually left is `NoteReindexWorkflow`, and its blocker is a corpus view rather
      than a lock** (`D-2026-08-27-what-a-second-background-worker-would-race-on`). Two pods hold
      independent emptyDir knowledge checkouts, so they can disagree about what the corpus *is*
      while neither writes anything the other conflicts with — which is the same root as the
      `note_file_fingerprints` row below, where the fingerprint is `mtime_ns:size`
      (`kg/graph.py:230`) and a fresh checkout changes it for every unchanged file. Work the two
      together or not at all.
      **Anchors:** `src/chemclaw/kg/graph.py`, `deploy/helm/chemclaw/values.yaml`,
      `deploy/helm/chemclaw/templates/poddisruptionbudget.yaml`.

- [ ] **A background-worker rollout that never becomes Ready is invisible until someone looks** —
      [S], the detection `8b23067` named as missing after measuring the review's proposed fix as
      worse than the status quo, and which was never written down as a row. `deployment-workers.yaml`
      ships `Recreate` because the singleton underneath it forbids two replicas, so the old process
      is gone before the new one starts; a new pod that never becomes Ready therefore leaves the
      `background-jobs` queue with no consumer while the release reports deployed. `--atomic` is
      not the fix and is refused elsewhere for its own reason (`migrate-job.yaml`). What is missing
      is an alert, expressible from what is already scraped —
      `kube_deployment_status_replicas_unavailable` on that Deployment, or the staleness of the
      worker's own `chemclaw_jobs_in_flight` — and it belongs beside `ChemclawWorkerNotPolling` in
      `prometheusrule.yaml`.

---

## 5 — Where the field moved past us

- [ ] **`GET /check-ins` is served and no surface reads it** — [S], `Chemclaw3_ui`. `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late` added the sweep that tells a requester which of their own questions are still waiting, and the route that serves the mailbox it writes (`api/routes/streams.read_check_ins`, claiming `CHECK_IN_KIND`). The UI has no card for it, so with `CHECK_IN_ENABLED` set a deployment sees check-ins only through a configured outbound channel — and `CHEMCLAW_DELIVERY_CHANNELS` is empty in every shipped deployment. **This is not the `/schedules` case**, which the BFF refuses by name as operator surface a chemist has no business reaching (`D-2026-09-14-two-gaps-the-code-had-already-argued-shut`): a check-in is addressed to the chemist. The shape is `/digests`' `/review` card one kind over, and the response model is `CheckInOut`. Own PR against `Chemclaw3_ui`.

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **Nothing mines the edit a chemist makes to a generated protocol** — [M], and the data for it
      starts accumulating now. `experiment_protocol_revisions` is append-only and carries
      `author_kind`, so `protocols.diff.diff_designs` between an `agent` revision and the `human`
      revision derived from it is a *labelled correction*: the field a chemist changed, from what to
      what, made by the person with the most context at the moment they had it. That is the
      highest-quality supervision this system can collect about its own suggestions and it is
      currently written and never read.

      **Deliberately not built yet, and the reason is the one `reject_widening` was deleted for**: a
      miner over an empty table is a mechanism whose only caller is its own test. What is owed first
      is a count — over the designs on disk, how many carry a human revision at all, and do the
      changed paths concentrate anywhere — and that count needs a deployment that has been used.
      The anchor when it does: `protocols/diff.py` and
      `experiment_protocol_revisions.author_kind`.

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

      **Two independent surfaces raised the ceiling within two days, which is the argument for this
      row rather than against it.** `D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`
      added the prescriptive protocol tools (29,500 → 33,000, measuring 32,184), and the eight
      infrastructure findings of 2026-08-29 added five more to `default` — `review_activity`,
      `request_external_input`, `check_pending_requests`, `review_commitments`,
      `assemble_evidence_pack` — measured at **2,170 tokens** after a trimming pass. Four of those
      five are what makes the manager persona answerable at all, so both are capability rather than
      drift. The allow-list's **-5,787** is larger than either surface cost and larger than the
      headroom now left. Still blocked on the live lane for the reason above.

      **Every absolute above is a lower bound, and the case is stronger rather than weaker for it.**
      All of them were measured on a basis the 2026-08-29 re-baseline corrected: the ratchet counted
      the registry's callables, not the tools the graph binds, and under-measured `default` by
      **8,126 tokens (24%)** — 34,379 reported against 42,505 paid, ceiling now 44,500. So 28,114
      and −5,787 both understate what this narrowing is worth, and the eleven names should be
      re-measured on the bound basis when the row is worked. What does not change is why it is
      blocked: the saving is still partly in endpoint tools no offline floor can see, and it still
      needs the skill gate beside the allow-list.

- [ ] **A tool schema is 72% description, and the rationale vein the old row named is already
      closed** — [M], re-measured 2026-09-14 on the bound surface
      (`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`).

      The row this replaces said the cost was Pydantic *class docstrings* carrying design
      arguments — "One `objectives` field rather than a lead objective plus a sidecar list (W3)" —
      published as JSON-schema descriptions. **That was fixed before this row was worked**:
      `science/bo/problem.py` carries a comment beside class after class saying the rationale is
      deliberately in a `#` comment rather than in the docstring, and `start_optimization_campaign`,
      quoted at 8,063
      chars of schema with 4,392 of description, now measures **1,565 tokens in total**.

      What the re-measurement found: 92 bound tools, **57,036 tokens of schema**, of which **41,070
      (72%) is description text** — and it is overwhelmingly caller guidance. By docstring section:
      `Args:` **8,482** over 72 tools, `Returns:` **4,747** over 76, `Raises:` **723** over 8. A
      scan for developer-rationale tells flags 28 paragraphs and most are `Args:` false positives.

      So there is no blanket cut here, and the per-paragraph judgment the old row asked for is worth
      about **309 tokens** — which is what it was worth, measured, once taken (64,907 → 64,598).
      The ceiling that lowering bought was dropped by the merge that resolved it against the
      harness-default raise and is restored by
      `D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising`; the shipped value is
      `CEILINGS["__default__"]` and not a figure here. What is left open is the part a test cannot decide: `Args:` and
      `Returns:` together are 13,229 tokens of every model call, and whether a shorter
      argument contract still reaches the right tool is a `make live-ab` question, not a reading
      question.

- [ ] **The probed surface has a long thin tail: 39 of 114 tools rest on one probe** — [S],
      measured 2026-09-15 (it read 45, measured 2026-09-14 and stale inside its own merge range —
      `feba79b` added 36 probes in it, 28 of them `process-chemistry.yaml`), and it replaces the concentration row rather than continuing it.

      **The concentration is gone and the row's headline was stale.** `gather_evidence` is in
      **139 of 333** probes — **41.7%**, against the 50% (116/232) the headline was written from and
      the 60% bound `tests/test_probe_coverage.py` already holds. Widened: 55% of tool-naming probes
      touch any retrieval tool and only **14%** touch nothing but retrieval, so "the corpus mostly
      measures one retrieval path" does not reproduce.

      What the same measurement found instead: **39 of 114 agent-callable tools are named by
      exactly one probe** — 34% of the surface resting on a single phrasing, where a probe the model
      happens to answer reads as coverage. It is thin and it is **not hollow**: zero of those 39
      rest on a bucket-C probe, which `test_no_tools_only_coverage_is_a_question_the_surface_cannot_
      answer` now holds, so a tool cannot arrive with coverage that never calls it.

      Deliberately **not** a ratchet on the count. A bound on "how many tools have one probe" blocks
      every new tool until somebody writes it a second question, which taxes adding capability
      rather than bounding risk. What is open is ordinary corpus work: second questions for the
      tools that matter most, chosen by what a deployment actually calls rather than by the list's
      order.

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

- [ ] **A profile should be able to supply prompt *blocks*, not only a string** — [M], and this is
      the general form of `D-2026-09-14-a-profiles-prose-is-text-this-repository-wrote`. The default
      prompt is 31 `PromptBlock`s, each declaring `requires`/`absent_unless`, so a sentence naming a
      tool this deployment lacks is dropped before the model reads it. A profile's `instructions:`
      is one opaque string and gets none of that — the exemption `instructions_for` states is for a
      *site's* manifest, which this repository cannot cut into blocks, and it is right about that.
      What it leaves open is that a site narrowing a profile's tools has no way to narrow its own
      prose either: the field would have to accept a list of `{text, requires, absent_unless}` with
      the same ten rules, and the reason it is a row rather than a commit is that it has **no
      caller** — all six shipped profiles are strings, the repository-owned half is now guarded by a
      test, and a second-domain deployment is hypothetical. Build it with the first site profile
      that narrows tools, not before. This is also the honest remainder of Wave 7's "the prompt into
      profile data": the prose is already data (`data/profiles/*.yaml`), what is not is its
      *structure*.

- [ ] **A cut tool result is unrecoverable, and the store that would hold it is downstream of the
      cut** — [M], the last open Wave 1 item ("make a cleared tool result retrievable by address").
      Measured 2026-09-14, and the two halves are not the same problem.
      `D-2026-09-14-the-lossy-step-is-the-cut-and-upstream-already-offloads` already established
      that the *clear* loses nothing — it runs over a deep copy inside `wrap_model_call` and the
      next turn re-derives the same reduction from the full thread. The **cut** in
      `agent/tool_result_size.py` is the lossy one, and driven through the real middleware a 65,000
      character result comes out at 59,999 with the middle replaced by a notice.
      `api/tool_results.py` is exactly the address that should hold it — content-addressed on the
      SHA-256 of the text, already served by `GET /sessions/{id}/tool-results/{ref}` — but
      `api/graph_stream.py` calls `trace.returned(...)` on the `ToolMessage` the graph *emits*,
      which is post-middleware, so `tool_result_blobs` stores the cut text and the removed middle
      reaches no store at all.
      **The plan this row first carried does not reach the goal, and that is checked rather than
      argued.** It said to hand `bound_tool_results` a sink so it stores the raw text before
      cutting. It would — and the *stream event's* `result_ref` would still address the cut text,
      because that ref comes from `ToolCallTrace.returned`, which `api/graph_stream.py` calls on the
      `ToolMessage` the graph emits. The store is content-addressed, so raw and cut are different
      rows by construction (driven: two refs, not one). Storing the raw bytes therefore puts them
      somewhere real and leaves every existing consumer pointing at the cut version — which is the
      feature looking done while nothing a chemist clicks has changed.

      So the open decision is *which consumer* is being served, and the two answers want different
      builds. **A chemist's "show the full result"** needs the trace's ref to be the raw one, which
      means the raw text reaching `ToolCallTrace` — and the trace sits downstream of the middleware
      by construction, so this is a plumbing question about the stream, not about a sink.
      **A model that can re-read what was cut** needs the notice to name a ref *and* a tool to
      fetch it, which is a new model-facing surface that re-inflates exactly what the budget just
      reclaimed, and wants the offload-and-pointer argument in
      `D-2026-09-14-the-lossy-step-is-the-cut-and-upstream-already-offloads` rather than this row.
      Pick the consumer first; `tests/test_layering.py` forbids `agent -> api` either way, so
      whatever is chosen arrives through an injected callable (`ResultSink` is already one) rather
      than an import.

- [ ] **Three subsystems want one missing column: who wrote this** — [M]. `Note.created_by` is
      `Literal["human", "agent"]` and `Note.source` is the ingest source, so **a note names no
      person** — found while scoping the conflict notice
      (`D-2026-09-14-a-contradiction-only-a-querier-sees-is-not-a-warning`, which addressed the
      subscriber instead and needs no column). `audit_events.agent` is the same shape one layer
      over (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted the claim
      rather than the column), and `session_messages` is the third, in the row below. Each is a
      schema change plus a backfill question over rows already written, and taking it three times
      in the corner each subsystem noticed it is how three subtly different answers to one question
      get shipped. Decide the shape once — what an author *is* when the writer is an agent acting
      for a person — then migrate each. Nothing is blocked on it today: every consumer that wanted
      it has an addressee it can reach without one.

- [ ] **Several humans in one session is five pieces, and the policy one has to be settled first**
      — [L], scoped in `docs/archive/PLAN-2026-09-14-multiplayer-and-the-open-delegation-questions.md`.
      Not the owner gate relaxed: measured, `session_messages` has **no actor column** so a shared
      transcript cannot say who wrote what; ownership is checked in 46 places under
      `src/chemclaw/api/`; `api/detach.py` holds one queue and one `_attached` flag, so a second
      reader *steals* events rather than seeing a copy; and two writers on one thread fork the DAG
      silently (Wave 2's measurement), which is what `SessionTurnClaims` prevents by refusing.

      The serialisation is already correct and only its *answer* is wrong — one turn at a time is
      the right semantic for a shared thread, so the 409 becomes a bounded queue rather than the
      claim being relaxed. Order: participants, attribution, queued turn, reader fan-out. **Settle
      the authority questions before the schema**: whose roles govern a tool call, whether B may
      approve a plan A's message produced, and whose `/memories/` load (they are namespaced per
      actor digest, so a shared session loads none, the sender's, or a session tier that does not
      exist). Cheap to decide now, a migration to decide later. A chat-room connector is separate
      work on top and wants 1–4 finished first.

- [ ] **Run the delegation comparison against a real gateway, and accept a negative result**
      — [M], and it is the gate on Wave 3's roster. `evals/delegation.py` and
      `data/evals/probes/delegation.yaml` exist and have never been run against a model; the blocker
      is an OpenAI-compatible endpoint, the same one #359/#360 wait on. **A negative result closes
      the question as legitimately as a positive one** — written down because the retired specialist
      team was added to be ready and stayed off, and because a disappointing answer is not a reason
      to re-open a measurement. What would *not* close it is another delegation-*rate* number:
      `D-2026-08-12` measured 2 of 15, `D-2026-08-13` measured 14/15 against 14/15 with the old arm
      at ceiling, and two of those probes span two specialists, so that figure had an unpassable
      floor before any model was involved.

- [ ] **A routing corpus where the right profile is not inferable from the question's surface**
      — [M]. Seven profiles ship and genuinely narrow (`evidence` reaches zero side-effecting tools,
      `safety` one, `default` all 49); what `D-2026-08-15` deleted is automatic routing between
      them. Re-opening it needs a corpus the retired one did not contain: cases where the profile a
      question *needs* differs from the profile its wording suggests, compared on **answers** rather
      than on which specialist was picked. Until that corpus exists a router is a guess with a
      metric attached, and this row is the corpus rather than the router.

- [ ] **A bucket-C probe's absence claim is prose, so nothing can check it against the surface** —
      [M]. `D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing` found
      six sites asserting the ICH Q3C/Q3D tables and the mutagenicity alert set were absent while
      the declared `safety` bundle bound all three, and three of them turned the assertion into a
      `forbids_claims` entry — so a model that looked a limit up and cited it scored as fabricating.

      **The existing guard cannot see this class and the reason is structural.**
      `tests/test_probe_coverage.py::test_no_tools_only_coverage_is_a_question_the_surface_cannot_answer`
      builds `by_tool` from `probe.expects_tools`; a probe that wrongly asserts a capability is
      absent names no tool, so it is invisible to a check that starts from the tools probes name.
      Nothing can read *"claiming an ICH guideline text or limits table is available to it"* and
      resolve it to `ich_impurity_limit`.

      What would catch it is making the absence claim **structured**: a required field on every
      bucket-C probe naming the capability it asserts is missing — a tool name, or an explicit
      marker when no tool name applies — which a test resolves against `available_tool_names()` and
      fails when the named tool is bound. That is `Chemclaw3-mcp`'s `CEILING_IS_ARGUED_ABSENT`
      shape, where an exemption has to be written down for a reader to believe there is one.

      The cost is annotating the bucket-C corpus, and it is the reason this is a row rather than
      part of that commit. The marker arm is the load-bearing half and also the weak one: an author
      who would write the absence claim wrongly will write the marker wrongly too, so the field
      buys a *reviewable* lie in place of an invisible one rather than an impossible one. Worth
      stating before anybody builds it, because "required field" reads as a stronger control than
      it is.

- [ ] **This environment's `API-KEY` comes and goes, and one row is blocked exactly while it is
      down** — [S], and it is operational rather than code. It was three until 2026-09-04, when the
      credential answered and the tool-utility A/B was built and run through it in one session
      (`D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter`) — which is the row's own
      prescription working: probe first, then measure in the same session. Measured 2026-08-25:
      `anthropic.AuthenticationError: 401` with and without the session's `ANTHROPIC_BASE_URL`
      cleared. **Re-measured 2026-08-27: the same variable answers** (a haiku call returned 200; the
      day's verifier-margin run spent ~120 calls through it), so present-and-rejected is a *state*
      of this environment rather than a fact about it, and the worse case remains the stale one —
      it reads as a defect rather than as a missing credential.
      **Nothing in the suite probes it any more**: `tests/test_prompt_caching.py` did, skipping with
      a reason that named which case it was, and that file went with the prompt-caching mechanism
      when the provider concept was removed (`D-2026-09-04-a-gateway-is-the-only-provider`). The
      key is also no longer usable by `src/` directly — every model call goes to the gateway
      `CHEMCLAW_LLM_BASE_URL` names, so this credential is only a credential *for* a gateway
      (`infra/live/e2e-full-stack/up.sh` maps it onto `CHEMCLAW_LLM_API_KEY` when one is
      configured). The *live* half of the eval plan (the bucket-C control arm, any external
      benchmark, grading any probe on the model's judgement) needs the working state and nothing
      else — probe first (`printenv 'API-KEY'`, one cheap call **through a gateway**), then run the
      measurement in the same session, because tomorrow's state is not evidence about today's.

- [ ] **Memory records; it does not change what the next turn does** — [L], and it needs an ADR
      before it needs code. Six tiers exist and all six are *read on request*:
      `memory/campaign.py`, `interaction.py`, `failure.py`, `playbook.py`, `progression.py`,
      `observations.py`, surfaced by `recall_observations`, `find_past_jobs` and `record_failure`.
      Nothing in that set changes the agent's behaviour on the next turn unless a human writes a
      `SKILL.md` — `skills/playbook-distillation/SKILL.md` is the distillation *judgment*, and the
      loop is manual end to end. The 2026 work (SkillRL, SkillForge and the self-evolving surveys)
      is specifically about abstracting recurring trajectories into reusable procedure
      automatically. What is owed first is a measurement rather than a mechanism: over the sessions
      on disk, how many recurring trajectories *are* there, and would a distilled one have changed
      a later answer? A generator built before that number is a routing hypothesis nobody measured,
      which is the mistake `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` already
      made once here.

      **The measurement is no longer owed; the corpus is.**
      `D-2026-08-27-count-the-trajectories-before-building-the-distiller` defines the recurring
      trajectory and ships `make trajectory-census` (`chemclaw.cli.trajectory_census`), which
      prints its own verdict against the greenlight numbers. It was blind to half the signal —
      recurrence was an identical tool-name *sequence*, so a corpus dense in repeated **failure**
      reported zero — and `D-2026-09-05-a-census-that-counts-only-success-is-blind-to-half-the-signal`
      gave it a second arm over tools that errored across sessions. Read `any_greenlit`, not
      `generator_greenlit`. Both arms report zero on a database that has never served a user
      (measured 2026-08-27: 0 sessions, 0 turns; `session_turns` 0, `observations` 0), so the
      block is **deployment history**, not effort. The day a deployment has sessions this row is
      one command to check.

      **The tier question this row used to carry is settled**
      (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`, and
      `D-2026-09-05-the-gate-is-deleted-not-dormant` which deleted the PR-gate and all 2,232 lines
      behind it). Knowledge is global the moment it is learned and is corrected rather than
      pre-approved; a **skill** is the opposite case, because it is injected into the prompt with
      no citation trail, and no agent path may write one
      (`agent/skill_backend.SkillsReadOnlyRefusal`). So a distilled playbook does not become
      knowledge through a review queue — it lands through `kg/record.py` like any other note, and
      what needs an admin is the `SKILL.md` a distiller would want to write. What is open here is
      the generator alone, plus the per-actor skills directory that tier needs, which is blocked on
      the same generator because nothing writes one until a distiller exists.

      **Review scaling and convergence, ideated 2026-09-05 and not built.** The owner asked how an
      admin avoids drowning in near-identical proposals and how local and global skills stay
      convergent. Every part of that ideation is downstream of the distiller, and the one piece
      that was built — a reviewer seeing every earlier version of a note
      (`D-2026-09-05-a-rejection-nobody-reads-is-a-decision-taken-twice`) — was built against the
      PR-gate's review queue and deleted with it hours later, on the same day. Recorded so it is
      not re-derived: promotion thresholds on skills (used N times **and** by ≥ 2 distinct
      chemists — D-161's two-threshold shape, and the single most effective flood control
      available); duplicate suppression in the **generator** rather than in a queue (propose an
      edit to the nearest existing skill unless none is close); cluster review over
      `cluster_by_similarity`; benefit-ranked triage over `evals/ab.py`, with the machine ordering
      and the human still deciding, which is why it does not re-open `D-2026-08-16`; and the
      convergence half — global-wins-on-conflict with the conflict surfaced, promotion retiring the
      local variants that fed it via `memory/supersede.py`, expiry on disuse read as a signal about
      the *distiller* rather than about review capacity, and `skill-validate` run on local skills at
      write time so form converges even where content does not. One constraint binds all of it:
      `D-2026-08-25` ends with **no Temporal Schedule opens a pull request**, so a reconciliation
      job may cluster, measure and report, and a human opens the proposal.

      **Two findings from the reviewed framework (WikiSkill, arXiv 2608.27454) are recorded because
      they contradict the obvious design and cost nothing to carry**: giving the *executing* agent
      the accumulated experience measured **worse** than not (63.7% → 60.9%), while giving it to the
      *proposer* was the largest ablation (+15.0pp) — so experience is compiled into skills, never
      injected into the turn; and rejected proposals were load-bearing input, which this tree no
      longer retains at all. `kg/proposal.py` and its `rejected_version` went with the gate, and
      `durable/retention.py:413` still refuses to prune `note_proposals` for a reason whose subject
      no longer exists. A distiller that wants its own rejections has to keep them itself.

### The upstream-capability register — what our pinned dependencies now ship that we build ourselves

*Re-derived 2026-08-25, and re-derive it whenever a dependency is bumped.* `make upstream-check` and
`tests/test_upstream_surface.py` guard the *shapes* this repository borrows — the coupling that
breaks on a bump. Nothing guarded its **decisions** against upstream shipping the thing, which is
why the Temporal LangGraph plugin sat five weeks old and reached no list here. This is prose rather
than a test, deliberately: what is being watched is judgement, and a test cannot hold one.

Pinned when the standings below were derived: `temporalio` 1.31.0 · `langchain` 1.3.15 ·
`langgraph` 1.2.11 · `langchain-core` 1.5.5 · `deepagents` 0.7.6. **Installed on 2026-08-29:
`langchain` 1.3.16, `langchain-core` 1.6.0, `deepagents` 0.7.8** — the other two unmoved. Three
bumps have landed since, and nobody has re-read the release notes against the middle column, which
is the one job this table asks for. Re-derive it with
`uv run python -c "from importlib.metadata import version; ..."` rather than trusting this line:
it is provenance for the standings, not a claim that they are current.

| Upstream ships | We | Standing |
| --- | --- | --- |
| `temporalio.contrib.langgraph.LangGraphPlugin` — graph nodes as activities, durable `interrupt()` | run two durability layers | **declined**, `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use` — we use no `interrupt()`. **Its second reason was false and is retracted**: that ADR wrote that "the human gate is already a Temporal workflow" via `agent/interaction_tools.py::start_approval`, and neither that module nor that function has ever existed in `src/` — the plan gate is a Postgres row and a refusal. See `D-2026-08-29-a-decision-that-waits-is-a-workflow`, which supplies the durable wait the claim described |
| `langchain.agents.middleware.ContextEditingMiddleware` / `ClearToolUsesEdit` | use it, on its own trigger since 2026-08-25 | **adopted** |
| `SummarizationMiddleware` | construct it switched off (`disabled_summarizer`) | **declined** — a summary is new model prose over content `agent/framing.py` marked untrusted, and the envelope does not survive it |
| `ModelCallLimitMiddleware` | subclass our own cap | **reverted**, `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` — measured, a cap of 2 ran 4 model calls |
| `ToolErrorMiddleware`, `ToolRetryMiddleware` | neither | **declined** — both trigger on raised exceptions and MCP tools never raise |
| `HumanInTheLoopMiddleware` | our own plan gate | **declined for plan approval**, `D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question` — an async `when` cannot be awaited (fails closed, silently), a new user message discards a pending interrupt and corrupts the thread, a mismatched resume bypasses the gate, and retention prunes an unresolved interrupt; **not** declined for per-call approval of an irreversible action, which is a different, still-open question. Restart condition is monitored by `tests/test_upstream_surface.py::test_the_interrupt_on_predicate_is_still_synchronous`: upstream shipping an async `when` |
| `deepagents.SkillsMiddleware` | use it, narrowed at the backend | **adopted**, with the narrowing on the backend because deepagents publishes skill *paths* into the prompt |
| `deepagents` `execute` filesystem verb | withhold it | **declined**, and answered elsewhere — `D-2026-08-25-a-sandbox-is-a-server-not-a-verb` puts the capability in the fleet instead |
| LangSmith tracing | first-party OTel + OpenInference | **declined** — proprietary, no OSS self-host, and its core value is prompt/response content in a third party |

#### MCP — the protocol under every tool, job and skill

*Added 2026-08-29 by the infrastructure audit (F6).* The table above watches four Python
distributions and had no row for **MCP**, which is the wire every connector, every endpoint tool and
every fleet server speaks. That is the register's own stated failure mode — *"a capability upstream
ships that this table does not mention is the gap this register exists to catch"* — one layer below
where it was looking.

Pinned: `mcp>=1.2.0,<2` here and in `Chemclaw3-mcp`, deliberately (`CLAUDE.md`: matching
`mcp.server.fastmcp` keeps `connector_app` line-for-line comparable with `connectors/server.py`).
The **2026-07-28 specification** and the roadmap dated 2026-08-22 have moved several things that
answer problems open in this file.

| Upstream ships | We | Standing |
| --- | --- | --- |
| **Progressive discovery** (roadmap, Core Primitives WG) — a client learns a server's tools as it needs them instead of ingesting the catalogue | ship every endpoint tool's schema on every turn, and pay 28,114 tokens for it | **watch, and it changes the shape of two open rows.** § 5's profile allow-list saves a measured 5,787 tokens (-21%) by narrowing *our* side; this narrows the *server's*. The allow-list is still worth doing — it is available now and it is ours — but a design that assumes the full catalogue arrives up front is the thing to avoid building on top of. Note what an offline floor cannot see either way: the saving is flat in the six `enumerate_*` endpoint tools |
| **Tasks** (`io.modelcontextprotocol/tasks`, SEP-2663) — poll-based `tasks/get`/`tasks/update`, moving toward core | run durable work as Temporal jobs behind a synchronous tool call, with our own push-back | **declined for durability, watch for the wire.** Durability stays Temporal's (D-002, and D-2026-08-10 §3 made that stricter, not looser). What Tasks would replace is narrower: the `request_timeout` a slow fleet server is called under, and `D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work`'s split between bounding the wait and bounding the work. Worth reading before F2's durable wait is designed, not after |
| **Server-initiated events / webhooks** (Triggers & Events WG) — servers tell clients work finished, without client polling | `durable/notify.py` → `session_events` → the front door's tailer | **declined.** The push-back is between *our* workflow and *our* front door; a fleet server is stateless by contract and has nothing to push. Reconsider only if a server ever holds state, which `Chemclaw3-mcp`'s own rule forbids |
| **Agent identity and delegation** — Workload Identity Federation (SEP-1933), ID-JAG, RFC 8693 token exchange, DPoP | a static bearer per server (`token_env`), with `X-Chemclaw-Actor` logged and explicitly never trusted | **the row that matters, and it is open.** D-2026-08-15 deleted our workload-identity federation, OBO and HPC identity bridge as 254 LOC whose only callers were their own tests — correct then, and the ADRs that designed them stand. What has changed is that the caller-side need is arriving (F1's effector seam is a write path that needs an on-behalf-of identity) *and* the standard now exists. Re-adding one is still a new decision; this row is where the trigger is recorded |
| **`ttlMs` / `cacheScope` on list results** (SEP-2549), ETags on tool calls (roadmap) | `tool_result_store` addresses results by content, and re-lists a connector's tools per turn | **watch.** The connector-side half is measured already (`chemclaw_connector_tool_schema_tokens`), and a TTL on the list is the cheap half of the context-floor problem |
| **MRTR** — `resultType: "input_required"` replaces server-initiated `elicitation/create` | `ask_clarifying_question`, a first-party tool | **declined.** Ours asks the *chemist* a question the model composed, through a surface that renders choices; MRTR is a server asking its client for input. Different question, same words |
| **Deprecated:** legacy HTTP+SSE transport, Dynamic Client Registration, `sampling`, `roots`, `logging` | streamable HTTP already; no sampling, roots or DCR anywhere | **already clear, on a dated clock.** The 12-month deprecation window is a migration obligation across both repositories and neither uses the removed surfaces. Confirm on the 2.x move rather than assuming |
| **Stateless protocol + `Mcp-Method`/`Mcp-Name` header routing** — no `initialize` handshake, no `Mcp-Session-Id` | `mcp.server.fastmcp`'s session manager, and `connectors/server.py`'s five documented traps around it | **the 2.x migration, and three of our five traps are its subject.** "The parent app must run the MCP session manager" and "the caller must be re-bound per tool call" are both artefacts of a stateful handshake. A stateless protocol does not make them safe; it makes them obsolete. Do not fix them twice |

**How to use this block.** Same rule as the table above — on a spec revision, ask *does upstream now
do this, and better?* — with one addition that is specific to a protocol rather than a library: a
row here binds **both repositories**, and `Chemclaw3-mcp` cannot see this file. A row that changes
answer needs an ADR here and an issue there, in the same change.

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

The open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md)
(`grep -c '^- \[ \]'` on that file counts them), grouped by the review that found them, with their
full measurements. That set **overlaps** this queue rather than extending it — promotion restates a row,
so a queued row is still open there under its original wording, and the header's "~185 further"
was a subtraction nobody could reproduce. They are open, not abandoned — promote one into the queue
above when it becomes the next thing worth doing, and delete it from here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Temporal broker, real
cluster, a real Databricks workspace — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.

## The same three questions cost 2.1x more on one boot than on another

Found by `make live-turn-cost`, the lane
`D-2026-09-14-a-cost-metric-that-reads-a-file-measures-the-file` built. Across two boots of the
**same commit**, the same scripted three-turn workload cost **900,198** and **429,076** billed
token-equivalents — stable and byte-identical within each boot across repeated runs, so this is a
property of the process rather than of the run.

What the ledger already says about the difference: `context_unreducible` true on 3 of 3 turns of the
expensive boot and false on 3 of 3 of the cheap one, the model calling a tool on 3 of 3 turns
against 1 of 3, and a per-model-call request of 299,826 characters against 214,206. The ~21,000
estimated tokens between them is the size of two or three connectors' tool schemas against a
measured total of 31,208 (`chemclaw_connector_tool_schema_tokens`), so **a bundle whose tools were
not bound on one boot is the leading candidate and is not evidence** — nothing was observed binding
a different set, and the inventory line was identical in both.

Why it matters beyond the lane: if it is a binding race, a deployment can serve a *narrower tool
surface* than it advertises, silently, and the only trace is a cost half what the other pod's is.
The next step is to record the bound tool names and the schema gauge at each boot and compare, which
`make live-turn-cost`'s regime line now makes visible from outside.

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

## A failed template run restarts at step 1, and the steps it already ran are recorded unread

`failed_template_record` (`durable/template_job.py`) writes a `job_records` row whose `result` is
`{"steps": completed}`, and its docstring says why: *"A five-step procedure that died at step four
ran four real steps, and discarding them would lose the work while recording only the failure."*
**Nothing reads it back.** `scope` and `results` are rebuilt empty on every execution, and
`ALLOW_DUPLICATE_FAILED_ONLY` means a relaunch is a fresh execution of the same id — so the work is
kept as a *record* and redone as *work*.

**Deliberately not now, and the reason is a measurement rather than a preference.** A `tool` or
`job` step re-runs through `science/calc/store.cached_compute`, so D-011 makes most of a retry a
cache hit rather than a recompute — the expensive half of a re-run is already free. What is *not*
cached is the `agent` step, whose tokens are re-paid in full, and `agent_step_max_attempts` is 1
(`D-2026-08-12`'s argument: a retried agent step re-runs its side effects), so an agent step is
also the likeliest place a run dies. Every shipped template has exactly one and it is always last —
so today the step that would be resumed *is* the step that failed, and resume buys nothing at all.

**Trigger.** A template whose `agent` step is not last, or a deployment with failed runs to count.
What is owed first is that count: over `job_records` rows with `state='failed'`, which step id they
died at and what the completed steps cost. A resume built before it is a mechanism whose only
caller is its own test — the shape `reject_widening` was deleted for.

**And the design constraint, so it is not rediscovered:** loading prior results into `scope` is a
database read, so it cannot happen in workflow code; it is an activity whose result enters history,
which is also what keeps replay deterministic.

Anchors: `durable/template_job.py::failed_template_record` and the `scope` rebuild at the top of
`TemplateWorkflow.run`; `science/calc/store.cached_compute`;
`D-2026-09-15-a-bound-that-stops-at-the-seam-is-not-a-bound` (which declines it and says why).

## A composed workflow is discoverable only by guessing a name wrong

`run_composed_workflow` lists a chemist's workflows in the refusal it gives an unknown name, which
is the whole of discovery. Within one session that is enough — the agent composed it and knows what
it called it. Across sessions it is not: a chemist who has forgotten the name learns the real ones
by guessing one, which works and reads like a bug.

**Not a third tool, deliberately.** A `list_composed_workflows` schema is re-sent on every model
call of every turn to answer a question asked once, and the prefix it would land in has just been
raised 1,800 to hold the two tools that exist
(`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction`). The cheaper shape is the
session's *opening* context — the same place `session_store` already puts what a turn resumes with
— where it costs one turn rather than every model call.

**Trigger.** A deployment where composed workflows outlive the session that wrote them: measure how
often `run_composed_workflow` refuses on an unknown name against how often it succeeds. A ratio
near zero says discovery is not the problem.

Anchors: `agent/workflow_tools.run_composed_workflow`'s refusal branch;
`templates/composed.ComposedStore.list_for`, which already answers the question and has one caller.

## Template step roles cross the durable boundary on an unsigned payload

`durable/template_activities.py::_acting_as` binds `StepIdentity.roles` — the requester's real role
set, lifted out of a workflow argument. Every other reader of that payload binds
`frozenset()` on purpose (`durable/interceptor.py::activity_context`, `D-2026-08-28`): a relayed
argument is data, not a verified claim, so a role taken from it is a role anyone who can enqueue an
activity could forge.

The template path is the exception and the exception is argued, not an oversight.
`authorize_job_step` is the **first** authorization a template step gets — a step launched by
another step has no front-door pre-check behind it — so binding empty there would refuse every
entitled template job rather than fail closed on a forgery. Measured: neutering only the role bind
leaves `test_an_expensive_job_step_is_refused_for_an_unentitled_requester` still refusing and fails
`test_an_entitled_requester_passes_the_same_gate` outright.

**What it rests on today is broker write access being restricted** — Temporal mTLS, enforced under
`entra_required` — which is a deployment property rather than a check this code makes. Closing it
properly means a **signed payload**: a Temporal codec (or payload converter) that signs
`StepIdentity` on the way out and verifies on the way in, after which `_acting_as` binds a verified
claim and the exception disappears. That is a new piece of work with its own release story (a codec
is cluster-wide and both sides must be deployed before either relies on it), not an edit.

**Trigger.** A deployment that runs `TemplateWorkflow` on a broker whose write access is not
restricted to this system's own workers — a shared cluster without mTLS, or a namespace other
teams can enqueue into.

Anchors: `durable/template_activities.py::_acting_as` (the bind and its eleven-line comment),
`durable/interceptor.py::activity_context` (the fail-closed reader beside it),
`tests/test_template_job_step.py` (the pair that fails in opposite directions).

Replaces "two producers bind a template step's ambient identity, and only one of them is needed",
whose title was its premise: the two producers disagree about `roles`, on purpose, and collapsing
them would refuse entitled work rather than weaken a refusal
(`D-2026-09-12-two-producers-of-one-identity-are-not-redundant-when-they-disagree`).

## A truncated argument document is completed by upstream and the tool runs on the guess

`D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure` §3 recorded this as open and named the
order to close it in: change the storm's document, **then** decide the `finish_reason` question.
Only the first half happened. The 2026-08-28 campaign replaced `'{"text": "unterminated'` with the
unclosable `'{"text": }'` — correct, and the only payload that reaches `invalid_tool_calls` — and
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` then closed
F6 against that payload. The truncation hazard went with the old payload and is now asserted by no
check and no row anywhere.

What is still true, and is not the same defect: LangChain runs a streamed call's argument fragments
through `parse_partial_json`, which closes an unterminated string and an unclosed brace. So
`'{"smiles": "CC'` — a stream cut mid-document — arrives as a **valid** `tool_calls` entry reading
`{"smiles": "CC"}` and the tool runs on a truncated molecule, with nothing anywhere saying the
document was incomplete. `tests/test_invalid_tool_calls.py::test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid`
pins that this is what happens; nothing decides whether it *should*.

The signal upstream leaves is `finish_reason` (`length` when the provider stopped mid-emission),
which is on the response and not on the call, so telling "the model finished this document" from
"the transport cut it" is a response-level question this middleware does not currently ask. Closing
it means deciding what a `length` finish with tool calls means — refuse the reply and re-ask, or
run the completion and say so — and that decision is what §3 asked for and did not get.
