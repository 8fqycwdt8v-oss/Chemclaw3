# Blind-spot remediation — 22 findings across four repositories

Source: a 46-agent audit (4 repo maps → 10 dimension hunts → 30 adversarial verifications →
synthesis). 31 candidates raised, 8 dropped as already tracked in an ADR/BACKLOG/DEFERRED, 1
dropped on verification, **22 confirmed**. Findings are numbered BS-01…BS-22 in the report.

Four pull requests, one per repository, each merged when its own CI is green.

## The pattern the audit found

Controls asserted in prose, a docstring, or a one-time manual run, never mechanically re-checked.
Every fix below either makes the machine enforce the claim or deletes the claim.

---

## Chemclaw3 (core) — 12 items

### Runtime correctness

- [x] **BS-01 (high)** `execute_activity` bounds how long a job runs once started, never how long it
      waits to start. 31 call sites in `durable/` set `start_to_close_timeout` and nothing else; a
      queue nobody polls waits forever, and `ScheduleOverlapPolicy.SKIP` then silently skips every
      later fire of that job family. `notify.py:102` already carries the fix and the measurement
      (75 s against a 30 s timeout) — generalize it. Add `execution_timeout` to the Schedules.
- [x] **BS-02 (high)** `_acting_as` (`durable/template_activities.py:170`) binds actor and
      session_id and drops `correlation_id`, which `StepIdentity` already carries as a required
      field. Every durable-job log line books `correlation_id='-'`. Same bug class the function's
      own docstring describes for session_id.
- [x] **BS-03 (high)** `core/mcp_session.open_session` sends only `Authorization`, so the calc
      backend — the one server running minutes-to-hours CREST/xTB work — receives no actor,
      session, correlation or `traceparent`. `connectors/identity.turn_headers()` already builds
      exactly the right dict.

### Resilience

- [x] **BS-18 (medium)** No circuit breaker: the per-turn connector connect path never consults the
      readiness state `/readyz` already computes, so every turn pays the full connect timeout
      against a connector known to be down.
- [x] **BS-07 (medium)** No admission control on the calc backend. Worker concurrency is capped per
      worker; replica count multiplies it against one shared pod. The fleet-ceiling check already
      exists for LLM turns and the Postgres pool — extend it, don't invent a new mechanism.

### Enforcement

- [x] **BS-04 (high)** The control keeping `manifests-internal` (`mount: backend`) out of the
      agent-facing connector surface is evidenced by a pasted error transcript in a docstring.
      Assert it: load the real manifest text and prove `ConnectorManifest` refuses it.
- [x] **BS-16 (medium)** `make mutants` is excluded from `ci` with no schedule, so the seven
      invariant-bearing modules have no automated mutation backstop. Add a scheduled job.
- [x] **BS-08 (low, core half)** Port registry: `bo` sits at 8816, outside the range Chemclaw3-mcp
      documents as core's. Make the boundary checkable rather than narrated.

### Documentation that has outlived its subject

- [x] **BS-13 (medium)** `values.yaml` calls the unset `framing_envelope_secret` fallback
      "predictable"; it is `secrets.token_hex(8)` — per-process random. The real failure is silent
      envelope mismatch across restarts and replicas, which is worse than what the comment says.
- [x] **BS-11 (low)** BACKLOG row for the `CalculationKey` collision is stale — closed by Field
      patterns in #248, a lighter fix than the row proposes. Delete it (repo rule: same commit).
- [x] **BS-12 (low)** BACKLOG row for the two unreachable chart credentials is stale — both are in
      `secrets.optionalKeys` now. Delete it.
- [x] **BS-10 (low)** `connectors/README.md` uses "chem has only a server" as its example; chem's
      server moved to Chemclaw3-mcp and the bundle now has none.

---

## Chemclaw3-mcp — 5 items

- [ ] **BS-05 (high)** No `pip-audit` step and no Dependabot. Core proved this pattern out after it
      caught real CVEs.
- [ ] **BS-14 (medium)** GitHub Actions on floating `@v4`/`@v5` tags. Core SHA-pins every action
      with a `# vX.Y.Z` comment, and Dependabot keeps the pins current — adopt both halves.
- [ ] **BS-17 (medium)** Zero OpenTelemetry instrumentation, so the `traceparent` core sends is
      received and discarded. Either continue the trace in `mcp_server_kit.connector_app` or stop
      claiming universal trace continuity. (Prefer continuing it — the header already arrives.)
- [ ] **BS-07 (medium, fleet half)** `servers/calc` offloads every heavy primitive through a bare
      `asyncio.to_thread` with no bound, so aggregate load is whatever the callers send.
- [ ] **BS-08 (low, fleet half)** The documented port block is wrong about core's range and no test
      checks it.

---

## Chemclaw3_ui — 8 items

- [ ] **BS-06 (high impact)** `Composer.tsx:845` sends on Enter with no `isComposing` guard, so
      committing a CJK candidate submits mid-composition. One-line fix, real user segment blocked.
- [ ] **BS-19 (medium)** `errors.ts:92` maps every 429 to a terminal, non-retryable
      `budget_exhausted`, and nothing reads `Retry-After` — collapsing a transient rate limit into
      a permanent budget cap. The same conflation was already fixed for SSE events at `errors.ts:114`.
- [ ] **BS-20 (medium)** `MAX_MESSAGE_CHARS` is a compile-time copy of an ENV-tunable backend
      setting. The RuntimeConfig bridge that would carry it already exists for `authMode`.
- [ ] **BS-21 (medium)** The structure sketcher has no accessible keyboard path and, unlike 19 other
      a11y decisions in this codebase, the gap is nowhere acknowledged. Document the SMILES route as
      the deliberate alternative and cover it.
- [ ] **BS-22 (low)** Scientific values render through bare `.toLocaleString()`, so a pKa reads
      `1,234.5` or `1.234,5` by browser locale. The CSV path already gets this right.
- [ ] **BS-09 (medium)** README and `server/proxy.ts` still describe "disconnect cancels the turn",
      superseded by `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`.
- [ ] **BS-05 (high)** No `npm audit` gate and no Dependabot.
- [ ] **BS-14 (medium)** Actions on floating tags.

---

## Chemclaw3_mock — 2 items

- [ ] **BS-15 (medium)** The mock Entra tenant mints one key at import with no way to inject an
      outage, a malformed body, or a rotation — while core's `test_entra_end_to_end.py` calls it
      "the companion piece" for exactly those three behaviours. Add the fault injection, or correct
      the claim. (Prefer adding it: the live lane is where those paths are otherwise never run.)
- [ ] **BS-05 (high)** No dependency scanning of any kind.

---

## Verification bar

Per repo, before its PR is opened:

- `make lint type test` green in core (Docker up, so the ~216 Postgres-backed tests actually run —
  a green run that skipped them is not evidence); `make check` in Chemclaw3-mcp; the repo's own
  gate elsewhere. Report what was skipped, always.
- Every behavioural fix carries a test that fails without it. A fix whose only evidence is that the
  suite still passes has not been shown to do anything.
- An ADR for each decision that is a design choice rather than a repair: BS-01, BS-03, BS-07,
  BS-18, and the BS-15 posture call.

## Review

**Chemclaw3, BS-01/02/03 (2026-08-27).** All three fixed, each with a test that was run against the
unfixed code first. Three ADRs:
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`,
`D-2026-08-27-a-step-runs-under-the-correlation-id-it-was-launched-with`,
`D-2026-08-27-the-backend-is-told-who-is-asking`.

- **BS-01.** One shared `queue_wait_timeout()` (`durable/publish.py`) passed as
  `schedule_to_start_timeout` at all 30 unbounded dispatched-activity call sites; `notify.py`'s
  stricter `schedule_to_close` stays. **Schedule-to-start, not schedule-to-close**: the latter caps
  every attempt together and would have deleted the retry budget everywhere. Measured on the test
  server: a ScheduleToStart timeout is *not* retried (3 attempts, 10 s bound → one failure at
  10.028 s), and before the change the same workflow against an unserved queue was still running
  when the test was killed at 90 s. Schedules gained `execution_timeout`.
- **BS-02.** `_acting_as` now binds the correlation id too. **The finding's premise was partly
  wrong and the test says so**: audit rows were *not* affected (`agent/audit.py` falls back to the
  id each step activity passes explicitly, so an audit-row test passes with the fix removed). The
  real victims are the ambient's readers — log lines, `ambient_provenance` on PR-gated proposals,
  and `ConnectorJobInput.correlation_id` for jobs a template launches. `memory_jobs` and
  `report_workflow` have no correlation id available to bind; the report half is a new BACKLOG row
  rather than an invented id.
- **BS-03.** `open_session` takes `identity_headers` (passed in, because `core` imports no
  sibling), `calc_session` supplies the existing `turn_headers()`, and the redirect guard comes
  with them over one shared `core/http.same_origin`. The durable path had *no ambient identity to
  send*: `CalcJobWorkflow` now reads the memo core already sets and the activity stamps it. The
  reaction labeller stays anonymous — `ingest` may not import `connectors` — and the ADR records
  what moving the builder into `core` would cost.

**Chemclaw3, BS-18/07 (2026-08-27, resilience).** Both fixed, each with a test run against the
unfixed tree first. Two ADRs:
`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`,
`D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling`.

- **BS-18.** The premise held exactly: `probe_connectors` had three consumers and the open path was
  none of them, and the two halves could not see each other because the snapshot lives on
  `app.state`. So the fix is a reader, not a mechanism — `connectors/reachability.py` is one dict,
  a recorder and a predicate, in its own module only because `health → registry → transport` would
  otherwise be a cycle. **Skip the dial outright rather than shrink the timeout**: a shrunk bound
  still pays a handshake attempt per connector per turn and needs a second underivable number.
  Recovery is two independent paths (the sweep records `healthy`; a verdict expires after
  `connector_breaker_window_seconds`), because a breaker with one path back is an outage amplifier.
  What a turn *reports* is unchanged — the degradation event and the counter still fire, since how
  we found out is not the chemist's business.
- **BS-07.** Extended the existing shape rather than inventing a third: same `Settings` validator,
  same self-disabling `0`, same derived chart factor (`chemclaw.calcWorkerProcesses`). **The
  runtime gauge had to be live rather than a configured capacity**, and that is a finding rather
  than a preference: the `calc` bundle's own MCP server pods dispatch to the same backend straight
  from a tool call with no per-process cap, so no product covers them —
  `chemclaw_calc_requests_in_flight` counts held sessions and is the only number that sees both
  halves. The shipped ceiling is `0` (inert) deliberately: it describes a pod in another release
  whose CPU this chart cannot see, and a number invented here would be a guess shaped like a
  statement. Settings live in `temporal.py`, not `calculators.py` — the calculation server reads
  that section's names under the same env prefix.

**Chemclaw3, BS-04/16/08/13/11/12/10 (2026-08-27, claims and their checkability).** Six fixed, one
premise disputed. One ADR: `D-2026-08-27-a-survivor-is-not-a-failing-build`.

- **BS-04.** The control is real and now has a regression test that fails without it: a literal copy
  of `manifests-internal/calc/connector.yaml`'s shape is loaded through the *loader*
  (`registry.discovered`), not just the model, because the loader is what a deployment actually
  runs, and the assertion is that the error names both the key and the file. Verified by relaxing
  `ConnectorManifest`'s `extra="forbid"` to `"ignore"` — the test then reports DID NOT RAISE.
  **The second half of the finding is disputed**: this repository's `CLAUDE.md` contains no pasted
  error transcript, and neither does anything under `src/`, `docs/` or `tests/`. `grep -rn
  'manifests-internal\|mount: backend\|Extra inputs are not permitted'` over the tree returns one
  hit, in an archived audit table about an unrelated `.env` field. The transcript is in the *other*
  repository's manifest comment, which is another lane's.
- **BS-16.** The measurement is what shaped this, and it contradicted the obvious design twice. A
  gate on `no_tests == 0` and `timeout == 0` was written first, from `D-2026-08-08`'s numbers — and
  over all seven modules this checkout has **34 and 2**, so it would have failed on its first fire.
  Then the same commit run twice gave 618 and 634 killed of 825 (74.9% / 76.8%): sixteen mutants
  changed verdict with no edit between the runs, so the survivor set is not reproducible to better
  than two points and the floor is sized against that spread rather than tucked under the best run.
  The job is cheap (2m57s warm, 3m45s cold), needs **real Postgres** or it manufactures survivors in
  two of the seven modules, and files a labelled issue on failure rather than trusting the
  scheduled-workflow email.
- **BS-08.** The real range is **8810–8819**: `molfp` 8811, `rxnfp` 8812, `calc` 8815, `bo` 8816,
  and the `make connectors` composite on 8810. `chem` (8858) and `safety` (8859) are outside it on
  purpose — their servers are `Chemclaw3-mcp`'s, so their addresses come from the fleet's registry.
  `tests/test_connector_ports.py` derives the set from the shipped manifests and asserts both
  directions of the disjointness; verified by moving `bo` to 8880 and `chem` to 8813, which fails
  one test each.
- **BS-13.** Both copies of the wrong claim corrected. The unset fallback is `secrets.token_hex(8)`,
  per process — so the failure is envelopes that stop matching across a restart or between replicas,
  not a guessable tag, and a durable session's oldest retrieved content is then read as prose.
- **BS-11.** Deleted. The `Field` patterns from #248 make the flat form a bijection: `calc_type`
  bars `@` and `:`, both hashes bar `:`, and `calc_version` is deliberately left free of both
  (`esol-delaney@2004` needs the `@`). `tests/test_store.py` parses the flat form back to prove it.
  The `stable_hash` rekeying the row proposed was considered and rejected in that commit.
- **BS-12.** Deleted; both `llmFallbackApiKey` and `temporalApiKey` are under `secrets.optionalKeys`.
- **BS-10.** `molfp` is the bundle that genuinely has only a server today; `chem` moved to the next
  paragraph, which is where the "hosted elsewhere" shape already lives.
