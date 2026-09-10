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

- [ ] **Nothing bounds the scratchpad memory store** — [M].
  `retention._NOT_PRUNED["store"]` read *"erasure reaches it per actor"* until wave 13, which is a
  disposal route that fires only on a leaver request — the reasoning the `session_owners` entry
  already rejects in its own words ("which a deployment that no one leaves never runs"). The entry
  now says **nothing bounds it**, which is the finding; this row is the decision. `store` is
  agent-writable (`agent/scratchpad.py`) with no size cap, no window and no clock, so a single agent
  looping a `remember` tool is the runaway case — the same shape `ingest/rejections.py` already
  answers with `_MAX_ROWS_PER_SOURCE` and least-recently-used eviction inside the writer's own
  transaction. A clock is likely the wrong instrument here for the reason it is wrong there. Decide
  between a per-actor (or per-namespace) row cap enforced by the writer and an explicit "unbounded,
  accepted" posture; either way the register entry changes in the same commit, and
  `tests/test_retention.py::test_no_disposal_entry_offers_actor_erasure_as_what_bounds_a_table` is
  what stops the next rewording leaning on erasure again.

- [ ] **A connector can claim a step-template launcher name, and the registry says it cannot** —
  [S], found 2026-09-05 reviewing the ambient-name guard. `_bound_by_this_process` refuses a bundle
  that claims an in-process tool, a scratchpad verb, `write_todos` or `task`. Its docstring adds
  that `run_<name>` template launchers are "a different name space that a bundle has no business
  claiming either" — and measured, a bundle declaring `run_bond_strength_survey` is **accepted**:

  ```
  NOT REFUSED: a connector may claim the template launcher 'run_bond_strength_survey'
  ```

  The cause is ordering rather than an oversight in the union. `chemclaw_agent
  ._register_generated_tools` is `[*job_tools(), *template_tools()]`, so `job_tools()` runs the
  collision check while `registered_tools()` still holds no launcher — measured empty at that
  moment. The consequence is the one the whole check exists to prevent, one name space out: the
  bundle's tool wins `tools_by_name` and a chemist asking for a template gets the connector's tool
  under the launcher's name, with no error.
  **Not a one-liner, which is why it is a row.** Closing it means either reading
  `chemclaw.templates.registry` from `connectors/registry` — a new import edge
  `tests/test_layering.py` would have to be told about, in the direction that module has so far
  avoided — or moving the collision check to after both registrations, which changes when a
  misconfiguration is reported. Which registry owns that name space is the decision.
  The false sentence is corrected in this commit; the gap is not. Anchors:
  `connectors/registry.py::_bound_by_this_process`, `agent/chemclaw_agent.py::_register_generated_tools`.

- [ ] **The JWKS fetch follows an ambient proxy and has no seam to stop it** — [M], opened
  2026-09-05 by the review of `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address`.
  `api/auth.py:85` builds a `PyJWKClient`, whose `fetch_data` calls `urllib.request.urlopen` —
  which resolves proxies from the process-global default opener and has no `trust_env`. Measured
  with a recorder standing in as the proxy: it received
  `GET http://login.microsoftonline.com/tenant/discovery/v2.0/keys`. **This is the anchor every
  bearer token is validated against**, so a proxy that could answer it could serve a key set of its
  own choosing. Two things bound the severity and neither closes it: a real tenant endpoint is
  `https`, where a proxy sees a CONNECT tunnel it can only open with a CA the pod already trusts
  (which is exactly what a TLS-terminating corporate proxy arranges); and the boot refusal added by
  that ADR stops a deployment that has *not* declared a proxy **and has `entra_required` on** —
  which is the Helm chart (`values.yaml` sets `CHEMCLAW_ENTRA_REQUIRED: "true"` on every
  component) and is **not** this repository's own defaults. This sentence said "every shipped one"
  and that was measured false on 2026-09-06: on `Settings()` defaults `proxied_destinations`
  charges nothing, so `make chat`, `make connectors`, CI, a hand-started worker and any site behind
  `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` boot with the hole open. For *this* row the identity-off
  half is moot — the JWKS fetch only happens when `entra_required` is on — but the sentence was
  being read as a statement about the boot refusal in general, and as that it is false. The
  asymmetry with the LLM seam stands.
  **Not a one-liner, which is why it is a row.** `PyJWKClient` takes `ssl_context` and no opener,
  so the only in-process fix is
  `urllib.request.install_opener(build_opener(ProxyHandler({})))` at import — a process-wide side
  effect on every library that reaches for `urlopen`, which wants its own decision rather than
  riding along. The alternative is vendoring `fetch_data`, which couples this module to a surface
  it does not otherwise use (`_match_kid` is already written the long way for that reason).
  Anchors: `api/auth.py::_client_for`, `core/netguard.py::refuse_proxied_egress`.

- [ ] **An external vector store's client builds its own httpx and is outside the proxy fix** —
  [S], opened 2026-09-05 by `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address`.
  `retrieval/vectors/qdrant.py:118` constructs `AsyncQdrantClient`, which builds its own httpx
  client internally and takes only `verify` from this repository — so `trust_env` stays at its
  default and a configured proxy would carry that traffic. It is **not** the LLM seam, so no prompt
  or bearer is on it; what is on it is embedded note text and the query vectors. Recorded rather
  than blind-patched for one reason: `qdrant_client` is not in this closure (`pgvector` is the
  shipped provider), so the claim "passing a client works" would be untested prose, which is the
  shape this repository keeps deleting. **The boot refusal covers less than this row said.** It
  fires only where `_env_reading_destinations` charges a destination — the OTLP endpoint and the
  Entra JWKS — so a deployment with `entra_required=false` and `otel_enabled=false`, which is what
  `.env.example` ships, boots with a proxy variable set and nothing charged (measured 2026-09-06:
  HTTP 200 through a real loopback proxy to an external listener, `netguard._refused` 0 before and
  after, the proxy's log showing the absolute-URI request line). The residual is a site that has
  declared a proxy **or** runs identity and tracing off, *and* runs the non-default vector store. Closing it needs the extra installed, then
  one measurement of whether the SDK accepts a caller-supplied client. Anchors:
  `retrieval/vectors/qdrant.py`, `core/http.py::gateway_client_kwargs`.

- [ ] **The egress guard is blind to gRPC and to Temporal, and those are its two highest-value
  destinations** — [M], opened 2026-09-06 by the wave-5 egress review, argued in
  `D-2026-09-06-a-redaction-that-only-covers-logrecords-covers-one-sink.md`. `arm()` patches
  `socket.socket`'s methods and the `socket` module resolvers; grpc's C-core and Temporal's Rust
  sdk-core open sockets through neither. Measured with the allowlist deliberately **empty** and no
  proxy variables set: `grpc.insecure_channel`, the OTLP gRPC span exporter and
  `temporalio.Client.connect` all reached an external listener with `chemclaw_egress_refused_total`
  at 0. `derive_allowed` adds `otel_endpoint` and `temporal_address` all the same, which reads as a
  bound and is not — the docstrings at `core/netguard.py` now say so in both places, which is the
  part that was cheap. **Why it matters beyond the general concession**: with
  `otel_include_sensitive_data` on, that exporter carries prompts and completions, so a wrong or
  hostile `CHEMCLAW_OTEL_ENDPOINT` exports them anywhere while both signals an operator would check
  (`chemclaw_egress_refused_total`, `chemclaw_egress_guard_armed`) report health. **And the two
  blindnesses compound**: measured with an ambient proxy left in place, grpc followed `https_proxy`
  to a *loopback* proxy — invisible to the socket guard because it is a compiled extension, and
  invisible to the NetworkPolicy because a sidecar shares the pod's netns. The module's fallback
  ("those are the NetworkPolicy's job") does not hold for that combination.
  **Not a one-liner, which is why it is a row.** Closing it means an `LD_PRELOAD`/seccomp layer or
  a per-library interception (grpc exposes no socket factory hook; `temporalio` dials in Rust), i.e.
  a decision about what enforces egress rather than an edit to this module. The cheaper half that
  remains open is the chart: `networkPolicy.egressDestinations` does not say it is the only layer
  bounding these two, nor what a loopback sidecar does to that. Anchors:
  `core/netguard.py::arm`, `::derive_allowed`, `deploy/helm/chemclaw/values.yaml` (`networkPolicy`).

- [ ] **Six live-lane httpx clients read the ambient proxy, one of them carrying a bearer** — [S],
  opened 2026-09-06 by the same review. `cli/live_probes.py:340` builds an `Authorization: Bearer`
  client carrying `live_probe_token`, and `cli/live_storm.py` (five sites), `cli/phoenix_publish.py`
  and `evals/live.py` build clients, all at httpx's default `trust_env=True`. None is on a path a
  chemist reaches, which is why it is [S] rather than the finding itself — but
  `core/netguard.py`'s docstring asserted "every first-party HTTP client here passes
  `trust_env=False`" as the correctness argument for charging two destinations instead of twelve,
  and that sentence was false for these six.
  `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` now enforces the
  property with these four modules in a named exemption list, so a *new* client anywhere else fails
  on the day it is written; closing this row is deleting the list, one keyword per site. It is a row
  rather than a patch only because those files belong to another surface than the one that found it.
  Anchors: `cli/live_probes.py`, `cli/live_storm.py`, `cli/phoenix_publish.py`, `evals/live.py`,
  `tests/test_netguard.py::_TRUST_ENV_LANE_EXEMPTIONS`.

- [ ] **The gateway boot guard reaches one process, and the worker is the other one** — [M],
  opened by `D-2026-09-04-a-gateway-is-the-only-provider`. `_refuse_unconfigured_llm_gateway` and
  `_refuse_unauthenticated_exposure` are called only from `api/app.py`, so a background worker
  never runs either — and `durable/template_activities.py` builds a graph inside an activity, so a
  worker pod *does* make model calls to `llm_base_url`. The chart is not affected (verified:
  `helm template` renders `CHEMCLAW_LLM_BASE_URL` into `chemclaw-config`, and 9 Deployments plus 3
  Jobs `envFrom` it), so this bites a non-Helm or partially-overridden deployment, which gets a
  silent loopback dial in the worker where the front door would have refused to boot.
  **Not a one-liner, which is why it is a row.** The guard's signal is `service_host` being
  non-loopback — a property of a *bind*, and a worker does not bind. Extending it means deciding
  what "exposed" means for a process that only makes outbound calls, which is a design question.
  The front-door-only scope is pre-existing (`_refuse_unauthenticated_exposure` has always been
  that way); what is new is that the ADR's argument — "loudly at boot rather than loudly on the
  first turn" — only holds for one of the two process kinds.

- [ ] **A standing plan approval authorizes any state-changing tool, not the plan's steps** — [L],
  from the 2026-08 security review (proven live). `plan_gate.enforce_plan_approval` refuses a
  state-changing call unless an approval exists for the current plan's identity — `plan_identity`,
  a hash of the todo *contents* — but it never compares the *tool being called* to anything in the
  plan. So once a human approves a one-line read-only plan ("look up the melting point of aspirin"),
  every tool in `authz.side_effecting_tools()` executes for the rest of that turn:
  `record_knowledge_note` (a knowledge-graph write / git push), `synthesize_memory`, every durable
  calc/BO launch. Combined with the unframed injection surfaces (connector output, `find_past_jobs`
  `plan_step`, ELN notes) this is the injection amplifier — untrusted text that reaches the model
  during an approved turn reaches the full write surface while the chemist believes they approved a
  lookup. The clean fix is **not** a patch: the plan is prose todos with no per-step tool
  declaration, so binding an approval to "its tools" requires the harness to enumerate the
  side-effecting tools each step will use (a `write_todos`/prompt schema change), capture that set
  on the `plan_approvals` row at approval, and refuse a call whose tool is outside it. Scanning the
  todo prose for tool names was rejected as fragile in both directions (a legitimate plan that does
  not spell the exact registered name would fail to authorize its own tool, making `plan_only`
  unusable — the worst outcome the gate's own docstring names). Until the declaration exists, the
  gate binds plan *content* only. Deliberately left as a feature rather than shipped as a heuristic.

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes the actor as an argument sourced from core's validated front-door
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:160` — the row named a
      field called `actor`, which does not exist), and never reads the header. The real reach is two
      columns on the synchronous MCP path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py::_recorded_provenance` (:374). The `unverified:<id>` marking is in place (D-2026-08-13),
      so what is open is that a caller still chooses the string. A bearer on the row above proves
      *core called*, not *which chemist*, so full closure needs an actor assertion bound to the call
      (OBO or a signed memo) — which is the `DEFERRED.md` warehouse row's blocker too.
      **Narrowed 2026-08-27** (`D-2026-08-27-a-bound-that-multiplies-…`): the claim no longer
      travels back out as provenance — `CampaignThread` dropped `opened_by`, because a reader of a
      resumed campaign cannot tell a marked actor from a verified one. Both columns keep the value
      for the audit trail, where that question can be answered. What stays open is unchanged: the
      string is still the caller's to choose.

- [ ] **`build_langgraph_agent(connectors=...)` accepts a tool that shadows a first-party name** —
      [S], the residual `D-2026-09-04-a-name-is-one-capability-across-every-namespace` names and
      leaves open, and whose `BACKLOG.md` row was never written. `connectors/registry.py`'s
      `_declared_tool_names` refuses a *manifest* claiming `record_knowledge_note`, and that is
      the path a deployment takes; the `connectors` keyword is the one that bypasses it, because
      `agent/langgraph_agent.py`'s `bound = [*(as_structured_tool(fn) for fn in tools),
      *(connectors or [])]` concatenates the two lists with no name check at all. Closed in
      practice and open in the type: the check belongs beside that concatenation, over the names
      the first list already declares.

## 2 — Answers that are wrong without saying so

- [ ] **RRF's premise is independent rankers and this system has correlated ones;
      `retrieval_fusion_k` is not the dial that fixes it** — [M], re-measured 2026-09-05 against
      current `HEAD`, and **both remedies this row used to propose are measured no-ops**. Keep the
      numbers here so nobody re-litigates them.

      The arithmetic stands: at `retrieval_fusion_k` 60 over lists of `retrieval_top_k` 8, the
      within-source spread is **1.11x** (rank 1 = 1/61, rank 8 = 1/68) against **2.00x** for being
      found twice, so a two-source note at rank *r* beats a one-source rank-1 note while `r < 62`.
      End to end on a 35-note corpus with three real legs, the note answering the query sits at
      position 2 in `graph` mode and **position 9 of 9** in `hybrid`.

      **Neither `k` nor `retrieval_source_weights` can close it, and that is arithmetic rather than
      tuning**: the agreement term contains no `k`, so a note found at rank 1 by two legs scores
      `2/(k + 1/w)` against a dense-only rank-1 note's `1/(k+1)` — the first wins for *every*
      positive `k` and `w`. Measured: lowering `k` to 20 or 10 changes the order on **0 of 7** real
      queries; at the minimum `k=1` the answer note reaches position 6, still below every two-source
      note. Tiering `graph`+`lexical` at weight 0.5 leaves it at position 9, inert.

      **The correlation is worse than "two of the three"**: on the real `knowledge/` corpus,
      `graph ∩ lexical` = 47/55, `graph ∩ vector` = 44/55, `lexical ∩ vector` = 41/53 — because the
      shipped `embedding_provider` is `hash`, which is token-count hashing, so *all three* legs are
      term-overlap rankers. The dense leg only becomes orthogonal under `openai_compatible`.

      **What was fixed instead**, because it was a defect rather than a tuning question: the fused
      list carried each chunk's *finder's* score, monotone with the fused order on **0 of 7**
      queries. `hybrid.restated_as_position` now reports the rank the fusion actually produced.

      **What would work is "one corpus, one vote"** — `ingest/documents/retriever.py` already fuses
      its own two legs internally so the share votes once, while the note corpus runs three legs as
      three votes over one corpus. Expressing that means the data-source manifest saying which
      sources are one corpus, which is an ADR rather than a setting. Scope note: with three note
      legs the merge cap never engages (24 chunks against 40), so today the cost is prompt *order*,
      not recall; it becomes recall at five or more legs. And `retrieval_mode` defaults to `graph`
      with `CHEMCLAW_DATA_SOURCES=graph,eln-json`, so **no shipped configuration runs RRF over note
      sources at all** — hybrid staying opt-in is the mitigation until the ADR is taken.

- [ ] **The 44 labelled (query, note) pairs in `knowledge.yaml` are unreadable as data** — [M],
      measured 2026-09-05. `data/evals/probes/knowledge.yaml` has **19 probes naming
      real `knowledge/` note ids inside their `direction:` prose, 44 pairs in total** — a labelled
      gold set against the *product* corpus that no gate can read, because `Probe` is
      `extra="forbid"` (`evals/probe.py`). `DEFERRED.md`'s claim that "the shipped graph has none"
      was corrected in the same commit as this row.

      **Score it in the live lane, not offline, and that is the finding.** Measured offline by
      running `GraphRetriever` on each probe's raw question: mean recall **0.636**, two probes at
      0.00 — below the gate's floor on day one, because probe questions are conversational chemist
      prose (10-27 terms) while the live agent reformulates before calling `gather_evidence`.
      Gating that offline would restate the `retrieval-cross-coupling-literal-miss` case 19 times
      without the `expect_pass: false` that makes it honest.

      The shape: add `expects_notes: list[str]` to `Probe`, transcribe the 44 pairs, and score it in
      `evals/live.py` beside `expects_tools` — `returned_ids` is already accumulated there, so it is
      the same three lines as `live.py:499-500`. Plus a cheap **offline** validator that every
      `expects_notes` id exists in `knowledge/`, which is the half CI can run. Its own PR: it needs
      a running front door to verify green.
- [ ] **Knowledge writes serialise cluster-wide on one advisory lock** — [M], re-measured
      2026-09-07 against real bare remotes (best of 3): **141.5 ms** per write at 100 notes,
      152.2 ms at 1,000, **252.0 ms at 10,000**. The O(corpus) half of this row is closed and its
      figures are deleted rather than corrected: the 2,916 ms it quoted was `git worktree add -B`
      inside `git_submitter.py`, and `D-2026-09-05-the-gate-is-deleted-not-dormant` deleted that
      module with the other 2,232 lines of the gate — `kg/git_writer.py` commits to the base branch
      and no worktree is created anywhere in `src/`. What survives is the serialisation:
      `git_writer.py:562-567` takes `_WRITE_LOCK` and then a Postgres advisory lock keyed on the
      remote, so every pod's every note write queues behind every other one. At 252 ms that is a
      ceiling near **14,000 writes/hour** for the whole fleet, which is not pressing — the row
      exists because the ceiling is fleet-wide rather than per-pod, so it does not improve by
      adding pods, and because `_cluster_lock`'s own docstring points here for the case it does
      *not* cover (several writer pods with a memory session store take no lock at all).

- [ ] **The fingerprint index is keyed by source and the citation is not, so two sources collapse
      to one note id** — [M], and it is the half `D-2026-08-27-a-fingerprint-is-keyed-by-its-source`
      deliberately left. Migration 063 made the write side `(source, id)`, which is what stops one
      site's chemistry being overwritten by another's. The read side still spells the bare form:
      `retrieval/retrievers.py` and `connectors/rxnfp/server/tools.py` both call
      `note_id_for_reaction(match.id)`, so a two-source deployment now returns **two hits that cite
      one id**, and `records._one_of` raises `AmbiguousReactionRecord` when a reader expands it.
      Better than silently citing the wrong run, which is what 063 fixed, and still not an answer.
      The qualified form and its separator were written and then deleted rather than left as a dead
      parameter no caller passed
      (`D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry`), so this starts from the six
      readers rather than from the spelling: they move together or the id means two things at once.
      Not urgent while one ELN is enabled anywhere; the ambiguity is loud when it happens, which is
      the one improvement 063 already bought.

- [ ] **A retracted ELN entry stays current evidence, and closing it is a five-part change** — [M].
      A withdrawn entry that simply disappears from an export is invisible to a cursor-based sync,
      so the run it produced keeps answering as current. This was built and then deleted on review
      (`D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry`), and the deletion is what
      makes the real cost visible — the sweep was the easy part. Whoever rebuilds it needs all five:
      (a) a producer — an *explicit* tombstone field, never absence, because an ELN fetch is a delta
      and "not seen this run" is the normal state of every entry ever ingested; (b)
      `durable/eln_sync.py::_BoundedIngest` must expose a public `inner`, or the capability walk
      stops at the production wrapper and the sweep silently cannot fire; (c) the unfiltered path in
      `retrieval/retrievers.py::FingerprintReactionRetriever.retrieve`, which consults the record
      store only when a filter is given — the ordinary `gather_evidence` sweep is unfiltered; (d)
      `connectors/rxnfp/server/tools.py::similar_reactions`, which never asks the store at all; and
      (e) `expand_note`, so a reader sees the withdrawal rather than a normal-looking record.
      Measured with (a) and (b) in place and the rest absent: `is_current` False, `eligible()` empty,
      and the retracted reaction still returned by the unfiltered sweep. Migration 066's column is
      reserved for this and 068 says so; `tests/test_eln.py` fails a re-add that does not bring the
      readers.

## 3 — Work that is lost, dropped or invisible

- [ ] **The two eval gates score literals written in their own case files** — [M], same review.
      11 of 13 baseline metrics are read from the case file rather than computed, so a metric that
      stops measuring and answers "perfect" passes both `make eval-strict` and
      `make eval-baseline-check`. These run in `make ci`, so this is a gate that cannot fail in the
      way it exists to fail.

- [ ] **`make kg-validate`'s two store-backed arms have no input in the shipped corpus** — [S], same
      review. 0 reaction citations and 0 `calc_refs` in the committed knowledge corpus, so the half
      of the validator its own docstring says CI runs is dead on every CI run.

- [ ] **The `note_proposed` SSE event is not a proposal, and the name is a two-repo contract** —
      [S], found 2026-09-05 in the gate-deletion review. Nothing reviews a note, so the accurate
      name is `note_recorded`; the literal is switched on by `Chemclaw3_ui`
      (`state/types.ts`, `chatStore.ts`, `TracePanel.tsx`, `chem/entities.ts`, `turnActivity.ts`)
      and by `evals/live.py`, so renaming it is a coordinated deploy with a skew window in which
      one side drops the event silently. The internal names and the text a chemist reads are
      already fixed; only the wire literal is left. Needs a rollout order (accept both, then emit
      the new one, then drop the old), which is why it is a row rather than part of that fix.

- [ ] **The detached settle of a cancelled `AwaitAnswerWorkflow` is racy** — [M], found 2026-09-04
      while fixing the stranded-row HIGH. `ParentClosePolicy.REQUEST_CANCEL` is strictly better than
      the alternatives (all three measured against a live broker), but the settle is scheduled from
      an already-cancelling workflow and landed on some runs and not others under a 15 s grace, so
      "the row always leaves the inbox" is not guaranteed. `asyncio.shield`, or a `due_at` reaper.
      `durable/awaiting.py`.

- [ ] **A legitimate re-ask of an answered question now fails loudly rather than waiting blind** —
      [M], the deliberate half-fix in `durable/pending_store.py`. Making it work needs `_OPEN`'s
      `WHERE` to accept `'answered'` **plus** an archive so attribution is not blanked: a migration
      keyed `(request_id, run_id)`, its `infra/sql/README.md` row, an INSERT grant, and a disposal
      decision in `durable/retention.py`.


- [ ] **A timed-out parse still runs to completion on the worker thread** — [L]. **The cheap half
      is closed**: `ingest/documents/sync.py::_parse_changed` now bounds its `asyncio.to_thread`
      with the front door's own `attachment_parse_timeout_seconds` and counts the outcome as
      `skipped_timeout` through every rendering a run is read through. What remains is the half
      that was always [L]: `agent/attachments.py:284` shields the future deliberately, so on both
      paths the timeout frees the caller and the slot and never the thread — no parser behind
      `parse_document` offers an interruption hook, so a hostile document still burns a worker to
      completion in the background. The only real fix is a killable subprocess, with pickling and a
      new child-OOM failure mode to classify (~150-250 lines).

- [ ] **Nothing checks the client half of a wire contract, and it has drifted twice** — [L], the
      row `D-2026-09-04-a-contract-has-two-halves-and-a-server-test-sees-one` says it is queuing
      and which was never written. `tests/fixtures/turn_events_contract.json` pins what this
      repository *sends*; nothing pins what a client accepts, so the `at_capacity` error code and
      `PendingPlansResponse.truncated` both shipped here and reached `Chemclaw3_ui` as an
      unhandled default — the second without anyone recording that it had not. The hand-written
      case in `tests/test_protocol_routes.py` is the only cross-repo assertion in the tree, and
      that ADR says plainly it does not scale to four repos. What it needs is one artefact both
      sides read: a published fixture, a generated types package, or a job in `make ci` that
      fetches the client's own declaration and diffs it against the fixture.

- [ ] **The awaiting collapse keeps the oldest frame of each state, not the newest** — [S], found
      2026-09-05 measuring `D-2026-09-05-a-push-nobody-claims-is-not-a-push`'s own collapse. That
      change is right — fifteen `waiting` frames for a closed question is the defect it was written
      for — but `awaiting_reported` suppresses a repeat of a state *already reported*, and the rows
      arrive oldest-first, so the frame that survives is the first of each run. Replayed against
      the backlog its ADR measured (one open, fourteen chases, an expiry) it emits
      `waiting reminders=0` then `expired`, never the `reminders=14` that was true at connect; the
      rows are consumed on that first claim, so the count never corrects on this channel.
      `GET /pending` still answers it. Emitting the newest needs a batch boundary
      `agent/session_events.stream_new_events` does not expose — it yields row by row — so the fix
      is either a batched yield or a one-frame hold flushed per poll, and neither belongs in a
      passing edit. Anchors: `api/routes/streams.py`, `agent/session_events.py`.

- [ ] **`JsonCommitmentExport` cannot run a destructive sweep, and the grant for one already
      exists** — [S], the row `ingest/commitments/json_export.py`'s `snapshot` attribute says is
      queued and which was never written. It is hard-coded `False`, so a commitment withdrawn at
      the site is never withdrawn here; the `DELETE` privilege was granted ahead of it
      deliberately, so the enabling half is the only part unbuilt. Not a flag flip: `snapshot`
      licenses deleting every commitment a pass did not see, so it is the operator's assertion
      that the export directory was complete — which makes it a `datasource.yaml` key defaulting
      to false, not a class attribute.

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

- [ ] **A result sink on the primary server opens connections no budget counts** — [S], found
      2026-09-05 beside the fleet-budget review. `publish/drivers/postgres.py` holds an un-pooled,
      unregistered connection, so it is invisible to both `pg_fleet_pools` and
      `chemclaw_pg_pool_max_size`. Harmless while a site points `CHEMCLAW_RESULT_SINKS` at a
      database of its own, and a silent under-count of exactly the kind this budget exists to
      prevent when it points at `postgres_dsn`'s server. Either register it the way
      `agent/checkpointer.py` registers its foreign pool, or state in `values.yaml` that a sink's
      connections are the operator's to add. Anchors: `publish/drivers/postgres.py`,
      `core/db.py::_FOREIGN_POOLS`.

- [ ] **A nested `asyncio.run` inside a pooled process can hang on loop teardown** — [M], found
      2026-09-05 by a fresh-context review of `core/db`. `_forget_pools_of_ended_loops`
      institutionalises abandoning a nested loop's pool and reclaiming it later, and nothing closes
      it *before* that loop ends — so `asyncio.run`'s teardown cancels the pool's background fill
      and then gathers it, while `psycopg_pool` treats `CancelledError` as a client exception,
      logs an empty `error connecting in 'pool-N': ` and **reschedules the retry**. The gather never
      completes. Measured, 15 runs an arm: abandoned pool at `pg_pool_min_size=2` hangs 7/15 and at
      8 hangs 15/15; closing it first or leaving `_POOLING` off hangs 0/15.
      `tests/test_db_pool.py::test_a_pool_whose_loop_has_ended_is_neither_counted_nor_left_holding_backends`
      covers this path by name and opens by pinning `min_size = max_size = 1` — the one value at
      which no second fill can be in flight; the same body at the shipped defaults hangs 8 of 12.
      Reachable only through `durable/eval_drift` -> `evals/retrieval._run_sync`, which is
      `eval_drift_enabled=False` by default and did not hang in 60 end-to-end runs, so this is a
      mechanism with no structural guard rather than a live outage. The fix is to close a pool
      before its loop ends rather than after. Anchors: `core/db.py::_forget_pools_of_ended_loops`,
      `evals/retrieval.py::_run_sync`.

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

- [ ] **Three pool gauges read three different instants of one scrape** — [S], found 2026-09-05.
      `bind_pool_metrics` binds `pool_size`, `pool_available` and `requests_waiting` as three
      lambdas that each call `pool_stats()`, so one `/metrics` scrape recomputes the walk three
      times and publishes a triple that never existed together. Harmless for a trend, wrong for the
      one question these gauges are read for together — is the pool full *and* are callers waiting
      — which is exactly the saturation reading D-119 introduced them for. A single cached snapshot
      per scrape, or one gauge family. Anchor: `core/db.py::bind_pool_metrics`.

- [ ] **`BoCampaignWorkflow` runs four sequential activities under a ceiling that funds one** —
      [M], measured 2026-09-06. `connector_queue_wait_timeout`'s "fits by construction" argument is
      a bound on **one** `q + w`: at the shipped numbers 10,170 + 300 = 10,470 s against a 25,200 s
      `connector_job_timeout_seconds`. But the BO child runs `propose_initial`, `_evaluate(seed)`,
      `propose_next`, `_evaluate` and `record_round` — at minimum four before a one-round campaign
      can finish, so 4 × 10,470 = 41,880 s over the ceiling, reachable on four waits of ~6,300 s
      each, well inside what the bound permits as normal. The docstring's own justification for one
      generous wait per queue — "what has to fit is the worst composite on it" — is the sentence
      this falsifies: the worst composite on `connector-bo` is `N × (q + w)`. The elegant fix is
      `continue_as_new` per round rather than at the rounds bound (`_carry_on` already carries
      exactly the state a round boundary needs), so the execution ceiling bounds a *round*; dividing
      the headroom by a declared per-child activity count is the fragile alternative. It lives in
      `connectors/bo/workflows.py`, so it is that bundle's change rather than core's. Anchors:
      `durable/publish.py::connector_queue_wait_timeout`, `connectors/bo/workflows.py`.

## 4 — Operating it

- [ ] **Nothing bounds what a helper writes into its caller's checkpointed state** — [M], opened
      by `D-2026-09-04-a-helpers-file-crosses-back-and-stays` while closing the *reading* half.
      A helper's `files` cross into the caller's state, and that crossing is deliberate — but the
      report beside it is bounded at `agent_max_tool_result_chars` (60,000) and the state write is
      bounded by nothing. Measured: a helper writing 2,000,000 characters lands **2,000,137** in
      the caller's `files`, which is a *checkpointed* channel, so it reaches a `checkpoint_blobs`
      row and every later turn of that session.
      **Read the scope before reaching for a cap.** This is not a context blow-out: nothing loads
      the whole channel into a prompt, and a `read_file` result crosses `bound_tool_results` like
      any other, so the model only ever pays for what it asks for. What it costs is checkpoint
      weight and retention. It is also **not a bound on delegation** — the caller's own
      `write_file` reaches it identically, so a cap belongs on the scratchpad rather than on the
      helper, and `agent/scratchpad.py` is where the permission set that would carry one already
      lives. `durable/retention.py` prunes checkpoints by thread, so the row is about the size of
      what accumulates between prunes rather than about an unbounded leak.

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
      — [M], opened by `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`. The corpus
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
      Nothing here needs new code; it needs a corpus and a run. Until it exists, no claim that
      helpers do or do not pay is evidence about this deployment.

- [ ] **A helper reaches no connector, and only the behavioural half of this row is still open**
      — [L], and it is gated on the row above rather than on an argument. The prose half is
      **done**: `D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock`
      corrected the three places that gave the bound as a concurrency measurement — two concurrent
      turns over one MCP tool object deadlock — which is real (D-110) but is about **sharing one
      session object**, so it never reached the question of a helper holding sessions *of its own*.
      **The constraint that binds is the lifecycle.** Connectors are opened by the *caller* — the runner, the CLI, the template activity — into an
      `AsyncExitStack` **before** the graph is compiled, and `build_langgraph_agent` is synchronous
      and receives them already open. The roster is fixed per compiled graph
      (`SubAgentMiddleware._subagents` is set once, `subagent_names` is a frozen snapshot), so a
      helper cannot open sessions at spawn time. Giving it its own set therefore means opening a
      second full set **eagerly, on every turn**, whether or not a helper is ever spawned: double
      the sockets, handshakes and server-side session state, against an unmeasured spawn rate, on a
      path whose tail already cost six sequential connect timeouts when a fleet went dark.
      **What would reopen it**: the row above showing that delegation pays *and* that the reading
      helpers do is connector-bound. Even then the cheap form is not a second eager session set but
      a lazily compiled roster entry — a change to a shape upstream owns, which belongs in
      `tests/test_upstream_surface.py`'s count before anything relies on it.

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

- [ ] **`delete_session` and the owner prune take two rows in opposite orders** — [S], not
      reproduced. `_session_delete_statements` deletes `session_turns` then `session_owners`;
      `retention._DELETE_SESSIONS` takes `session_owners` then `session_turns`. The window is narrow
      — the route claims the live lease first and the prune re-checks it inside the DELETE — but a
      retention statement holding the owner row microseconds before the route's claim lands can
      deadlock, and Postgres aborts one side.

      **"Order the two consistently" was examined on 2026-08-28 and is not available**, which is
      what this row now records instead of an instruction that cannot be followed. Each order is
      required by its own invariant — but only one of the two paths is *forced*, and a first
      telling of this correction claimed both were. **Erasure** must remove session-scoped rows
      before `session_owners`, because its statements re-resolve through a subquery over that table
      every time (measured by reordering `_ERASE`: `session_turns` keeps a row). **The
      single-session delete is not forced** — `_SESSION_DELETE`'s predicates are
      `session_id = %(session_id)s` lookups, and reversing it strands nothing (measured). It shares
      erasure's order because `_session_delete_statements` *derives* it, which is a coupling worth
      keeping rather than an invariant of that path.
      `_DELETE_SESSIONS` must take the ownership row *first*, because the lease deletion reads that
      DELETE's `RETURNING` — which is what makes "a lease goes only if its ownership row went" true
      rather than intended; deleting leases first would collect the lease of a live turn whose
      ownership row the re-check then spares. Reversing either side trades a deadlock window for a
      correctness bug, and the deadlock is one statement wide, self-healing on the retention side
      (a Temporal activity retries) and has not been reproduced. **Keep both orders; the row stays
      open only as the record that the obvious fix was tried and rejected.**

- [ ] **Settle `pytest-xdist` on a real runner** — [S].
      The `check` job is 87% one step: `make lint type cov` was **12m06s of a 13m56s job** on
      `d8c312a`, of which lint is 1s and type 68s (measured), so ~11 min is the suite itself.
      `D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one` took the free
      half — lint and type now run in parallel in `static` — and deliberately left this one open,
      because the evidence for it is a *reading* rather than a measurement.
      **What the reading says**: the suite looks parallel-safe already. `tests/pg.py` suffixes its
      `TEST_SCHEMA` with a fresh `uuid4` at import time (it was `os.getpid()` until 2026-09-04, and
      this row went on naming the pid for a day after the commit that removed it), so an xdist
      worker — its own process, re-importing the module — draws its own Postgres schema with no
      change at all, and the two files that use Temporal go through
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

- [ ] **A memory run reads every source whole, three times** — [M]. `durable/memory_jobs.py::
      read_corpus` walks each active ingest source from `datetime.min` on every ingest half, so
      each of the three memory jobs (`build_campaign_notes_activity`, `build_playbook_notes_
      activity`, `build_optimization_notes_activity`) reads the whole record once per activity per
      scheduled run.
      **This row used to say the walk was a full table scan and that was false for the one shipped
      source that pages** (`D-2026-09-07-a-corpus-read-that-stops-at-one-page-is-not-a-corpus`):
      measured against the warehouse adapter over a 12-row corpus at `fetch_limit: 5`, `read_corpus`
      returned **5 of 12** reactions and called the read `complete` — the oldest 500 entries of an ELN
      at the shipped binding default, distilled into notes as what the deployment knows. That is
      fixed: the read pages, and a source still reporting rows waiting makes the read incomplete.
      The cost is what is left, and it is now real rather than hypothetical — the scan happens
      because the read became correct. `ElnAdapter` (`ingest/eln/adapter.py`) has exactly two
      methods and neither is a fetch-by-id, so there is still no cheaper read to reach for: closing
      this means either a fetch-by-id on the adapter protocol (every source pays) or a derived
      store of mapped `OrdReaction`s.
      **Found while building the protocol condenser and deliberately not fixed there**
      (`D-2026-08-25-the-structure-is-discarded-at-the-note-boundary` records the reasoning): a
      derived store would have answered it as a side effect, and answering a scaling problem as a
      side effect of a retrieval change is how a store nobody decided on gets built. It is also the
      trigger on the `DEFERRED.md` row for reagent/solvent set diffs in the turn-time comparison —
      one change answers both.

- [ ] **A published calculation names no reaction, note or compound context** — [M]. `grep -n
      "reaction_id\|note_id\|citation" src/chemclaw/publish/` returns nothing:
      `schema/result-store/001_core.sql` models a `subject` of kind `reaction` and
      `subject_member` rows with roles, and neither carries the id of the `reaction_records` or
      `reaction_labels` row the calculation was about. So a result computed for the product of ELN
      entry `EXP-1001` cannot be joined back to the run that motivated it, in either direction. The
      two stores are also separate databases (`sink.yaml` targets `chemclaw-results`;
      `corpus_molecules.id` is a bare standardized SMILES against `compound.canonical_smiles`), so
      the join has to be designed rather than discovered. **Needs an ADR.** Deliberately not taken
      while the row below is open: `D-2026-08-26-a-route-is-not-a-shape` records the composite half
      of that path being inert for a release with no test noticing, because every test started at a
      projector rather than at a hook — deciding a cross-reference against a store nobody has run
      repeats exactly that. **Trigger:** the results store gets a live target.

- [ ] **Structure identity is canonical SMILES and nothing else** — [M]. No InChI, InChIKey,
      formula, molecular weight, CAS or external registry number exists anywhere in `infra/sql/` or
      `schema/`; `051_reaction_labels.sql:72` states the omission as a decision ("nothing asks, and
      this tree deletes dead columns") and it was right when written. What now asks is a
      cross-system join — an identifier a site's other systems can match on, and one that survives a
      `STANDARDIZATION_VERSION` bump, which a `standard_smiles` string by construction does not.
      **Needs an ADR, and the honest form of it is "name the reader", not "add a column"**: an
      InChIKey nothing queries is precisely the dead column that comment refuses. Candidate readers
      to argue in it: `schema/result-store/001_core.sql`'s `compound` row, and a lookup that stays
      valid across a re-standardization. Note the ordering constraint with the solvate row in §2 —
      any identifier minted before that fix inherits the collapse.

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
- [ ] **Five tables still say "nothing bounds it"** — [M].
      `durable/retention.py`'s `_NOT_PRUNED` is the register that makes this visible, and it is
      doing its job: it names every table in the schema and does not invent an answer where none was
      taken. Eight entries carried that wording — five of them also saying *no decision is on
      record*, which an earlier version of this row quoted as though it were the same set; the
      2026-08-28 erasure pass closed three of the eight
      (`note_proposals`, `plan_approvals`, `turn_costs` — all three are kept through a data-subject
      erasure, so the decision *was* on record one module over, and a derived test now couples the
      two registers). The remaining five are `molecule_fingerprints`, `reaction_fingerprints`,
      `user_preferences`, `predictions` and `measurements` — and `user_preferences` is the weakest
      of the five, because `leaver._ERASE` already deletes it per actor, so what is open there is a
      clock rather than a policy. Plus a sixth question of a different
      kind: `tool_result_blobs` has a window and it ships at 0 "as a deliberate uniformity rather
      than a considered policy for this table", which `retention.py` itself flags as the
      highest-volume table in the set.
      Each needs its own answer rather than one sweep — a fingerprint is derived and rebuildable but
      expensive to rebuild, a preference is the person's and goes on erasure rather than on a clock,
      and `predictions`/`measurements` are the calibration ledger nothing has yet filled. What is
      owed is five decisions, not five `DELETE`s, and the register is where each belongs.

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

- [ ] **The `stated`-quote ambient reads the whole table's tail on every turn once a database has
      other sessions in it** — [M], found reviewing `agent/session_store._SELECT_RECENT_USER_ROWS`,
      the read `api/runner._turn_ambient` runs once per turn on the answer path. The statement is
      `WHERE session_id = %s AND message_shape = %s AND message_original IS NULL AND
      message->>'type' = 'human' ORDER BY id DESC LIMIT %s`, and the comment above it says
      `(session_id, id)` (`infra/sql/008_sessions.sql`) serves the scan. In a table with one session
      in it, it does. In a busy one it does not: Postgres has no statistics for the *expression*
      `message->>'type'`, so it mis-estimates that predicate's selectivity, sees `ORDER BY id DESC
      LIMIT 20` and walks the primary key backwards expecting to stop early. Measured on a replica
      of the table with its real indexes — one 12,000-row session plus 120,000 newer rows from 300
      other sessions, `VACUUM ANALYZE`, warm cache, 4 reps — the planner chose `Index Scan Backward
      using session_messages_pkey` and discarded **119,740** table rows to return 20, on **every turn**,
      growing with the whole table rather than with the session. The same statement forced onto
      `session_messages_session_idx` visits **60** of them (20 kept, 40 removed), because `(session_id,
      id)` *is* `session_id = %s ORDER BY id DESC` and carries the sort for free. Two independent
      measurements agreed on the row counts and disagreed on the milliseconds by 50x, which is why
      this row states counts: the wall clock is machine- and payload-dependent and the plan flip is
      not. **What the fix is, is the decision, and this row deliberately proposes none.**
      `CREATE STATISTICS` on the expression, a partial or expression index that makes the human rows
      directly addressable, and hoisting the type test out of SQL are three different bets about a
      table nobody has measured in production — and an index added to force a plan is a cost every
      write pays forever. Note first that the degradation is invisible (the answer is correct, only
      slow) and that it is bounded by `durable/retention.py`, so a deployment that prunes hard may
      never reach it. Anchors: `agent/session_store.py::_SELECT_RECENT_USER_ROWS`,
      `api/runner.py::_turn_ambient`, `infra/sql/008_sessions.sql`.

- [ ] **The checkpointer's write volume is quadratic in a thread's length** — [L], stated by
      `D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record` under "what this does not fix"
      and queued here because nothing else records it. Upstream's `_dump_blobs` rewrites the whole
      `messages` channel on every superstep, so a 139.6 kB conversation cost **16.7 MB of WAL**, and
      the per-thread prune that ADR shipped does not reach it — measured, the prune *adds* ~4%
      (3.51 → 3.64 MB over 20 turns, reproduced twice). The only mechanism that would is a
      destructive trim of thread state, which contradicts
      `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` — so this is a decision about
      that trade, not a patch. Anchors: `agent/checkpointer.py::_PRUNE_SUPERSEDED`,
      `core/config/memory.py::checkpoint_retain_per_thread`.

- [ ] **The checkpoint sweep and a live turn are two writers, and only the read side notices** —
      [M], stated by `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers`. `aput` writes blobs
      first and the `checkpoints` row second, so a turn whose blobs land before `_DELETE_ORPHANED`'s
      snapshot and whose row lands after it loses them; the guard that ADR shipped is on the
      **read**, which detects the loss rather than preventing it. Nothing synchronises the two
      parties without a lock on the turn-serving write path, and taking one there is the decision
      this row is for.


## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **`_quote_supports` cannot tell whether the figure a quote carries is about *this* slot** —
      [S], and it is the honest limit of a rule that is otherwise doing its job. A `stated` slot
      attests a value, and the check now relates value to quote for every quote: the value's figures
      have to be the quote's, compared as numbers; a figure written in words satisfies that; a value
      carrying no figures needs the quote's own tokens. That refuses every fabrication measured so
      far, on quotes of any length.

      What it cannot do is *attribution*. A chemist who wrote "24 wells" has stated a figure, and
      nothing in the string says whether that 24 is the plate format, the run cap or a coincidence —
      so `max_runs='24'` quoting "24 wells" passes, and it should not.
      **The exposure grew on 2026-09-04** and the rule did not change:
      `D-2026-09-04-a-quote-is-evidence-about-a-person-not-about-a-turn` widened the haystack from
      this turn's message to the thread's user turns, so there is more of the chemist's own text for
      a figure to coincidentally match. That strengthens the case for the count this row already
      asks for rather than altering what it asks. Closing that needs the slot's
      identity to be part of the judgment, which means either a per-slot unit vocabulary (a *well*
      is not a *run*, an *hour* is not a *gram*) or asking the model to point at the span and
      checking the *span's* neighbourhood rather than its digits.

      **Deliberately not built yet**, because the first form is a table of units that will be wrong
      for the first ask nobody anticipated and the second is a second model call inside a check that
      currently costs a regex. What is owed first is a count: over real turns, how often a `stated`
      slot's quote carries a figure that belongs to a different slot. The anchor when it does:
      `agent/protocol_design_tools.py::_quote_supports` and its test file's case table.

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

- [ ] **A tool schema is 38% developer rationale, and it ships on every turn** — [M], and it is
      what `§ 5`'s deferral row turned into once measured. `science/bo/problem.py`'s nested models
      carry design arguments in their class docstrings — *"One `objectives` field rather than a lead
      objective plus a sidecar list (W3)"* — and Pydantic turns a class docstring into the schema
      `description`, so `convert_to_openai_tool` ships them. Measured 2026-08-25 on the `default`
      profile: `start_optimization_campaign` is 8,063 chars of schema, 4,392 of it description and
      **3,047 of that elaboration past the first paragraph**; `record_knowledge_note` 4,259/2,262/663.
      Those two are 25% of the profile's 12,536-token tool budget between them, and both are already
      in `tests/test_context_floor.py::KNOWN_OVERSIZED`.

      **Not a blanket cut.** Some elaboration is genuinely the caller's — when to supply categorical
      descriptors changes what the model should send — so this is per-paragraph judgment: rationale
      moves to a `#` comment, guidance stays in the docstring. **And it does not ship until the live
      lane can show every probe still reaching its tool**, because a cheaper prompt that stops
      finding tools is a regression with a good-looking metric. **The before-figure now exists**:
      `make live-ab`'s 2026-09-04 run reached the expected tool on **133 of the 171** probes that
      name one, per-probe in `tasks/live-test/transcripts/ab/evidence.json`, so the comparison this
      was blocked on is a re-run rather than a new instrument.

- [ ] **Half the probe corpus tests one tool** — [S], and only the *concentration* half is still
      open. `gather_evidence` is in `expects_tools` for **125 of 292** probes (re-counted
      2026-09-05 — the numerator is unchanged and the **denominator was stale**, 292 top-level
      probes today rather than 288, so the concentration is 43%; 124/261 on 2026-08-27, 116/232 on
      2026-08-25, and the corpus keeps growing while the concentration does not shrink with it);
      `find_notes` 96; `expand_note` 60; bucket C is **48** probes against bucket A's **173** — 169
      was this row's own figure and is the *paired* count from the A/B run below, four short of the
      corpus, which is a different quantity wearing the same sentence. The tail is thin. So the
      corpus still mostly measures one retrieval path, and widening it is what remains here.

      **The second consequence is closed and it was the one blocked on a credential.**
      `D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` builds the arm
      (`make live-ab`, a control profile with `tool_names: []`) and runs it over all 221 bucket-A
      and bucket-C probes: ChemToolAgent's finding reproduces — on bucket A tools **helped 31% and
      hurt 23%**, with 19 questions the toolless model correctly declined turned into fabricated
      ones — and bucket C came out the *other* way, falsifying the hypothesis it was built on. The
      record is `docs/archive/tool-utility-2026-09-04.md`. What that run is not evidence about is a
      deployment's own model: it measured `claude-haiku-4-5-20251001`, and re-running on a site's
      model is one command.

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

## A note write costs ~1.8 s, and a backfill is one write per record

Measured over the ORD backfill: 103 records per 3.1 minutes, steady, with the cost in the
commit-and-push cycle rather than in mapping (the whole 10,011-record corpus maps in 0.3 s). That is
a little over two hours for the mock's 4,251 ingestible records. A real deployment's first sync is a
decade of records, where this is days.

**Half of this closed itself and half did not.**
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted the branch per note, so the
"4,251 branches in a repository nobody can list" half is gone. What remains is the serialized
commit-and-push, which is the same 1.8 s: a backfill and an incremental sync still want different
write shapes (one commit per batch for the first, one per note for the second). Found by the
2026-08-18 corpus-fidelity pass, re-scoped 2026-09-05.

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
