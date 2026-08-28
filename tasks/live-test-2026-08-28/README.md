# Live e2e campaign — 2026-08-28

A multi-hour pass over the three-repo stack (`Chemclaw3` + `Chemclaw3-mcp` + `Chemclaw3_ui`) driven
entirely by `chemclaw.cli.mock_llm`. **No live LLM call is made anywhere in this campaign**, and the
claim is checked rather than asserted — see *Verification* below.

## Posture

`Chemclaw3_mock` is **not** in this session, so the lane runs its three-repo posture. The following
are **NOT RUN** — not passed, not skipped-green:

| Out of scope | Why |
| --- | --- |
| `mock-vendor` (`search_building_blocks`, `get_price`) | served by Chemclaw3_mock |
| the seeded ELN/ORD corpus and `make live-data` | its exports live in Chemclaw3_mock |
| the Entra-enforced posture | the mock tenant is Chemclaw3_mock's `app/entra/` |
| UI full-stack scenarios 4 and 5 | they assert against the two rows above |
| `make helm-validate` | no `helm`, `kubeconform` or `promtool` in this container |
| `make live-probes`, `make eval*`, `live-verifier-margin` | they grade answers, and a scripted model emits tool calls without *choosing* them — both arms would measure the script |

## Findings

Each was found by running the thing, root-caused, fixed with a test or a live re-measurement, and
merged. All four turned out to be one shape; see
`docs/decisions/D-2026-08-28-a-lane-primitive-must-verify-the-act-it-was-asked-for.md`.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | `restart-postgres` restarted nothing on a Docker lane while logging that it had; the storm's E3 check scored PASS over a bounce that never happened | compose branch for both Postgres verbs; the check now reads `pg_postmaster_start_time()` either side and fails if it did not move |
| 2 | The lane started five of six published fleet manifests; the front door refused to boot on the missing `pyexec` | the start list is derived from the manifests actually mounted |
| 3 | `processes.sh env`, the documented contract for a second shell, carried the minted credentials and not the resolved fleet checkout — every chaos restart died | the contract carries `CHEMCLAW_MCP_REPO` |
| 4 | The next restart killed the front door, declined to start it for want of a model posture the contract also omitted, printed "live stack up" and exited 0 | the contract carries the model posture; `restart <name>` asserts `<name>` came back |
| 5 | The F-family check *"a truncated argument document is reported, not swallowed"* could never pass: LangChain's `parse_partial_json` closes the string, so truncation never reaches the repair path — a fact `agent/model_calls.py` documents | split into `f-malformed-json` (genuinely unparseable, exercises the repair path) and `f-truncated-arguments` (pins that a cut document is completed and *run*). Family F now 9/9 |
| 6 | An unparseable tool call surviving its repair reached a metric and a log and **nothing on the chemist's stream** — `answered=False, tools_failed=[]`, a blank box with no cause | `_report_repair` publishes a `ToolFailureSignal` per unrepaired call, with `reason=None` (no gate refused it; it is a fault) |
| 7 | **Every durable calculation returned HTTP 401 behind a `/readyz` reporting `connectors_unhealthy: 0`** — `calc` is a backend, so the health probe structurally cannot see it. The contract carried the four fleet tokens it *minted* and dropped the four it *inherited*; `CHEMCLAW_CALC_MCP_TOKEN` was present and is a different variable one letter away | `fleet_token_vars` derives the set from the mounted manifests; verified with `env -i` + contract only + a real xTB job, 5/5 |
| 8 | **The soak ran five rounds, reported `24/24 checks passed` each time, and wrote nothing.** The whole Prometheus exposition went through the environment at an exec boundary; past `MAX_ARG_STRLEN` the exec fails and no line is written. The record is the only artefact a soak produces | both scrapes go to `mktemp` files; `json.dumps` still builds the line. Third distinct way this script has lost its own record |

### Check bugs found in the campaign's own instruments

Recorded separately because they are not product defects, and reporting them as such would have been
the campaign's worst possible output.

| Instrument | What it claimed | What was true |
| --- | --- | --- |
| Family `T` dry-run checks (×14) | `refused=0` — the dry-run control did not fire | The control fires. A refusal is *raised*, so it arrives as `tool_failed` and the error `ToolMessage` is suppressed; the check read `result_previews`, which is necessarily empty for a raised refusal. `TurnResult.failure_messages` added |
| My own reachability probe | "a default-profile turn reaches 17 of 99 tools; 83 unreachable" | The default profile advertises **91 of 99**. I parsed the tool list out of the model's error string, which is truncated at 300 characters without closing its bracket |
| UI fixture tier | 40 failures | A bare `npm run build` produces a client that refuses `AUTH_MODE=dev`; the failure mode is 40 × 30s `locator.click` timeouts with the cause buried in `[WebServer]` output. Rebuilt with the flag: 6 failed, 65 passed, 57s |
| Storm families A and E (first run) | noise 26%, lease 10.4s | Measured against a box a subagent was loading. Re-measured idle: still 21% noise — so the A rows are honest, and the campaign's own rule (never share the box with a storm) was violated twice before it stuck |

## Suite results

| Suite | Result |
| --- | --- |
| `Chemclaw3` full pytest, Postgres **and** Temporal up | 5,751 passed, 14 skipped (6 helm, 3 truncated git history, 2 no model credit) — the Postgres- and Temporal-gated files ran rather than skipping |
| `Chemclaw3-mcp` `make check` | 1,521 passed, 7 skipped; mypy clean over 120 source files; no known vulnerabilities |
| `Chemclaw3-mcp` `make offline-run` | 1,518 passed **with the network namespace removed** — the vendored corpora are provably sufficient |
| `Chemclaw3_ui` vitest | 772 passed over 75 files |
| `Chemclaw3_ui` Playwright fixture tier | 65 passed, 6 failed — all one pre-existing `transcript.spec.ts` assertion on `main` |
| `Chemclaw3_ui` Playwright mock-model tier (new) | 4 passed, 4 failed on first live run; failure 4 was finding #7 above |

## Runs

| Wave | Artefact | Verdict |
| --- | --- | --- |
| 1 · durable smoke | `live-jobs.log` | 5/5 — a real GFN2-xTB reaction energy through Temporal, cached, deduped, wedged-worker pending |
| 2 · storm, 8 families | `storm-wave2.md` | see the report |

## Verification

1. **Zero live LLM calls.** `GET :8820/__mock/stats` reconciled against the turn count, plus
   `ANTHROPIC_API_KEY` unset in every process the lane starts. Independently, this environment's
   `API-KEY` is present but its account has no credit (`invalid_request_error: credit balance is
   too low`), so a live call could not have succeeded even by accident.
2. **Coverage run vs planned.** The storm declares `FAMILIES` and names the difference itself; the
   NOT RUN table above is the rest.
3. **What drifted.** `.live/soak.jsonl` and `make live-soak-report`.

## Drift, over the first 8 recorded soak rounds

Readable only after finding #8 — before that fix the record was empty while every round reported
success. Full fit in `soak-report-r8.md`; `rounds with a non-zero exit: none`.

| Series | Round 1 | Round 8 | Fit |
| --- | ---: | ---: | --- |
| api RSS (KB) | 588,732 | 607,032 | **grows and slowing** — +5,414 then +1,202 KB/round (± 560). Decelerating is the caches-filling signature, not a leak; 8 rounds is a baseline, not a verdict |
| `chemclaw_pg_pool_size` / `_available` | 13 | 16 | grows and steady, max 48, `requests_waiting` flat at 0 |
| `calculation_results` rows | 15 | 15 | **flat across 279 turns** — D-011's "a persisted result is never recomputed", visible as a number |
| `session_turns` rows | 8 | 8 | flat, while `session_messages` grows +144/round — turn rows reclaimed, messages retained |
| `audit_events` rows | 2,937 | 3,539 | +86/round, steady |
| `turn_costs` rows | 1,435 | 1,659 | +32/round — one per turn started, as expected |
| turns started / failed / empty | 55 / 20 / 18 | 279 / 90 / 81 | +32 / +10 / +9 per round; the ~31% failure rate is families F and H doing their job |
| disk free | 20 GB | 20 GB | flat |

The container was reclaimed at round 8 and **the record survived** — which is the whole reason the
soak is checkpointed per round rather than held in a process.

## The parallel audit pass

Twelve agents, one per area, each in an isolated worktree, each required to produce a test that
**fails against current code** before fixing anything and to report what it could not prove. **All
twelve landed**; every branch is merged and every worktree reclaimed.

| Area | What it found |
| --- | --- |
| `Chemclaw3_ui` | The six `transcript.spec.ts` failures were **not** a windowing regression — the spec seeds `chemclaw3.chat.v2`, the persist key from before `chatStorageKey` partitioned storage per account, so under `AUTH_MODE=dev` the seed went to a slot nothing reads. Six pass with no `src/`, `server/` or `MessageList` change. Also: `/readyz` withholds connector names deliberately (it is unauthenticated), so scenario 8 could never pass in **either** live tier, and both closed with a `not.toContain('unreachable')` that is **vacuously true of a body naming no connector**. Plus two fields dropped in transit — `result_ref` (so a reload silently lost a tool result, the exact failure the backend added it to prevent) and `title`/`updated_at` (ten restored conversations rendering as ten identical "Earlier conversation" rows) |
| tool surface | **Refuted the backlog row this campaign filed.** No `calc` collision; 45 in-process + 31 connector tools bind; executable-but-not-accepted is **0**. The row rested on the truncated-error-string probe that had already been retracted here — the probe was withdrawn, the conclusion drawn from it was not. But the same function held a larger defect: `_validate` skipped argument checking for any name it could not resolve in-process, so the LOAD-1 guard covered **22 of 99** names, and `similar_molecules(query=…)` — LOAD-1's own argument name — was green-lit by the guard written to make LOAD-1 impossible. One `tool_signatures()` now serves both readers; coverage 22 → 53 |
| ingest / publish | Five backlog rows closed, two of which were one defect: the rejection ledger was written by the *fetch*, which can answer neither question a ledger row needs (measured 0.317/0.304/0.320 s per 5,000-entry fetch, 18–38% of the call, ≈1.7 h added to a 100k backfill). And a data-correctness bug shown against Postgres — a recomputed logD, **−1.8497 → +1.3492**, wrote **0 rows**, because the outbox identity hashed the request rather than the result, so a changed calculator's answer was dropped as a duplicate |

**Open backlog rows: 40 → 35.**

### What the audit pass cost, and the rule it produced

Twelve concurrent agents put a 4-core box at load 35–48. Under that:
- the live stack could not be brought up (`props` missed its readiness budget) — checked for an OOM kill before concluding it was contention; there was none, 11 GB free;
- four UI vitest tests failed that pass **10/10** in isolation;
- two backend tests failed that fail **identically at `HEAD~1`** under the same load;
- no agent could complete a full `pytest -q`; one measured its own progress at ~2.5%/hour.

So the rule this campaign now runs on: **mass auditing and live measurement alternate, they do not overlap.** Scaling coverage is free; scaling load on shared hardware only manufactures contention artifacts, and five of this campaign's false signals came from exactly that.

Corollary, learned the same way: **verify each agent's gate yourself.** One reported all-green and four tests failed on merge. Its work was sound; its box was not mine.

### The rest of the pass

| Area | What it found |
| --- | --- |
| front door / SSE | A tool call could end with **neither a result nor a failure**: `_from_update` dropped a `Command`-wrapped `ToolMessage(status="error")` down the same branch as an already-signalled one, so the one case its comment existed for was the one case it deleted. Also three route-level error events naming no turn and a fourth minting a correlation id present in no log line, no audit row and no access log; and `arguments` was the only field in the whole event contract that went through `json.dumps`, so a CJK question reached the wire as literal `\u5496` and spent 200 audit characters on 75 source glyphs |
| config / metrics | `chemclaw_repeated_tool_calls_total{tool}` booked **the model's own string** — 141 characters of model-authored text on the unauthenticated `/metrics`, under a declaration claiming the label was "bounded by the registered tool surface". Two derived lists reported clean *with the enumerations that prove it*: 394 settings, 0 without a consumer; 113 metrics, 0 without a producer |
| middleware | The same two labels, found independently — three audits converging on one defect class is decent evidence it was real rather than a reading |
| security | The live lane's credential file was **world-readable**: `( umask 077; … ) > file` applies the redirection in the child *before* the body, so the file is created under the inherited umask — measured `-rw-r--r--`, 12 credential lines, three lines below a comment saying "0600". The same block's trailing `&&` list returned 1 on an unset last variable and silently killed `processes.sh up` under `set -e`. Plus two unbounded model strings reaching the SSE stream, measured at 50,112 and 50,000 characters |
| durable | A **completed** job killed by the ceiling written to outlast it: `wrapper_execution_timeout` sized its headroom from a setting that bounds none of its four post-child steps. Driven against a real broker — the fixture job completed, its record was written, and the wrapper was killed mid-publish; `TIMED_OUT`, and an execution timeout is not delivered to workflow code, so no push-back ever ran. Also settled two open campaign questions: a FAILED workflow **does** write a `job_records` row, and the SIGKILL check could never fail because its precondition was the *wrapper* reporting RUNNING — true from launch, on core's queue, before any bundle worker holds anything |
| retrieval / KG | A **validator-accepted** fusion weight empties a leg: at 0.1 a source's rank-1 hit lands at fused index 40 of 48, behind five other sources' complete tails, keeping 0 chunks. And the starvation counter measured *list order* rather than contribution — lexical read 8/73 with **nothing truncated on any of 20 sweeps**; counted correctly it is 73/73 |
| deploy | A migration that cannot replay (`ADD CONSTRAINT` with no `DROP`, and the re-runnability test matches only `CREATE`), an egress-port map read by nine hardcoded key names, a `deps-audit` clean-control that asserted an audit of **zero packages** was a pass, and two more lane primitives that verified nothing. `git fetch --unshallow` took the migration-immutability guard from **3 skipped to 173 passed** — the first time it was actually asked its question |
| fleet (`Chemclaw3-mcp`) | The egress guard's three *documented* escapes had no test; all three verified to genuinely escape (`ctypes` `libc.connect` rc=0, a subprocess printing `CONNECTED`, `_socket.socket` connected) and now pinned, with the reason the largest hole exists pinned too. And `rxnlabel`'s three hand-written pattern tables were **12 of 60** covered, measured by mutating each literal in turn — now 59/59. Two ligand patterns matched nothing their own comments named: the NHC pattern demanded a formal −1 carbon (IMe, IMes, IPr, SIMes all missed) and the "phosphoramidite" pattern demanded three oxygens, which a phosphoramidite cannot have |

## The repaired storm, and what it found

The `audit-storm` pass asked every check in `live_storm` a single question: **what would a run that
did nothing score?** Of 68 checks in the full matrix, **19 scored PASS having observed nothing and
one could not fail at all** (`accepted + failed == turns`, with `failed` *defined* as
`turns - accepted` — an identity). Family B counted an unbounded `count(*)` over the audit trail, so
it was answered by residue: 366 audited calls reported for a run that contributed 3.
`_require_mock_lane` pinged the mock's stats endpoint and concluded no real model would be driven —
a mock left running by an earlier lane answers that ping while the base URL points anywhere else.
And the reconciliation the mock counter's own docstring calls its purpose **did not exist**: family D
restarts `mock-llm`, which zeroes the counter, so the previously published "516 mock requests served"
was the count from the middle of the run.

Re-run at volume on a quiet box with the repaired instrument: **818 turns driven, 607 mock requests
served, 58 of 69 checks passed.** The failures are informative rather than noise, which is the point.

### The biggest product finding of the campaign

Every tool of every in-tree connector bundle returned **nothing**: `t-calc-properties` scored
`announced=5/5 returned=0`, and the same for `molfp`, `rxnfp` and `bo` — 25 tools, a whole
capability gone — while `/readyz` reported `connectors_unhealthy: 0` and the chemist was told
*"similar_molecules is not a valid tool"*. This is measured from `announced` vs `returned` on the
stream, **not** from the truncated error string that produced this campaign's one retracted claim.

Three linked causes, each measured on the running lane:

1. `connectors_dev --export-env` mints a **fresh** credential set on every call, and `restart` is
   `up`. Family A restarts the front door once per admission cap, so the storm re-minted every
   bundle token mid-run and started core presenting secrets the running connectors process had
   never seen. Measured: the connectors process started 21:03 held token hashes differing from the
   contract rewritten at 21:28.
2. The contract was **narrowing**: an inherited fleet token was written only when the invoking shell
   happened to have one, so a second shell that sourced `env` and restarted dropped them all.
3. Writing the contract was not enough — `up` starts processes from its own environment, so a
   carried-forward value reached the *file* and not the front door, the workers or the connectors
   process.

Fixed as one rule — **a credential's lifetime is the lane, not the invocation that minted it** — and
verified end to end: the nine behaviours that scored `returned=0` now score **6 of 9 fully green**,
with the mint file unchanged across restarts of the front door, a worker and the connectors process.
Keying reuse on "the connectors process is running" was tried first and is one scope too narrow:
`restart connectors` then re-minted and orphaned the front door.

**The health surface is the other half and is not fixed.** `calc` is a *backend*, so `/readyz` never
reaches it, and no probe authenticates — a stack can report ready while every calculation 401s.
Filed rather than patched.

### A refusal the wire could not name

Of the three residual failures, one was a second product defect. `compute_atomic_descriptors` told
the chemist **"an internal error occurred"**. What the calc backend had actually said:

> atomic polarisabilities, dispersion coefficients and atomic multipoles require the `'xtb'` binary,
> which is not installed in this deployment. Nothing here approximates them … The partial charges,
> bond orders and Fukui indices from `compute_electronic_properties` and `predict_site_reactivity`
> do not need it.

A deployment fact, the reason there is no fallback, and the two tools to use instead — discarded at
the last hop, because `McpRequestRefused` is a plain `Exception` and the sanitiser's pass-through
family admits only `ValueError`. A refusal that crossed a process boundary has **already** passed
the far side's sanitiser, so sanitising it again keeps nothing back.

Two links in that chain were found only by measuring: the refusal arrives inside a nested `anyio`
`ExceptionGroup`, and **the group's leaf is not the refusal** — it is the exception the `async with`
raised while unwinding, with the refusal on its `__cause__`. The first version of the fix passed its
unit test and left the live lane unchanged. That is now a lesson in `tasks/lessons.md`.

The third residual is a real output-schema defect (`campaign_progress` advertises 16 properties with
`additionalProperties: false` and returns 18, because `model_json_schema()` defaults to *validation*
mode and computed fields exist only in the serialisation schema) and is being fixed separately.

### Two lane defects an offline suite cannot produce

- **A pid is not an identity.** Both lane scripts carried their own `running()`, each reading
  `kill -0 $(cat name.pid)` — which answers whether *a* process with that number exists. Measured:
  `props` recorded as pid 3422 at 17:56 and killed at 18:07; a bring-up at 20:53 logged
  `props already running (pid 3422)`, skipped the start, and died at `props did not become ready`.
  `soak.sh` reads the same file with `ps -o rss=` to build the front door's memory-drift series, so
  a recycled number does not only skip a start — it puts a stranger's resident set into a published
  measurement. Now the process's `/proc/<pid>/stat` start time is recorded beside the pid.
- **`server_tools_module` treated a bundle package's absence as a broken import.** The lane mounts
  `Chemclaw3-mcp/manifests` on `CHEMCLAW_CONNECTORS_DIR`, so `discovered()` legitimately yields
  names with no `chemclaw.connectors.<name>` package at all — one level above the two cases the
  guard knew. The mock model refused to start with `ModuleNotFoundError: No module named
  'chemclaw.connectors.props'` while every offline suite stayed green, because a suite that mounts
  only this repository's own directory can never produce the case.

### A gate parametrized over the tree is a whole-tree gate

Six audit branches each ran `tests/test_docstring_paths.py` and each reported it green. The **merge
of the six was red in that same file**, three failures. Nothing regressed: the test is parametrized
*per referring file*, so a branch that adds a dangling pointer to `tests/test_service.py` fails only
that one case, and a sibling running its own subset never collects it. Every agent's "green" was
true and none of them was evidence about the branch. The correction is not "run the whole suite" —
that cost every agent hours and produced only BoFire wall-clock timeouts — it is to re-run the
*unparametrized* gates whole after a merge. They cost seconds; this took 8 s to clear.

## The final baseline

Four storm runs on the same stack, and the sequence is the campaign in miniature:

| run | score | what changed between it and the previous |
| --- | --- | --- |
| `storm-repaired.md` | 58 / 69 | the instrument repaired — 19 checks that had scored PASS on a do-nothing run now measure something |
| `storm-after-fixes.md` | 65 / 70 | the credential lifetime fixed, so every in-tree connector tool answers again |
| `storm-baseline.md` | 68 / 70 | the backend-refusal and output-schema fixes; the sweep's noise checks pass on a quiet box |
| **`storm-final.md`** | **69 / 70** | the broker probe stopped reading a cached client; the zero-live-model floor counted forwards |

**819 turns driven, 595 mock requests served, 10 of 10 families ran, 1,112 s wall clock, zero live
model calls.**

The single remaining failure is `t-calc-xtb-descriptors`, and it is a true statement about this
deployment rather than about the code: no `xtb` binary is installed, so the calc backend refuses
`compute_atomic_descriptors` and `compute_surface_potential` — in its own words, naming the two
tools that do not need it. A `BACKLOG.md` row carries the honest fix (a declared precondition the
storm checks against the backend's readiness) and states explicitly that it must **not** be closed
by reading the refusal's wording and calling it a pass.

For comparison, the run this campaign started from scored **27/31** — on an instrument where 19 of
68 checks could pass having observed nothing. The two numbers are not comparable, and that is the
result: the earlier one was not measuring what it reported.

### Four denominators for one floor

The zero-live-model reconciliation took four attempts, and the first three were all the same
mistake — a bound defined by *subtracting* the ways a turn can fail to reach a model, which asserts
those ways have been enumerated. Each attempt found one the last had missed: shed statuses that
omitted 503; any non-200 *response*, which misses the turns that time out with no status at all;
and turns that opened a stream, which over-counts because a turn can be answered 200, stream, and
be refused before a model is asked anything (measured, 121 streamed against 116 served).

What held is counted **forwards** from the one event with no exceptions in it: a turn that produced
an answer certainly asked a model at least once. Deliberately loose, and loose in the safe
direction. Both lessons are in `tasks/lessons.md`, including the second one — that attempts 2 and 3
each shipped behind a green unit test, because the test drove counters set by hand and could only
confirm the arithmetic already assumed.
