# Four-repo end-to-end campaign — 2026-08-28

Driven by a self-paced `/loop`. **This file is the loop's state**: each tick reads it to find the
current stage, advances exactly one stage (or polls one long-running stage), and writes the verdict
back here. That is what makes the campaign survive the ~hourly container reclaim that
`infra/live/soak.sh`'s header records killing a scratch loop at round 5 of 200.

Every verdict below resolves to an HTTP status, a row count, a Temporal workflow state, a declared
metric, or an event written to disk — `live_storm.py`'s standing rule and the D-2026-08-03
correction behind it. Nothing is scored from prose.

## Lane facts

- Repos on disk: `/home/user/Chemclaw3`, `Chemclaw3-mcp`, `Chemclaw3_ui`, `Chemclaw3_mock` (cloned
  this session; it is the fourth repo `up.sh` needs for `mock-eln`:8090 and `mock-vendor`:8091).
- Every `up.sh` call carries `CHEMCLAW_MCP_REPO` / `CHEMCLAW_MOCK_REPO` / `CHEMCLAW_UI_REPO`,
  because the defaults point at `/workspace/...` which does not exist here.
- Host: 4 CPUs, 15 GB RAM, ~30 GB free. `soak.sh` stops itself below a 4 GB disk floor.
- Model credential present as `API-KEY`; `up.sh` maps it to `ANTHROPIC_API_KEY` itself.

## Stage ledger

Status: `pending` · `running` · `PASS` · `FAIL` · `skipped (reason)`

| # | Stage | Command | Status | Evidence |
| --- | --- | --- | --- | --- |
| S0 | Baseline, all repos | see below | **PASS** (4/4 repos) | `.live/baseline/*.log` |
| S1 | Four-repo bring-up | `make live-e2e-full-stack` | **PASS** after F2 | `.live/e2e-*.log` |
| S1b | Wiring check | `/readyz`, `chemclaw_connectors_unhealthy` | **PASS** | — |
| S2 | Durable path, no LLM | `make live-jobs` | **PASS 5/5** | — |
| S3 | Template args vs live | `make live-template-args` | **PASS 9/9** (after lane re-run) | — |
| S4 | Real-model probes | `make live-probes` | **blocked (F1)** | `tasks/live-test/transcripts/` |
| S5 | Plan gate | `make live-plan-gate` | blocked (F1), mock route TBD | `tasks/live-test/m12-plan-gate/` |
| S6 | UI full-stack | `npm run test:e2e:full-stack` | **blocked (F1)** | — |
| S7 | Storm | `make live-storm` | **28/31** as run (3 open; F5 fixed then, F6 closed 2026-08-29) | `tasks/live-test/storm.md` |
| S8 | Corpus convergence | `make live-data`, polled | **PASS 19/19** after F3, F4 | `.live/e2e-corpus-backfill.log` |
| S9 | Degradation (Temporal down) | `make live-degradation` | blocked (F1), mock route TBD | `tasks/live-test/m12-degradation/` |
| S10 | Soak + drift | `make live-soak`, `make live-soak-report` | **PASS** — 12 rounds, no drift | `.live/soak.jsonl` |

**Sequencing that matters.** S1 starts the ORD corpus backfill, which drains for 2h+ — so S2–S7 run
over the top of it and S8 is *polled*, never blocked on. S9 requires Temporal **stopped**, so it
runs after everything needing Temporal up, and infra is restarted behind it before S10.

## S0 — baseline (before any edit)

`tasks/lessons.md` (2026-08-28): the baseline is the only artefact that can tell "my change broke
this" from "this was already broken". `make test` runs with Postgres up — without it the suite
skips a large set and still prints green.

- Infra: `make live-infra` up (postgres, temporal, temporal-ui containers healthy); `make db-migrate`
  applied **71 migrations**, `converted 0 stored message(s)`.
- Chemclaw3: `make lint` **PASS** · `make type` **PASS** (`mypy --strict`, **734 source files**) · `make test` **PASS** — **5753 passed, 14 skipped**, 829s, exit 0, Postgres up (`.live/baseline/cc3-test.log`).
  The 14 skips are named, not folded into the pass: **9** `helm is not installed`, **3** truncated
  git history (`fetch-depth: 0`), **2** the credential finding F1 below.
- Chemclaw3-mcp: `make check` **PASS**, exit 0 — ruff clean, `mypy --strict` clean over **120 source files**, **1521 passed / 7 skipped** in 334s, `pip-audit`: no known vulnerabilities (`.live/baseline/mcp-check.log`)
- Chemclaw3_ui: `typecheck` **PASS** (exit 0) · `lint` **PASS** (exit 0) · `test` **PASS** — 74 files, **761 tests passed**, 40.1s (`.live/baseline/ui-*.log`)
- Chemclaw3_mock: `pytest` → **PASS**, 39 passed, exit 0 (`.live/baseline/mock-test.log`); venv built at `/home/user/Chemclaw3_mock/.venv`, which the lane needs for `mock-eln`/`mock-vendor`

## Findings

### F1 — the model credential is present but unfunded (blocks 4 of 11 stages)

**Measured twice, independently.** The baseline surfaced it as two skips in
`tests/test_prompt_caching.py`, and a direct call confirms it:

```
POST https://api.anthropic.com/v1/messages  ->  http_status=400
{"type":"error","error":{"type":"invalid_request_error",
 "message":"Your credit balance is too low to access the Anthropic API. ..."},
 "request_id":"req_011CeVpRiCPHUynVbPAgmywp"}
```

So `API-KEY` exists and `up.sh` maps it (it only asserts the variable is non-empty, line 267),
but **no real turn can complete**. This is a lane/account fact, not a code defect — there is
nothing in any of the four repositories to fix, and it is the user's call to resolve.

Caught at baseline rather than at S4 after a two-hour bring-up, which is what the baseline is for.

**Consequence for the stage table**, split by what each stage actually measures:

- *Blocked on real-model behaviour* — S4 probes and S6 UI full-stack. Both score what a real
  model chose to do (`full-stack.spec.ts`: "did the turn call `predict_pka` and cite its result").
  A mock cannot stand in without measuring something else and calling it the same name.
- *Possibly recoverable against the mock* — S5 plan gate and S9 degradation. Both measure
  **harness mechanics** (plan -> approve -> execute -> re-gate; `capability_degraded` precedes the
  first token), not model quality. `core/config/llm.py` has an `openai_compatible` provider with
  `llm_base_url`, and `cli/mock_llm.py` already serves one on :8820 for the storm. Whether the
  lane supports pointing the front door at it is the next tick's question.
- *Unaffected* — S1, S1b, S2, S3, S7, S8, S10. The durable path, the manifest checks, the storm
  and the soak all run with **no LLM call at all** by design.

## What this run is not evidence about

Stated up front so the final report cannot imply otherwise: the live OpenShift cluster,
`helm`/`kubeconform` render against a real API server, and the browser→Entra tenant hop (MSAL talks
to `login.microsoftonline.com`; mocking that is mocking a login UI, not a key set). All three stay
open edges in `docs/planning/BACKLOG.md` and cannot be closed by this lane.

### F2 — the four-repo lane could not start its own front door

**Symptom.** `up.sh up` exits 1 at `api exited before becoming ready`. Every other process comes
up: mock-eln, mock-vendor, props, rxnpredict, calc, chem, safety, connectors, and all four Temporal
workers report ready. Only the front door dies, and the lane's own message names a log rather than
a cause.

**Root cause**, four lines deep in `.live/api.log`:

```
WARNING chemclaw.connectors.health: connectors unreachable at startup:
        pyexec (unreachable: ConnectError: All connection attempts failed)
ERROR   chemclaw.connectors.health.ConnectorsUnavailable: connectors_required is set but
        these connectors are unreachable: pyexec (unreachable)
```

`up.sh:268` puts `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR`. That directory holds **five**
manifests — `chem`, `props`, `pyexec`, `rxnpredict`, `safety` — so `pyexec` is *discovered*, and
its manifest carries its own loopback default (`http://127.0.0.1:8899/mcp`), so the front door
health-checks it. But **no lane script starts it**: `up.sh` starts props, rxnpredict, calc, and the
two mocks; `processes.sh` starts chem and safety. `grep pyexec` over both scripts matched nothing.
Under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable discovered connector is fatal at startup
rather than degraded at call time, so the front door refused to boot.

This is a **cross-repo coupling failure**, not a typo: adding `manifests/pyexec/` in Chemclaw3-mcp
silently broke Chemclaw3's four-repo lane, because the manifest directory is the contract and
nothing ties "a manifest is on the path" to "something serves it". The lane has no test — it is a
manual lane by design — so the first thing to notice was a dead front door two hours into a
campaign.

**Fix** (this repo, `infra/live/e2e-full-stack/`): `start_pyexec()` mirroring `start_props`, called
beside its peers during bring-up, added to the `restart <name>` set, and a row in the README's
process table. The alternative — narrowing `CHEMCLAW_CONNECTORS_DIR` to exclude it — was rejected:
the lane exists to exercise every advertised capability, and a front door advertising fewer tools
than a real deployment is the wrong thing to measure.

**S1 re-run, after F2** — `bringup2 exit=0`. `pyexec started` / `pyexec ready` /
`pyexec credential accepted (HTTP 406)`, then `api ready` and `full stack up`.
(406 is the expected answer: an MCP endpoint reached without the streaming Accept header, which is
what `assert_credential_accepted` checks for — it proves the credential was taken, not refused.)

**S1b** — the wiring check the lane README insists on, because absence of an error is not success:

```
$ curl -s localhost:8000/readyz     -> {"status": "ready", "connectors_unhealthy": 0}
$ curl -s localhost:8000/metrics    -> chemclaw_connectors_unhealthy 0
```

### F3 — the corpus backfill could never find its ground truth

**Symptom.** Bring-up ends with `WARNING: corpus backfill failed`, and **exits 0 anyway**. The ORD
half of the corpus is then permanently invisible, which is precisely the failure `cli/live_data.py`
was written to detect in the data — its own module docstring says a previous run "reported 638 note
proposals and called ingestion proven" while 57% of the corpus had never entered the system.

**Root cause.** `live_data.py` derived the published factor tables by walking up from the ORD export
directory with three `.parent`s. The lane sets `ord_export_dir` to
`<mock repo>/data/eln/exports/ord` — **four** levels below the repo root — so the derivation landed
on `<mock repo>/data` and produced `<mock repo>/data/app/eln/real_data`, a path that has never
existed. The tables are at `<mock repo>/app/eln/real_data`. The lane's own error message named the
correct path while the code kept computing the wrong one.

It survived because the derivation was an expression inside `main()`: `tests/test_live_data.py`
existed and had **no** reference to `real_data` at all, because nothing could reach the arithmetic
without running the whole lane against a seeded checkout.

**Fix** (this repo, `cli/live_data.py`): extracted `_default_real_data()` — the unit that was
untestable — corrected to `parents[3]`, with two tests proven red against the pre-fix code.

**And the fix's own first version was wrong**, which is worth recording rather than quietly
amending. `parents[3]` raised a bare `IndexError: 3` on the *shipped* default
(`ord_export_dir = "data/eln-exports/ord"` — relative, three parts), so every invocation outside
this lane crashed from inside `pathlib` naming neither the bad setting nor the flag that fixes it.
My two tests both passed, because I wrote them from the same understanding of the layout that
produced the off-by-one — the exact pattern `tasks/lessons.md` records for tests written alongside
their own change. The helper now returns `None` for an underivable path and `main` reports which
setting it could not derive from. Two further tests cover that domain.

**Verified**: the backfill now starts — `eln-backfill-epoch: still draining`, the workflow running
on the broker, which is S8's long pole and expected to take over two hours.

**S2 — the durable path, end to end, with no model involved.** Job
`compute_reaction_energy`, workflow `calc-compute_reaction_energy-e7812db80ea86202`, launched in
1.8s against Temporal `localhost:7233` and Postgres `chemclaw@localhost:5432`:

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-28T21:07:24+00:00 |
| calculation cached in Postgres | PASS | 3 `xtb*` rows in `calculation_results` |
| job recorded in Postgres | PASS | `calc/compute_reaction_energy` by `admin@localhost` |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 3 -> 3 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |

That fourth row is D-011 measured rather than asserted: a persisted result is not recomputed. And
the whole path crosses the repo boundary — the physics answered from `Chemclaw3-mcp`'s `servers/calc`
on :8860 while the orchestration, the cache and the job record stayed here, which is
`D-2026-08-16-the-physics-leaves-the-cache-stays` working as designed.

**S3 — every template's tool arguments against the running servers.** `9 step(s) checked`, exit 0,
covering all four connectors the templates reach: `chem` (5 steps), `safety` (2), `calc` (1),
`molfp` (1).

The first attempt exited **3** with `0 step(s) checked, 9 unreached` — a *lane* failure, not a
product one, and the permitted single re-run is what distinguished them. I had invoked it without
`eval "$(bash infra/live/processes.sh env)"`, so `CHEMCLAW_CONNECTOR_URLS` was unset and four
connectors "did not come up for this scope" while being perfectly healthy on their ports.

Worth recording that the validator **refused to report green over nothing**: it exited non-zero and
named all nine unchecked steps rather than passing with a count of zero. That is the same failure
mode `live_storm.py`'s header calls the most expensive one under load, and here the tool got it
right on its own.

**S8 progress** — the backfill is draining, measured from Postgres rather than from the log:

```
reaction_records = 2783    (of ~4251 ingestible; ~1.8s each)
corpus_reactions = 0
```

## S8 — the corpus, converged and checked by value

The backfill finished before the container reclaim. **4,282 `reaction_records`**, and the check that
matters reads **`corpus is reachable | PASS | 4251/4251`** — the README documents this same line
failing at `1936/4251`, so the ORD half is now complete rather than partially invisible.

| dataset | published | seeded | mapped | refused |
| --- | ---: | ---: | ---: | ---: |
| bh_amination_hte | 3955 | 3955 | 3955 | 0 |
| suzuki_miyaura_flow_hte | 5760 | 5760 | 0 | **5760** |
| santanilla_amidation_screen | 96 | 96 | 96 | 0 |
| santanilla_sulfonamidation_screen | 96 | 96 | 96 | 0 |
| nielsen_deoxyfluorination | 80 | 80 | 80 | 0 |

The 5,760 refusals are **declared, not discovered**: Perera, *Science* 2018, 359, 429 publishes the
second coupling partner only as `2a, Boronic Acid`, so no structure exists to map and the adapter
refuses rather than inventing one. A dataset declared unreachable that *started* ingesting would be
red here too — the check is bidirectional, which is what makes it a regression detector.

Also passing on real values, not counts: 644 published zero-yields survive seeding as exactly 0.00%
(a truthiness test would read them as missing), and every mapped dataset carries its published
factors *and* yield row by row.

### F4 — a live check asserted the opposite of a merged ADR, and failed 0/12 forever

The first run was **18/19**. The failure: `prose yields its numbers`, `0/12`, first miss
`uspto-suzuki-biphenyl-1: prose says (82.0, 4.0), record carries (None, None)`.

That reads like a broken extraction. It is the opposite. `ingest/eln/json_adapter.py` states, and
`D-2026-08-26-a-transcription-may-not-infer-a-setpoint` decides, that a headline setpoint the entry
does not state is **left absent rather than read out of the prose** — because the first regex match
in a procedure is the *addition* temperature far more often than the reaction's, and a transcription
nobody reviews may not present a derived number as a recorded one. The check asserted precisely the
behaviour the ADR forbids, so it could only ever fail, and it made `make live-data` exit 1 forever
for a reason that was correct.

**Its stated premise was also false, and one measurement settles it.** The docstring justified the
check by claiming that without the extraction "the condition is simply gone, and nothing downstream
can tell the difference between 'ran at 82 °C' and 'temperature unrecorded'". Measured on that exact
record:

```
headline temperature_c = None   time_h = None
  step 2  kind=TEMPERATURE  temperature_c=82.0  duration_h=4.0
          :: "The mixture was stirred at 82 °C for 4.0 h under nitrog..."
```

Nothing is lost. The condition is recorded on the step that actually states it, verbatim, with both
numbers — which is `_segment_steps` working exactly as its own docstring describes.

**Fix**: the check now asserts what the ADR requires, in *both* directions — the prose's numbers
must reach a step, **and** the headline must stay absent unless the entry stated it as a structured
field. That is strictly stronger than what it replaced: the old form could not have caught an
inferred headline at all, because it demanded one. Now `12/12`, with the denominator reported so a
check that stops matching anything cannot pass quietly. `make live-data` exits 0.

## S7 — the storm: stress, chaos and adversarial, on a mock model

**28/31 checks passed** (`tasks/live-test/storm.md`). The first run read 27/31, and **all four of
its failures were my own lane misconfiguration rather than defects** — the distinction the
one-permitted-re-run rule exists to draw, and it changed the verdict on three of them.

What the storm proves when it is pointed at a correctly configured lane:

- **The admission cap is load-bearing and its knee is resolvable.** Goodput rises 0.58 -> 1.15
  answered/s from cap 2 to cap 32 and stops improving at **cap 16**, against a measured 13%
  within-cap noise floor. Every offered turn is accounted for at every cap.
- **Fan-out is honest.** Announcements match results whole (1/1), fragmented (1/1) and in parallel
  (6/6) — the defect that check was written for is one call announcing ten times against one result.
- **The durable collision holds.** 12 simultaneous identical launches produce exactly one run, and
  `calculation_results` moved 51 -> 51: D-011 under contention, not just in sequence.
- **A Postgres bounce is survivable.** 24/24 in-flight turns survived it and a fresh turn answered
  2.1s later, with the front door never restarted.
- **Tool bodies really ran**, asked of the audit trail rather than the stream: 366 `find_notes`,
  151 `gather_evidence`, 145 `expand_note` audited calls.
- **Adversarial input does not get through**: an injection string is treated as a search string
  (`audit_events` 718 -> 719 — a dropped table would read as 0), unicode round-trips through
  Postgres exactly, an unparseable reaction SMILES does not kill the turn, and arguments that parse
  but cannot be true are refused rather than answered.

### F5 — the chaos primitive could destroy the lane it was testing

`processes.sh restart <name>` is "kill, then `up`", and `up` dies on a missing Chemclaw3-mcp
checkout. So a restart invoked without `CHEMCLAW_MCP_REPO` killed the process, failed to restore
it, and left the lane worse than it found it.

That is the wrong order anywhere and disqualifying here, because this verb is **the primitive the
storm's chaos family uses**. Measured: family D called `restart mock-llm`, the kill succeeded, `up`
died, and the rest of the run drove a lane with no model — surfacing as unrelated red checks two
families later (a durable collision reporting zero job records, a broker outage that never
recovered). The cause was invisible from every one of those checks.

**Fixed**: every precondition `up` would die on is now checked *before* anything is killed, and the
refusal says plainly that nothing has been killed. Verified with `CHEMCLAW_MCP_REPO=/nonexistent` —
`restart` refuses and the target keeps its pid and stays alive.

### F6 — the adversarial probe could not fail-report, and the reachable case was untested (CLOSED 2026-08-29)

`f-malformed-json` sent `'{"text": "unterminated'` and asserted the bad call must be reported.
**That check could never pass.** Measured on the two payloads:

```
'{"text": "unterminated'  ->  repaired to {'text': 'unterminated'}
'{"text": }'              ->  JSONDecodeError  ->  invalid_tool_calls
```

LangChain runs a streamed call's fragments through `parse_partial_json`, which closes an
unterminated string and an unclosed brace. `agent/model_calls.py` already states this, and even
corrects an earlier draft of its own docstring for the same confusion. So the probe asserted an
outcome the system is documented and measured never to produce, while the *reachable* case — the
one `RepairInvalidToolCalls` exists to read — went entirely untested. A permanently red check
**and** a blind spot over the middleware written for it.

**Corrected** to send JSON-shaped, unclosable arguments, and verified against the live stack that
this changes what happens: `result[0]` went from `'matches=[] total_matches=0 widened=False'` to
`None`, so the tool no longer runs and the call really does reach the invalid path.

**It is still red, and now for a reason worth having.** `chemclaw_invalid_tool_calls_total` is
declared and carries **no samples** after a run that deliberately emits unparseable arguments, and
no `tool_failed` reaches the stream — so the call is a silent no-op, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed`.

**Left open deliberately.** Root-causing it means entering LangChain's streaming tool-call assembly,
this system's most defect-prone seam by its own history (STREAM-1, LOAD-1, the `stream_events` v3
revert). That is not a change to make unreviewed at the end of a long autonomous run, so it is
handed over with its measurement and a named next step: find why an `invalid_tool_calls` entry
reaches neither the counter nor `wrap_model_call`.

**Closed on 2026-08-29 by
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`, and one
half of the measurement above was wrong.** The counter *does* increment — measured on this same
lane, `chemclaw_invalid_tool_calls_total{tool="find_notes"} 2`, once per repair attempt — and the
entry reaches `wrap_model_call` intact, so the streaming assembly named as the place to look is
sound at every step from the provider up. The silence was one layer away: `tool_failed` is raised
by `agent/tool_authz.announce_tool_failures`, a `wrap_tool_call` middleware, and a call whose
arguments never parsed never enters the tool chain at all. Family F is now 8/8 on this lane.

### Two more, triaged rather than fixed

- **`a disconnected session accepts a new turn without waiting out the lease`** — PASS in run 1
  (accepted after 0.2s, codes `[409, 200]`), FAIL in run 2 (11.1s, codes `[409, 409, 409, 409]`).
  One pass means it is not reproducible, so by the campaign's own rule it is **not** called a
  defect on this evidence. It is timing-sensitive under load and worth a dedicated look.
- **`a job survives its connector worker being SIGKILLed mid-flight`** — FAIL in both runs, but
  with wildly different numbers (`FAILED 597s later` vs `FAILED 13s later`). Reproducible enough to
  be real, and **not** diagnosed here: run 1 was measuring a lane whose model was already dead, so
  only run 2's evidence counts and one run is not a root cause.

## S10 — the soak: 12 rounds, and nothing drifts

12 rounds, **22/23 checks in every single round**, round times 74-77 s. Fitted by
`chemclaw.cli.soak_report`, which refuses to name a slope it cannot resolve — so these are
measurements, not eyeballed trends. Full fit in `tasks/live-test/soak-2026-08-29.md`.

The result that matters is the *shape*, not the totals. Every growing series has a **first-half
slope equal to its second-half slope**, which is what rules out a leak: load accumulates, cost per
unit of load does not.

| series | first | last | verdict |
| --- | ---: | ---: | --- |
| round seconds | 77 | 75 | flat (slope **-0.1 ± 0.2** s/round) |
| disk free | 20 GB | 20 GB | flat |
| `chemclaw_turn_duration_seconds_sum` | 444 | 2828 | +216.2 then **+216.8** /round (± 1.3) |
| `chemclaw_turn_duration_seconds_count` | 76 | 417 | +31.0 then **+31.0** /round |
| `rows audit_events` | 509 | 1444 | +85.0 then **+85.0** rows/round |
| `rows session_messages` | 1319 | 2903 | +144.0 then **+144.0** rows/round |
| `rows calculation_results` | 6 | **6** | flat — D-011 holding across 12 rounds of repeats |
| `chemclaw_event_streams_open` | 0 | 0 | flat — no stream leak |
| `chemclaw_context_unreducible_total` | 0 | 0 | flat — no context-length pressure |

`calculation_results` staying at **6** across twelve rounds is the strongest single line here: the
cache is asked for the same work repeatedly and computes it once, which is D-011 measured over time
rather than in a single collision.

The one failing check per round is **F6**, open at the time of this run — constant at 1, never
cascading. (Closed 2026-08-29; a soak has not been re-run since.)

---

# Campaign summary

**Nine stages run, seven green, one at 28/31, two blocked. Six findings, four fixed.**

| # | Stage | Result |
| --- | --- | --- |
| S0 | Baseline, four repos | **PASS** — 5753 + 1521 + 761 + 39 tests |
| S1 | Four-repo bring-up | **PASS** after F2 |
| S1b | Wiring check | **PASS** — `connectors_unhealthy 0` |
| S2 | Durable path | **PASS 5/5** |
| S3 | Template args vs live | **PASS 9/9** |
| S4 | Real-model probes | **BLOCKED (F1)** |
| S5 | Plan gate | **BLOCKED (F1)** |
| S6 | UI full-stack | **BLOCKED (F1)** |
| S7 | Storm | **28/31** — F5 fixed, F6 open at the time of this run (closed 2026-08-29) |
| S8 | Corpus by value | **PASS 19/19** after F3, F4 |
| S9 | Degradation | **BLOCKED (F1)** |
| S10 | Soak + drift | **PASS** — 12 rounds, no drift |

| # | Finding | Status |
| --- | --- | --- |
| F1 | Model credential present but unfunded — blocks 4 stages | **open, user's call** |
| F2 | Four-repo lane could not boot its front door (`pyexec` discovered, never started) | fixed |
| F3 | Corpus backfill could never find its ground truth (off-by-one path) | fixed, 4 tests |
| F4 | A live check asserted the opposite of a merged ADR, failing 0/12 forever | fixed |
| F5 | The chaos primitive could destroy the lane it was testing | fixed |
| F6 | Unparseable tool call is a silent no-op; the probe could not even reach it | open then; **closed 2026-08-29** |

**Three of the six findings were checks that were wrong, not code that was wrong** (F4, F6, and
half of F2's diagnosis). That is worth stating plainly: a campaign that assumed every red was a
defect would have "fixed" the adapter to violate `D-2026-08-26-a-transcription-may-not-infer-a-setpoint`,
and would have reported four storm failures that were the runner's own misconfiguration.

## What this run is not evidence about

- **Anything requiring a real model.** S4, S5, S6 and S9 never ran. The storm and soak used a mock
  by design, so nothing here says how a real model behaves in this stack.
- **The live OpenShift cluster**, `helm`/`kubeconform` render against a real API server, and the
  browser -> Entra tenant hop. All three remain open edges in `docs/planning/BACKLOG.md`; this lane
  cannot close them.
- ~~**F6's root cause.**~~ **Closed 2026-08-29, and this row was wrong about the code.** The
  counter is not "provably never incremented" — it increments twice per turn, once per repair
  attempt, measured on this lane. What was missing was the `tool_failed` event; see the F6 section
  above and
  `D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`.
- **The SIGKILL-recovery and lease-race checks**, for the reasons the S7 section gives.
