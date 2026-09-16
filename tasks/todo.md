# A probe corpus that can name a tool the fleet serves

Closes `docs/planning/BACKLOG.md`'s row *"A bundle declared only in the fleet reaches an agent
surface with no probe covering it"*.

## The problem, stated as the two tests that disagree

`tests/test_probe_coverage.py` holds the corpus against `available_tool_names()`, which reads
`CHEMCLAW_CONNECTORS_DIR`. So the corpus is caught between two assertions pointing in opposite
environment directions:

| test | fails when |
| --- | --- |
| `test_every_agent_callable_tool_is_probed_or_exempt` (L88) | the fleet **is** mounted — 121 tools, 7 unprobed |
| `test_no_probe_expects_a_tool_that_does_not_exist` (L99) | the fleet is **not** mounted and a probe names one of its tools |

There is no bucket a configuration-dependent probe can sit in today, so three servers shipped this
session (`thermalsafety`, `suitability`, `kinetics`) are unmeasurable by the corpus in every lane.

## Why not the other fix

Declaring the three as in-tree bundles was measured and rejected: 11,624 tokens over 20 tools
(3,764 + 4,471 + 3,389) would move from `FLEET_PUBLISHED_ALLOWANCE` — a bound on a lane nobody
deploys — into `PREFIX_BOUND`, which both compaction defaults derive from, for every deployment.
`tests/test_context_floor.py` records that refusal three times already.

## Plan

- [x] 1. `tests/siblings.py` — `fleet_published_tool_names(root)`: parse each published manifest's
      `endpoint.tools`. The cheap tier (`sibling_root`, YAML off disk), not `sibling_python`.
- [x] 2. `src/chemclaw/evals/probe.py` — `Probe.needs_bundle: str | None`. `Probe` is
      `extra="forbid"`, so a YAML-only change is impossible; this is the declaration that a probe's
      tool expectations are conditional on a bundle being bound.
- [x] 3. `src/chemclaw/evals/live.py` — apply `expects_tools` only when `needs_bundle` is bound.
      A probe whose bundle is absent degrades to its bucket-C form: no tool expected.
- [x] 4. `tests/test_probe_coverage.py` — a probe may name a tool off the local surface **iff** it
      declares `needs_bundle`; verify the pairing against the fleet's own manifests when a sibling
      checkout exists, and skip with `SIBLING_SKIP` (counted by `conftest._report_sibling_skips`)
      when it does not.
- [x] 5. Re-bucket the probes that this unblocks: pc-01, pc-04, pc-05, pc-06, pc-08, pc-09.
- [x] 6. ADR + delete the BACKLOG row + `make lint type test`.

## What the probes actually need, checked rather than assumed

Most of these **under-specify the tool's inputs**, so a mechanical re-bucket would be wrong:

| probe | tool | the question supplies |
| --- | --- | --- |
| pc-06 | `oxygen_balance_screen` | a SMILES — and that tool takes a **molecular formula and refuses a SMILES**, so the answer composes `resolve_compound` (declared here) with it. `CC(=O)Oc1ccc([N+](=O)[O-])cc1[N+](=O)[O-]` is C8H6N2O6. The one clean cross-repo composition in the set. |
| pc-05 | `heat_removal_capacity` | area and jacket temperature, **not** U — so the answer states the assumption or asks |
| pc-09 | `continuous_reactor_conversion` | a residence time, **no rate constant** |
| pc-08 | `semibatch_accumulation_profile` | a dose time and temperature, **no rate constant or volumes** |
| pc-01 / pc-04 | `adiabatic_temperature_rise`, `mtsr`, `tmr_ad` | no calorimetry at all — and that server supplies **no default for a number that carries the safety argument**, so asking for the DSC/ARC numbers is the correct answer |

So these are bucket **B**, and `expects_tools` is any-of (`live.py:540`), which lets "called the tool"
and "asked for what the tool needs" both count where that is genuinely right.

## The property that makes the claims lane-independent

`gr-25`'s wording — *"a limit recalled from memory rather than looked up"* — is correct in **both**
lanes: with no tool bound, any number is necessarily recalled and so already forbidden. So only the
tool *expectation* needs gating, never `forbids_claims`. Same for an alert.

## Review

**Done, and the shape changed twice under measurement.**

1. `tests/siblings.py::fleet_published_tool_names` — the cheap tier, YAML off a shallow clone.
   Driven: 48 tools across 8 published bundles.
2. `Probe.needs_bundle` — a field, because `extra="forbid"` made a YAML convention impossible, which
   is the right outcome: the declaration is checked rather than written in prose.
3. `live._tool_expectation_applies` — reads the surface *and* `capability_degraded`. The `evals -> agent`
   import edge already existed for the live judge's TLS clients; its recorded reason said that was
   the only thing in `evals` needing it, so the reason was extended rather than a second edge added.
4. `tests/test_probe_coverage.py` — the phantom check forgives per name and only off-surface.
5. pc-06 and an-04.
6. ADR, ledger, BACKLOG row deleted, `make type` clean.

**Two things I had wrong in the first pass and fixed under measurement.**

*The helper was not surface-aware.* `_fleet_expected_tools()` first returned every name on a
`needs_bundle:` probe, which would have exempted an-04's `predict_pka` — an in-process tool — from
the phantom check. A probe may legitimately mix fleet and local tools, so the forgiveness has to be
per name. The first draft also carried a test asserting no `needs_bundle:` probe may name a local
tool, which is the same mistake as an assertion: it would have forbidden an-04 outright. Deleted.

*The re-bucket was going to be mechanical and would have been wrong.* Five of the seven probes the
three servers touch **under-specify the tool's inputs** — pc-05 gives no U, pc-08/pc-09 no rate
constant, pc-01/pc-04 no calorimetry at all. Expecting the tool there would have rewarded the exact
shape pc-05's old direction warns about: *an assumed coefficient with a computed answer is the most
dangerous shape this question has*. Only pc-06 and an-04 supply what their tool takes, so only those
two ship.

**Both guards driven against their own defect** rather than asserted: a one-letter typo in
`oxygen_balance_screen` fails the pairing test naming `thermalsafety::oxygen_balance_screeen`, and
deleting the `needs_bundle:` line makes the same name a phantom again. The scoring test has a middle
arm asserting a *bound* uncalled tool is still a miss, so a gate stuck at `False` fails rather than
silently stops measuring.

**The gate found a third copy of the invariant, which is why running it mattered.**
`tests/test_live_probes.py::test_every_expected_tool_in_the_shipped_corpus_exists_on_the_agent_surface`
asserts the same rule over the live runner's own loader, and went red on pc-06. It had already
drifted before this change: `load_probes` does not recurse, so it covered 336 probes to the other's
338. The exemption now has one definition and that file imports it; the loader gap is named rather
than merged away. Driven both ways — deleting pc-06's `needs_bundle:` fails both assertions.

**What this deliberately does not do.** It does not declare the three bundles — measured at 11,624
tokens over 20 tools into `PREFIX_BOUND`, which both compaction defaults derive from — and it does
not touch the five under-specified probes, which want "name the tool, ask for its inputs" and need
no new machinery to say so.
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

### W24 review (Chemclaw3, plus the W22 follow-up that outranked it)

**Planned:** a W22 privilege escalation, then eight durable-layer races. **What the measurement
changed:** five of the nine rows were wrong about something load-bearing, and in four of those the
row's own proposed fix was the wrong one.

- **The W22 escalation was real and the gate was the wrong place to look for it.** `enforce_plan_approval`
  reads the stamped scope and never the live plan, which is correct and is *why* the hole stayed open:
  the scope is stamped by reading the **live** plan once the 409 freshness guard has matched, and that
  guard compared an identity over step *text*. Driven through the real route, a plan shown as
  authorizing nothing came back authorizing `record_knowledge_note` and `watch_for` on the chemist's
  own hash. The identity now covers the declaration; `status` still does not.
- **W24.1's mechanism was not the one the row named.** The dispatch race did not reproduce — 0 of 78
  settles lost once the cancellation arrived where the clause could see it — so `asyncio.shield`, the
  "cheap arm", is declined. Three uncovered windows did lose settles, and the worst of them is not a
  lost settle at all: `notify_session_best_effort` caught `ActivityError(cause=CancelledError)` and
  carried on, leaving the wait **RUNNING** on its seven-day timer 30 s after its parent was terminated.
- **W24.3's victim is a coin flip.** 16 of 16 deadlocks, the route losing 9 and the prune 7 — so
  "self-healing on the retention side" covered half the occurrences and the other half was a 500 with
  no retry behind it.
- **W24.4's central claim was already retracted in the code**, and the brief's replacement assertion
  was itself half wrong: the psycopg half *is* pinned, with a control arm. What was unheld is that
  `aput` still *uses* the pipeline.
- **W24.8's two premises about the machine and the CI target both needed correcting** (same-shape
  box; CI runs `make cov`), and its speed claim holds at 1.92x and 2.20x — but **the row's four
  predicted timing failures were real and I dismissed them on two lucky runs**; see Half B below.

**What Half B found — about my own work:** two things, both worth keeping.

1. My first reproduction of the W24.3 deadlock **passed six times out of six with the subject broken**.
   It raced the cycle and asserted the retry in one test, and released the other side before the delete
   had taken any lock, so no cycle ever formed. The fix was to split the two: race the cycle (asserting
   *exactly one* side aborts, so two commits fail rather than pass silently) and inject the abort.
2. Two mutation attempts in W24.6 were **invalid SQL** and reddened thirteen tests for the wrong
   reason. Thirteen red lines look exactly like success, which is `tasks/lessons.md`'s
   "a mutation that did not apply reads like a mutation that survived" in its other form — the
   mutation applied and broke the fixture rather than the subject.
3. **I wrote an ADR claiming a parallel run's failure set was "identical", on two runs, and changed
   `make test`'s default on the strength of it.** The verification run taken *because* the default had
   changed failed two extra tests, one of them on the list of four the brief predicted and that ADR
   dismissed. Five runs put the rate at 2-in-5 and 1-in-5. The ADR is renamed and rewritten around the
   real finding, the default is back to serial, and the lesson is that a claim about a *set* being
   stable carries its repetition count or it is not a claim.

**What is left:** the `due_at` reaper (a `BACKLOG.md` row, now covering terminate-without-cancel and
worker loss rather than ordinary operation); the unbounded `tools` declaration and the 0/49 ratchet's
argument-driven blind spot (two new rows, each with its own measurement); and `-n auto` on a high-core
box, which nothing here measured. The W22 item's durable/Temporal and connector-job tool bodies were
not driven on a real worker under the plan gate.

---

## Wave A — a helper that can actually research (2026-09-15)

### What changed the plan

The plan was a six-name specialist roster from `data/profiles/*.yaml`. Measured what
survives `helper_profile`'s narrowing per profile, and four of six held **nothing**:

| profile | declares | in-process | survives |
| --- | --- | --- | --- |
| computation | 41 | 2 | 1 |
| design | 8 | 2 | 1 |
| evidence | 15 | 8 | 7 |
| property-lookup | 5 | 1 | 0 |
| reporting | 8 | 8 | 3 |
| safety | 6 | 1 | 0 |

Their whole point is connector tools, and a helper reaches no connector. So the roster was
gated on the connector bound, not on the roster design.

### The bound, driven rather than argued

`tests/test_subagents.py::test_a_helper_holds_no_connector_tool` rests on "two concurrent
readers of one MCP tool object deadlock". Driven against two real `Chemclaw3-mcp` servers
on loopback, over **one** `HeldConnectorSession`:

- `props.solvent_properties`, 2/4/8/16/32 concurrent: 42 / 55 / 94 / 197 / 348 ms, **0 errors**.
- `pyexec.run_python` at 1.88 s per call, 4 concurrent: **1.99 s wall** — fully overlapped,
  0 errors. The server's own log shows three ~1.9 s requests completing within 60 ms of
  each other on one session.
- A call that fails mid-flight beside a fast one: both resolve correctly and the session
  answers a third call afterwards.

So the deadlock claim is false for this shape. The second half of the bound —
misattribution in the connector's log — does not reach a helper either: identity is bound
from the ambient context when the **session** opens (`core/call_identity.py`), and a helper
is the same actor, session and correlation id by
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`. The headers are correct.

And the lifecycle argument of
`D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock`
never applied to *sharing*: the caller's tools are already open when `_subagents` runs.
Sharing costs **zero** extra sockets, which is better than either alternative that ADR weighed.

### Steps

- [ ] `_subagents` takes `connectors` and passes it to the helper's build.
- [ ] The helper's connector half is narrowed by `side_effecting_tools()` beside the
      in-process narrowing, so one switch (`helper=True`) still carries the whole attenuation.
- [ ] `general_purpose_helper`'s description and `HELPER_BRIEF` stop saying a helper cannot
      call a connector — a model that believes a false bound wastes a turn learning otherwise.
- [ ] Replace `test_a_helper_holds_no_connector_tool` with one that proves the caller's
      read-only connector tools reach the helper and its state-changing ones do not.
- [ ] Count delegation (`chemclaw_subagent_spawns_total`). CLAUDE.md records that nothing
      counts how often `task` is called, and that absence is what left the roster question
      unanswerable twice.
- [ ] Re-measure the specialist table with connectors bound, and decide the roster on that
      number rather than on the one above.
- [ ] ADR + `tests/test_context_floor.py` re-run (the helper is a second graph, so the
      caller's ceiling should not move — assert it rather than assume it).

### Wave A — done

- [x] `_subagents` takes `connectors` and passes it to the helper's build.
- [x] `helper_connectors` narrows the connector half by `side_effecting_tools()`, beside
      `helper_profile`'s in-process narrowing — one switch, two halves, one set.
- [x] `general_purpose_helper`'s description and `HELPER_BRIEF` corrected.
- [x] `test_a_helper_holds_no_connector_tool` replaced by
      `test_a_helper_holds_its_callers_reading_connectors_and_none_that_act`, **driven red on all
      three edits it claims to catch** (the call site, the pass-down, the narrowing) before being
      believed. Its acting name is derived from `side_effecting_tools()`, not transcribed.
- [x] The upstream closure read is declared in `tests/test_upstream_surface.py`.
- [x] Delegation counting: **already existed and three documents said it did not.** Driven,
      `chemclaw_tool_calls_total{outcome="ok",tool="task"} 1`. Pinned by
      `test_a_delegation_is_counted_where_every_other_tool_call_is`.
- [x] Cost stated: helper prefix **26,626** tokens against the caller's **66,316**. No second
      ceiling — a strict subset plus a smaller prompt is an inequality, asserted by
      `test_a_helpers_prefix_is_bounded_by_the_one_this_file_already_ratchets`. The caller's
      ceiling did not move.
- [x] ADR `D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened`, ledger row, topic
      row, `CLAUDE.md` paragraph, and the closed `BACKLOG.md` row deleted.

**Roster decision: not built.** Re-measured with connectors reachable, every profile now holds
something (`computation` 12, `evidence` 14, `design` 4, `safety` 4, `reporting` 3,
`property-lookup` 1). It stays unbuilt for the reason that did not change — a named partition is a
routing hypothesis, and six descriptions in `task`'s schema are paid on every model call of every
turn. What changed is that the *excuse* is gone: the spawn rate is on `/metrics`, so the next
argument about this has a number under it.

**Review.** The interesting part was not the code, it was that a bound restated twice had never
been run, and that one of its three supporting claims was false the day it was written. Two
sessions had corrected the *reason* for the bound without driving the bound. The lesson is already
in `tasks/lessons.md` in a weaker form; the sharper statement is that **correcting a justification
is not evidence about the thing it justifies**, and a correction that leaves the behaviour in place
should say explicitly which arm it did not run.

A second, unrelated finding worth keeping: a scratchpad script named `csv.py` shadowed the stdlib
module, so *any* python run from that directory executed it, and it overwrote
`src/chemclaw/protocols/export.py`. Four earlier sessions blamed subagents running
`git checkout -- <path>`. The real cause was a filename.

## Wave C — a campaign's suggestion becomes arms (2026-09-15)

**The gap was retyping, not judgment.** A BO suggestion is `Candidate.params` — a bare
`dict[str, float | str]` with no units, no level labels and no roles. `draft_experiment_protocol`
needs `Factor`s whose levels carry labels and structures, and `ProtocolArm`s citing those labels
exactly. Nothing connected the two, so the model read the candidate table and typed the arms out by
hand; a transposed value there is a different experiment with nobody able to see it.

- [x] `protocols/from_bo.py::factors_and_arms` — the pure translation. It lives in `protocols/`
      because `tests/test_layering.py` allows `protocols -> science` and allows neither
      `science -> protocols` nor `connectors -> protocols`; and it is the *right* side on the
      argument that file already makes — protocols reads prescriptive shapes, and a design space is
      one.
- [x] Four refusals rather than papering over: two parameters slugging to one `Factor.name`
      (silently merging makes a consistent design describing experiments nobody planned, and
      nothing downstream could see it); a parameter over 96 settings; runs that name or omit a
      parameter the problem does not (a `factor_levels_declared` blocker, later and worse); and a
      parameter the runs never vary — a **setpoint**, reported in `constants`, never dropped.
- [x] Repeats become `replicate_of` the first occurrence, which is what a screening design's centre
      points *are*.
- [x] One `_label()` formats both the factor's level and the arm that cites it, because
      `factor_levels_declared` matches them by string equality — `80` vs `80.0` fails a design that
      is correct.
- [x] `agent/protocol_design_tools.experiment_arms_from_campaign` makes it reachable, taking a
      `campaign_id` rather than an `OptimizationProblem` so the schema stays small.
- [x] Classified **read-only** in `authz`, explicitly — the plan gate lets a read run while a plan
      is still being built, and "what would this campaign's next experiments look like as a plate"
      has to be answerable before somebody approves drafting them.
- [x] Nine tests, four driven red by mutation (label formatting, replicate marking, structure
      carry-through, the constants split) before being believed.

**The cost was caught by the ratchet and paid down rather than waived.** The tool's first docstring
cost **626** tokens against 290 for `read_experiment_protocol` and 191 for
`find_experiment_protocols`, and pushed `tests/test_context_floor.py` 26 tokens over its ceiling.
Trimmed to **431** by moving the rationale into a comment, per
`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`. The ceiling did not move.

**What it deliberately does not do.** No `ProtocolBody` — the charge table, the steps, the
analytics and the hazards are judgment over the chemistry and stay the model's. And no units: an
`OptimizationProblem` carries none anywhere, and `quantities_are_plausible` reads *setpoints* rather
than a factor's levels, so nothing downstream catches a temperature factor whose 80 might be °C or
mol%. `notes` says so per parameter; it is the one gap this translation cannot close.

## Wave D — the two template bounds that did not bound (2026-09-15)

**Asked as an analysis of how well the agent composes multi-tool workflows, and it found the seam
sound where it is argued and two ceilings stated over paths that do not enforce them.** The
composition machinery itself is not missing: `chemclaw.templates` is a fixed sequence of
`tool`/`job`/`agent` steps run as one durable workflow behind a single `run_<name>` launcher, so
the model spends one tool call and the deterministic steps cost no model calls at all. What the
measurement found is that both of its bounds stop at a seam.

- [x] **An `agent` step's prompt is bounded at the model's edge** —
      `durable/template_activities.bounded_prompt`. `bound_tool_results` is an entry of
      `tool_call_middleware`; a `tool` step runs through `invoke_governed`, which folds
      `tool_governance_middleware` — right for that step, which has no model, and silently wrong
      for the next one, whose prompt interpolates the result and does. Measured: a payload a chat
      turn cuts to 60,000 characters reached the step's model at **245,700**, and unreclaimably,
      because both compaction edits are for history and a step is one `HumanMessage` with none.
- [x] **In the activity, not the sequencer** — a settings read in workflow code feeds an activity
      *argument*, which replay recomputes, so cutting there would fail a run on non-determinism
      rather than bound anything.
- [x] **`_notice` takes its remedy as a parameter.** Its last sentence assumes the model asked for
      the text; false for a prompt a template interpolated, where "narrow the question" sends it to
      re-fetch what it was already handed. `TOOL_REMEDY` / `STEP_REMEDY`, and the tool form is
      byte-identical to what it was.
- [x] **The run ceiling covers the procedure** — `agent/template_surface.run_ceiling_problems`,
      read by `make template-validate` *and* by `registry.unrunnable_reason`. The config validator
      could only require that one step fits, as its own docstring conceded; one `job` step is
      39,330 s against a run ceiling of 45,330 s, so two in one file miss by 33,330 s — silently,
      since an execution timeout is not delivered to workflow code and `_notify_failure` never
      runs. All nine shipped templates fit with 4,200 s of headroom, so this is latent, which is
      when a bound is worth adding.
- [x] `Settings.template_step_ceilings` — one definition, asked for the max by the config floor and
      for the sum by the gate. Identical arithmetic to what it replaced: `tool`/`agent` 900,
      `job` 39,330.
- [x] `chemclaw_template_prompt_truncated_total{template}`, because a cut nothing counts is the
      invisible kind. A separate counter from the tool one: the remedies differ — a tool's own
      ceiling versus a narrower step or a field path.

**Declined, each with the decision that already governs it**, and stated here because reading them
as missing parts is the easy mistake: parallel/fan-out steps
(`D-2026-08-25-the-loop-is-a-composite-not-a-template`), agent-authored templates
(`D-2026-08-12`'s plan-gate exemption holds *because* nothing at run time can create one), and
resume-from-failed-step — deferred on a measurement rather than a preference, since D-011 makes
most of a retry a cache hit and every shipped template's one `agent` step is last, so the step that
would be resumed is the step that failed. `BACKLOG.md` carries that row and its trigger.

### Review

`D-2026-09-15-a-bound-that-stops-at-the-seam-is-not-a-bound` records both defects, both
measurements and the three declines. Verified with the infrastructure up (`dockerd`, `make up`,
`make db-migrate`) rather than against a suite that would have skipped the Postgres-backed half.

## Wave E — the three declines, built (2026-09-15)

Wave D declined three things on merged decisions. Asked again for all three, so they are built —
and the two that collide with a decision are built so the decision still holds, rather than by
ignoring it.

### E1 — parallel steps, derived rather than declared

- [x] **This is not the thing D-2026-08-25 declined.** That ADR is about a *loop*: a fan-out over a
      collection whose size is known only at run time, which needs iteration and expressions and is
      why the loop lives in a composite. **Static parallelism is a different question** — which
      already-declared steps may run at the same time — and the template already answers it:
      `_step_references` is the dependency graph, and `_references_resolve_and_point_backwards`
      guarantees it is a DAG by refusing a forward reference. So no new YAML key: concurrency is
      *derived*, and a template that declares no dependency between two steps gets it for free.
- [x] Interaction with Wave D that must not be missed: `run_ceiling_problems` sums the step
      ceilings because steps were sequential. With parallelism the bound is the **critical path**.
      Sum is still correct-but-pessimistic; the fix is the longer path through the DAG.
- [x] Failure semantics: a sibling still running when one branch fails must be cancelled, not
      orphaned, and the failure record must name the step that actually failed.

### E2 — agent-authored workflows, without the escalation the exemption would grant

- [x] **The coupling is real and is closed rather than argued away.** `D-2026-08-12` exempts a
      template `agent` step from the plan gate *because* a template is human-authored and
      uncreatable at run time. So an agent-authored one **does not inherit that exemption**:
      `author_kind` is on the template, the exemption keys on `human`, and `write_tools` is refused
      outright on an agent-authored draft — the agent cannot grant itself a write path because the
      field is rejected at validation, not filtered at run time.
- [x] One tool, not one per draft: `run_composed_workflow(name, inputs)`. Generating a `run_<name>`
      launcher per draft would put an unbounded, agent-written schema into the prompt prefix, which
      `tests/test_context_floor.py` exists to prevent.
- [x] Same `Template` model, same `step_problems`, same `run_ceiling_problems` — a draft that would
      not pass `make template-validate` cannot be stored.
- [x] A draft may only name tools the composing actor is authorized for, checked at compose time
      *and* again at run time, where `_acting_as` already decides against the real requester.

### E3 — resume from a failed step

- [x] The completed steps are already recorded (`failed_template_record`) and never read. Read them.
- [x] Constraint that decides the shape: it is a database read, so it cannot be workflow code. An
      activity, whose result enters history and so keeps replay deterministic.
- [x] The guard that makes it safe: a run's id is `hash([name, inputs])` and the *template* is
      pinned per run, so an edited template relaunching under the same id must not resume against
      step results produced by the old definition. Resume only on an exact template match.

### Review

All three built. What changed against the plan, and why, because two of them did:

**E1 was the smaller change the plan thought it was, and the ceiling interaction was real.** The
wave runner started as `asyncio.wait(FIRST_EXCEPTION)` plus sibling cancellation and was reverted to
the house `gather(return_exceptions=True)`: `asyncio.wait` appears nowhere in this tree's workflow
code, a cancellation inside an activity arrives as `ActivityError(cause=CancelledError)` rather than
`asyncio.CancelledError`, and a cancel is itself a command, so the failure path would issue a
different number of them depending on which branch lost. Two of the nine shipped templates turned
out to already have independent steps.

**Nothing in the plan anticipated the replay control, and it was right to fire.**
`tests/test_workflow_replay.py` failed on both archived `TemplateWorkflow` histories: the resume
read and the wave schedule both change the command sequence, and the background worker deploys
`Recreate`, so the new generation inherits every unfinished run. Both are behind one
`workflow.patched("template-waves-and-resume")` marker, with the old path written in the new code's
own terms rather than kept as a second loop.

**E2's security design changed shape once the code was read.** The plan said "re-attach the plan
gate to an agent-authored template's steps". That is not a control: a step runs in an activity with
no session, so `enforce_plan_approval` would refuse *every* write for want of a plan nobody can
approve. The rule that ships instead restores `D-2026-08-12`'s premise rather than re-arguing its
conclusion — no side-effecting tool, no durable job, no `write_tools` — and the sharp case was not
the one the plan named: a `tool` step calling `record_knowledge_note` is a bigger hole than
`write_tools:`, because nothing in the chain asks whether a human saw the sequence.

**What it cost, measured**: 978 tokens of prefix against 34 tokens of headroom, so the ceiling rose
to 69,000 and the thread allowance fell 40,500 → 38,700. And a composed workflow cannot run a
calculation, which is a real limit and is stated in the tool's own description rather than
discovered.

### Verify

- [x] `make lint type test` green, with the skip count stated.
- [x] All ten validators pass.
- [x] Parallel: measured on wall clock against a real Temporal server — two independent 1s steps
      peak at 2 in flight and finish under 2s; a chained pair peaks at 1 and does not.
- [x] Authored: every refusal driven in both directions, both store backends.
- [x] Resume: a run resumes from a record, declines a record from a different template version, and
      a first run is unchanged.
- [x] Parallel: a template whose steps are independent runs them concurrently, measured, and one
      whose steps chain still runs in order.
- [x] Authored: a draft naming a write tool is refused; a draft's agent step is plan-gated.
- [x] Resume: a run that failed at step N re-runs only from N.

## Wave F — a composed workflow may launch a job, once a human has said so (2026-09-15)

`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction` refuses a `job` step in an
agent-composed workflow, and named the cost in the same breath: every durable job launcher is
state-changing, so the rankings and conformer searches these procedures exist to sequence are
exactly what a composed one may not contain. Asked to widen it.

**What is approved is one version of one workflow, not an actor.** The phrase that opened this was
"a standing per-actor approval for job launches", and per-actor is the broader reading and the wrong
one: it never lapses, so a workflow re-composed into something else inherits the permission granted
to what it used to be. The approval is keyed on `template_fingerprint(document)` — the same hash
resume already uses — so **re-composing lapses it automatically**, with no clearing logic to forget.
That is `plan_approvals`' own shape one layer up: the human approves a *plan hash*, and a rewritten
plan is a different key.

**The human must be the one who approves, and the agent must not be able to reach the path.** So it
is a REST write on the front door, not a tool. A tool would be the agent approving its own
composition, which is the whole trap — `agent/skill_backend.SkillsReadOnlyRefusal` refuses for the
same reason one seam over.

- [x] **F1** Migration: `composed_workflows` gains `approved_fingerprint`, `approved_by`,
      `approved_at`. Declared in all five registers the last wave learned about.
- [x] **F2** `composed.authored_problems` splits. The always-refused set stays exactly as it is —
      a side-effecting `tool` step and `write_tools` are refused *approved or not*, and that is the
      line: a `job` step is bounded compute the approver read in the document, while `write_tools`
      is a permission the model spends later on a call nobody has seen.
- [x] **F3** Composing a workflow with `job` steps is allowed and stored **unapproved**; running it
      is refused until an approval stands, with a refusal that says who must approve and how.
- [x] **F4** `POST /workflows/{name}/approval` — authenticated human, owner only, approves the
      fingerprint it is shown. Audited.
- [x] **F5** Checked at run time against the run's own deployment, as the existing rule is.

### Verify

- [x] A composed workflow with a `job` step: composes, refuses to run, runs after approval, and
      refuses again after it is re-composed.
- [x] No agent path can approve: asserted, not assumed.
- [x] `make lint type test` green with the infrastructure up, skip count stated.

### Review

Built as planned, with two corrections the recon forced and one the suite did.

**The audit is the row, not an `AuditEvent`.** The first version wrote one from the route; no route
in this tree does, because that trail is the tool-call middleware's and is shaped around a tool, an
outcome and a latency. A human decision is audited here by being a row naming who made it —
`plan_approvals.actor`, `pending_requests.answered_by`, `effects.approved_by` are each that shape,
and `approved_by`/`approved_at` join them.

**One approver and not two.** `api/routes/pending.py`'s `SECOND_PERSON_KINDS` forbids the requester
of an irreversible effect from approving it, and the question was whether that applies. It does not,
and the difference is not a convenience: there the requester is a human and the second human *is*
the control; here the composer is the agent, so the approver is already the second party. This is
`decide_plan`'s shape.

**The self-approval scan had to be narrowed from reads to writes.** It first flagged
`run_composed_workflow` for reading `approved_fingerprint` — which is the enforcement. A scan that
forbade the read would be asking the gate not to look at the thing it gates on.

Also worth recording because it cost time and was not mine:
`test_no_adr_cites_a_commit_a_squash_will_strand` fails on a **shallow** checkout, naming nine ADRs
nobody touched. `git fetch --unshallow` turns it green. Its sibling in the same file skips with that
exact reason; this one has no such guard.

## Wave G — four fresh-context reviews of everything waves D–F built (2026-09-16)

Three reviewers were run against the composed-workflow seam with Docker, Postgres and Temporal up,
each asked to drive rather than read; a fourth pass swept the declarations. Two ADRs record the
result: `D-2026-09-16-a-wave-costs-its-slowest-member-once-per-batch` and
`D-2026-09-16-an-approval-binds-to-the-version-that-was-shown-on-every-surface`.

**The security argument held.** Nothing broke the gate: the `unapproved_jobs` check is strictly
before `start_template_run` with no TOCTOU, the fingerprint covers every field including
`write_tools`, a step naming both a tool and a job produces a `JobStep` and drops the tool, both
composition tools are themselves side-effecting so no composed workflow can contain either, SQL
owner scoping is exact, and an irreversible effect still fails closed inside an approved job.

**What did not hold was the *surfaces*.** The same property was enforced on one path and merely
described on another, three times over:

- [x] The CLI approved "as it stands", argued from a premise the agent falsifies by acting in the
      same terminal under the same owner. Read-then-bind now, with the procedure rendered.
- [x] Two store backends disagreed about what a save may touch. Aligned on carrying an approval
      forward: the agent's write must not erase the record of a person's decision, and the lapse is
      the fingerprint's job. `GET` returns a derived `approved` so nothing renders a stale approver.
- [x] Only one of two launchers validated its inputs. Moved into `start_template_run`.
- [x] `run_ceiling_problems` sized a wave at one slow step however wide it is — 501 independent
      steps passed the ceiling. `_batches` bounds the wave and `ceil(width / limit)` sizes it, from
      one pinned number.
- [x] `authored_problems` failed open on an unrecognised step kind.
- [x] `approve` took an `approver` both callers satisfied with the owner, which left `leaver.py` a
      person-column its predicate could not reach. Parameter removed.
- [x] No delete: `DELETE /workflows/{name}` and `/forget-workflow`.
- [x] The cap's guard read its own page size; `list_for` fetches one past the cap.
- [x] `composed_workflows.session_id` had no reader in code; the approval screen is one now.
- [x] Eight stale present-tense claims, each falsified by its own pull request, including the
      plan-gate exemption argued from "nothing at run time can produce one" at the two places a
      reader would look, and probe `ws-19` scoring the shipped behaviour as a fabrication.
- [x] The wave scheduler and the resume path had no ADR at all, while the one merged decision that
      discusses them declines both.

### Review

**What the reviewers got wrong, and it matters.** One reported that a departing *approver* who is
not the owner is neither erased nor counted — a real gap in the shape of `note_proposals.decided_by`.
It is unreachable: both callers of `approve` resolve against the caller's own rows, so `approved_by`
can only be the owner. The finding was still worth acting on, because the *store* permitted the
state the write path cannot produce. Removing the parameter is what turns "unreached" into
"unrepresentable", and that is the difference the register test could not see.

**The measurement that changed a design.** The 501-step case looked like a missing
`MAX_COMPOSED_STEPS`. It is not: once a wave's cost stops assuming unbounded parallelism, the run
ceiling that was already there refuses it, and a second number would have been a magic one. Fixing
the arithmetic fixed the class; capping the document would have fixed the example.

**The process failure is in `tasks/lessons.md`.** A reviewer mutation-testing `template_job.py`
restored it from its own scratch copy over my edits, and when stopped mid-restore left
`if False:  # MUTATION` in the file. A subagent shares this working tree.
