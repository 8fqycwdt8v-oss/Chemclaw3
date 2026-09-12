# Waves 21–30 — production-readiness hardening across the family

**Numbered 21–30, not 1–10.** Twenty waves are already merged here; restarting at 1 would make
every commit message, ADR and review note ambiguous about which effort it belongs to. The request
was for ten waves, and these are the next ten.

**The goal, stated so it can be checked.** A codebase a deployment team can take to production:
every perimeter control actually enforced rather than declared, every resource bounded, every
failure visible, every gate it claims to run actually run, and every number in prose either
measured today or deleted. Not "fewer lines".

## The premise, measured rather than assumed

The three repositories this session was opened on were surveyed at `252eb38` / `1b997de` /
`637d5cc` before this plan was written. What the survey found decides its shape:

| Repository | Gate today | Registered open work | What the survey actually found |
|---|---|---|---|
| `Chemclaw3` | `make lint` ✅ `make type` ✅ (840 files) | 67 queued rows + 221 archived findings + a `DEFERRED.md` with triggers | The queue is real, anchored and mostly **unworked**. No cleanup sweep needed; the rows are the work. |
| `Chemclaw3-mcp` | `make check` = lint+type+test+deps-audit | **no** BACKLOG, **no** DEFERRED, **no** `docs/decisions/`, **no** lessons log | 0 TODO/FIXME. Strong posture, three real holes: unbounded MCP sessions, two heavy servers with no admission ceiling, and a supply chain that proves a property of `uv.lock` and not of any shipped image. |
| `Chemclaw3_ui` | 19-step CI incl. axe + container assertions | `ISSUES.md` (4 open) + 5 known gaps | Already hardened: 0 TODO, 0 `as any`, 1 `@ts-expect-error`, 0 empty catches. Its gap is **gate reproducibility**, not code quality. |

**Baseline, measured before a line was changed** (`252eb38`, this environment, Docker up):
`make lint` ✅ · `make type` ✅ 840 files · `make test` **8,627 passed, 79 skipped, 0 failed in 18:07**.
The skip count is the number that matters: the offline default skips **216**, so a run taken without
`dockerd` is not evidence about the durable layer, the session store or retention. Every wave reports
this pair — passed and skipped — in its PR body (R6).

**So this plan rejects the default reading of "refactoring".** There is no duplication to extract and
no dead-code harvest left — waves 16–20 did that and measured the tree DRY with a median function of
nine lines. What is unfinished is the part that decides whether a deployment survives contact with
production: controls that exist in prose, bounds that exist per-call but not per-pod, and gates that
exist in a Makefile but not in the pipeline that ships the bytes.

**Scope is four repositories, not three.** `Chemclaw3_mock` is cloned into this session
(`b42572f`) because W30.7's four-repo live lane cannot run without it — it serves the two ELN
datasources, the stand-in Entra tenant and an example HTTP-transport MCP tool. It carries no
`CLAUDE.md`; its conventions come from its own `README.md` and `ISSUES.md`. It gets a branch and a PR
only if a wave actually needs a change there.

**Two asymmetries drive the ordering.** The fleet has no decision record at all, so its waves must
*create* one as they go (W21 opens `Chemclaw3-mcp/docs/decisions/` and its own register). And the UI
is nearly closed, so it appears in four waves only, on the items its own `ISSUES.md` already names.

---

## Rules every wave inherits

These are not aspirations. Each is here because this family has already paid for its absence, and
each names the lesson it comes from.

- **R1 — Scout before implementing.** Every row is re-verified against `HEAD` first. `BACKLOG.md`'s
  own header records a pass that found **17 of its rows not workable as written**; correcting or
  deleting a stale row is as much a contribution as the code would have been. A wave's first output
  is a verification table, not a diff.
- **R2 — Measure it, don't argue it.** Every claim a wave makes gets a number from a run, and the
  script that produced it is kept in the wave's brief. Prose is evidence about its author, never
  about the code.
- **R3 — Mutate the fix, watch the test fail.** Before the commit message. 7 of 30 tests written in
  one day survived mutation, all of them asserting *the shape of a thing rather than its effect*
  (`tasks/lessons.md`). A test and the fix it guards, written together, share a blind spot.
- **R4 — No number in non-test prose unless a test fails when it goes stale.** Otherwise the number
  belongs in the test and the prose gets the test's name (`D-2026-09-03`,
  `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).
- **R5 — A deletion is a behaviour change until proven otherwise.** Enumerate the tables, columns,
  metrics and config keys the removed thing was the only **writer** of, then find every **reader**
  of each. A reader with no producer returns `[]` and passes its own tests
  (`D-2026-09-05-a-reader-with-no-caller-passes-its-own-tests`).
- **R6 — The gate is the pipeline, not the habit.** Per wave, per repo:
  `Chemclaw3`: `sudo -n dockerd &` → `make up` → `make db-migrate` → **`make ci`** (16 steps, not
  `make check`), and the skip count from `tests/conftest.py`'s epilogue is **reported in the PR
  body**. A local run with Postgres down is not evidence about the durable layer.
  `Chemclaw3-mcp`: `make check` **and** `make offline-run`.
  `Chemclaw3_ui`: `npm ci` then every step `.github/workflows/ci.yml` runs, `npm run test:e2e`
  included — `vitest` green is not CI green (`tasks/lessons.md`, 2026-09-05).
- **R7 — One decision, one ADR, one deleted row.** `D-YYYY-MM-DD-<slug>.md`, a row in
  `docs/decisions/README.md`, and the `BACKLOG.md`/`DEFERRED.md` row deleted **in the same commit**
  that closes it — never struck through, never annotated.
- **R8 — One repo, one branch, one PR.** Branch `claude/10-wave-refactoring-plan-aqeurx` in each
  repo; merge to `main`; delete the remote branch; then reset the designated branch off the new
  `main` for the next wave. A merged PR is never reused.
- **R9 — An unavailable gate step is tried once before it is recorded as unavailable.**
  `kubeconform` was deferred to CI eight times on the strength of a "not installed" message and took
  one `curl` and a `cp` (`tasks/lessons.md`). `helm` and `kubeconform` are absent here today.

---

## The three decisions taken before W21

Asked and answered by the owner, recorded here so no wave re-litigates them:

1. **The `[L]` rows are built, not just recorded.** The standing plan approval's scope (W22.1), what
   enforces egress (W21.3), the checkpointer's write volume (W28.4) and a first external benchmark
   (W27.7) are implemented rather than moved to `DEFERRED.md` with a trigger. This is the expensive
   answer and it is the reason W21, W22, W27 and W28 each carry a second PR slot. An
   `LD_PRELOAD`/seccomp egress layer is the single largest item in the plan.
2. **`Chemclaw3_mock` is in scope**, for the reason above.
3. **`Chemclaw3_ui`'s two large files are left alone.** `results/renderers.tsx` (2,021 lines) and
   `state/chatStore.ts` (1,869) are the only size outliers in a repository that surveyed clean, and
   the second carries measured persist-budget and throttle logic pinned by five test files. Splitting
   them is churn with regression risk and no robustness gain. The UI is worked on its own registered
   items instead.

## The team shape, and why a wave has two halves

Each wave runs as a team of subagents, then a **separate** team reviews what the first one merged.
The split is the point: this family's best findings have all come from fresh context reading a diff
whose rationale it was never told (`D-2026-09-03`, four reviews → four stale sentences;
`D-2026-09-05`, five reviews → nine defects, four of the same unlooked-for shape).

**Half A — build (before the PR)**

| Role | Count | Brief |
|---|---|---|
| Scout | 1 | R1. Re-verify every row against `HEAD`; produce the verification table; correct or delete what is stale. Runs first, alone. |
| Implementer | 2–4 | One coherent slice each, own commits, no shared files. Each writes the test before the fix and mutation-checks it (R3). |
| Adversary | 1 | Mutates every test the wave added and reports which survived. A survivor is a defect in the wave, not a note. |
| Claim auditor | 1 | Reads the whole diff against every docstring, ADR, README and `values.yaml` line it touches. R4. Runs `prose-validate`, `make upstream-check`. |

**Half B — review (after the merge, before the next wave)**

3 reviewers, fresh context, given **only** the merged diff and the repository's own rules — never
the implementers' reasoning. Each is asked a different question, because a single "review this"
prompt converges on the same findings:

1. *Does the control work for the attacker / the operator it is for?* Drive it, don't read it.
2. *What does this diff make false?* Every number, docstring, ADR sentence and chart comment.
3. *What does this diff orphan?* R5, run both directions — writers with no reader, readers with no
   writer.

Whatever they find is fixed in a **second PR inside the same wave**, merged the same way. A wave is
not closed while a reviewer finding is open.

---

## W21 — The perimeter: egress, the ambient proxy, and refusing at boot

The controls most often *declared* rather than enforced. Five of these are live `BACKLOG.md` rows
whose own text says the stated guarantee is narrower than the sentence asserting it.

- [x] W21.1 `Chemclaw3` — the JWKS fetch follows an ambient proxy with no seam to stop it
      (`api/auth.py::_client_for`, `core/netguard.py::refuse_proxied_egress`). **This is the anchor
      every bearer token is validated against.** The decision this row posed — a process-wide opener
      install versus vendoring `fetch_data` — was not the decision: `proxy_open` consults
      `proxy_bypass` per request, so the fix is host-scoped. Closed by
      `D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared`.
- [x] W21.2 `Chemclaw3` — delete `tests/test_netguard.py::_TRUST_ENV_LANE_EXEMPTIONS`: **eight**
      constructions at `trust_env=True`, one carrying a bearer (`cli/live_probes.py:340`,
      `cli/live_storm.py` ×5, `evals/live.py`) and one that is not an httpx client at all
      (`cli/phoenix_publish.py`, which takes `http_client`). List deleted, so there is no exemption
      left to be module-granular. Same ADR.
- [ ] W21.3 `Chemclaw3` — the egress guard is blind to gRPC and to Temporal, its two
      highest-value destinations, and they compound through a loopback sidecar. Measured: all three
      reached an external listener with the counter at 0. **Decide what enforces egress** (LD_PRELOAD
      / seccomp / per-library) and close the cheap half now: `networkPolicy.egressDestinations` must
      say it is the only layer bounding these two. ADR.
- [x] W21.4 `Chemclaw3` — the gateway boot guard reaches the front door and not the worker, and
      `durable/template_activities.py` makes model calls from a worker. **Split, and the closable
      half is closed** (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`):
      `refuse_unconfigured_llm_gateway` moved to `core/llm_gateway.py` and is called from
      `create_app`, `api/mcp_face.main`, `durable/background_worker.main` and `cli/chat.main` — the
      three components of `deploy/entrypoint.sh` that make model calls, plus the terminal front
      door; the two connector components are shown by an import check to be unable to reach the
      gateway. The exemption is no longer a bind: `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` is stated by
      the lanes that mean it. `tests/test_llm_gateway_guard.py` starts the real processes, each arm
      against a positive control. **Deciding what "exposed" means for a process that only dials out
      is the half that stays open** — it is `_refuse_unauthenticated_exposure`'s signal, it is a
      design question rather than a move, and `BACKLOG.md` carries it under its own title.
- [x] W21.5 `Chemclaw3` — the Qdrant client builds its own httpx outside the proxy fix. Measured in
      a scratch venv: a caller-supplied client is a `TypeError`, `trust_env` passes straight through
      `**kwargs`. One keyword. Same ADR.
- [ ] W21.6 `Chemclaw3-mcp` — **create `docs/decisions/` and a `BACKLOG.md`.** The fleet has no
      decision record, so every argument in its `CLAUDE.md` is unanchored prose. This wave's own
      findings are its first rows. (Prerequisite for R7 in every later fleet wave.)
- [ ] W21.7 `Chemclaw3-mcp` — **this row was wrong and the Scout pass corrected it.** It said
      `tests/test_fleet.py:666` covers `MCP_EGRESS_ALLOW` and not `MCP_EGRESS_GUARD`. Measured: it
      covers **both**. What is actually open is four things, three of them found by looking:
      (a) `_egress_offences` (`tests/test_fleet.py:641`) matches `^ENV\s+(MCP_EGRESS_[A-Z_]+)=` under
      `re.MULTILINE`, and `servers/rxnlabel/Containerfile:78` and `servers/rxnpredict/Containerfile:56`
      both set `MCP_EGRESS_GUARD=on` as a backslash-continuation — so for **two of seven** servers,
      flipping it to `off` or adding an allowlist beside it passes the ratchet. Worse,
      `test_the_allowlist_check_bites` (`:700`) drives only the own-line form, i.e. the one shape
      those two files do not use: the bites-test certifies the arm the tree does not exercise. This is
      `Chemclaw3`'s own "a basis that is re-derived rather than observed will agree with itself
      forever", one repository over.
      (b) **No ratchet protects any resource bound.** `_egress_offences` discards every env pair not
      named `MCP_EGRESS_*`. **This row said twelve bounds and named `calc` as the one server whose
      bounds are constants and therefore safe; both were measured false on 2026-09-12.** The shipped
      ratchet derives **42** by AST over `packages/*/src` and `servers/*/src`, covering both
      mechanisms — an env read converted by `int`/`float`, and a numeric `pydantic-settings` field
      under an `env_prefix`. `calc` is not the exception but the **worst case**: `CalcSettings`
      (`servers/calc/.../engine/config.py:64`) carries `env_prefix="CHEMCLAW_"`, so
      `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS=99 CHEMCLAW_XTB_MAX_ATOMS=99999` moves that pod 4 → 99
      and 500 → 99999 — on the server whose calls take minutes to hours. The survey missed it because
      an `os.environ`/`getenv` grep cannot see a pydantic-settings field, which is why the set had to
      be derived rather than listed.
      (c) `grpc` is absent from `no_egress.FORBIDDEN_MODULES`, and measured it opens real connections
      with the guard armed and the counter flat — `grpcio` is lockfile-reachable through `tensorboard`
      under `rxnpredict`'s ML extras, so the static scan is the only in-repo layer that could see it.
      (d) `egress.py`'s docstring concedes **four** channels outside the guard; that repository's
      `CLAUDE.md` names three, omitting `_socket.socket` — the one the runtime guard provably cannot
      reach. And `no_egress.py`'s docstring concedes neither `ctypes` nor `grpc`, so a reader of a
      clean scan cannot learn the case is outside both layers.

**Acceptance** — R6 in both repos; an LD_PRELOAD/seccomp decision recorded either way; the counter
and `chemclaw_egress_guard_armed` shown to move under a driven gRPC attempt, or the ADR stating
plainly that they cannot.

## W22 — Authorization, attribution and name-space integrity

Four rows where a control's *scope* is wider than its name, plus the two name spaces a connector can
quietly capture.

- [x] W22.1 `Chemclaw3` — a standing plan approval authorizes any state-changing tool, not the
      plan's steps [L]. **Built**, closed by
      `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`: a plan step declares the
      tools it will call (`agent/plan_scope.ScopedTodoListMiddleware`, a required schema field so
      an omission is a retryable tool error rather than a fall-through), the decision stamps the
      union onto `plan_approvals.scope` (migration 095), and the gate reads the **row** rather than
      the live plan, so a rewrite that widens a step's declaration under an unchanged `content`
      hash gains nothing. Before/after is `tests/test_plan_scope.py`'s ratchet over the whole of
      `side_effecting_tools()`. `HumanInTheLoopMiddleware` stays declined for the plan gate itself.
- [x] W22.2 `Chemclaw3` — the unauthenticated `X-Chemclaw-Actor` header becomes durable attribution.
      **Row corrected, not closed** — the caller still chooses the string. What was wrong was a
      docstring: `_recorded_provenance` said the bundle declares `auth: mode: none` so "anything
      that can open a socket to it can name any chemist it likes", over a manifest declaring
      `mode: bearer`; driven, `/mcp` answers 401 with no token and 401 with a wrong one. The
      forgery is a *token-holder's*. The `unverified:` prefix stays on its own argument (a bearer
      proves core called, never which chemist), and
      `tests/test_bo_provenance.py::test_the_threat_model_this_module_states_is_the_one_its_manifest_declares`
      fails whenever docstring and manifest disagree, in either direction.
- [x] W22.3 `Chemclaw3` — a connector can claim a `run_<name>` step-template launcher.
      Reproduced **cold** (`make connector-validate`, a fresh pod) and refused only warm, as "an
      in-process tool" — the wrong operator-facing reason. Closed by
      `D-2026-09-12-a-tool-list-is-a-name-space-whichever-argument-it-arrives-on` with neither of
      the two fixes the row named: `_bound_by_this_process` asks
      `chemclaw_agent.template_tool_names()` over the `connectors -> agent` edge its own import
      already uses, so no new edge and no change to when a misconfiguration is reported.
      `chemclaw.templates.registry` owns the name space.
- [x] W22.4 `Chemclaw3` — `build_langgraph_agent(connectors=...)` accepts a tool that shadows a
      first-party name. Reproduced: 61 tools bound, the first-party writer gone from
      `tools_by_name`, **and the name still classified state-changing** — so every gate fires on the
      first-party identity while the connector's body runs. Refused at the concatenation, same ADR.
- [x] W22.5 `Chemclaw3` — **row deleted, premise false.** The two producers disagree about `roles`
      on purpose: the interceptor binds `frozenset()` (a relayed argument is data, `D-2026-08-28`),
      `_acting_as` binds the real set because `authorize_job_step` is a template step's *first*
      authorization. Measured, neutering only the role bind leaves the unentitled refusal standing
      and fails the entitled arm — the collapse would refuse every legitimately entitled template
      job step. Replaced by the residual row "template step roles cross the durable boundary on an
      unsigned payload", with its trigger;
      `D-2026-09-12-two-producers-of-one-identity-are-not-redundant-when-they-disagree`.
- [ ] W22.6 `Chemclaw3-mcp` — verify bearer-on-`/mcp` against a **running** server for all seven,
      not off the source: a mounted MCP surface bypasses the enclosing app's dependencies. The
      fleet's own `CLAUDE.md` says verify this way and no test does it per-server.
- [ ] W22.7 `Chemclaw3-mcp` — `assert` as a runtime invariant on caller-derived data
      (`predictors/forward/molecular_transformer.py:37`), stripped under `python -O`, and it
      interpolates raw caller SMILES past the fleet's own `_MAX_ECHO_CHARS` truncation.
- [ ] W22.8 `Chemclaw3_ui` — `src/api/client.ts:538` is the one unencoded path interpolation among
      eleven `encodeURIComponent` call sites. Low impact, breaks the invariant; fix and pin it.

**Acceptance** — a driven probe per control: an approved plan refusing a tool outside its steps; a
forged `X-Chemclaw-Actor` not reaching `audit_events`; a bundle claiming `run_*` refused; a live
`tools/call` on each fleet server refused without a bearer.

## W23 — Resource ceilings: what bounds a pod, not a call

The fleet survey's highest-consequence finding sits here, and it is the same shape as four Chemclaw3
rows: a per-call bound with nothing bounding N of them.

- [ ] W23.1 `Chemclaw3-mcp` — **no ceiling on concurrent MCP sessions.** `sessions.py` reaps idle
      sessions at 1800 s but nothing caps `_server_instances`; at the measured ~149 kB/session an
      authenticated caller holds ~268 MB before the first expiry, against pods requesting `256Mi`.
      No `MCP_MAX_SESSIONS`, no admission on `initialize`. Add the ceiling, refuse promptly, count it.
- [ ] W23.2 `Chemclaw3-mcp` — `rxnpredict` (torch) and `rxnlabel` (RXNMapper, `MAX_BATCH=500`) are
      the two heavy servers with **no `engine/admission.py`**, against their own `CLAUDE.md` rule.
      Ceilings must count what the pod *spends*, not calls — the `servers/calc` lesson.
- [ ] W23.3 `Chemclaw3-mcp` — `chem` has `engine/admission.py` and no `test_admission.py`.
- [ ] W23.4 `Chemclaw3` — nothing bounds the scratchpad memory store: agent-writable, no size cap,
      no window, no clock; a looping `remember` is the runaway. Decide a per-actor row cap in the
      writer's own transaction (the `ingest/rejections.py` shape) or an explicit accepted-unbounded
      posture — and change `retention._NOT_PRUNED["store"]` in the same commit.
- [ ] W23.5 `Chemclaw3` — **six tables still say "nothing bounds it"** [M]. One decision per table,
      recorded.
- [ ] W23.6 `Chemclaw3` — nothing bounds what a helper writes into its caller's checkpointed state.
- [x] W23.7 `Chemclaw3` — a timed-out parse still runs to completion on the worker thread [L]:
      the wall clock frees the caller, not the CPU. **Done** — and the row understated it. The pod
      *was* bounded (`_ParseSlots` shed the third upload in 2.00 s); the defect is that a slot is
      released only when its thread finishes, so two non-terminating parses took the replica's
      upload path down **permanently** — driven, `in_flight` stayed at 2 five seconds after both
      callers were freed and every later upload was shed. The parse now runs in a forkserver child
      the thread `SIGKILL`s on the deadline (10 ms warm, vs 0.97 s for a fresh interpreter).
      Driving it found `netguard._host_of` refusing the forkserver's own `AF_UNIX` socket as egress
      while its docstring claimed local IPC was exempt.
      `D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica`.
- [ ] W23.8 `Chemclaw3` — `BoCampaignWorkflow` runs four sequential activities under a ceiling that
      funds one.

**Acceptance** — a driven saturation probe per ceiling: N+1 concurrent sessions/calls refused
promptly (not queued), the refusal counted, and the pod's RSS bounded across the probe.

## W24 — The durable layer: races, lock order, loop teardown

Defects that only appear under concurrency, on the layer a production deployment runs continuously.
Postgres and Temporal are up in this environment, so every one of these is drivable.

- [ ] W24.1 `Chemclaw3` — the detached settle of a cancelled `AwaitAnswerWorkflow` is racy [M].
- [ ] W24.2 `Chemclaw3` — a nested `asyncio.run` inside a pooled process can hang on loop teardown.
- [ ] W24.3 `Chemclaw3` — `delete_session` and the owner prune take two rows in opposite orders: a
      deadlock by lock ordering.
- [ ] W24.4 `Chemclaw3` — the checkpoint sweep and a live turn are two writers and only the read
      side notices.
- [ ] W24.5 `Chemclaw3` — the awaiting collapse keeps the oldest frame of each state, not the newest.
- [ ] W24.6 `Chemclaw3` — a legitimate re-ask of an answered question fails loudly rather than
      waiting blind.
- [ ] W24.7 `Chemclaw3` — a result sink on the primary server opens connections no budget counts.
- [ ] W24.8 `Chemclaw3` — settle `pytest-xdist` on a real runner [S]. A 24-minute suite is why R6
      gets skipped; this is the wave that can afford it.

**Acceptance** — each race driven to failure on the pre-fix code and to green on the post-fix code,
in the same test. A race fixed without a reproduction is a race that was not understood.

## W25 — Readiness, health, and what an operator can see

A production deployment is judged on what it reports when something breaks. Seven rows say it
currently reports health.

- [ ] W25.1 `Chemclaw3` — `/readyz` cannot bound a Postgres that accepts the socket and stops
      answering.
- [ ] W25.2 `Chemclaw3` — neither net sees one Postgres server that two DSNs spell differently [M].
- [ ] W25.3 `Chemclaw3` — three pool gauges read three different instants of one scrape.
- [ ] W25.4 `Chemclaw3` — a front door scaled to zero renders a release in which every pod refuses
      to start.
- [ ] W25.5 `Chemclaw3` — the background worker is a singleton with no PDB, **and the PDB is not the
      fix**; and a worker rollout that never becomes Ready is invisible until someone looks.
- [ ] W25.6 `Chemclaw3` — a stalled append-only feed has no first-party signal (`corpus_cursors`).
- [ ] W25.7 `Chemclaw3-mcp` — three `except Exception` swallows in `rxnlabel`
      (`engine/mapping.py:52,134`, `engine/naming.py:73`) turn a torch OOM, a corrupt weight file and
      an `EgressForbidden` into "this reaction could not be named", with **no counter and no test**.
      Same shape in `rxnpredict/engine/predictors/__init__.py:92` — a silently degraded ensemble.
      A degradation that is not counted is a degradation nobody sees.
- [ ] W25.8 `Chemclaw3-mcp` — join `engine/readiness.py` to the degradation paths by a test: today a
      broken image can start, pass the probe and serve degraded.
- [ ] W25.9 Install `helm` + `kubeconform` (R9) and make `make helm-validate` part of the local gate
      for the rest of this effort.

**Acceptance** — per signal, break the thing and show the signal move: pause Postgres mid-query;
kill a weight file; scale the front door to zero and render the chart.

## W26 — Data integrity, provenance and retraction

The answers a chemist acts on. Each row here is a way the record can be right and the answer wrong.

- [ ] W26.1 `Chemclaw3` — a retracted ELN entry stays current evidence; closing it is a five-part
      change [M]. The highest-consequence correctness row on the queue.
- [ ] W26.2 `Chemclaw3` — the fingerprint index is keyed by source and the citation is not, so two
      sources collapse.
- [ ] W26.3 `Chemclaw3` — structure identity is canonical SMILES and nothing else: no InChI, no
      InChIKey [M].
- [ ] W26.4 `Chemclaw3` — a published calculation names no reaction, note or compound context [M].
- [ ] W26.5 `Chemclaw3` — `_quote_supports` cannot tell whether the figure a quote carries is about
      *this* slot.
- [ ] W26.6 `Chemclaw3` — knowledge writes serialise cluster-wide on one advisory lock [M] (a
      correctness-adjacent throughput bound on the one write path, `kg/record.py`).
- [ ] W26.7 `Chemclaw3-mcp` — `rxnpredict/engine/cache.py:48,56` falls back to **raw caller text** as
      a cache key when RDKit refuses canonicalisation: two spellings of one molecule, two rows, and
      an unvalidated key.
- [ ] W26.8 `Chemclaw3-mcp` — the `rxno_id` named-reaction → ontology table is unaudited. Validate it
      against itself, the `servers/props/tests/test_dataset.py` pattern.

**Acceptance** — each as a behavioural test over real Postgres: retract an entry and show it leaves
the evidence set; ingest one structure under two spellings and show one row.

## W27 — Answer honesty: retrieval, and the gates that score it

Three of these gates currently score literals written in their own fixtures, which means they cannot
fail. That is worse than no gate, because it reports green.

- [ ] W27.1 `Chemclaw3` — the two eval gates score literals written in their own case files [M].
- [ ] W27.2 `Chemclaw3` — `turn_cost_ratio` scores a fixture, not the system: the 32% prefix growth
      `tests/test_context_floor.py` caught would leave its `baseline.json` row untouched. **Unblocked
      now**: `API-KEY` is present in this environment, so a live lane can persist real `TurnCost`
      rows and the case can be fed from them. Run it while the credential exists.
- [ ] W27.3 `Chemclaw3` — the 44 labelled (query, note) pairs in `knowledge.yaml` are unreadable as
      data because `Probe` is `extra="forbid"` [M]. Closing this also closes the `DEFERRED.md` row
      whose parenthetical rested on them.
- [ ] W27.4 `Chemclaw3` — RRF's premise is independent rankers and this system has correlated ones.
- [ ] W27.5 `Chemclaw3` — `make kg-validate`'s two store-backed arms have no input in the shipped
      corpus: two arms of a validator that cannot fail.
- [ ] W27.6 `Chemclaw3` — half the probe corpus tests one tool [S] (the concentration half).
- [ ] W27.7 `Chemclaw3` — no external benchmark has ever been run [M]. `make eval` gates 23 metric
      values over 15 cases, all first-party. Decide: run one, or record in `DEFERRED.md` with its
      trigger. Do not leave it implied.
- [ ] W27.8 `Chemclaw3-mcp` — add ruff `S` (flake8-bandit) + `ASYNC` + a coverage floor. The fleet
      selects `E,F,I,UP,B,SIM,RUF` and has **no coverage measurement anywhere**; `S` mechanically
      surfaces W22.7 and W26.7.

**Acceptance** — mutate the thing each gate scores and show the gate go red. A gate that stays green
under a deliberate regression is the finding, not the test.

## W28 — Cost and scale: the prefix, the O(corpus) read, the write volume

What decides whether the system is affordable and whether it survives a real corpus.

- [ ] W28.1 `Chemclaw3` — the `default` profile carries eleven names it could narrow, worth 5,787
      tokens [M]; and a tool schema is 38% developer rationale, shipped on every turn.
      Both move `tests/test_context_floor.py` — re-baseline in the same commit, never raise the
      ceiling to accommodate prose (`tasks/lessons.md`).
- [ ] W28.2 `Chemclaw3` — a memory run reads every source whole, three times [M].
- [ ] W28.3 `Chemclaw3` — the `stated`-quote ambient reads the whole table's tail on every turn once
      a database has history.
- [ ] W28.4 `Chemclaw3` — the checkpointer's write volume is quadratic in a thread's length [L].
      Decide: fix, or `DEFERRED.md` with a measured trigger.
- [ ] W28.5 `Chemclaw3` — a note write costs ~1.8 s and a backfill is one write per record; a real
      first sync is days. A backfill and an incremental sync want different write shapes.
- [ ] W28.6 `Chemclaw3` — nothing has measured how many rows a real corpus produces [M]. Measure it;
      it is the input to W23.5 and W28.4.
- [ ] W28.7 `Chemclaw3_ui` — the SMILES parse blocks the main thread (~0.3 s parse + ~1.7 s draw);
      the 600-char cap bounds the unrecoverable failure, not the slow one. Move it to a worker
      (`ISSUES.md` known gap (e)).
- [ ] W28.8 `Chemclaw3_ui` — `ISSUES.md` Issue 6: `MAX_JOB_STREAMS = 3` fits one tab and two tabs
      429. BroadcastChannel leader election, built properly — it was filed rather than half-built
      because a botched election loses notifications.

**Acceptance** — a before/after number for every item, from the same script, in the PR body.

## W29 — Supply chain and delivery: gates that actually run on the bytes that ship

The fleet's supply-chain gate proves a property of `uv.lock` and **not** of any shipped image. Two
of three repositories ship through a Jenkins pipeline that can skip its own gate.

- [ ] W29.1 `Chemclaw3-mcp` — **image drift is unaudited**: no Containerfile reads `uv.lock`; all
      seven re-resolve with pip, and 11 of 100 packages differ for `rxnpredict` with `pandas` off by
      a major version. `uv sync --frozen` or `--require-hashes` in the images. The fleet's own
      largest self-declared open item.
- [ ] W29.2 `Chemclaw3-mcp` — eight permanent `--ignore-vuln` suppressions with **no expiry
      mechanism**: the Makefile says in as many words that nothing goes red when a fix ships. Give
      each a version assertion against the lock, so a shipped fix turns the suppression red.
- [ ] W29.3 `Chemclaw3-mcp` — `Jenkinsfile:32` `RUN_GATE` defaults **false**: images are built and
      published from revisions whose `make check` never ran in that pipeline, and nothing verifies
      Actions was green for `env.REVISION`.
- [ ] W29.4 `Chemclaw3-mcp` — `ci.yml:70` duplicates the lint command inline instead of calling
      `make lint` — the *exact* defect the comment above it says was found and fixed for `make type`.
      And `ci.yml:142` re-runs a strict subset of `ci.yml:80`.
- [ ] W29.5 `Chemclaw3` — the image vulnerability scan is not merged as a gate [M], and the
      runbook's claim about it is false. Turn it back on with its contradiction resolved.
- [ ] W29.6 `Chemclaw3` — two of the four deployables have no chart, so a release changes their bytes
      and nothing renders them.
- [ ] W29.7 `Chemclaw3` — the note reindex prunes a shared index against one pod's disk [M].
- [ ] W29.8 `Chemclaw3_ui` — **the gate-reproducibility gap**: CI steps ④⑪⑫⑬ and the whole
      `container` job are inline shell with no npm script, so they cannot be run locally; `npm run
      smoke` and `npm run check:openapi` are wired into nothing; and `Jenkinsfile` is a second,
      narrower gate that omits `npm audit`, contrast and e2e. One `npm run ci`, one gate definition,
      both pipelines calling it.
- [ ] W29.9 `Chemclaw3` — snapshot refresh has no named owner or cadence in any fleet server README
      (`MODULES.md` open question (c)). Assign or record the posture.
- [ ] W29.10 `Chemclaw3` — **the dependency gate and GitHub disagree, and the gate's own config file
      says otherwise.** Measured 2026-09-12: `make deps-audit` reports no known vulnerabilities over
      both the production closure and the full one (212 and 246 packages), while a push to this
      repository returns `GitHub found 7 vulnerabilities on ... default branch (1 high, 6 moderate)`.
      `--no-dev` is *not* the gap — both arms were run. `.github/dependabot.yml` declares two
      ecosystems, `uv` **and `github-actions`**, and the actions are SHA-pinned while nothing in
      `make ci` audits them; that same file's header asserts "the pipeline already *detects* a
      vulnerable closure — `make deps-audit` runs `pip-audit` against `uv.lock`, blocking, in both
      workflows", which is true of Python and silent about the ecosystem the file adds an updater for
      twelve lines later. Determine which ecosystem holds the seven, then either widen the gate or
      correct the claim. Anchors: `Makefile::deps-audit`, `.github/dependabot.yml`,
      `.github/workflows/image.yml`.

**Acceptance** — build one fleet image and diff its resolved packages against `uv.lock` (expect
zero); flip a suppressed advisory's pinned version and show the gate go red; run the UI's new `npm
run ci` locally and show it covers what `ci.yml` runs.

## W30 — Cross-repo contracts, and the production-readiness sign-off

The last wave closes the seams between repositories — the only place a defect can hide from all
three suites at once — and then states, with evidence, what a deployment team is getting.

- [ ] W30.1 `Chemclaw3` + `Chemclaw3_ui` — nothing checks the client half of a wire contract, and it
      has drifted twice [L]. This is the row that justifies the wave.
- [ ] W30.2 `Chemclaw3` + `Chemclaw3_ui` — the `note_proposed` SSE event is not a proposal and the
      name is a two-repo contract. Rename across both, in the order a deployment can survive.
- [ ] W30.3 `Chemclaw3` — `propose_report` proposes nothing and the string is a **registered Temporal
      activity name**: register both for one deployment cycle, drop the old one after the queue
      drains. A release procedure, and the new ADR has to say what it now names.
- [ ] W30.4 `Chemclaw3` — the labelling client is the one MCP leg with no identity or trace on the
      wire (`ingest/labels/labeller.py:216`); closing it means deciding where identity stamping for a
      **non-connector** MCP client belongs, which is a layering decision.
- [ ] W30.5 `Chemclaw3` + `Chemclaw3-mcp` — run `tests/test_sibling_manifest_agreement.py` and
      `tests/siblings.py` against a real sibling checkout (both are present in this environment) and
      report what they *actually* compare, including the `calc` seam's hardcoded tool names that no
      manifest covers in either direction.
- [ ] W30.6 `Chemclaw3_ui` — decide `ISSUES.md` Issue 5 (`/s/:sessionId` implies sharing and is not
      shareable) and Issue 8 (token in `sessionStorage`; the BFF cookie design exists on PR #11 and
      is blocked on tenant admin, not code). **Do not re-derive Issue 8** — record the posture.
- [ ] W30.7 All three — the full four-repo live lane: `make live-infra`, `make live-up`,
      `make live-probes` with the `API-KEY`→`CHEMCLAW_LLM_API_KEY` mapping beside a gateway, and
      `infra/live/e2e-full-stack/up.sh` across all four checkouts. This is the only step that
      exercises the system as a deployment runs it.
- [ ] W30.8 All three — **the production-readiness record**: one ADR per repo stating what is
      enforced, what is bounded, what is measured, and what is explicitly accepted as unbounded or
      unproven, each clause naming the test that holds it. Every remaining row moves to
      `DEFERRED.md` with a trigger or stays in `BACKLOG.md`; nothing is left implied.

**Acceptance** — a green four-repo live run with the probe set answered, and a readiness ADR in each
repository in which every claim names a test.

---

## Review

*(Filled in per wave as it closes — one short section each: what was planned, what the measurement
changed, what Half B found, and what is left. Empty until W21 merges.)*

### W21 — pre-merge review of PR #350 (three fresh-context reviewers, fixes applied before merge)

**Planned:** land the egress/gateway wave. **What the review changed:** eight code findings and six
prose ones, all fixed on the branch so `main` never carries them.

- **The compiled layer's DNS exemption was open at four entry points and the C header denied it.**
  `getaddrinfo` was interposed; `gethostbyname`, `gethostbyname2` and both `_r` forms resolved an
  off-allowlist name with the counter flat and nothing logged, so a name was a live exfiltration
  channel through the port-53 exemption. The whole family shares one `check_name` now, refusing
  exactly as glibc was *measured* answering NXDOMAIN. `res_query`/`res_search` and the
  `dlopen`/`dlsym` path are named in the uncovered list rather than chased; `sendmmsg` came off that
  list because it was measured sending.
- **Three shipped chart Jobs never reached the entrypoint**, so none carried `LD_PRELOAD` — the
  Schedules Job runs on every `helm upgrade` and dials Temporal over gRPC. They are components now,
  and the new test *derives* the bypassing set from the templates instead of listing it.
- **`is_loopback_host` missed five spellings that all reach loopback** (`127.1`, `2130706433`,
  `0x7f.1`, `0177.1`, and the unspecified address as a destination). `core/http.parse_host` answers
  every spelling `connect(2)` accepts; the unspecified address stays out of the shared predicate,
  because a bind and a destination disagree about exactly that one.
- **Both "guard disarmed" alerts read `max(...) < 1`** and so could not fire for a single disarmed
  pod. `promtool test rules` over a two-pod series is the regression.
- **`_bypass_ambient_proxy` raced on `os.environ`** — 1 of 5 hosts retained under concurrency, and
  the loser's key set then comes from the proxy. One lock.
- **Prose:** the repository's own quickstart did not boot after the bind exemption was retired
  (`README.md`, `api/README.md`); the gateway ADR's "seven sentences, all corrected" was itself the
  eighth; `ARCHITECTURE.md` stated a count of one over three; the runbook gave dead advice about a
  collector and split the counters three ways over a code that splits them two; three files said
  "three arms" against a four-arm table; and two ADRs cited branch SHAs a squash merge strands —
  `tests/test_decision_log.py` now asks `git merge-base --is-ancestor` about any commit an ADR cites.

**What is left:** `test_an_ipv4_mapped_address_is_not_a_way_around_the_check` still skips where
`AF_INET6` cannot be created, which is this sandbox — the unwrapping is measured by a reviewer's
harness and by no ratchet here. No local lane builds the `.so`, which `infra/README.md` and the ADR
now say out loud rather than conceding generically.
