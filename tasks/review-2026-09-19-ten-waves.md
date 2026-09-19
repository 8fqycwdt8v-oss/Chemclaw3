# Ten-wave review session — 2026-09-19

## Why

Two weeks of change (2026-09-05 → 2026-09-19) across the three repositories in the family, measured
rather than estimated:

| Repository | Base | Files | Insertions | Deletions |
| --- | --- | --- | --- | --- |
| `Chemclaw3` | `63b58ea` (2026-09-05) | 1558 | 193 201 | 28 524 |
| `Chemclaw3-mcp` | `f8f74bc` (2026-08-29) | 435 | 52 643 | 1 994 |
| `Chemclaw3_ui` | `919c68a` (2026-09-04) | 188 | 27 341 | 2 515 |

That is too much surface for one review pass to hold in context, which is the whole reason this is
ten waves with a fan-out per wave rather than one long read. Waves 1–5 are scoped to **the change**;
waves 6–10 review the codebase as a whole, on the premise that a change can be individually correct
and still leave the system unshippable.

## Shape of a wave

1. Fan out subagents over disjoint slices of the wave's scope, each with its own context.
2. **Verify every finding myself** before believing it — this repository's own rule (`CLAUDE.md`,
   "Measure it, don't argue it"). A subagent's confident prose is evidence about the subagent.
3. Fix what survives verification. Root cause, not band-aid.
4. `make lint type test` (serial) in each repository touched, Postgres-backed tests included —
   the daemon is started, so a skipped Postgres lane is not acceptable evidence here.
5. Commit on `claude/10-wave-code-review-k93v0d`, push, open the PR, wait for CI, merge, delete.
6. **Post-merge focused review** of exactly what merged, in fresh context, before the next wave
   starts. A fix is where the next defect goes.

## The ten waves

Waves 1–5 — the change:

- **Wave 1 — the agent layer.** `src/chemclaw/agent/` (+13 188/−1 903, 69 files): `turn_graph.py`
  and `handoff.py` (both new), `tool_result_shape.py`, `state.py`, the loop cap, compaction,
  the spend cap, the skill backend, the plan gate. Highest-risk slice: it is where authority lives.
- **Wave 2 — config, API and CLI.** `core/` (+7 107), `api/` (+4 642), `cli/` (+3 780): the budget
  derivation, the event contract, streaming, the front door, identity.
- **Wave 3 — data and durability.** `durable/`, `kg/`, `ingest/`, `memory/`, `retrieval/`,
  `publish/`, `science/`, `protocols/`, `analytical/`, `deliver/`, `operations/`, `evals/`.
- **Wave 4 — the MCP fleet.** `Chemclaw3-mcp`: four new servers (`unitops`, `thermalsafety`,
  `suitability`, `kinetics`), `mcp_server_kit` (+6 605), `calc`, `rxnpredict`, `rxnlabel`.
- **Wave 5 — the frontend.** `Chemclaw3_ui`: `src/components`, `src/state`, `src/hooks`,
  `src/api`, `src/chem`, `server/`, `shared/`, `e2e/`.

Waves 6–10 — the whole system, change or no change:

- **Wave 6 — cross-repo contracts and regression surface.** Does the change hold at the seams the
  three repositories meet on (manifests, ports, identity headers, the turn-event contract, OpenAPI),
  and did it break anything it did not touch?
- **Wave 7 — security and identity, end to end.** Authorization, authentication, the egress
  posture, secret handling, prompt injection, sandboxing, the dependency closure.
- **Wave 8 — production readiness: deployment and operability.** Helm/OpenShift, resource limits,
  probes, observability, migrations, retention, failure modes, runbook truth.
- **Wave 9 — correctness and reliability of the whole tree.** Concurrency, the durable layer,
  error handling, data integrity, scientific correctness inside the semiempirical tier.
- **Wave 10 — claims against code, test quality, and the final gate.** The corpus this family keeps
  is full of prose that was once true; plus whether the tests prove what their names say, dead code,
  and one full green gate per repository with its skip count named.

## Log

(appended per wave)

### Wave 1 — the agent layer (2026-09-19)

Six reviewers over disjoint slices of `src/chemclaw/agent/` (+13 188/−1 903, 69 files). Every finding
below was **re-driven by me** before it was believed; two did not survive that and are recorded as
such, because a reviewer's confidence is evidence about the reviewer.

Fixed, each with the mutation that reds its guard watched failing:

1. **The durable-memory gate failed open on three spellings of one path** (`agent/authz.py`).
   `writes_durable_memory` tested `startswith("/memories/")` on the model's own spelling while
   `FilesystemMiddleware` normalises through `validate_path` before routing. `memories/a.md`,
   `/./memories/a.md` and `memories/sub/b.md` all answered `False` and all routed to Postgres — so
   under an unapproved plan the plan gate short-circuited and on a dry run the refusal never fired.
   Now normalised first, which also closes the exact-root `/memories` case the blanket deny was
   masking.
2. **The checkpointer's schema stamp refused every live session's next ordinary turn** on any deploy
   that adds a per-turn counter (`agent/checkpointer.py`). Five of the six stamped channels are
   `UntrackedValue` — never written to a checkpoint by *any* build — so the refusal pre-empted
   nothing for them and cost the fleet. The stamp is now the restorable channels only
   (`('active_agent',)`), with the excluded half named as `UNTRACKED_CHANNELS` so the partition test
   still reds on an accidental drop.
3. **The delivered truncation notice described an intermediate, not the tool**
   (`agent/tool_result_size.py`, `agent/tool_framing.py`). Framing nests the size cap and re-bounds
   after escaping; both place the notice at the same relative offset, so the second cut deleted the
   first's sentence and re-derived its arithmetic from the 60 kB middle. A 500 000-character
   connector result reached the model as "451 of 60,102 characters removed". The tool's real output
   size now travels on `response_metadata`, and the same flag stops the truncation counter and its
   log row firing twice per cut.
4. **Both turn caps were per-branch inside one superstep, so a `task` fan-out multiplied them**
   (`agent/loop_cap.py`). Measured `1 + W·(cap − 1)`: 25 model calls against a cap of 4 at width 8,
   and 193 against the shipped cap of 25. The count was right and the *bound* was not. A turn-wide
   count now lives on the watch object that already existed for this exact reason, with the cap
   comparison and the channel advance kept as two numbers so the channel is not inflated.
5. **A peer carried the rostered profile's authority, not the root's** (`agent/turn_graph.py`).
   Only `tool_names` was bounded; `harness_enabled`/`harness_autonomy` (which decide whether the plan
   gate is attached at all), `mcp_server_names` and `skill_names` travelled unbounded, and the
   connector half of the surface was never intersected. The derivation is inverted: a peer is built
   **from the root** and brings only `PEER_OWNED_FIELDS`, so a field added to `AgentProfile` is
   root-derived rather than fail-open.
6. **Two peer names minted one handoff tool** (`agent/handoff.py`) — `-` folds to `_`, so
   `property-lookup` and `property_lookup` are two legal profile names, one unreachable node, and two
   functions of one name in the request. Refused at build time beside the existing duplicate-name
   guard.
7. **The resume carry took `max()` over an additive reducer** (`api/graph_stream.py`), seeding a
   resumed turn with 4 of the 25 calls it had spent. It now folds the superstep the way `TurnTotal`
   does, and still cannot walk backwards.
8. **`propose_skill` hand-copied three of `validated_skill`'s four checks** and omitted the
   shipped-name conflict, so a proposal the accept route answers 409 for was recorded as `open` and
   reported as awaiting a decision with no way to accept it.
9. **An unreadable `tools:` key widened a skill's visibility** (`agent/skill_manifest.py`) — a
   missing entry reads as "declares nothing", which `ToolScopedSkills` leaves visible to everyone.
   Now scoped to nothing, keyed on the directory name `skill-validate` pins to the frontmatter.
10. **`review_commitments` defanged 2 of 8 externally-supplied fields** on a stated ground that was
    false: only `kind` and `state` are `Literal`s. The whole row goes through `defanged_payload`.

Deferred with a `BACKLOG.md` row rather than fixed, each for a stated reason: the `/mine` backend
predicate, the superseded-proposal state, the two turn paths that open no watch, and whether a
*restorable* channel addition should still drain sessions (that one is an ADR, because `D-2026-08-13`
chose the name comparison deliberately).

Did **not** survive my re-driving: the claim that `_signal_event`'s unguarded `NoteRecordedEvent`
tail is a live hazard — `mypy --strict` genuinely catches a new unhandled union member, driven with a
probe signal (2 `union-attr` errors). And the notice-arithmetic defect does **not** reproduce without
the `SERVED_BY` stamp, so my first reproduction was a false negative until I gave the request real
connector metadata; worth recording because the same mistake would have dismissed a HIGH.

## Wave 4 — the MCP fleet (`Chemclaw3-mcp`)

Merged as `d816f09` (#95): two safety numbers that were reassuring and wrong.

1. **`thermalsafety` treated a stated reference of 0 °C as an omission.** `reference_temperature_c`
   defaulted to `0.0` and the `or` fallback could not tell "not given" from "given as zero", so a
   TMR_ad asked at an ice-bath reference was silently extrapolated from the process temperature.
   Now `None`-defaulted with an explicit `is None` test; `tool-surface.json` regenerated, and the
   whole diff to the ratchet is `"default": 0.0 → null` and `"type": "number" → "number|null"`.
2. **The semi-batch RK4 integrator reported zero conversion for a dose too fast to integrate.**
   The step count was the caller's and nothing checked it against the real-axis stability limit, so
   a stiff dose diverged, the negative excursion was *clamped*, and the tool answered "no reaction"
   for the most dangerous case it can be asked. Now the step count is raised to
   `dose_time·λ / 2.785`, a dose past `MAX_INTEGRATION_STEPS` is **refused** by name (pointing at
   the mixing-limited regime and `thermalsafety`), and a real divergence raises instead of clamping.

### The rest of the fleet (read-only, driven)

Four findings survived, all reproduced; the fixes are Wave 4b.

- **A pod with `MCP_SESSION_IDLE_TIMEOUT_SECONDS=0` is permanently wedged by 7 empty `DELETE /mcp`s.**
  The unusable-session discard is installed only inside the reaper's setup path, and the
  compensating sweep reclaims only *terminated* sessions. Driven: `sessions_live` 1→8 at a ceiling
  of 8, every later handshake 503 forever.
- **`rxnlabel`'s `naming._namer` latches `_TRIED` unconditionally**, so a transient construction
  failure (MemoryError/EMFILE on first import) makes the pod Unready for the life of the process —
  while its sibling `mapping._mapper` recovers. Driven side by side: mapper 1→2 attempts and
  recovers, namer stays at 1 across 20+ probes.
- **`calculation_key` mints well-formed `xtb-absent` keys** for `optimize_geometry`,
  `relax_structure` and `compute_hessian` — the invariant `identity.py` claims holds "for every tool
  on this server". Mitigated by the readiness gate, confirmed 503 in that configuration.
- **The same missing-binary condition reaches the model as an opaque `internal error occurred`** on
  the opt/hess path, where `xtb_atomic.require_binary` argues at length that it must reach the
  chemist.

Clean under real drives: caller re-bind per tool call (alice's handshake → bob's call read bob), the
full readiness cause matrix (no transient produces a 503), secret redaction including tracebacks,
the admission ceiling (2 admitted / 6 refused in 50 ms), the session ceiling under a 64-way barrier
burst, the process-group kill against its naive counterfactual, and the metric-label clamp against
hostile tool names. No unbounded metric label anywhere in the slice.

## Wave 5 — the frontend (`Chemclaw3_ui`)

**No XSS and no token leak** — driven rather than read: 17 payloads through the real `Markdown` path
and hostile CXSMILES through real RDKit-WASM, all neutralised.

Three reproduced correctness defects:

1. **`resumeInterruptedTurn` counts prior identical questions over the *trimmed local* list while
   the search runs over the *full server* transcript.** A reload during a repeated question binds a
   stale answer into the new turn's bubble and marks it `done`. Driven: a three-week-old "no alerts
   fired" landed in a fresh genotox turn.
2. **`JobRecordSummary.state` is not declared in `src/api/client.ts`**, so `/jobs` renders a failed
   durable run byte-identically to a completed one — including "finished 6h ago".
3. **`Digest.disputed` and `Digest.headlines` die at the store**, so a digest card shows bare note
   ids and never says the corpus disagrees with a match.

Plus a decoded-then-dropped `answer.checks_run`, the already-recorded CSP/RDKit break (ISSUES.md
Issue 10, confirmed), and a `node="[object Object]"` attribute on every model-authored link, image
and code span.
## Wave 1 post-merge audit (`27fe89c2`) — the fixes were not all held

Every one of the ten fixes was mutated, in a worktree pinned at the merge commit. **Eleven mutations
stayed green over a live false statement; ten of those are real.** Ranked:

1. **`_carry_forward` is a measured no-op.** Its new docstring's load-bearing premise — "one `updates`
   payload is a whole superstep" — is false for this LangGraph version: driven with four parallel
   nodes writing one `TurnTotal` through `graph_events`' own call shape, the stream delivers **one
   node per payload**, so `base` has already advanced when the 2nd..Wth writers arrive and each
   contributes `max(value - base, 0) == 0`. The pre-fix `max()` produces the identical carry, and a
   full revert is green over 105 tests. A turn spending 25 calls across 8 helpers still resumes
   seeded `{"model_calls": 2}` — 23 calls of fresh allowance, exactly what the docstring says it
   must never do. `subgraphs=True` makes the real `task` case worse, since each helper has its own
   namespace and therefore its own payload.
2. **Five fixes ship no guard at all** — `authz.writes_durable_memory` (231 tests green on the
   revert, and it is an *authorization* gate: reverted, four spellings of a durable per-actor write
   short-circuit both the plan gate and the dry-run refusal), `turn_graph`'s peer narrowing (100
   green in full, 86 on the connector line alone, 149 with harness fields added back), `handoff`'s
   minted-name refusal (100), `_carry_forward` (105), and `commitment_tools`' defang (60). The
   existing peer-authority invariant test is blind to the connector half because `_mesh` passes
   `connectors=[]`; and `turn_graph.py:200` claims a test holds `PEER_OWNED_FIELDS` in both
   directions while the name appears in no test in the tree.
3. **`_is_untracked` misses the shape `state.py` itself cites as its origin.** It tests for a channel
   *instance*; upstream declares its own untracked channel with the *class*, and `state.py:49` quotes
   that spelling verbatim. Adding one channel that way puts it back in the stamp with all 19 tests
   green — and driven against real Postgres, a session written by the previous build has its next
   ordinary turn refused, which is the fleet-wide deploy failure the fix exists to close.
4. **The cut notice still overstates between the ceiling and ~4x it**, and the new guard is
   content-blind: `"Z" * n` never escapes, so swapping only the payload at the same three sizes reds
   merged code. A 59,900-character result — *under* the ceiling — reports `179,894 of 239,552
   characters removed`.
5. **The `_defanged` branch of that same fix is unguarded** (50 tests green while a 500,000-character
   `read_file` goes back to reporting `348 of 60,008`), and the count-once half has nothing behind it
   at all — the metric double-counts under mutation and the test only asserts `> before`.
6. Three more, each green: the skill sentinel is conservative only for the one `available` value its
   guard uses (set it to a real tool name and every unreadable-manifest skill becomes visible to any
   profile holding that tool, 91 tests green); `proposal_tools`' shipped-name refusal has no
   behavioural guard, only an import allowlist; and the loop-cap floor test calls `before_model`
   with no watch, which is the one configuration production never has — `api/runner` opens one on
   every turn, and with a watch held a thread already holding 25 assistant messages is authorised a
   26th call.

Plus: three live false sentences and now-dead code in `skill_manifest.py`, and a claim in the merged
commit body that `pending_tools` and `memory_tools` were closed — both still do two-field escaping,
over models carrying no `Literal` at all, which is *weaker* than the case the commit argued from.

**What this says about the wave, and it is the finding rather than the footnote:** the review log
recorded "each with the mutation that reds its guard watched failing" for all ten, and it was true of
five. The mutation rule in `tasks/lessons.md` exists for exactly this, and it was applied selectively.

Clean under the same audit: `make lint`, `mypy --strict` over 918 files, the authz fix's semantics
against `deepagents`' real router (12 spellings probed; the one remaining `False` is `//memories/a.md`,
which the router also sends to the default backend, so there is no gap), the new per-message span walk
at 0.44 µs regardless of payload size, the handoff collision caught whichever peer is current, all four
new `BACKLOG.md` rows verified against source, and `defanged_payload` leaving real SMILES byte-identical.

## Wave 7 — security and identity, end to end

Driven rather than read, against real listeners: 258 unauthenticated probes over all 43 front-door
routes × 6 credential shapes (no header, empty bearer, malformed JWT, Basic, an attacker-supplied
`X-Chemclaw-Actor: victim-oid`, and an `alg:none` JWT claiming `roles:["admin"]`). **Every one refused
except the three intentional probe routes.** The attacker-chosen actor header never reached an actor —
it is a send-side header only.

### The one HIGH: the redactor's own rendering step defeats its own rules

`core/logging.py`'s `PASSWORD` and `access_token|api_key|client_secret|…` patterns anchor on
`KEY["']?\s*[=:]\s*["']?`. In a `json.dumps`-escaped string the key is followed by a literal
backslash, so `["']?` matches nothing, `[=:]` meets `\`, and the rule never fires — while
`_redacted_field` renders every non-string `extra=` value with `json.dumps(value, default=str)` and
*then* scrubs. So the function that creates the escaping is the function whose rules the escaping
defeats. Its docstring claims the opposite outcome for exactly these three shapes.

```
plain=ok    escaped=LEAK   "{\"password\": \"W4rehousePw\"}"
plain=ok    escaped=ok     "{'password': 'W4rehousePw'}"      <- single quotes still caught
plain=ok    escaped=LEAK   "{\"api_key\": \"sk_live_9f3a2b1c8d7e6f\"}"
plain=ok    escaped=LEAK   "{\"client_secret\": \"Zx8~Q9abcdef123\"}"
```

The `extra=` half is latent — no caller in `src/` passes a container today. **Two callers are not
latent**, because they run `redact_secrets` over arbitrary text that then leaves the process:
`kg/record.py:168` commits the result to Git, and `deliver/message.py` sends it. Both driven leaking.
`core/tracing.py:107` shares the gap.

### Three more, each driven

- **A handoff is not refused under dry-run, and a dry-run turn durably reassigns the conversation.**
  `handoff.py:15` says it is refused; `dry_run_refusal` gates on `side_effecting_call` and
  `transfer_to_<peer>` is in neither half of that predicate. The other three claims in that sentence
  are true — the audit row lands and the receiving peer's write *is* refused, so the contextvar
  survives the `Command(goto=…, graph=Command.PARENT)` hop. But `active_agent` is checkpointed, so a
  turn the chemist marked "do nothing" leaves the conversation assigned to a different agent while
  the refusal on that same turn says "Nothing was started."
- **The plan gate's empty-session early return leaves nine side-effecting tools with no gate at all.**
  Its comment says "not a hole: those paths still pass through `enforce_tool_authz` and
  `authorize_trigger`". Measured for an authenticated user holding no app role: 12 of 15
  side-effecting registry tools are callable, three of those are in `expensive_actions()`, and the
  remaining **nine** are refused by neither. Reachable with an empty session id on the two paths the
  comment itself names. `run_composed_workflow` is the one that does *not* fail open, because it
  re-checks at run time.
- **A whitespace-only actor passes the reject-if-absent rule** and mints its own memory namespace —
  the "memory nobody can erase" that `scratchpad.py:41` says it avoids.

### Clean, with what drove it

The metric-label cardinality sweep in both repos (5 junk paths and a unique session id folded to
`route="<unmatched>"`; 8 caller-invented tool names including a traversal string and a 200-character
name folded to `tool="<unknown>"`). Bearer on `/mcp` — the fleet's own 22 tests against real uvicorn
listeners, plus **19 hand-driven mount-bypass spellings** (`/mcp/`, `//mcp`, `/./mcp`,
`/healthz/../mcp`, `/%2e%2e/mcp`, `/..;/mcp`, …) all 401. Both authority invariants under
composition, including a hostile profile naming every side-effecting tool plus odd spellings, used as
both a roster specialist and a peer under a one-tool root: nothing reached. The egress guard's nine
rebindings match the prose in both directions, and three of the four channels it names as outside
itself are genuinely open (the fourth has no installed instance — `grpcio` is in `uv.lock` but absent
from `uv export --frozen`, which is what the images install, so the fleet's own measurement of it
cannot be re-run). No shell, no web tool, and 10 real turns of path traversal reaching no host file.
19 credential shapes × 5 record channels = 95 cells, 90 clean, the 5 failures being the HIGH above.
On the UI: MSAL in `sessionStorage`, transcripts keyed by Entra `oid`, a CSP with no `unsafe-inline`
and no `unsafe-eval`, and the single `dangerouslySetInnerHTML` sink driven with 9 crafted CXSMILES
carrying `<script>`, `<img onerror>` and `"><svg onload>` — no markup reached the SVG. Worth
recording that this RDKit build *drops* the label rather than escaping it, which is a weaker
guarantee than escaping.

## Wave 6 — cross-repo contracts and the regression surface

**The Python↔Python seam is in good shape and genuinely tested; the core↔UI seam is not.** Eight core
response fields added in the last three weeks never reached the UI — several added *specifically* to
fix a named failure — and two are **destroyed** in transit rather than merely untyped.

- **`GET /notes/{id}` sends `confidence: null` and `NoteSheet.tsx` calls `.toFixed(2)` on it.** Four of
  the five note producers in `memory/` mint notes with no confidence. Driven by taking a real note
  through core's real projection and running the literal UI expression in node:
  `TypeError: Cannot read properties of null (reading 'toFixed')`. `source` and `compound_smiles` are
  the same nullability lie; both call sites happen to guard.
- **The digest's `disputed` and `headlines` are dropped by `addDigests`, and the server's copy is
  already gone** — `claim_unconsumed` is a destructive mailbox read, so the store entry is the only
  copy. Core's own docstring names both consequences: `headlines` exists because "without it this
  route answers with note **ids** and a client can do nothing but print them", `disputed` because "a
  chemist who happens to ask is told, and a chemist watching the subject is not."
  **The UI's own contract test already sees these two and passes green**, because its axis 6 *lists*
  sent-but-undeclared fields instead of failing on them.
- **The plan-approval card collects a yes to a tool scope it never displays.** `scope` is on
  `PlanStatusOut` and `PendingPlan` but not on `PlanEvent` — and `plan_hash` is on the *stream*
  precisely so a client need not fetch, so `Prompts.tsx` returns early and the only path carrying
  `scope` is never called. `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` §3
  states the requirement in as many words. Not a gate bypass — `plan_identity` hashes each step's
  declaration, so a widened scope 409s — but non-disclosure.
- **The wiring order both repos publish silently removes the `safety-screening` skill.** The fleet's
  `safety` manifest omits `skills:` deliberately ("whoever wires this server up must keep that skill
  reachable"); `skills_dirs()` derives from the **winning** manifest; and both repos' published
  snippets put the mounted directory first. Driven both orders: mounted-first gives
  `safety declares skills: []` and `safety-screening reachable: False`. The remedy
  (`CHEMCLAW_SKILLS_DIR`) is named in no wiring document in either repo. Invisible to the existing
  cross-repo test **by construction**: `_SURFACE_FIELDS` is three keys and `skills` is not one of
  them. Checked all five uncompared keys across the three ported bundles — `safety.skills` is the
  only divergence.
- **A degraded label is stamped as current and never re-labelled.** The fleet returns a per-answer
  `version` and a `degraded` list specifically "so it is stale against a healthy pod and re-labels";
  core's `ReactionRepresentation` is `extra="ignore"` and declares neither, and `label_sync` writes
  one pass-level `labeller_version()` — which probes components rather than this call, so it reports
  the healthy string. The row leaves `stale()` and nothing revisits it.
- **`/jobs` and `/pending` are paginated, the server says so, and the UI shows neither page nor
  marker** — against server-side additions whose own docstrings name the symptom ("the listing looked
  complete"; "35 waiting rows rendered as 20 with no marker anywhere in the response").
- **Two false cross-repo claims, in the two files whose job is to be right about the other repo.**
  `test_sibling_manifest_agreement.py` declines to check `rxnlabel` because it has "no
  `tool-surface.json`" — written nine days *after* the fleet shipped one, in a wave titled "the
  cross-repo contracts", in the file whose subject is
  `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it`. And
  `science/calc/__init__.py` says `CALCULATION_EPOCH` "is the one constant both repositories must
  change in the same PR", which `connectors/calc/remote.py` already records as false.

Clean, driven: all 18 SSE event models diffed against all 18 UI valibot members — membership
identical, every field set identical, nothing dropped either way, and all six closed sets matching
exactly. The whole 23-type `protocols.ts` surface field-for-field identical. Every fleet manifest
loaded through core's real loader; `manifests-internal/` still a startup error naming the file. All 11
servers' manifest-vs-running-server proofs green. The port registry agrees with every manifest and
every Deployment in both trees, with no collisions, and the mcp CLAUDE.md's cross-repo port claims are
all true today. The epochs **compose** — all four core×server combinations distinct, a bump on either
side alone invalidating every row. Header spellings match character-for-character in both directions.

## Wave 8 — deployment and operability

Both HIGHs are a monitoring system failing in the specific incident it was written for.

- **A durable job whose activity result the broker refuses for size is invisible to every first-party
  metric, and our own log line says the attempt completed.** `_ObservedActivity.execute_activity`
  wraps `self.next.execute_activity(...)`, but payload conversion and `RespondActivityTaskCompleted`
  happen *after* that returns, in the worker's task handler — outside the `try/except` that books
  `chemclaw_activity_failures_total`. Driven against the live broker at 6 MB (over the gRPC frame
  limit) and 3 MB (over the server's blob limit): `activity.finished … completed` both times, the
  counter flat, and the 6 MB workflow still `RUNNING` two minutes later. So
  `ChemclawActivityRetryStorm` reads a series that stays flat, and `ChemclawDurableJobsFailing` is
  worse than blind — it is **actively suppressed**, because `jobs_finished_total` is booked inside the
  workflow that never reaches its terminal write, so `started` climbs and `failed` does not. The only
  evidence is a Rust-side WARN that is not on Python's logging, carries no correlation id, and is
  worded "Network error while completing activity".
- **A connector that is up but returns 500 or garbage on `/mcp` is reported healthy.** The sweep is
  `GET /healthz` and nothing else: `{"status":"ready","connectors_unhealthy":0}` against a connector
  that fails every call, with `ChemclawConnectorsUnhealthy` measured at 0. A real turn does tell the
  chemist and does move `chemclaw_connectors_unreachable_total` — which no alert reads, no runbook
  mentions, and which is unlabelled, so even on a dashboard it cannot say which connector. The log
  line prints the `ExceptionGroup` unwrapped, so the HTTP 500 never reaches it.
- **The chart instructs operators to watch a series the alert set and the runbook do not carry**
  (`chemclaw_turn_spend_caps_total`), and a spend-capped turn additionally books
  `chemclaw_turn_empty_answers_total`, firing an alert whose description *and* runbook entry both say
  "No error counter moves" — measured false. The chemist gets two contradictory error events in a row
  with opposite `retryable` flags.
- **`/readyz` answers 200 `ready` against a completely un-migrated database** under the chart's
  shipped `CHEMCLAW_SESSION_STORE=postgres`. The positive-evidence-only trade is argued at the site,
  but the shape it admits is wider than the case it argues: no schema at all is indistinguishable
  from an unreadable ledger, and the pod joins the Route and fails every session write. The
  schema-*behind* case, by contrast, answers 503 naming the migration and both remedies.
- **`mcpFace.route.enabled=true` renders a Route the chart's own mcp-face ingress policy does not
  admit** — no router peer, while the front door in the same file has one.
- Three lows, each an inert control: `chemclaw_tool_calls_total`'s error outcome has no alert;
  `ChemclawCalcBackendOverCommitted` is guarded `> 0` against a shipped `"0"`, so **the alert
  protecting every calculation in the system is inert as shipped**; and a worker with Temporal down
  exits before its probe port opens, so "Temporal is down" and "the image is broken" look identical
  from outside the container log.

Clean, driven: all three chart refusals fire in order, each naming the value and why. Every rendered
workload's full env actually constructs `Settings` — 37 containers across four renders, all green —
and the connection-budget guard is genuinely derived, refusing with a computed sentence naming both
sides at `maxReplicas` 40 and 200. No PDB anywhere can block a scale its HPA can reach; no
single-replica Deployment has one. 105 migrations idempotent, guardedness driven by truncating the
ledger, and **both documented replay recipes verified end to end** — every lock or rewrite hazard
documented at its own site, with no undocumented one found. All 56 `chemclaw_*` names in the runbook
resolve in `src/`, every `make` target and every named table exists, and every rendered alert has a
runbook entry. The live lane came up on 11 processes with **no credential of any kind** and ran 337
probes green. And a sweep of all 19 numeric settings defaulting to `0` found no second instance of
the `document_parse_memory_bytes` unhandled-sentinel shape.
