# Full-stack e2e — Chemclaw3 + Chemclaw3-mcp + Chemclaw3_mock + Chemclaw3_ui

Second run of `infra/live/e2e-full-stack/`, and the first one to reach the end. The
[2026-08-13 run](full-stack-e2e-2026-08-13.md) brought the stack up and then stopped dead on a UI
defect that left **6 of 8** scenarios unrun; it also produced three cross-repo fixes it could not
land, because it had no push access. Those patches were attached to that report and are gone —
no branch for any of them exists on any companion repo, and
`infra/live/e2e-full-stack/README.md:27` had been citing one of them (`register_requested`) as
though it existed.

This session had what that one lacked: a working Anthropic credential, Docker, and push access to
all four repos. Everything below was run against the live four-repo stack.

Front door `:8000` · `props` `:8850` · `rxnpredict` `:8857` · `chem` `:8858` · `safety` `:8859` ·
`calc` `:8860` · `mock-hpc-eln` `:8090` · `mock-vendor` `:8091` · UI `:5173` / BFF `:8787` ·
Temporal `:7233` · Postgres `:5432/chemclaw`.

**Model under test: `claude-haiku-4-5-20251001`** (`CHEMCLAW_AGENT_MODEL`). The judge stays on
`claude-sonnet-5` (`live_probe_judge_model`) — deliberately a different model from the one under
test, so grading is not the system marking its own work.

## Headline: the stack now works end to end, and did not before

| | 2026-08-13 | 2026-08-17 |
| --- | --- | --- |
| UI scenarios run / passed | 1 / 1 (6 never ran) | **8 / 8** |
| Probe corpus | 190, Sonnet | 230, Haiku (see below) |
| Two consecutive turns in one conversation | impossible | works |
| Chemclaw3-mcp servers started by the harness | 2 of 5 | 5 of 5 |
| Cross-repo fixes landed | 0 (patches lost) | 3, each its own PR |

Seven defects were found by running the thing. None was visible to any existing test suite, and
two of them were being actively misreported as healthy.

## Bugs found and fixed

### 1 · `mock-vendor` died at import — `mcp` was unpinned · `Chemclaw3_mock`

`mcp>=1.2` with no upper bound resolved to `mcp==2.0.0`, which removed `mcp.server.fastmcp`, the
module `app/mcp_tools/vendor_server.py` imports `FastMCP` from. No code changed; the resolver moved
underneath it. This killed the whole bring-up. `Chemclaw3-mcp`'s own `mcp-server-kit` already pins
`mcp>=1.9,<2` for exactly this reason. **Fixed**: pinned `mcp>=1.2,<2`; resolves to 1.29.0, import
verified, 28 tests pass. *This is the same bug the 2026-08-13 report found and could not land.*

### 2 · The harness started two of the fleet's five servers · `Chemclaw3`

`up.sh` was written when `Chemclaw3-mcp` had two servers. It grew to five. This failed three
separate ways, and the three are worth separating because only the first is loud:

- **`chem` and `safety` are connectors Chemclaw3 dials.** Under `CHEMCLAW_CONNECTORS_REQUIRED`,
  unreachable means the front door does not start at all:
  `ConnectorsUnavailable: ... chem, safety`.
- **A token has two halves in two places.** The `start_*` function gives the server the value it
  verifies; a separate `export` gives the front door the value it sends. With only the first,
  `/readyz` reported `chem=healthy, safety=healthy` — `/healthz` is unauthenticated — while every
  `/mcp` call was rejected and turns emitted `capability_degraded` naming no cause.
- **`calc` is not a connector and still has to run.** Its manifest correctly stays off
  `CHEMCLAW_CONNECTORS_DIR`, but `connectors/calc/remote.py::calc_session` dials it on a cache
  miss. With it down, `/readyz` is **entirely green** and every calculator tool fails at call time.
  This is how `predict_pka` failed on the first real turn of this run.

**Fixed**: all five started, all five tokens exported, README table corrected. Recorded as
`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two`, whose rule is
**`/readyz` is a connector probe, not a dependency probe**.

### 3 · The BFF died at import on a fresh checkout · `Chemclaw3_ui`

`server/index.ts` checks for a missing client directory, warns "static assets will 404" — then
hands that same missing path to `sirv`, which calls `readdirSync` on construction and throws ENOENT
one line later. Under `npm run dev` the client is Vite's job and the BFF only proxies, so the
warning was right and the crash was wrong. Vite still came up and proxied `/api` to a dead port, so
the browser saw **502 on every call** rather than this stack trace. **Fixed**: a missing
`dist/client` yields a 404 asset handler, which is what the warning already promised.

### 4 · Vite's dev proxy stripped every header on SSE responses · `Chemclaw3_ui`

The `proxyRes` hook called `res.flushHeaders()` to defeat buffering. But `http-proxy` emits
`proxyRes` **before** it copies the upstream headers onto `res`, and guards that copy with
`!res.headersSent`. Flushing first sent an empty header block and turned the copy into a no-op: the
body streamed perfectly while `content-type` — and the BFF's CSP and `nosniff` headers — never
arrived at all. The client checks the content type before it will parse, so every browser turn died
on `Expected an event stream but received ""`, while the identical request straight to the BFF on
`:8787` was flawless.

Measured, before the fix:

| path | `content-type` |
| --- | --- |
| direct to BFF `:8787` | `text/event-stream; charset=utf-8` |
| through Vite `:5173` | *absent* |

That asymmetry is why curl-to-the-BFF cannot clear this path — and the 2026-08-13 session's
elimination table did exactly that, testing `:8787` and `:4331` and concluding the proxy was fine.
**Fixed**: write the upstream head before flushing, keeping both the early flush and the headers.

**On the relationship to the 2026-08-13 headline finding**: that report described a *different*
signature — store settled, DOM stale — whereas what reproduced here was a hard SSE error and no
settled store at all. Its Playwright config serves a built client through its own BFF with no Vite
proxy in the chain, which is a different path again. So this fix is not demonstrably the same bug,
and it is not claimed to be. What is measured is that after these two fixes, two consecutive real
turns in one conversation both render and re-enable the composer, which is precisely what that
report said a chemist could not do.

### 5 · The documented `fake_a`/`fake_c` predictors were inert · `Chemclaw3-mcp`

`base_doubles.py` promised an operator could set
`CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` and get a working tool surface with no model
weights. Nothing in the package ever constructed a double — only `tests/conftest.py` did. Measured
before the fix, with both variables set exactly as `up.sh` sets them:

```
forward: []      conditions: []
```

So the harness's rxnpredict scenarios were exercising an empty registry. **Fixed**:
`register_requested()`, called from `tools.py` after `discover_predictors()`. Measured after:

```
forward: ['fake_a']    conditions: ['fake_c']
```

and with the default configuration, still empty — the negative is the important half, and it is
tested three ways (`default`, `"*"`, and a real predictor of the same name winning). 6 new tests,
77 pass, `ruff` and `mypy --strict` clean. Confirmed live: a forward-prediction turn now returns
`contributing_models: ["fake_a"]`. *This is the same bug the 2026-08-13 report found and could not
land.*

### 6 · `make live-routing` was a dead target · `Chemclaw3`

It invoked `live_probes --suite routing`. D-2026-08-15 deleted the specialist team, the challenge
panel and the routing measurement together; the target outlived all three and failed at argparse
(`invalid choice: 'routing'`). Its comment also still said "three M12 suites" when two remain.
**Fixed**: target and `.PHONY` entry removed, comment corrected.

### 7 · `make live-storm` failed hundreds of turns in, with the wrong reason · `Chemclaw3`

Run against the four-repo lane, the storm drove a few hundred turns and then died on
`no .live/run/mock-llm.pid — is the lane up?`. The lane *was* up; it was simply serving a real
model, and `processes.sh` starts `mock-llm` only when `CHEMCLAW_LLM_BASE_URL` names it. **Fixed**:
a preflight that asks the mock's own stats endpoint before any work and names the setting that
fixes it, so the storm now refuses in under a second instead of after real spend.

## Scenario results — the eight UI scenarios

`npm run test:e2e:full-stack` in `Chemclaw3_ui`, real browser, real model, serial single-worker.

| # | scenario | subsystem | 2026-08-13 | now |
| --- | --- | --- | --- | --- |
| 1 | shell paints against the real front door | Chemclaw3 | PASS | **PASS** |
| 2 | solvent question reaches `props` | Chemclaw3-mcp | FAIL | **PASS** |
| 3 | reaction question reaches `rxnpredict` | Chemclaw3-mcp | never ran | **PASS** |
| 4 | sourcing question reaches `mock-vendor` | Chemclaw3_mock | never ran | **PASS** |
| 5 | evidence from seeded ELN/ORD data | Chemclaw3_mock | never ran | **PASS** |
| 6 | durable job launched and tracked | Temporal + mock HPC | never ran | **PASS** |
| 7 | PR-gate review queue renders | Chemclaw3 | never ran | **PASS** |
| 8 | `/readyz` reports every connector healthy | Chemclaw3 | never ran | **PASS** |

**8/8**, in 1.0 minute.

Scope of what these assert, stated plainly: the checks are on **tool use and surface reachability**
— that the trace names a plausible tool and the panel renders — not on the chemistry of the answer.
That is deliberate (a real, small model would make the suite measure the model), but it means
"scenario 4 passed" says mock-vendor's surface was exercised, not that pricing was verified.

## Durable jobs — `make live-jobs`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED |
| calculation cached in Postgres | PASS | 17 `xtb*` rows in `calculation_results` |
| job recorded in Postgres | PASS | `calc/compute_reaction_energy` by service-account |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 25 → 25 |
| wedged worker yields a pending job | PASS | returned the id after 20 s, then COMPLETED once resumed |

**5/5.** The fourth row is D-011 measured rather than asserted: a re-launch of a persisted
calculation added **no** cache rows and rejoined the existing run instead of recomputing.

## The probe corpus — 230 probes on Haiku, judged by Sonnet

`make live-probes`, full corpus, transcripts and grades in `tasks/live-test-2026-08-17/transcripts/`.

| verdict | Haiku, n=230 | Sonnet baseline, n=190 |
| --- | ---: | ---: |
| served | 70 (30%) | 56 (29%) |
| partial | 40 (17%) | 44 (23%) |
| unserved | 26 (11%) | 28 (15%) |
| **fabricated** | **90 (39%)** | **62 (33%)** |
| ungraded | 4 (2%) | 0 |

**Read this comparison with care.** The corpus grew from 190 to 230 probes between the two runs, so
these are rates over different question sets, not a matched pair. The honest summary is: *served*
is flat, *partial* and *unserved* both fall, and **fabrication rises by about six points**.

That last number is the model, not the plumbing, and it is the expected cost of the Haiku
configuration. The mechanism is visible in the graded examples: `an-01` invented a complete HPLC
method — gradient table, column dimensions, buffer concentrations, flow rate — for a question whose
`direction` explicitly forbids presenting one, and never disclosed that no chromatography model or
column database exists behind it. That is a small model filling a capability gap with plausible
prose, which is precisely the failure the `fabricated` verdict exists to name.

The integration signals, which are what a four-repo run is actually evidence about, are healthy:

| signal | value |
| --- | ---: |
| answered at all | 230 / 230 |
| **failed silently** (no answer, no error) | **0** |
| **answers citing a note no tool returned** | **2** / 230 |
| expected tool reached | 126 / 175 |
| answers using no tool at all | 73 / 230 (17 of them on bucket-A questions the surface covers) |
| durable jobs started | 2 |
| median turn | 11.2 s |

Zero silent failures is the one to keep: every turn that went wrong went wrong *loudly*. Two
answers out of 230 cited a note no tool returned — the grounding check working, and finding almost
nothing.

The bucket split says where the fabrication concentrates:

| bucket | probes | served | partial | unserved | fabricated |
| --- | ---: | ---: | ---: | ---: | ---: |
| A (surface covers it) | 118 | 31 | 17 | 21 | 46 |
| B (partial coverage) | 61 | 13 | 14 | 5 | 29 |
| C (no tool should be used) | 51 | 26 | 9 | 0 | 15 |

Bucket A is the worst of the three in absolute terms — 46 fabrications on questions the tool
surface *does* cover, plus 17 bucket-A answers that used no tool at all. Those are not capability
gaps; they are the model declining to reach for a tool that was there. On a larger model that
number should fall, and it is the single most useful thing to re-measure if this configuration is
ever considered for anything but cost control.

## Chaos — kill and restart `props` mid-session

| check | result | observed |
| --- | --- | --- |
| `/readyz` reflects the kill | PASS | `props: healthy → unreachable` |
| `chemclaw_connectors_unhealthy` moves | PASS | `0 → 1` |
| the front door survives | PASS | `/healthz` 200 throughout |
| the turn degrades honestly | PASS | `capability_degraded` naming `props`; the model declined and pointed to the SDS/CRC rather than inventing a flash point; `unsupported_claims: []` |
| restart restores health | PASS | `healthy` again, metric back to `0`, no front-door restart needed |
| the tool is reached again after recovery | PASS | `solvent_properties` called; `-11.0` among the returned numbers |

**6/6.**

## Data access and ingestion

The mock's ELN corpus is really ingested, and the PR-gate really receives it:

| observation | value |
| --- | --- |
| `note_index` after `make reindex` | 38 curated notes |
| `note_proposals` after the ELN sync | **638** |
| `calculation_results` | 50 |
| `session_messages` | 1,439 |

Two things worth recording, neither a Chemclaw3 defect:

- **One mock ELN entry is legitimately rejected.**
  `santanilla-orgsyn-boronate-well-Y36` carries `yield_percent = 119.43`, which fails
  `OrdReaction`'s `<= 100` validation. Loud, counted, and correct — but it means the mock ships one
  record that can never ingest.
- **The 10,011 ORD export files are all "late arrivals".** They share one mtime (the moment the
  repo was cloned) and carry older payload timestamps, so once the sync cursor passes that instant
  none of them can qualify again. Chemclaw3 handles this exactly right — an aggregated, bounded
  WARNING that names the remedy ("re-run the sync with an explicit earlier `since` to backfill
  them") rather than dropping them silently. The gap is in the **harness**, which never backfills:
  a first bring-up should sync ORD from an explicit early `since` before anything advances the
  cursor. Filed below rather than fixed, because it is a harness change that wants its own run.

## What did not run

- **The storm and soak pass.** `make live-storm` needs the lane pointed at the mock model
  (`CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1`), which is a different bring-up from the
  four-repo real-model lane and would have torn down the stack the rest of this run needed. The
  preflight added in bug 7 is what makes that requirement legible; the pass itself is still owed.
- **`make live-plan-gate`**, for the same reason — it needs `CHEMCLAW_HARNESS_AUTONOMY=plan_only`.
  Scenario 6 exercised the durable path without the gate in front of it.
- **`make live-degradation`** as a suite. Its property — `capability_degraded` precedes the first
  token — was checked by hand in the chaos round above, with Temporal up rather than stopped.

## The gate

| gate | result |
| --- | --- |
| `make lint` | clean |
| `make type` | clean — 597 source files |
| tests covering this diff | **107 passed** (`test_live_storm`, `test_live_probes`, `test_live_jobs`, `test_m12_probes`, `test_decision_log`, `test_repo_map`, `test_deferred_register`) |
| `make test` (full) | 4,178 tests, still running at the time of writing — see below |

**The full suite takes hours in this environment, and that is worth recording.** With Docker up —
which is the configuration `CLAUDE.md` explicitly asks for, so the ~157 Postgres tests do not
silently skip — the suite collects 4,178 tests and progresses at roughly 3% per ten minutes, CPU-
bound at ~186% on four cores rather than blocked on I/O. Running it alongside the live stack's
thirteen processes made it materially worse; the numbers above are from a run with the stack down.

This does not contradict `CLAUDE.md`'s "a green `make` locally means a green CI" — it is a
statement about wall-clock, not correctness. But it does mean the documented pre-push gate is not
something a session can casually run to completion, and a session that needs to should start it
early rather than at the end. The targeted subset above is what actually exercises this diff:
everything changed here is the Makefile, `up.sh` (shell, untested by pytest either way), one new
function in `live_storm.py`, and documents.

## Follow-ups

- Backfill ORD on first bring-up in `up.sh` (see above).
- Run the storm/soak lane and the two M12 suites, each in its own correctly-configured bring-up.
- `Chemclaw3_mock`'s `yield_percent = 119.43` record: either correct the fixture or decide that an
  over-100% raw yield is data the adapter should carry rather than reject.
