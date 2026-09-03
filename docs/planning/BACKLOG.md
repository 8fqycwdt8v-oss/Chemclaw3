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

- [ ] **A standing plan approval authorizes any state-changing tool, not the plan's steps** — [L],
  from the 2026-08 security review (proven live). `plan_gate.enforce_plan_approval` refuses a
  state-changing call unless an approval exists for the current plan's identity — `plan_identity`,
  a hash of the todo *contents* — but it never compares the *tool being called* to anything in the
  plan. So once a human approves a one-line read-only plan ("look up the melting point of aspirin"),
  every tool in `authz.side_effecting_tools()` executes for the rest of that turn:
  `propose_knowledge_note` (a knowledge-graph write / git push), `synthesize_memory`, every durable
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

## 2 — Answers that are wrong without saying so

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

- [ ] **A timed-out parse still runs to completion on the worker thread** — [L]. **The cheap half
      is closed**: `ingest/documents/sync.py::_parse_changed` now bounds its `asyncio.to_thread`
      with the front door's own `attachment_parse_timeout_seconds` and counts the outcome as
      `skipped_timeout` through every rendering a run is read through. What remains is the half
      that was always [L]: `agent/attachments.py:284` shields the future deliberately, so on both
      paths the timeout frees the caller and the slot and never the thread — no parser behind
      `parse_document` offers an interruption hook, so a hostile document still burns a worker to
      completion in the background. The only real fix is a killable subprocess, with pickling and a
      new child-OOM failure mode to classify (~150-250 lines).

- [ ] **A schedule whose every run is killed by the ceiling reads as healthy on
      `describe_schedules`** — [M]. `durable/schedules.py:399` — `ScheduleHealth` carries `paused`,
      `last_run`, `runs_total`, `skipped_overlap`, `running_now` and `note`, and no run outcome;
      `_describe` reads none either, because `ScheduleInfo.recent_actions` names the workflow and
      when it started and nothing more. Measured against a live broker: a schedule built like
      `_build_schedule` whose every run is killed reports `runs_total` climbing, `last_run`
      advancing, `running_now` 0 and `skipped_overlap` 0 — byte-identical to a healthy job, while
      the wedge the ceiling replaced had a distinctive signature on that same surface (`last_run`
      frozen, `running_now` stuck at 1, `skipped_overlap` climbing). So the ceiling is a real fix
      and it moved the failure to a surface that says nothing. Recovering the status costs one
      `describe` per schedule on the front door's own event loop, which is why it was not taken
      here; `config/temporal.py` no longer claims otherwise, but
      `D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait.md` still does and wants a
      superseding ADR — as do three further claims in the pair of 2026-08-27 ADRs that the tree has
      since falsified or fixed: that "the ELN and corpus syncs are cursored" — `document_sync.py:238`
      says in its own words that it keeps no `sync_cursors` row, and `corpus_sync.py` keeps one only
      for a source whose binding sets `append_only`
      (`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop`), never in the release mode this claim
      was made about — that
      "a run with no memo stamps nothing" (`CalcJobWorkflow` defaults the memo read to
      `settings.service_actor_id`, so the durable path delivers `service-account`), and that the
      queue-bound AST rule "fails on any dispatched activity call with neither queue bound" (it was
      evadable by an import alias, a direct function import or a subpackage until
      `tests/test_activity_queue_bound.py` was widened).

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

- [ ] **No deployment declares a context window, and the overrun indicator cannot see the prefix**
      — [M], found reviewing `D-2026-08-28-the-budget-is-the-control-not-the-trigger` after it
      merged, and it is that change that made a latent gap live.
      `context_budget.effective_trigger` subtracts the request's own prefix from the budget **only**
      `if window:`; `llm_context_window_tokens` defaults to 0, `.env.example` ships 0, and
      `grep -rn CONTEXT_WINDOW deploy/ infra/ docs/guides/runbook.md` returns nothing. So the ~30k
      prefix — instructions, the skills listing, every tool schema — is never charged against
      `agent_context_token_budget`.
      While the group floor shipped at 12 an ordinary thread sat at ~4k and this could not bite.
      With the floor off the window fills the budget by design, so a request measures **~135,700
      estimated tokens** (99,924 thread + 35,773 prefix) against a configured 100,000 — over a
      128k model window, and at this repository's own measured 2.2x ratio for structured chemistry
      payloads, well over 200,000 billed.
      **And the one thing that would say so reads clean.** `compaction._record_overrun` compares
      the *thread* against the thread's budget, which the window edit has just cut to fit by
      construction, so `chemclaw_context_unreducible_total` moved by **0** on exactly that request
      — while its own docstring calls it "the only leading indicator this system has for a
      context-length failure". `agent_context_calibration_min_calls = 20` also pins the ratio at
      1.0 for the first twenty model calls of every process, i.e. every pod restart.
      Three candidate fixes and they are not equivalent, which is why this is a row rather than a
      patch: declare `llm_context_window_tokens` in `deploy/` (cheapest, and it needs a number
      nobody here knows); subtract `prefix_tokens()` unconditionally (changes what
      `agent_context_token_budget` *means* — thread spend becomes request spend — which is
      `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`'s decision to revisit, not a review
      pass's); or add a window-aware arm to `_record_overrun` so the indicator at least fires where
      a window is declared. **The first two are a decision with an owner.**



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

- [ ] **The corpus drain is the one ingest pass with no metric** — [S].
      `chemclaw_ingest_records_total{source,outcome}` is emitted by the ELN sync
      (`ingest/eln/sync.py::_count_records`), the document sync
      (`ingest/documents/sync.py::_count_records`) and the labelling pass
      (`ingest/labels/enrich.py::_count_records`, under `source="labels"`). `ReactionCorpusWorkflow`
      emits none: `CorpusReport`'s `read`/`recorded`/`skipped` reach the activity's log line and
      Temporal's history, and nothing else. So a dashboard built on `chemclaw_ingest_*` shows a flat
      line for a healthy corpus feed, and `skipped` — the count of rows dropped for no usable SMILES
      or no citation, which is the number that says a feeder regressed — has no series at all.
      Found while writing `docs/guides/feeder-pipelines/`, whose §2.3 has to tell an operator this in
      prose because the metric they would otherwise reach for does not exist.
      **The fix is the wrapper the ELN sync already uses**, one call site, with `source` naming the
      data source rather than the pass — the three outcomes partition the rows the pass saw, exactly
      as `ingest/documents/sync.py::_record_pass` documents for its own. Do it when a deployment actually runs
      a corpus feeder; until then the gap costs nobody anything, which is why it is [S] and here
      rather than done.

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
      `durable/memory_jobs.py::read_corpus` calls
      `fetch_new_entries(datetime.min.replace(tzinfo=UTC))` on
      every ingest half, so each of the three memory jobs (`build_campaign_notes_activity`,
      `build_playbook_notes_activity`, `build_optimization_notes_activity`) walks the whole record
      from the beginning of time, once per activity. (This sentence also named `all_reactions()`,
      which exists nowhere in `src/` — a reader following the anchor found nothing and had no way to
      tell whether the row or the tree was wrong.) On the two
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
      (`infra/sql/063`) records where each feed's drain stopped, and nothing reads `updated_at`:
      `ingest/labels/cursor.py::load_corpus_cursor` selects `after` only. The module declines a lag gauge for a
      stated reason — a keyset position is opaque, so "how far behind" would have to be invented,
      unlike `sync_cursors`' datetime twin which exports `chemclaw_ingest_cursor_lag_seconds`. What
      was offered instead does not hold, and `cursor.py`'s module docstring now says so: `ReactionCorpusWorkflow`
      returns **one** report aggregated over every source at the end of the whole `continue_as_new`
      chain (`durable/corpus_sync.py::ReactionCorpusWorkflow`), not one per pass, and builds it without `has_more` — so
      a feed whose source stopped exporting looks exactly like a feed with nothing new. Two shapes
      would close it and they are not equivalent: a per-source outcome (fixes
      `CorpusSyncOutcome`'s own docstring, which claims "per source" and aggregates), or a staleness
      gauge over `corpus_cursors.updated_at` — age since the last *advance*, which is a real number
      even when the position is opaque. **The second is now buildable and was not when this row was
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

---

## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **`predict_reaction_conditions` is built in the fleet and unreachable from any deployment
      here** — [M], and it is now the biggest single gap in the protocol pipeline
      (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`). `Chemclaw3-mcp`'s
      `servers/rxnpredict` is **built** and serves six read-only tools — among them
      `predict_reaction_conditions`, an ensemble of open predictors with the per-model spread
      returned beside the consensus — and `grep -rn rxnpredict src/` in this tree finds one
      docstring mention in `publish/record.py` and nothing else: no bundle under
      `src/chemclaw/connectors/`, no `connectors:` entry in `deploy/helm/chemclaw/values.yaml`, no
      token obligation. Re-verified 2026-08-29. So
      `skills/protocol-generation` routes a chemist's "how would people run this" question entirely
      through precedent (`conditions_for_similar_reaction`, `reagent_frequency`), which answers from
      what *this* corpus holds and is silent on a transformation nobody here has run.

      **It is a default-surface decision before it is a diff**, and the `pyexec` row below is the
      worked precedent: `registry.enabled()` is "discovery is enablement until you say otherwise",
      so a manifest stub turns six tools on in every fresh checkout, needs six probes
      (`tests/test_probe_coverage.py`), and raises the context floor. Unlike `pyexec` the capability
      is uncontroversial — six read-only predictors, no code execution — so the argument is about
      the enablement default rather than about the tool. Whichever way it goes it also owes a
      `values.yaml` entry, an `egressPorts` entry for 8857, and the `CHEMCLAW_RXNPREDICT_TOKEN`
      obligation `chem` already models.

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
      **8,126 tokens (24%)** — 34,379 reported against 42,505 paid, ceiling now 43,500. So 28,114
      and −5,787 both understate what this narrowing is worth, and the eleven names should be
      re-measured on the bound basis when the row is worked. What does not change is why it is
      blocked: the saving is still partly in endpoint tools no offline floor can see, and it still
      needs the skill gate beside the allow-list.

- [ ] **Ten `KNOWN_OVERSIZED` tools are one defect wearing ten names** — [M], and the honest
      trigger is upstream rather than here. Each takes a **domain document** as its argument — a
      BoFire campaign declaration, the note frontmatter contract, a structured ask, a laboratory
      procedure, a job spec — and `convert_to_openai_tool` inlines every nested pydantic model in
      full rather than emitting `$defs` and `$ref`. Measured 2026-08-28 while narrowing the protocol
      pair from 6,231 tokens to 3,380: `SpeciesRole`'s class docstring shipped **three times** in one
      schema and `RequestField`'s **four times**, purely because the same model appears at several
      fields. A `$ref`-emitting conversion would cut every entry at once and touch no first-party
      code.

      **It said four until the basis was corrected, and the extra six are the same defect, not new
      debt.** The 2026-08-29 re-baseline measured the tools the graph *binds* rather than the
      callables the registry holds, and six that read as under `MAX_SINGLE_TOOL_TOKENS` were over it
      all along — `rank_species` 885 as a callable, **1,094** as the object the model is sent, and
      likewise `rank_species_across_solvents`, `compute_reaction_energy`, `survey_bond_strengths`,
      `refine_ensemble` and `profile_rotation`. Nothing was added; the six were invisible for eleven
      weeks to the test written to catch exactly them. It widens this row rather than changing it:
      the entries are still nested-model inlining, and the job specs make the `$defs` question
      *more* worth answering, not less.

      **So the row is a measurement, not a rewrite.** What is owed first: does the installed
      `langchain_core` have a switch for it, and do the providers this deployment targets accept a
      `$defs`/`$ref` parameter schema? Both are one script. The alternative fix — taking the payload
      as a JSON string or a scratchpad path — is **rejected on the record** in
      `tests/test_context_floor.py`: it drops the schema to ~150 tokens and takes constrained
      generation with it, on exactly the calls where a malformed argument is most expensive.
      Anchors: `tests/test_context_floor.py::KNOWN_OVERSIZED`, `protocols/models.py`,
      `science/bo/problem.py`.

      **A big schema costs graph-build time as well as prompt tokens — and the larger half of that
      was fixed on `main` while this row was being written** (2026-08-29). The four protocol tools
      raised the per-turn compile by **30 ms (209 → 239, +14%) for four tools out of ~98**, isolated
      by deleting the import that registers them, and profiling put **79% of the whole build** in
      `langchain_core.tools.convert.tool` → `validate_arguments` → `create_model`: every build
      re-derived a pydantic model from every tool's signature, so build time was proportional to
      schema size exactly as prompt cost is.

      **That is history rather than an open item.** `agent/tool_schema.py::as_structured_tool` now
      converts each registered callable once per process, keyed on the function object, and the
      merged tree measures **38.7 ms** with all four protocol tools present — so the build-time
      half of this row is closed, and the bound in
      `tests/test_langgraph_connectors.py::test_compiling_the_graph_per_turn_stays_within_the_maf_agent_build_budget`
      came *down* to 250 rather than up. It is recorded here because the
      finding still holds where the cache cannot reach: a tool's schema is still generated once,
      and the *prompt* cost above is unchanged and paid every turn. Anchors:
      `tests/test_context_floor.py::KNOWN_OVERSIZED`, `protocols/models.py`,
      `science/bo/problem.py`.

- [ ] **The module a chemist actually reads has no test file** — [M], found 2026-08-29 by the
      fresh-context review that took `render.py` apart
      (`D-2026-08-29-a-check-a-reader-never-sees-is-not-a-check`). No test anywhere imports
      `render_markdown`, `run_sheet_rows` or `summarise`: the whole assertion surface across the
      suite is two lines in `tests/test_protocol_design_tools.py` (`startswith("# SM-3 Suzuki")`
      and `"## Evidence" in readout.markdown`). Coverage over the protocol test files is **76%**,
      and the uncovered block is `summarise`'s screen/campaign branch — the sentence a chemist reads
      about a plate is executed by no test.

      That is why every one of that module's defects was found by reading rather than by running:
      a document that printed the body's conditions over the arm's, a partial setpoint override
      blanking a row, fifteen dropped leaf fields including `base.waste` and every unit, and
      `summarise` branching on the ask rather than the design. Each is fixed and each was verified
      by hand; none has a regression test, because writing thirty of them is its own change with its
      own argument about what a rendering test should assert. Anchor: `protocols/render.py`.

      **Closed 2026-08-30** by `tests/test_protocol_render.py`, written in the review-fix cycle that
      re-took the module apart and found nine more defects — including a page that told a chemist
      1 bar N2 for an arm running at 50 bar H2. Eleven of its thirteen tests fail on the previous
      renderer. Keep this row only until the coverage figure above is re-measured.

- [ ] **A truthful `stated` quote from an earlier turn cannot be represented** — [S], found
      2026-08-30 by the fresh-context review of the agent surface. `require_quotes_are_verbatim`
      checks the quote against `get_current_user_text()`, which is the message that started *this*
      turn, and `structure_experiment_request`'s own docstring says to call it "first … while
      correcting it is still cheap" — i.e. iteratively, across turns. Measured: a chemist who wrote
      "24 wells, no DMF, by Friday please." on turn 1 and "ok go ahead" on turn 3 gets the intake
      refused, because `'24 wells'` is not in "ok go ahead".

      So on the ordinary multi-turn path the honest `stated` is unrepresentable, and the remedy the
      message prescribes records a real chemist constraint as a model inference — the mislabelling
      the check exists to prevent, running the other way. The refusal message now says which message
      is checkable rather than "the text you were given", which was itself untrue: it *was* given,
      one turn earlier.

      **Fixing it properly means widening the ambient to the thread's user turns**, which is a read
      at the stamp site in `api/runner.py` (and `cli/chat.py`), on the hot path, per turn. Prior
      turns are still the chemist's own words so the anti-spoofing argument is unaffected — the
      question is only what that read costs and where it comes from. Anchors:
      `core/turn_text.py`, `agent/protocol_design_tools.py::require_quotes_are_verbatim`.

- [ ] **A second sign-off at the same revision overwrites the first, and both callers are told
      204** — [M], found 2026-08-30 by the fresh-context review of `protocols/store.py`.
      `expected_revision` is a compare-and-set on the *document*, never on the *status*, so two
      people looking at revision 1 can approve and abandon it and both writes succeed: measured
      **100/100** over `asyncio.gather`, with the final header 29/71 either way across runs.
      Sequentially the same thing needs no race at all.

      The evidence survives — `experiment_protocol_status_events` records both moves with their
      actors and revisions, and the newest event agreed with the header 100/100 — so this is "nobody
      is told at the time" rather than a lost record. What it costs is `advanced()`'s stated
      guarantee that an `abandoned` design stays abandoned unless a *person* moves it: a second
      person's `set_status` un-abandons it silently, and a design retired because the starting
      material decomposes is back in the `draft` listing.

      **Not fixed here because the fix is a contract change.** Closing it properly means the caller
      stating the status it saw (`expected_status`), which is a new field on `StatusIn`, on the
      store Protocol and on both backends, and a matching change in `Chemclaw3_ui`'s sign-off panel
      — an optional field nobody sends would be a control that exists only in the docstring, which
      is the failure mode `map_to_hpc_identity` is this tree's standing example of. The half that
      needed no contract change shipped: `require_movable` refuses `approved` and `executed` on a
      design holding only the structured ask, which was a lab record saying an experiment had been
      run against a document with no procedure in it. Anchors: `protocols/store.py::set_status`,
      `api/schemas.py::StatusIn`.

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
      **125 of 288** probes (re-counted 2026-08-29; 124/261 on 2026-08-27, 116/232 on 2026-08-25 —
      the corpus keeps growing and the concentration is not shrinking with it, 43% against 47%);
      `find_notes` 96; `expand_note` 60; bucket C is **48** probes against bucket A's 169; the
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
