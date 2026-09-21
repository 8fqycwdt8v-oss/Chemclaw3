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
row this queue has ever carried. How many rows this file holds is a `grep`, not a sentence —
`grep -c '^- \[ \]' docs/planning/BACKLOG.md`. **The archive is not counted at all**, and that is
the point of it being a record: its findings are plain bullets rather than checkboxes, so no `grep`
answers "how many are open there", because none of them is open. The two sets do not subtract
either: promoting a row **restates** it, so a queued row is still open there
under its original wording, and matching the two sets by title matched only a small minority of
what this queue held when that was measured. §5 is the first thing here that is not a defect this
repository found in itself, and none of its rows is in the archive at all. The overlap is real
and unmeasurable by `grep`, which is why neither number is a difference.

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
`tests/test_connector_registry.py::test_the_health_probe_follows_an_override_that_moves_the_path_too`
had already pinned; and five of the anchors it wrote no
longer resolved to the construct they named by the time the branch was audited, four of them off by
a handful of lines. None of that is neglect — it is a measurement taken hours before the tree moved
under it, which is the failure mode this file has rather than an exception to it. Re-measure on the
way past; a row you had to correct before you could work it is a row that was worth opening.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **A plate's results never reach the design that prescribed them, so the round trip
  `hte-campaign-design` promises in its own closing section is handwork** — [L]. A design reaches
  `DesignStatus.executed` and nothing attaches what came back. Results enter only through
  `ingest/eln` as `reaction_records`, with no link to the `design_id` that asked for them, so the
  plate -> observations -> `suggest_next_experiment` path a chemist is told to expect is somebody
  retyping a table. It also starves two things that already exist: `campaign_progress` has to be
  fed observations by hand, and the `DEFERRED.md` row on mining the agent-to-human protocol diff
  ("the highest-quality supervision this system can collect about its own suggestions, and it is
  currently written and never read") has no corpus because nothing joins a stored design to its
  outcome. Wants its own ADR before any code: the open question is whether a result hangs off the
  design, off `reaction_records` with a `design_id` column, or off a third table, and that decides
  whether a plate run outside this system can ever be attached. Named in
  `docs/archive/IDEATION-2026-09-20-process-development-hte-and-protocol-prediction.md` §3.3.

- [ ] **A step template naming a fleet tool has no gate that checks its argument keys** — [M].
  `make template-validate` name-checks a tool whose bundle is declared but not served and reports
  `arguments unchecked`; `make live-template-args` is the only check that reads a running
  connector, and no lane in CI runs one. So the process-development templates the ideation asks
  for (§5: a thermal envelope, a solvent swap, a crystallisation first pass) would ship with
  argument names verified against nothing, which is the fabricated-argument shape
  `D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` refuses one layer over. Either read
  the sibling's `tool-surface.json` at validation time the way
  `tests/test_sibling_manifest_agreement.py` already reads its manifests, or decide these templates
  wait for a lane that can run `live-template-args`. The first is the smaller change and the one
  with a precedent.

- [ ] **A site-supplied regex from a datasource manifest runs against warehouse cell text with no
  timeout, so a catastrophic pattern hangs the ingest activity** — [M].
  `ingest/eln/warehouse/expr.py:234` (`_regex`, `re.search` per row) and `:357`
  (`_compiled_regex`, which validates the pattern and still returns a plain `re.Pattern`) take
  `options["pattern"]` straight from the `datasource.yaml` binding. The pattern is checked for
  *syntax* and never for backtracking behaviour, and Python's `re` has no timeout, so one
  `(a+)+$`-shaped manifest pattern against a long free-text column pins a worker thread until the
  activity's `start_to_close` expires — then the retry re-runs the identical pattern over the
  identical page, which is the `_BAD_DATA_TYPES` argument in reverse. Operator-controlled input, so
  it is not the untrusted-input case the rest of this section holds, and that is the whole reason it
  is queued rather than fixed in the same commit as the sibling ReDoS in `core/logging.py`: the
  remedies are different. Either bound the *input* (`as_text(value)[:n]` per cell, the cheapest
  honest bound and the one this seam can take without a dependency), or move the match off-thread
  with a wall clock, or reject a pattern whose shape is a known amplifier at
  `datasource-validate` time. Decide which, because a fix that only shortens the input is a
  mitigation and should say so.

- [ ] **A chemist has no in-product way to ask for a skill of theirs to be published** — [S].
  `D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` makes the promotion unit a
  *document* rather than a queue entry, which is what keeps `api/routes/proposals.py` owner-scoped
  by construction and keeps an administrator out of anybody's personal namespace. The accepted cost
  is this: a chemist who thinks their skill should act on everyone reads it back from
  `GET /skills/mine/{name}` and sends an admin the body out of band. A second queue is the shape
  that ADR refuses on `D-2026-09-05`'s own measurement, so the cheap fix is a *flag* rather than a
  queue — one boolean on the personal row and an admin listing that reads it — and it is worth
  building only once somebody actually wants a promotion and cannot get one.

- [ ] **`ToolScopedSkills` is applied to neither stored tier, so a skill about tools the turn cannot
  reach is still offered** — [S]. Was the personal tier's row; the organisation's inherits it and
  makes it worse, because an org skill naming a connector one profile lacks is offered to every
  chemist on that profile rather than to the one person who wrote it. The narrowing reads
  `declared_tools`, which parses frontmatter off a *directory*, and both stored tiers are stored
  rather than filed — applying it as-is means parsing every body out of the store inside a
  possibly-synchronous `ls`. The cheap shape is to parse on **write**, in the two publish routes,
  and keep the declared tools beside the body; it fixes both tiers at once, which is why this is one
  row rather than two.

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
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:184` — the row named a
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

- [ ] **A `STANDARDIZATION_VERSION` bump retires the fingerprint rows and re-keys nothing, so the
      graph keeps a note per superseded spelling forever** — [L],
      `src/chemclaw/core/chem.py::compound_id`, `src/chemclaw/ingest/eln/compound.py:85-92`.
      Driven against the live Postgres at `std7` -> `std8`: the fingerprint half works exactly as
      designed — `CC[NH3+].[Br-]` keys to `compound-b3ba1c117ed7` under `ecfp:…:std7` and
      `compound-bb572bdd9031` under `…:std8`, and a std8 similarity search returns only the
      corrected row. `compound_id` carries no version, so the std7-era `compound_note` keeps its own
      id in the knowledge graph with no cleanup path, which is the **first** consequence the fix
      commit named ("two `compound_id`s and two `compound_note`s for one substance") and the half a
      definition bump does not reach.

      Secondary and driven: `compound_dependencies` re-derives `compound_id(note.compound_smiles)`
      and returns `[]` when it no longer matches the note's own wikilink — so a std7-era note
      re-submitted under std8 silently loses its compound dependency rather than failing.

      Not fixed here because the two candidate fixes are both decisions rather than defect fixes:
      folding the version into `compound_id` invalidates every stored id at every future bump and
      breaks every citation to one, and rewriting the notes is a migration over layer 4 that
      `kg/record.py` — append and supersede, never rewrite — has no verb for. The recovery that
      *does* exist is `docs/guides/runbook.md:1996`: delete the corpus's `corpus_cursors` row and
      re-run the ELN sync. Weigh it against `src/chemclaw/durable/retention.py:498`, which records
      that a bump is "a permanent doubling" of `molecule_fingerprints`/`reaction_fingerprints`
      because the runtime role holds no `DELETE`.

- [ ] **A bare guanidinium salt never reaches the neutralisation branch, so it does not collapse
      onto its free base** — [M], `src/chemclaw/core/chem.py::_is_organic`. `standardize` reaches
      `Uncharger` only when some fragment is `_is_organic`, which requires a carbon bonded to
      hydrogen or to another carbon, and guanidinium's carbon has three nitrogen neighbours.
      Measured: guanidine hydrochloride has `organic == 0`, returns before both the strip and the
      neutralisation, and does not collapse — before the `std7`/`std8` work and after it. Metformin
      and acetamidine are covered only because their substituents happen to make them organic by
      that test, which is why the class-scope claim in
      `tests/test_compound_identity.py::test_an_amine_salt_drawn_as_an_ion_pair_is_its_free_base`
      has been narrowed to the shipped corpus. Nothing in `data/` contains a guanidine today, so
      this is latent; widening `_is_organic` is the fix to weigh, and it moves every fragment-count
      branch at once, so it needs the `standardize` behaviour table in
      `test_the_standardization_version_is_pinned_to_the_behaviour_it_names` re-measured and almost
      certainly a version bump.

- [ ] **Four first-party refusal gates are outside the weekly mutation backstop** — [S],
      `pyproject.toml` `[tool.mutmut].source_paths`. `agent/authz.py` is covered and
      `agent/plan_gate.py`, `agent/skill_backend.py`, `agent/spend_cap.py` and `agent/loop_cap.py`
      are not, although each is a control of the same kind — a refusal whose surviving mutant is a
      tool call that should not have happened. `core/chem.py` and `core/logging.py` were added on
      2026-09-19 for exactly that argument, after three mutations of theirs passed a full subset.
      Not added in the same change because the list is short on purpose — the comment above it
      records that the run is hours long — so each addition needs its runtime measured rather than
      assumed. Measure `make mutants` with one of them added before adding the rest.

- [ ] **The substructure deadline test asserts a timing ratio where it means a record count** —
      [S], `tests/test_molfp.py::test_a_scan_past_its_deadline_stops_instead_of_matching_the_rest_of_the_corpus`.
      Its own docstring states the property as *"went on matching every remaining record"*, which is
      a claim about records; the assertion is `bounded < unbounded / 2` over two live wall-clock
      measurements. That is a proxy, and it is the kind that fails on somebody else's machine: the
      bar was a quarter until it failed `main` twice in one morning at 0.270 and 0.271 while
      measuring 0.186-0.206 on an idle developer machine, for no reason but fixed setup the bounded
      run carries and the unbounded run amortises.

      The bar is now a half, with the spread and a deadline-mutation table in the docstring, so the
      control is measured rather than assumed — it still fails a leak to half the corpus (0.554).
      What is left open is replacing the proxy. Counting records is **not** a test-only change: on
      this corpus the scan takes the indexed path, and `substructure_index.labels_matching` chunks
      by *time slice* rather than per record, so there is no counting point without changing the
      module under test. The cheap shape is for `labels_matching` to return how many records it
      reached beside its labels — `ScanOutcome` already carries two caveats and would carry a third
      — after which the assertion is machine-independent and this row and the bar both go.

- [ ] **`Chemclaw3_ui` has no surface for the four `/skills/mine` routes, the six `/skills/org`
      ones, or the proposal queue** — [M], opened by
      `D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved` and widened by
      `D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius`. That ADR grants the
      personal tier its exemption from review *on the condition* that a chemist can see what is
      acting on their turns and remove it — and the only thing that can currently exercise the
      condition is `curl`. The routes are there, driven and tested
      (`tests/test_api_local_skills.py`, `tests/test_api_org_skills.py`,
      `tests/test_api_proposals.py`); what is missing is the half a person can reach.

      **The organisation's tier owes the condition more, not less**, because it is in the prompt of
      every turn every chemist takes rather than one person's. Its reads are open to every
      authenticated caller for exactly that reason, so the surface is two: a reader anybody gets
      (the names, one body verbatim, and the version list that is the only place "what changed and
      who" exists for a tier with no commit log) and an administrator's half behind the privileged
      role (publish, revert to a listed hash, retire). The revert is the one worth designing rather
      than generating: it is what somebody reaches for when a published skill is making every
      answer worse, and it has to show the bodies it can put back.

      It is a row in this repository rather than only in the frontend's because the condition is
      this repository's claim: `SECURITY.md` and `ARCHITECTURE.md` now say a personal skill is
      inspectable and removable, and until the UI ships that is true of an API rather than of a
      person. The shape is the smallest one that discharges it — a list of names, the body of one
      verbatim, a delete, and the save the agent's draft is posted through — and the two refusals
      worth rendering rather than swallowing are the 409s: a name this deployment already ships,
      and the row cap, which says in its detail why it exists (every personal skill is in the
      prompt of every turn its owner takes).

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

- [ ] **What `graph` means as a retrieval source: a lexical rule of its own, or the leg that reads
      the index** — [M], measured 2026-09-16 by the dependency audit that proposed deleting one of
      the two lexical rankers and had to withdraw it. Anchors: `agent/graph_tools.py::_scan_notes`
      and its `_relevance`, `retrieval/retrievers.py::LexicalRetriever`,
      `retrieval/vector_index.py::note_reindex_effective`, `CHEMCLAW_DATA_SOURCES`.

      Two lexical rankers run over one corpus, and `_scan_notes` justified its 151 ms event-loop
      stall (836 ms at eight concurrent, 10k-note corpus) by saying there is no database to push the
      scan into — which is false: `note_index` exists with a GIN `tsvector` and `search_lexical`
      reads it. **The removable leg is the opposite of the obvious one.** Ablated at a *matched slot
      budget* — three legs at k=8 deliver 24 slots against one leg's 8, so comparing them unmatched
      measures the budget rather than the ranker — the Postgres `ts_rank` leg strictly dominates the
      in-process BM25-lite: 42 gold notes found to 40, 24 in the top 3 to 19, and the graph leg
      contributes **zero** gold notes the Postgres leg misses.

      **It is still not removable, for a reason that is not about ranking.**
      `note_reindex_effective` schedules the reindex only when `lexical` or `vector` is in
      `CHEMCLAW_DATA_SOURCES`, and the shipped default is `graph,eln-json` — so in the default
      deployment the index nothing maintains is the one the survivor would read, and deleting the
      graph leg's own ranking retrieves nothing from the knowledge graph. The narrower removal that
      fits inside one file is a measured regression on its own: dropping just `_relevance` moves
      graph-alone mean gold rank 4.72 → 5.67 and loses a gold note from the shipped three-leg arm,
      39 → 38, which `retrieval_recall` gates on. The two rules are also not one rule in two
      spellings — driven against live Postgres, `couplings`, `coupled`, `dry` and `films` hit
      `ts_rank` and miss the substring rule, while `ester` matches `polyester` for `term_coverage`
      and produces no lexeme at all for the server; `tests/test_note_search.py` pins both directions.

      What closes this is a decision and a default, not a retriever edit: either `graph` keeps
      meaning a lexical rule of its own, or it becomes the leg that reads the index and the reindex
      stops being conditional on a source nobody enables. Re-run `make retrieval-arms` on both sides
      of it, and read the `hybrid` row above first — the audit quoted its 3.69 headline as a reason
      to cut a leg, and that configuration finds three fewer gold notes.

## 3 — Work that is lost, dropped or invisible

- [ ] **A re-proposal of a *superseded* body is answered as "already waiting to be decided" and can
  never be proposed again** — [S]. **Moved here from "1 — Untrusted input reaching a privileged
  surface", where it was misfiled**: nothing untrusted reaches anything, and no surface is
  privileged — the defect is that a chemist's decision has nowhere to land and the model is told a
  falsehood about its own proposal, which is this section's subject.
  `behaviour_proposals.propose`'s
  `ON CONFLICT (actor, kind, name, content_hash) DO NOTHING` then re-reads the row *in whatever
  state it is in*, and `_arrival` branches only on `stored.decided` — `superseded` is deliberately
  not a decision, so it books `outcome="already_open"`. Driven on both shipped backends: propose V1,
  propose V2 (V1 superseded), re-propose V1 → the row stays `superseded`, `GET /proposals?state=open`
  never shows it, `POST /proposals/skill/<name>` with V1's hash 409s, and the model tells the chemist
  it is waiting for their decision. The module's own rule is that "an unchanged re-proposal cannot
  reopen a **rejection**"; the idempotence is being applied one state too widely. Either revive a
  superseded row to `open`, or give `_what_became_of_it` a fourth branch that says so.

- [ ] **Three row-projecting tools defang a whole page on the event loop, and one of them is not in
  the offload test** — [M]. `commitment_tools.review_commitments`,
  `pending_tools.check_pending_requests` and `memory_tools.recall_observations` each escape every
  string in every row of their page synchronously, which is correct (the field-level carve-outs each
  let five to eight unvalidated fields through — see
  `tests/test_tool_framing.py::test_every_row_projecting_tool_escapes_its_whole_row`) and is not
  free. Measured on a realistic `Commitment` on a loaded box: **6.9 to 39.2 us/row, 5.7x** the
  two-field form, so **7.8 ms** of synchronous loop time per `review_commitments` at `_MAX_PAGE` of
  200 against ~1.4 ms before. A post-merge audit measured the same comparison at 19.3x and 18.9 ms;
  the ratio moves with the row's string lengths and with machine load, and neither figure is small
  next to the **2.6 ms** that `skill_manifest.declared_tools`' own docstring calls "the hazard
  `tests/test_event_loop_offload.py` exists for". None of the three is in that file.
  **Not fixed here because the cheap fix is the wrong one**: wrapping the comprehension in
  `asyncio.to_thread` moves 7.8 ms off the loop and buys a thread hop per call on the page sizes that
  do not need it, and the real question is whether the *page* is the right unit — a 200-row page is
  already more than a model reads. Trigger to revisit: any of the three appears in a turn-latency
  profile, or `_MAX_PAGE`/`observation_max_results` is raised.

- [ ] **A chemist's own `/scratch/` writes are unbounded and, by default, permanent** — [M].
  `agent_subagent_files_max_chars` bounds only what a *helper* hands back: it is applied in
  `rewritten_command_files`, which rewrites a `task` return's `Command`. A caller's own
  `write_file` goes through `StateBackend`, which writes the `files` channel directly as a channel
  write and reaches no middleware, so nothing caps `write_file`'s `content` argument. The channel
  is a `DeltaChannel` and accumulates; it is checkpointed under `thread_id`; and in the shipped
  configuration nothing ever deletes it — `checkpoint_retain_per_thread` prunes *superseded*
  copies only (the newest checkpoint still holds every file whole), `retention_enabled` is
  `False` and `retention_checkpoints_days` is 0, so the only route that removes a scratch file is
  `make user-erase`.

  **This is what is left of the row `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer`
  closed**, and it is worth stating separately because that row's framing is now wrong in the
  reader's favour: the bulk of the 20,712 kB it costed was the helper's inherited checkpointer
  rather than this channel — most of it, not "~98%", because that arm's own cap reclaimed 8.8% of
  the 20,712 — so the amplification argument for urgency is gone while the unbounded surface is
  not.
  It stays a decision rather than an edit for the reason it always did — a cap here truncates a
  chemist's own document, which is a different act from truncating a helper's. Anchors:
  `agent/scratchpad.py`, `deepagents.backends.state.StateBackend`, `agent/tool_result_size.py::_bounded_file`.

- [ ] **The helper file budget is charged to siblings that wrote nothing, and two write verbs are
  charged to nobody** — [M], opened by
  `D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep`, which closed the fan-out's
  fail-open and states both of these as what it did not do. Two halves of one resource, the
  caller's `files` channel.

  **The over-charge.** `batch_siblings` divides the remaining budget by the batch's calls *naming*
  `task`, because the siblings' results do not exist when it runs. Most `task` calls read and write
  nothing, so one helper filing a note beside seven silent ones is charged an eighth: driven at the
  shipped budget, a 199,999-character note lands whole at width 1 and as 25,000 at width 8. The
  bound holds — an unclaimed share is wasted, never spent — so this is lost allowance rather than a
  hole. The shape that would be exact is a trim over the **merged** channel after the superstep,
  where every real contribution is visible, and it is not free: the exemption that keeps a
  chemist's own documents out of this budget is `rewritten_command_files` comparing each command
  against the state *before* it, and a post-merge trim has nothing to compare against, so exact
  accounting has to buy the channel provenance first.
  `test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` is what a naive
  version breaks, which makes this a design with an ADR rather than an edit.

  **The unbounded half.** `write_file` and `edit_file` reach the same channel through
  `StateBackend`'s `send(...)` and return a plain `ToolMessage`, so they never pass
  `rewritten_command_files` and **nothing bounds them at all** — while everything they store is
  charged into `held` against every later helper. So the tightest arm of this budget is spent by
  the arm nobody measures. Anchors: `agent/tool_result_size.py::batch_siblings`,
  `_bounded_file`, `agent/tool_result_shape.py::rewritten_command_files`,
  `deepagents.backends.state.StateBackend`.

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

      **This row is now the only live instance of its shape, which is worth saying because it is
      not the whole shape.** "A guard satisfied while the thing it protects is false" was worked on
      `tests/test_repo_map.py` and `tests/test_readiness_record.py` by
      `D-2026-09-18-a-mutation-watched-failing-is-half-a-guard` — six mutations, all green over a
      live false statement, all closed. None of that touches this row: what is open here is the
      *universe* those two prose-contract guards read and the patterns they read it with, and no
      derivation used there reaches it. A reader who takes the class as closed would skip this.

- [ ] **An agent-recorded note the model could not date reaches no subscriber who has a
      watermark** — [M], found 2026-09-15 in the review of the wave 2/4/7 merge.
      `durable/digest._is_new` reads an absent `valid_from` as *open-ended* — true for as long as
      anyone has known — and therefore as not news, which is correct about the field and wrong
      about the question a digest asks. Measured on the shipped corpus: **33 of 40 notes carry no
      `valid_from`**, across ten types (`compound` 9, `playbook` 5, `campaign` 3, `interaction` 3,
      `job-result` 3, `bo-candidate` 2, `failure-mode` 2, `optimization-campaign` 2, `report` 2,
      `experiment-proposal` 1). Two producers are closed —
      `retrieval.harness.report_note(drafted_on=…)` and
      `durable.job_record.note_with_run_provenance(ran_on=…)`, both cases where validity and
      arrival are the same day by construction. `agent/graph_tools.py:554`
      (`record_knowledge_note`) is not: the model may legitimately not know when a fact became
      true, and defaulting `valid_from` to today would trade a silence for a false claim about
      chemistry. **The real fix is an arrival signal separate from `valid_from`**, which the
      subscription watermark cannot express today: `agent/subscriptions.py:68` bounds
      `last_seen_note_ids` to one day of matches *on purpose* (DARK-7), and an undated-id set
      grows with the corpus instead. The candidate worth measuring is the notes repository's own
      git history — one `git log --diff-filter=A --name-only` over `knowledge/` gives every note's
      add-date in one subprocess, cacheable behind the same corpus fingerprint `load_notes` already
      uses. Measure that scan on a 10k-note corpus before building it.

- [ ] **A `pending_requests` row whose run was terminated, or lost with its worker, has no
      collector** — [M]. What is left of the `pending_requests` settle row after
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

- [ ] **Nothing bounds what a turn costs the front door's memory** — [M], opened 2026-09-18 by
      `D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared`, which sized
      `resources.service` against a resident set with **no turn in flight**: 431.9 MiB measured on
      the real uvicorn front door with its lifespan run and `/healthz` served, no agent graph
      compiled, no connector session open, no checkpointer write. The memory request is now derived
      against that floor plus the parse path, and `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` is 12 —
      so the one part of the pod that scales with load is the part no number covers. The CPU half
      *is* measured (0.581 s of CPU over 8.32 s of wall clock per turn, which is why
      `requests.cpu` is 1); the memory half has never been. What it wants is the same shape as the
      parse measurement: drive a turn against the mock LLM and express the peak as MiB per admitted
      permit so `test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares` can take a
      third term. **Sample a memory cgroup's `memory.max_usage_in_bytes`, not `Pss`** — this row
      said `Pss` until `D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`
      measured what that is: a system-wide proportional share that understates a cgroup's charge —
      driven, one unchanged process read 10.3% less `Pss` while six unrelated siblings mapped the
      same shared objects, and got it back when they exited, where `Rss` moved 0.016%. Anchors:
      `deploy/helm/chemclaw/values.yaml` `resources.service`,
      `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`.

- [ ] **One parse budget serves two pods with four times the difference in room** — [S], opened
      2026-09-19 by `D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`.
      `CHEMCLAW_DOCUMENT_PARSE_MEMORY_BYTES` is derived from the front door — 1Gi limit, 523 MiB
      idle, two parse slots — and the background worker reads the same value with a 4Gi limit and a
      share binding whose `max_file_bytes` is 50 MiB. Measured, a 50 MiB delimited export needs more
      than 512 MiB to render even after `_parse_csv` stopped materialising its reader, and a 24.5 M
      character one needs more than 160 MiB: those files are now refused on a pod that could have
      afforded them. It is counted (`skipped_unreadable`) and the refusal names the ceiling, so this
      is a cost rather than a hole. The cheap shape is a per-Deployment override of the one env key
      the chart already renders; the honest alternative is lowering the binding's `max_file_bytes`
      to what one parse can hold, which is a site's declaration rather than this chart's. Wants a
      real corpus before either. Anchors: `src/chemclaw/core/config/sources.py`
      `document_parse_memory_bytes`, `src/chemclaw/ingest/documents/binding.py` `max_file_bytes`,
      `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`.

- [ ] **A parse refused by a C parser's own allocation failure arrives unnamed** — [S], opened
      2026-09-19 by `D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`.
      `parse.too_large_to_read` names the ceiling on both paths that raise `MemoryError` — extraction
      and the pickling of the answer — but a library that handles its own allocation failure never
      raises one: measured, `lxml` answers "Unable to allocate output buffer", so a markup-heavy
      `.docx` reaches a chemist as the generic "could not be read". Both are refusals and neither
      costs the pod; what is lost is the one sentence that tells a chemist to split the file. The
      shape worth trying is reading `RLIMIT_DATA` back in the broad arm and saying so when the
      process is at its ceiling, which is a state rather than an exception type. Anchors:
      `src/chemclaw/ingest/documents/parse.py::parse_document`,
      `src/chemclaw/ingest/documents/parse.py::too_large_to_read`.

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

- [ ] **A batched flush books the whole batch as notes recorded when any one of them committed** —
  [S]. `kg/git_writer.py::BatchingNoteWriter.flush` returns
  `notes=len(batch) if outcome.written else 0`, and `outcome.written` is `notes > 0` on the *inner*
  write, which is one commit however many files it carried. Driven against a local bare remote: a
  batch of four where three notes were byte-identical to what the tree already held and one was new
  committed once and reported `notes=4`, so `chemclaw_notes_recorded_total` moved by four for one
  note reaching the graph — the same class of error
  `D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes` fixed in the other direction, now
  overcounting instead of undercounting. Backfill-only (`cli/backfill_corpus`); the conversational
  path is one note per write and is exact.

  **Not fixed as a defect because the honest number is not available at that layer and making it
  available is a contract change.** `WriteOutcome.notes` means "notes that reached the graph", and
  the inner writer knows only "something was committed": it has the bytes each target held (`prior`)
  and could count the files that changed, but that number counts *dependency* notes and retirement
  rewrites too, which is a third meaning of the field beside the two D-2026-09-14 already weighs. The
  fix is either `WriteOutcome` carrying the paths it wrote so the batcher can count its own subjects,
  or a decision that the counter counts note *files* — both of which are ADR-sized rather than a
  commit and a test. Anchors: `kg/git_writer.py::BatchingNoteWriter.flush`,
  `kg/record.py::WriteOutcome`, `D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes`.

- [ ] **`retrieval_source_weights` has no upper bound, and the mix it produces is not a property of
  the weight alone** — [S]. `core/config/retrieval.py`'s validator refuses non-finite and
  non-positive weights and stops there. Driven over five legs at `retrieval_fusion_k=60`, counting
  the retriever of each kept chunk: uniform weighting keeps `graph 2 / lexical 2 / share 2 /
  vector 1 / warehouse 1` out of eight, `{"graph": 10}` keeps `graph 8` and nothing else, and at a
  thirty-chunk cut it keeps `graph 22` against 2 from each other leg. That is the starvation
  `D-2026-08-01-a-cap-that-starves-a-source` is about, reachable through a knob rather than a cap.

  **Not reachable at the shipped numbers, which is why it is a row.** `retrieval_top_k` is 8 and
  `gather_evidence_max_chunks` is 40, so five legs offer at most 40 candidates into a cut of 40 and
  the merge cap never binds — no weight can starve anything until a deployment raises the leg count
  or lowers the cap. And there is no ceiling to add: the validator's own docstring argues the point
  ("a weight has no upper bound to clamp toward"), and measured, the damage is a function of
  `weight x legs x cut` rather than of the weight, so a bound belongs on the surviving *mix* — a
  per-source floor in the merge — not on the number a deployment writes down. Anchors:
  `core/config/retrieval.py::_weights_are_positive`, `retrieval/hybrid.py::reciprocal_rank_fusion`,
  `core/config/retrieval.py::gather_evidence_max_chunks`.

## 4 — Operating it

- [ ] **A worker whose broker is down never opens its probe port, so "Temporal is down" and "the
  image is broken" are the same picture to everything but the container log** — [M].
  `durable/background_worker.py:98` calls `connect()` before `Worker(...)` is built and therefore
  before `durable/serve.py::serve_worker` opens the probe surface, so the process exits 1 at
  `core/temporal_client.py:209` and `:9000/healthz` and `/readyz` never answer at all. Driven
  2026-09-19 against a dead address: exit 1, a clear `SubsystemUnavailableError` in the log, and both
  probe routes unanswered (`curl` → no connection). The PodMonitor target simply disappears, so
  `ChemclawTargetDown` fires for this exactly as it fires for a broken image.
  **Not fixed here, and the reason is that the obvious fix may be worse than the gap.** Opening the
  probe surface before connecting means every worker entrypoint changes shape, and it turns a
  crash-loop that Kubernetes retries with its own backoff — and that self-heals the moment the broker
  returns — into a pod that sits up and unready indefinitely, which is the state `serve.py`'s
  `worker_ready` argument would then have to cover for a worker that has no client at all. The log
  does distinguish the two causes today; what nothing distinguishes them by is a *probe* or a series.
  Trigger to revisit: a second dependency joins `connect()` ahead of the probe surface (so the log
  line stops being decisive), or an operator reports diagnosing a broker outage as a bad image.

- [ ] **The readiness sweep cannot see a connector that is up and broken, so nothing notices it
  until a turn does** — [M]. `connectors/health.py::_probe` asks `GET <base>/healthz` and nothing
  else, so a pod answering 200 there and 500 (or an ingress error page) on `/mcp` is reported
  `healthy`: driven 2026-09-19, `/readyz` said `{"status":"ready","connectors_unhealthy":0}` and
  `chemclaw_connectors_unhealthy` held 0 while every call failed. The *turn* now reports it —
  `chemclaw_connectors_unreachable_total{connector}` and `ChemclawConnectorsDegradingTurns` at
  `for: 0m` — so the case is covered wherever there is traffic, which is why this is a row and not a
  fix. **A `tools/list` probe was measured and declined**: against the four connector apps this
  repository serves, on loopback with no TLS, `GET /healthz` is 3.6–4.2 ms and a full MCP
  handshake + `tools/list` + teardown is 50–71 ms — 12–19x — on a route the kubelet runs every 10 s
  with `timeoutSeconds: 5` derived from a 2 s per-endpoint budget; it needs the front door to hold
  every connector's bearer token to *probe* rather than only to *call*; and it mints an MCP session
  per sweep, which the serving side bounds as memory. For that it would move detection from a rule
  that fires on the first degraded turn to a gauge behind `for: 10m`. What is left unbought is
  detection on an **idle** deployment. Trigger to revisit: a deployment reports a connector that was
  broken for longer than its traffic gap — or `connectors_required` is used as a *runtime* gate
  rather than a boot gate, at which point the sweep's verdict has to be as strong as a turn's.
  Guarded by `tests/test_connector_health.py::test_a_connector_healthy_on_healthz_and_broken_on_mcp_is_reported_by_the_turn`,
  which asserts the sweep's `healthy` verdict, so changing this decision turns that test red rather
  than leaving two documents disagreeing.

- [ ] **The turn-wide model-call floor only binds where a loop watch is open, and two paths open
  none** — [S]. `agent/loop_cap._LoopWatch.calls` is what makes a `task` fan-out share one iteration
  allowance (it was `1 + W*(cap - 1)` calls before, measured at 25 against a cap of 4 over 8
  helpers, and 193 against the shipped cap of 25 at width 8). `api/runner.py` opens the watch per
  turn, so the front door is bound. `durable/template_activities.py` opens only
  `begin_context_watch()`, and the CLI opens nothing — so on those two paths the cap still falls
  back to the per-branch channel snapshot and a fan-out still multiplies it. The same gap exists for
  the spend cap's ambient (`set_turn_usage`/`begin_spend_watch`), which those two paths also skip.
  Either open both watches wherever a turn is driven, or move the pair into one `turn_ambient`
  context manager every driver has to enter.

- [ ] **A peer that is genuinely restorable still drains every live session on the deploy that adds
  it** — [M]. `agent/checkpointer._first_party_channels` no longer stamps `UntrackedValue` channels,
  which was the systemic half: a routine per-turn counter refused the next ordinary turn of every
  Postgres-backed session while pre-empting nothing, because no build's checkpoint holds such a
  channel. What is left is the *restorable* case, and `active_agent` is the live instance — a
  session from before it existed is refused on its next turn even though every reader of that
  channel uses `state.get(...)` with a default and the feature ships off. The guard's stated failure
  is "a node indexes that channel and raises a bare `KeyError`", which is a property of how the
  channel is *read*, not of whether the name is new. Deciding whether to narrow the stamp to
  channels that are read without a default — and deriving that rather than asserting it — is an ADR,
  because `D-2026-08-13` chose the name comparison deliberately.

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
      model, so it belongs with the delegation row below rather than ahead of it.

- [ ] **The delegation experiment: run it against a gateway** — [M]
      (issue #359), opened by `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` and
      the gate on Wave 3's roster. **It is one row because it was four**, and four statements of a
      single blocked experiment made the queue read four times more blocked than it is: "the
      delegation A/B has a comparator and no runner", "measure whether delegation pays", "run the
      comparison against a real gateway", and the first step of the helper-provenance row above.

      **What is left is a credential and a gateway URL, and nothing else.** `make live-delegation`
      is the runner: four arms over `data/evals/probes/delegation.yaml`, `MINIMUM_REPEATS` repeats
      per (task, arm), `delegated` observed per repeat off `audit_events` and `billed_tokens` off
      `turn_costs`, one `DelegationReport` per arm against the `no-helper` baseline. It has been
      driven end to end against `cli.mock_llm --catalogue delegation`, which exercised every
      compliance bucket the comparator carries, and such a run **exits non-zero on purpose**: a
      double supplies the *decision* to delegate, so what it proves is the runner.

      **Two of the four arms need the front door started a particular way, and no flag can do it
      from the client side.** A helper's model is `model_routes["helper"]` and a peer roster is
      `agent_peer_roster`; both are read by the process that builds the agent. `helper-routed`
      therefore needs `CHEMCLAW_MODEL_ROUTES='{"helper": "<a smaller model>"}'` and `peer` needs
      `CHEMCLAW_AGENT_PEER_ROSTER` naming another profile. The suite prints what each arm needs and
      reports an arm that could not have complied as `undelegated`, which is the honest
      intention-to-treat reading and not a pass — so a run that forgets either posture produces a
      report saying so rather than a quiet zero.

      **The handoff act itself has never been observed, and that is the one gap that is not a
      credential.** `treatment_tools("handoff", …)` is unit-tested against a name
      `agent/handoff.handoff_tool_name` mints, and no run has yet recorded a real
      `transfer_to_<peer>` row, because a `transfer_to_…` tool is absent from
      `available_tool_names()` — the six name spaces that function documents do not include the
      handoff one — so `cli/mock_llm._validate` refuses a behaviour that calls one and the scripted
      peer arm answers directly instead. Either widen that function (it is also what the skill,
      template and prose validators read, and `agent_peer_roster` ships empty, so the addition is
      inert by default) or drive the peer arm against a gateway with the roster set. The second is
      the gateway run anyway.

      **What the instrument must not be.** The deleted corpus
      (`data/evals/probes/m12/routing.yaml`, removed with the specialist team) measured
      **delegation rate** over fifteen one-tool probes. Rate is a mediator rather than an outcome,
      and a one-tool question gives isolation no mechanism by which it could appear, so that
      instrument could not observe the benefit it was built to detect: its two runs disagree
      sevenfold (2/15 through the front door with connectors and history, 14/15 on the compiled
      agent with neither, one sample per probe) because they measured different systems, and two of
      the fifteen probes span two specialists, so the figure had an unpassable floor before any
      model was involved. What replaces it is outcomes per **task** — a judge verdict on
      `VERDICT_SCORES`' four-point scale, billed tokens from `turn_costs`, wall clock — through one
      harness, on one pinned model, with at least three repeats. The denominator problem disappears
      the moment the unit is a task. **A negative result closes the question as legitimately as a
      positive one** — written down because the retired specialist team was added to be ready and
      stayed off, and a disappointing answer is not a reason to re-open a measurement.

      **The arms measure this instrument's prose rather than the shipped prompt, and that is a
      deliberate trade to state before anybody reads a number off it.** A profile's `instructions:`
      replace the deployment's domain guidance wholesale, so the three arm profiles carry one shared
      body and differ only in their `Delegation:` paragraph
      (`D-2026-09-14-tools-were-never-the-variable` is what happens when they do not). The
      comparison is therefore internally valid and is not a reading of the default agent. Closing
      that gap needs a profile dimension that *appends* to the shipped prose instead of replacing
      it, which is its own decision and is not on this row.

      **The run needs a gateway, and this environment's credential is a state rather than a fact.**
      Nothing in `src/` dials a vendor (`D-2026-09-04-a-gateway-is-the-only-provider`), so `API-KEY`
      is a credential *for* a gateway rather than one this stack can use: probed 2026-09-13 it
      answered 200 against the vendor with no gateway configured, and a gateway probed for
      `make live-ab` answered HTTP 400, "credit balance is too low"; probed again 2026-09-20 the
      variable was empty and no gateway was configured at all. So probe first —
      `printenv 'API-KEY'` plus one cheap call **through a gateway** — and then run the measurement
      in the same session, because tomorrow's state is not evidence about today's. Until the run
      exists, no claim that helpers do or do not pay is evidence about this deployment.
      Anchors: `src/chemclaw/evals/delegation_run.py`, `src/chemclaw/cli/live_probes.py`
      (`--suite delegation`), `data/evals/probes/delegation.yaml`, `data/evals/profiles/`,
      `src/chemclaw/cli/delegation_behaviours.py`, `infra/live/e2e-full-stack/up.sh`.
- [ ] **`tool_result_blobs` ships its retention window at zero, and nobody chose zero** — [S].
      `retention_tool_results_days` defaults to **0** (`core/config/memory.py:125`), which is the
      same value as every other window that means "off", so a deliberate uniformity is
      indistinguishable from an unconsidered default. Decide whether a tool-result blob has a
      retention answer of its own, and if it does not, say so in `durable/retention.py`'s register
      the way `predictions` does rather than by sharing a zero.

      What this row used to carry — *"six tables still say `nothing bounds it`"* — is **false of
      the current code**, re-checked 2026-09-15 before cutting it down: the phrase appears three
      times in `durable/retention.py` and every one of them is a meta-comment *about* the
      historical wording (`:287`, `:292`, `:447`), never a register entry. Each of those six tables
      now states a decision of its own, so the ones this row was opened to collect have been taken.
      **Anchors:** `src/chemclaw/core/config/memory.py`, `src/chemclaw/durable/retention.py`.

---

## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **One sibling bundle that will not import takes the whole allowance bound with it** — [S].
  `tests/test_context_floor.py::_sibling_tool_tokens` passes every name in `SERVED_ELSEWHERE` to
  **one** subprocess and returns `{}` on any non-zero exit, so a single missing dependency in
  `Chemclaw3-mcp`'s venv skips
  `test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound` for **all** of
  them — and `PREFIX_BOUND`, which `core/config/agent.py` derives both compaction defaults from, is
  that allowance plus the ceiling. Observed on 2026-09-16: the sibling's `.venv` lacked `molmass`,
  which its own newest commit had just added to `servers/thermalsafety/pyproject.toml`, and the test
  skipped. The skip is *counted* by `tests/conftest.py::_report_sibling_skips`, so it is honest — but
  it is wider than it needs to be, and the file's own header argues that "a check that quietly
  shrinks is worse than one that says what it did not look at". A per-bundle subprocess would skip
  only the bundle that will not import and name it; the cost is one process spawn per bundle on a
  test that already spawns one. Anchor: `_sibling_tool_tokens` in `tests/test_context_floor.py`.

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
      on a deactivated aryl chloride" currently gets whatever those 40 notes happen to say.

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

- [ ] **Three subsystems want one missing column: who wrote this** — [M]. `src/chemclaw/kg/note.py`'s `Note.created_by` is
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

- [ ] **A routing corpus where the right profile is not inferable from the question's surface**
      — [M]. Seven profiles ship and genuinely narrow (`evidence` reaches zero side-effecting tools,
      `safety` one, `default` all 49); what `D-2026-08-15` deleted is automatic routing between
      them. Re-opening it needs a corpus the retired one did not contain: cases where the profile a
      question *needs* differs from the profile its wording suggests, compared on **answers** rather
      than on which specialist was picked. Until that corpus exists a router is a guess with a
      metric attached, and this row is the corpus rather than the router.

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

#### Everything that is not the agent framework

*Added 2026-09-16 by a dependency audit across all three repositories.* The two blocks above watch
four Python distributions and one protocol. That is the axis this project revises most often, and it
is **not** where the hand-written code was: an audit that read every package for "is this a library's
job" found its results in units, encodings, path matching, tokenizers, substructure search, row
mapping and table rendering — none of which any row above could ever have mentioned.

The standing question is the same one, so the discipline is the same: *does a library already do
this, and better?* What the audit added is the shape of a good answer, because three of its own
proposals came back wrong on contact with the code.

| Adopted | Standing |
| --- | --- |
| `httpx-sse`, `pathspec`, `charset-normalizer`, `pint`, `tiktoken` (prefix only), `bisect`, `networkx.utils.UnionFind`, `psycopg` `class_row`/`executemany`, `rdkit.rdSubstructLibrary`, numpy+scipy clustering | **adopted**, `D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency`. Most were already resolved in `uv.lock` through a *runtime* requirer, so the cost was a declaration line. **Resolved is not the same as in the image**, which this row first got wrong: `deploy/Containerfile` installs `uv sync --frozen --no-dev`, and `pathspec` arrived only through `mypy`, a dev-group tool — so it, like `pint`, is a new install in every shipped image. `uv export --frozen --no-dev` is what answers that, per package, for whatever the lock says today |
| ruff `TID253` as a second layering belt | **declined here, adopted in `Chemclaw3-mcp`**, `D-2026-09-16-a-flat-ban-cannot-express-a-matrix` — this repository's policy is a `(package, stack)` matrix and `TID253` is one rule code over one global list, so `per-file-ignores` cannot narrow it per edge at all; it also sees one of the three import scopes the policy distinguishes |
| deleting the second lexical ranker | **declined on measurement.** The duplication is real and *inverted* — the Postgres leg strictly dominates — but `note_reindex_effective` makes the index conditional on sources the default does not enable, so the survivor is unreachable. `graph` meaning "the leg that reads the index" is a `data_sources` decision, not a retriever edit |
| fifteen libraries — listed with their reasons directly below, because a pointer is not a record | **declined.** This row used to say "each with a measurement, in the audit report and the ADR above", and neither held any of them: the ADR names none, and the audit report was a scratch file the next branch overwrites. A register whose whole purpose is that re-proposing one is a detectable failure cannot rest on a document that does not survive, so the reasons are written out here |

**The fifteen declined, one line each.** Where the audit recorded a measurement it is here; where it
did not, this says so rather than inventing one, because "declined, measurement not recorded" is a
re-proposal a future session can settle in an afternoon and a fabricated number is one it cannot.

| Declined | Would have replaced | Why not |
| --- | --- | --- |
| `tabulate` | the Markdown table emitters now behind `core/markdown.py` | The part worth having in one place is the *honesty rules* `memory/comparison.py` argued for — one spelling of an absent cell so `drop_empty_columns` can see it, escaping the backslash before the pipe, no width padding. A library that renders cells uniformly pushes those back out to every call site |
| `detect-secrets` | `core/logging.py`'s `_STRUCTURAL_SECRETS` vendor-prefix regexes | Scan-shaped: it returns spans rather than redactions, has no equivalent of the `(?P<keep>…)` group that keeps a redacted line saying *which* credential failed, carries no ReDoS bounds of its own, and declares `requests`, which is on the sibling fleet's forbidden-import list. Its *pattern inventory* is still worth reconciling against — that is a live item, not this decline |
| `rank_bm25` | `retrieval/retrievers.py`'s in-process lexical scan | It rebuilds its index per construction, so it pays exactly the cost being complained about — the scan measures 151 ms per call and 836 ms at eight concurrent on a 10k-note corpus. The answer is the GIN-indexed `tsvector` `retrieval/vector_index.py` already maintains, not a second in-process ranker |
| `dimorphite-dl` | ionisable-site perception in `science/calc/logd.py` | The same rules exist three times across two repositories and a *fitted* pKa calibration sits on top of them. A fourth implementation desynchronises the three and invalidates the calibration; the taken fix is transcribing the existing rules to a SMARTS table that reproduces today's partition exactly |
| `yoyo` / `alembic` | `core/migrate.py` plus `infra/sql/` | **Declined, measurement not recorded.** What a re-proposal has to weigh is on record in that module's docstring: whole-file sends through the simple-query protocol so a `DO $$ … $$` block applies intact, `pg_advisory_xact_lock` over the single-transaction run, and a `lock_timeout` that bounds the wait for a table lock without bounding the work |
| `slowapi` | `api/rate_limit.py` | **Declined, measurement not recorded.** |
| `secure` | the browser security headers in `api/middleware.py` | **Declined, measurement not recorded.** The header set and its ordering constraint (SEC-5: stamped by pure ASGI middleware that never buffers, installed so a default 500 still carries them) are that module's, and are what a swap would have to preserve |
| `asgi-correlation-id` | the correlation id `api/middleware.py`'s `_RequestObservability` mints | **Declined, measurement not recorded.** Ours is not a log-decoration id: it is the key the audit trail joins on and the value `core/call_identity.py` sends to the connector fleet |
| `pytest-postgresql`, `testcontainers` | `tests/pg.py`'s connect-check, migration and per-test schema isolation | **Declined, measurement not recorded.** The constraint a swap has to meet is in that module: every table is created in a dedicated schema, never the running system's, and an unreachable server has to become a *counted* skip rather than a failure |
| `respx` | the `httpx.MockTransport` doubles in `tests/test_delivery.py`, `test_live_storm.py`, `test_live_benchmark.py` | **Declined, measurement not recorded.** |
| `ase` / `cclib` | geometry and structure handling in `science/calc/geometry.py` and `structures.py` | **Declined, measurement not recorded.** Note the boundary rather than the library: after `D-2026-08-16-the-physics-leaves-the-cache-stays` what remains here is the cache, the ledger and the wire models — a parser adopted here would be adopted on the wrong side of that line |
| `RestrictedPython`, `pebble` | nothing in this repository | **Declined, measurement not recorded**, and it is a scope decline rather than a library one: there is no code-execution tool here to sandbox. `agent/scratchpad.py` withholds `execute` deliberately and `pyexec` is served out of `Chemclaw3-mcp` |
| `EnsembleRetriever` | `retrieval/hybrid.py`'s Reciprocal Rank Fusion | **Declined, measurement not recorded.** What a swap would have to keep is `hybrid.restated_as_position` overwriting `score` with the merged rank, which `retrieval/evidence.py` and `fanout.py` both read |

**Three findings worth more than the adoptions**, because they generalise:

1. **An adopted API's defaults are part of its surface.** `GetMatches` defaults `useChirality=True`
   where `HasSubstructMatch` defaults it `False` — 514 matching molecules became 0, which reaches a
   chemist as "no precedent exists". Diff the defaults, not the semantics you assume they share.
2. **A number quoted from a row is not the row.** The audit cited a 3.69 mean gold rank against 4.69
   to justify deleting a retrieval leg; that configuration finds **three fewer gold notes**, which is
   what `retrieval_recall` gates on. A better mean over a smaller found-set is not a better retriever.
3. **A cache key is a claim about what changes.** `molecule_fingerprints` has no revision column and
   `_upsert` rewrites in place, so count, max id and max `created_at` are all unchanged when a
   structure string changes. The honest key was a digest of the data already fetched.

---

## The turn-time comparison cannot diff what the ELN gives structured

- [ ] **The turn-time comparison cannot diff what the ELN gives structured** — [M].
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

## A discriminating check can name a note, and cannot name a tool's output

`D-2026-09-20-a-swept-axis-is-a-choice-an-invented-argument-is-a-lie` dispatches a check onto 9 of
the 12 shipped calc jobs and refuses the other three on one ground: `scan_coordinate` needs
`atoms`, `profile_rotation` a `torsion`, `survey_bond_strengths` `cleavages`, and **nothing in the
number those jobs return says which atoms were driven** — a scan over the wrong pair reads exactly
like a scan over the right one.

**The enumerators that would ground them already exist**, which is the part worth writing down
because the ADR's first draft got it wrong. `connectors/chem/connector.yaml` declares
`enumerate_bond_cleavages`, `enumerate_torsions`, `enumerate_tautomers`,
`enumerate_protonation_states`, `enumerate_stereoisomers` and `enumerate_degradants`, served from
`Chemclaw3-mcp`; `BondCleavageSpec` is documented as "one bond to break, **as `chem`'s
`enumerate_bond_cleavages` reports it**". The two halves were built to fit each other and nothing
joins them.

What is missing is the joint. `CheckCall.subjects` carries note ids and nothing else, so a check
cannot say "enumerate this compound's cleavages, then compute their bond strengths": the
enumeration's output is a set of structures that exist in no note. Closing it means **a second
grounded source beside the corpus** — a deterministic tool whose output *is* the candidate set,
under the same rule (the model selects the enumerator and its subject; it writes no member of the
result).

That is also what the chemist's "which molecule will be generated" question needs.
`rank_species` can already rank a candidate set, but only one every member of which is already a
written-down note, so a substitution product nobody has recorded cannot be ranked — and a
substitution-product enumerator is the one shape the six above do not cover, so it is two pieces
rather than one.

Anchors: `hypotheses/models.py::CheckCall`, `hypotheses/dispatch.py::STRUCTURE_FIELDS`,
`connectors/calc/specs.py::BondCleavageSpec`. The file that shows this has fired is
`tests/test_hypothesis_dispatch.py::test_the_dispatchable_job_set_is_exactly_what_is_pinned`, whose
pinned set of 9 would have to grow. Decision:
`D-2026-09-20-a-swept-axis-is-a-choice-an-invented-argument-is-a-lie`.

## Everything else

The long-form findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md),
grouped by the review that found them, with their full measurements. **That file is a record and
not a second queue**, which its own header says and this paragraph used to contradict: it carried
the reviews run between 2026-07-24 and 2026-08-15, and a share of them name a subsystem that no
longer exists — the Microsoft Agent Framework, the HPC/Nextflow/Seqera tier, the PR-gate, the GxP
hash chain, the specialist team. A finding there is **provenance to promote from**, not work
somebody is waiting on: read it for what was measured, on what date, by which pass, and with what
evidence, then write the row here from the tree as it stands today. Nothing there is scheduled and
nothing there is a commitment; its findings are plain bullets rather than checkboxes for that
reason, and there is deliberately no `grep` that counts them — a record's length is not a number
anybody has to act on.

Promotion **restates** a finding rather than moving it, so this queue and that record overlap
rather than partition — the header's "~185 further" was a subtraction nobody could reproduce, and
matching the two by title matched a small minority of what this queue then held.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Temporal broker, real
cluster, a real Databricks workspace — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.

## The same three questions cost 2.1x more on one boot than on another

- [ ] **The same three questions cost 2.1x more on one boot than on another, and a binding race is the leading candidate** — [M].
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

- [ ] **5,760 ORD records — 57% of the seeded corpus — cannot be ingested at all** — [L].
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

## Template step roles cross the durable boundary on an unsigned payload

- [ ] **A template step's roles cross the durable boundary on an unsigned payload** — [L].
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

- [ ] **Two timing bounds red the gate for machine load, and the ADR that documents one of them
      says it passes serially** — [S], found 2026-09-20 when `check` went red on PR #421 with a diff
      containing **zero files under `src/`**.
      `tests/test_conflicts.py::test_a_disjoint_dated_corpus_scans_in_linear_time` took 1.84 s
      against a bare `< 1.5` wall clock, and
      `tests/test_context_budget.py::test_a_burst_of_cold_prefix_measurements_leaves_the_loop_schedulable`
      measured 1.289x against a fixed `/ 1.3` margin — 0.9% short. Both pass 3-of-3 serially on an
      unloaded machine; both failed inside a 46-minute run competing with three subagents.
      **The documentation is the part worth fixing first.**
      `D-2026-09-13-a-stable-failure-set-is-not-two-green-runs` tabulates the second test as failing
      **2 of 5** parallel runs with the serial column reading an unqualified **"passes"**. It passes
      serially *on an unloaded machine*: parallelism was never the mechanism, load is, and `-n 4` is
      one way to produce it. A merged ADR is never edited, so a reader of that table today is told
      the serial gate is safe from this and it is not.
      **The patch.** Both assertions bound a ratio or a wall clock against a constant. Each should
      compare against a control measured in the same process at the same moment — which the second
      test already half does (it has the on-loop control) and then spends on a fixed 1.3x margin that
      load eats. `tasks/lessons.md` already carries the rule this is an instance of: a timing bound
      must separate the two *outcomes*, not the two speeds.
      Anchors: `tests/test_conflicts.py`, `tests/test_context_budget.py`,
      `docs/decisions/D-2026-09-13-a-stable-failure-set-is-not-two-green-runs.md`.

- [ ] **A streamed tool call cut mid-document is completed by upstream and the tool runs on the guess** — [M].
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

## A calibrated calculator names a fleet tool through a caller the seam walker cannot resolve

- [ ] **`_CALIBRATED` puts a `calc` tool name on the wire outside every check that watches the
  seam.** Found on 2026-09-18 while closing a `Chemclaw3-mcp` row whose own stated fix —
  "that repository's `_CALLERS` tuple" — described work `D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names`
  had already done, which is worth knowing before implementing any cross-repository row's
  prescription: re-measure it against the other tree first.
  `connectors/calc/server/tools.py::_CALIBRATED` maps a property name to a fleet tool name
  and `_calibrated` hands it to `remote_version`, which asks the server `calculation_key` for that
  tool. Three things make it invisible to
  `tests/test_sibling_manifest_agreement.py::test_the_calc_seam_calls_only_tools_the_fleet_records_serving`:
  `remote_version` is not in `_DISPATCHERS`, its tool argument is a tuple-unpacked local rather
  than a literal, and the names live in a dict value rather than at a call site. Measured on
  2026-09-18: both names it currently holds — `predict_solubility` and `predict_pka` — are covered
  by other sites, so nothing is unchecked today and a *third* calibrated row would be. The obvious
  fix is not available: teaching `_literal_strings` to resolve a value out of a named module-level
  table would put one module's private data structure inside a generic walker, which is the
  allowlist-of-its-own-exceptions shape `ARCHITECTURE.md` already refuses for the layering rule. So
  the question is whether the table should declare its tool names somewhere the walker already
  reads, or whether `remote_version` should take a literal. Wants a measurement of which calibrated
  calculators are actually planned before either is built.
  Anchors: `src/chemclaw/connectors/calc/server/tools.py::_CALIBRATED`,
  `src/chemclaw/connectors/calc/remote.py::remote_version`,
  `tests/test_sibling_manifest_agreement.py::_DISPATCHERS`,
  `docs/decisions/D-2026-09-18-a-seam-read-in-one-direction-cannot-see-a-surface-grow.md`.
