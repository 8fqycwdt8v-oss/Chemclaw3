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

- [ ] **Nobody has measured what this system's own workflows carry, so the worker's cache bound is
  asserted at a placeholder** — [S], opened 2026-09-22 by
  `D-2026-09-22-the-ceiling-that-holds-memory-is-the-cache-not-the-task-slot`, which closed the
  unbounded-memory half of the old `max_concurrent_workflow_tasks` row. A cached workflow is
  measured at **~85 KiB fixed plus ~1.05x its own state plus a history term**; what is *not*
  measured is either of the last two for the workflows this repository actually runs, and the
  history term's own coefficient is unsettled — two runs put it at 0.95 and at ~1.8 KiB per
  signal, so the constant is named an *allowance* rather than a measurement. `tests/test_workers.py`
  asserts the inequality at `_STATE_THE_BOUND_IS_ASSERTED_AT_KIB = 256` and
  `_CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB = 175`, placeholders wearing names that say so — together
  the shape of a long-lived campaign parent, rather than a reading of anything.
  `BoCampaignWorkflow` and the durable calc workflows are the ones to sample; the harness is the
  parked-workflow RSS sweep the ADR describes, pointed at the real broker because the dev-server
  binary is not fetchable here. **Sampling them is what would let the ceiling move**: it ships at
  750 rather than the SDK's 1,000 only because the allowance is conservative, so a real reading
  either justifies the headroom or returns it.

  **The other half of that ADR stays declined and needs a different measurement.**
  `max_concurrent_workflow_tasks` is left at the SDK's 100 because its bound is CPU, the worker's
  limit is 2 cores, and nothing has measured what a workflow task costs. A number chosen without
  that is the unexamined posture the old row was about, one value further on. Anchors:
  `core/config/temporal.py::worker_max_cached_workflows`,
  `tests/test_workers.py::test_the_workflow_cache_fits_the_memory_the_chart_asks_for`.

- [ ] **The IPv4-mapped arm of the compiled egress guard is unmeasured here** — [S], the last of
  "the egress guard is blind to gRPC and to Temporal" after
  `D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py`. The blindness
  itself is closed: `core/netguard_preload.c` interposes libc's `connect`, `getaddrinfo`, `sendto`
  and `sendmsg` through `LD_PRELOAD`, armed by `deploy/entrypoint.sh` from the allowlist
  `netguard.derive_allowed` returns, and driven against a real gRPC server over a non-loopback route
  it refuses the plain socket, `grpc` and `temporalio` alike — grpc's own C-core reporting
  `connect failed: ... Operation not permitted` — while loopback and an allowlisted address continue
  to work. **The git half is closed** by
  `D-2026-09-22-a-destination-that-is-a-name-is-still-a-destination`: `netguard.derive_allowed`
  resolves `git remote get-url` inside the note checkout, so both guard layers arm with the host,
  and it does so only once `note_repo_dir` has moved off its default.

  **What is left is one arm nobody here can drive.** `test_an_ipv4_mapped_address_is_not_a_way_around_the_check`
  skips on a host without `AF_INET6`, which includes this sandbox, and the skip carries the reason
  in its message. The unwrapping it would measure is the one place the compiled guard reads an
  address rather than a name, so a wrong answer there is a bypass rather than an outage — which is
  why an unmeasured arm is worth a row rather than a shrug. It needs a host with IPv6 enabled, not
  new code. Anchors: `core/netguard_preload.c`, `core/netguard_preload.py`, `deploy/entrypoint.sh`,
  `kg/git_writer.py`.

## 2 — Answers that are wrong without saying so

- [ ] **`standardize` is not idempotent on ferrocenyl palladacycles, so `compound_id(raw)` is not
      the id of `compound_note(raw)`** (issue #485) — [M], opened 2026-09-27 by the seeded-corpus measurement of
      `D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned`. Three of the 129
      molecules in `Chemclaw3_mock`'s ORD seed — the dtbpf-, dppf- and Josiphos-type Pd G3
      precatalysts — standardize to a kekulé Cp anion from the raw string and to the aromatic one
      from that standard form, so `standard_smiles(standard_smiles(x)) != standard_smiles(x)`.
      Driven: `compound_id(raw)` is `compound-bd1143cc135d` while `compound_note(raw).id` and every
      `similar_molecules` hit cite `compound-626ec3b8d0ae` — so `compound_dependencies` on a note
      carrying the raw string links an id no note is written under. It is also why a molecule-row
      re-key moves those three keys although the bump did not touch them. The candidates are
      iterating `standardize` to a fixed point (a bump, and the cost of a second pass on every
      structure) or finding which `Cleanup`/tautomer step re-aromatizes and pinning it. Anchors:
      `core/chem.py::standardize`, `core/chem.py::compound_id`, `ingest/eln/compound.py`.

- [ ] **`plan_gate.py` is paired with one of the five test files that cover it** — [S], opened
      2026-09-23 by the review of the wave that added it to the mutation backstop.
      `tests/test_plan_gate.py` takes the selection from 66% to 87% of the module; adding the four
      siblings that already cover it — `tests/test_plan_scope.py`, `test_plan_state.py`,
      `test_plan_inbox.py`, `test_plan_link.py` — reaches **90.7%**, closing lines 218-235 and 670.
      Those residual lines are precisely the `no_tests` mutants `[tool.mutmut]`'s own comment says
      pairing exists to prevent.

      **Not done in that wave because the price is another full run, not a config edit.**
      `tests/test_mutation_workflow.py` pins the floor to the *selection* as well as the population,
      by design, so four more files red it and the floor has to be re-measured — ~80 minutes. The
      run that would pay for it is one that has another reason to happen: the next `source_paths`
      addition, or a `no_tests` share that stops having headroom (it is 11.7% against a ceiling of
      16.0, so it has some). Bundling this with the next re-measurement costs nothing; taking it
      alone costs an hour and a half for ~4 points on one module. Anchors: `pyproject.toml`
      `[tool.mutmut].pytest_add_cli_args_test_selection`, `tests/test_mutation_workflow.py`.

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

- [ ] **The helper file budget is charged to siblings that wrote nothing** (issue #463, #489) — [M],
  opened by `D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep`; its other half, the
  two write verbs nothing bounded, is closed by
  `D-2026-09-26-a-helpers-unbounded-write-verbs-take-the-scratch-cap`.
  `batch_siblings` divides the remaining budget by the batch's calls *naming* `task`, because the
  siblings' results do not exist when it runs. Most `task` calls read and write nothing, so one
  helper filing a note beside seven silent ones is charged an eighth: driven at the shipped budget,
  a 199,999-character note lands whole at width 1 and as 25,000 at width 8. The bound holds — an
  unclaimed share is wasted, never spent — so this is lost allowance rather than a hole. The shape
  that would be exact is a trim over the **merged** channel after the superstep, where every real
  contribution is visible, and it is not free: the exemption that keeps a chemist's own documents
  out of this budget is `rewritten_command_files` comparing each command against the state
  *before* it, and a post-merge trim has nothing to compare against, so exact accounting has to buy
  the channel provenance first.
  `test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` is what a naive
  version breaks, which makes this a design with an ADR rather than an edit. Anchors:
  `agent/tool_result_size.py::batch_siblings`, `agent/tool_result_shape.py::rewritten_command_files`.

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
      model; `make live-delegation` has now run against one
      (`D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model`), so the harness exists.

---

- [ ] **The 2026-09-27 live-run fixes have not been re-verified against a real model** — [S]. Three
  defects a real-model run found (DeepSeek V4 Pro via OpenRouter) are fixed and pass against
  scripted models only: gas-phase energies over charged species are refused
  (`connectors/calc/compose.py::require_solvent_for_ions`), a capped turn still answers
  (`agent/loop_cap.py`), and the verifier's revision note no longer leaks into answers
  (`api/runner.py::_REVISION_NOTE`, measured 10/18 → 0/18 in isolation). Re-run pc-03, a delegation
  probe that caps, and a revised answer through the four-repo lane against the gateway; about $15 of
  the run's $25 budget is unspent.

- [ ] **The connectors dev server stalled for 52 s under probe load, and nothing explains it** —
  [S]. During `make live-probes` on 2026-09-27 the connectors process (`cli/connectors_dev.py`,
  :8810) logged nothing from 23:26:57 to 23:27:49 and then released every queued request at once;
  the front door recorded 12 MCP handshake timeouts and 33 turns that lost the bo, calc, molfp and
  rxnfp tools. No tool call preceded it, and the host was heavily loaded, so host overload and a
  blocking call on the server's event loop are both open. Reproduce on an idle host before fixing
  anything.

- [ ] **Three live-lane papercuts cost evidence during the 2026-09-27 run** — [S].
  `cli/live_storm.py` accepts `--families DH` but rejects the obvious `--families D,H`;
  `infra/live/processes.sh restart api` truncates the front door's log, losing the evidence of the
  run before it; and the lane leaves `CHEMCLAW_FRAMING_ENVELOPE_SECRET` unset (`.env.example`), so
  the prompt-injection framing tag uses a per-process random nonce and two processes frame
  differently.

- [ ] **`test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce` hit its 180 s timeout
  under load** — [S]. Seen once in the gate container at load 143–270
  (`tests/test_context_floor.py::test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce`);
  the full suite passed in CI on the same head. Not yet shown to be a flake on a normal runner —
  measure its wall time on an idle host before loosening anything.

## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

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

- [ ] **About a third of tools still rest on one probe, and they are the compute and job tail** (issue #487)
      — [S], narrowed 2026-09-24. Derived from `tests/test_probe_coverage.py::_probes` and
      `::_expected_tools` with `load_profiles()` called first (two earlier measurements disagreed
      on exactly that): 47 of 124 tools had one probe. The seven whose effect persists past the turn
      — preferences, watches, skill proposals, plate observations, the results store, the knowledge
      graph, a saved workflow — now have a second phrasing (ws-21..24, pt-08, du-11, du-12). What
      remains is the semiempirical and prediction surface (`run_*`, `compute_*`, `enumerate_*`,
      `predict_*`), where a missed call costs a re-run rather than a state change. It is **not** a
      ratchet on the count, for the reason it never was: that taxes adding a tool rather than
      bounding risk. Choose the next second questions by what a deployment calls —
      `audit_events` per tool name — once one exists to read.

- [ ] **`deep-research` has no index behind it** (issue #486) — [M]. `agent/research_tools.py::gather_evidence`
      sweeps the knowledge graph, the ELN, the mounted document share and the fingerprint store —
      every one internal. `skills/deep-research/SKILL.md` describes a capability whose corpus is
      whatever notes exist (41 on this checkout, 2026-09-22). `Chemclaw3-mcp/MODULES.md` files `litsearch`
      (Europe PMC / OpenAlex / Crossref bulk, built at image time, no egress) as *proposed*, and says
      in as many words that it "gives Chemclaw3's existing `deep-research` skill a real index".
      ChemRAG measured **+17.4% average relative gain** from a chemistry corpus and — the design input
      that matters — that corpus choice is task-dependent: reaction prediction wants literature,
      nomenclature wants structured databases. A process chemist asking "has anyone run this coupling
      on a deactivated aryl chloride" currently gets whatever that corpus happens to say — this row
      said 39 and then 40 for one count three sentences apart, which is why the number now appears
      once, with the date it was measured.

- [ ] **A shared session serialises by refusing and streams to one reader** (issue #488) — [M]. What is left of
      the multi-human-session work after `D-2026-09-27-in-a-shared-session-the-sender-governs`
      settled the authority questions and shipped membership (`session_members`, the sender
      governing each turn, a plan decided only by its author). Three pieces, in dependency order:
      **a queued turn** — a member's message while another participant's turn runs is refused 409 by
      `SessionTurnClaims`, which is the right serialisation and the wrong answer; it becomes a
      bounded wait with a position, and the lease already handles a dead holder. **Reader fan-out** —
      `api/detach.DetachableTurn` holds one queue and one `_attached` flag, so a second participant
      reattaching *steals* events rather than seeing a copy; it needs N readers with per-reader
      backpressure so one stalled browser cannot hold the turn (the bound
      `service_sse_send_timeout_seconds` sets for one reader). **The plan inbox for members** —
      `GET /plans/pending` pages the caller's *owned* sessions, so a plan a member's turn wrote in
      somebody else's session reaches that member only through the in-turn card; it wants the
      sessions `GET /sessions/shared` lists as well. A chat-room connector is separate work on top
      and wants all three finished first.

- [ ] **A routing corpus where the right profile is not inferable from the question's surface**
      — [M]. Seven profiles ship and genuinely narrow (`evidence` reaches zero side-effecting tools,
      `safety` one, `default` all of them — `authz.side_effecting_tools()` answers 54 as of 2026-09-22, where this row said 49); what `D-2026-08-15` deleted is automatic routing between
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

- [ ] **The turn-time comparison cannot diff what the ELN gives structured** (issue #490) — [M].
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

- [ ] **PR #321's review findings never landed, and the PR is too stale to rebase** — [M].
  `claude/tool-integration-storage-review-3zupm4` (108 files, +6,840, opened 2026-09-05) is
  superseded in part — migration numbers 082–084 are taken and the calculation-epoch work landed
  another way — but its decision files and `result_composites` never reached
  `docs/decisions/README.md` or the tree. Extract what is still open from its review document into
  rows here, then close #321.

- [ ] **Dependency bumps held back from Dependabot need hand-made PRs** — [S]. Dependabot group #431
  was closed because it broke three things at once: mutmut 3.8 removed `Config.ensure_loaded`
  (`pyproject.toml` `[tool.mutmut]`), deepagents' `task` schema grows to 929 tokens against the
  900-token bound `tests/test_context_floor.py` holds, and rdkit 2026.3.6 moves torsion-handle
  literals that this repository and Chemclaw3-mcp both assert (`tests/test_calc_rotation.py`) — that
  one must land as a coordinated pair with the fleet. Bump the safe members in a hand-made group and
  the rest one at a time.

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

- [ ] **5,760 ORD records — 57% of the seeded corpus — cannot be ingested at all** (issue #477) — [L].
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
