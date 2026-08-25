# Implementation plan — closing the 2026-08-25 field benchmark

**Status: W3.2 is built and open as [`Chemclaw3-mcp` #12](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp/pull/12),
not merged. Everything else is planned and not started.**

The findings and their measurements are in
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md);
the queued rows are `docs/planning/BACKLOG.md § 5` (twelve) plus one in § 4 (the credential guard).
This file is how they get done: the order, the spec for each, and the check that says it is done.
Nothing here restates the *why* — that is the review's job and duplicating it is how both go stale.

**Two rules this plan is arranged around.**

1. **Nothing that cannot be graded gets built.** Six of the thirteen rows are improvements to a
   number, and four of those numbers do not exist yet. So the first wave builds the instruments and
   nothing else — a test that pins the context floor, a test that pins probe coverage, a working
   live lane, and a suite that is not red for an environmental reason. Until those land, "the tool
   surface got cheaper" is an opinion.
2. **Two rows are decisions, not tasks.** The Temporal plugin gets a timeboxed spike and an ADR
   that may say *no* — a plan that assumes the answer has pre-decided it, which is the failure
   `D-2026-08-15` records about the specialist team. Code execution was the second, and it is no
   longer open in that form: W3.2 now carries a concrete proposal (`pyexec`, one stateless tool in
   the fleet that already cannot reach the network), so its ADR decides a *design*. The direction
   was settled by the thing the abstract question kept missing — the declination on record refuses
   two specific sandboxes and says nothing about execution as such.

Sizes are the backlog's: **[S]** ≤ a day, **[M]** a few days, **[L]** a week or more.

---

## Wave 0 — Build the instruments (nothing else starts until these land)

### W0.1 — Guard the live tests on reachability, not on a variable being set · [S]

*Row: § 4, "Two tests guard on a credential being present…". Blocks: an honest `make test`, and W0.4.*

**Spec.** `tests/test_prompt_caching.py:304` skips on `"API-KEY" not in os.environ`, and
`test_which_shipped_profiles_clear_the_cache_floor` reads `os.environ["API-KEY"]` directly. Both
test for the *variable*, not the *credential*. On this box the variable is set and the value is
rejected, so both run and die on a 401 and `make test` returns `3 failed` on an unmodified tree.

**Steps.**
1. Add one module-level helper — `_live_credential() -> str | None` — that returns the key only if
   an unbilled `client.messages.count_tokens(...)` round-trips. Cache it at module scope; it must
   run at most once per session.
2. Replace both guards with `@pytest.mark.skipif(_live_credential() is None, reason="no reachable
   Anthropic credential")`, and have the reason distinguish *absent* from *rejected* — the two are
   different operator problems and a skip line is where an operator reads which one they have.
3. Do **not** broaden it to catch every exception: a 401 and a 529 are not the same thing, and
   swallowing a rate limit into a skip is how a suite stops testing.

**Acceptance.** With no `API-KEY`: 2 skipped, reason says absent. With a rejected `API-KEY`:
2 skipped, reason says rejected. With a working one: both run. `make test` green on all three.

**Also fix nothing else here.** `test_reizman::test_bo_campaign_finds_high_yield` timed out at 180 s
under four concurrent pytest processes and passes in 49 s alone. It is recorded beside the row so
nobody hunts a BoFire regression; it is not a defect and it does not get a "fix".

---

### W0.2 — Pin the static context floor in a test · [S]

*Row: § 5 #1, first half. Blocks: W1.2, and it is what grades it.*

**Spec.** `tests/test_context_floor.py`. Compose what a turn is compiled against for the default
profile — `instructions_for(profile)`, every tool in `_capability_tools(profile)` serialised as
name + description + `args_schema.model_json_schema()`, and the skills listing
`SkillsMiddleware` publishes — and assert the total against a committed ceiling.

**Details that decide whether this test is worth having.**

- **Count with `langchain_core.messages.utils.count_tokens_approximately`**, not a hand-rolled
  `len/4`. The review used `chars / 4` because it is the estimator
  `agent_context_token_budget` budgets against; a *test* should use the one function the repo can
  keep consistent, and the review's numbers are then the historical figure rather than the gate.
- **Assert a ceiling, not equality.** An exact figure fails on every docstring edit and gets
  bumped without thought, which is a ratchet that ratchets the wrong way. Ceiling + the measured
  figure in the failure message.
- **Assert per-profile, not once.** `registered_profile_names()` — a narrow profile that is not
  cheaper than the full one is a profile that is not narrowing anything, and that is a second bug
  this test finds for free.
- **The failure message is the deliverable.** It must print the per-tool breakdown sorted
  descending, so the person who tripped it sees which schema they grew.

**Acceptance.** `pytest tests/test_context_floor.py -q` green; deliberately adding a 3 kB docstring
to a tool turns it red with a message naming that tool.

---

### W0.3 — Pin probe coverage of the tool surface · [S]

*Row: § 5 #2, first half. Blocks: W2.1, and it is what grades it.*

**Spec.** `tests/test_probe_coverage.py`, modelled on `tests/test_repo_map.py` — which already
proves the pattern of checking a declaration against the tree **in both directions**.

1. Every name in `available_tool_names()` appears in at least one probe's `expects_tools`, **or** in
   a committed exemption list with a one-line reason per entry.
2. Every name in any probe's `expects_tools` is in `available_tool_names()` — a probe naming a tool
   that no longer exists is a probe that can never fail correctly. (Measured today: zero such, and
   that is worth keeping true.)

**The exemption list is the design decision here.** Some of the seventeen genuinely should not have
a probe — `write_todos` is the plan surface and `data/evals/probes/m12/plan_gate.yaml` drives it as
a *conversation*, which is the right shape and not an `expects_tools` entry. Each exemption names
the suite that covers it instead. An exemption with no such pointer is not an exemption, it is a
hole with a note on it.

**Acceptance.** Red on today's tree naming all seventeen. Green after W2.1 with an exemption list
that is short and each of whose entries names another suite.

---

### W0.4 — Make the live lane start · [M]

*Row: § 1, "No live lane in this repo can start". Blocks: W2.1's verification, W2.3, W2.4, and the
live half of everything the review could not measure.*

**Spec.** `infra/live/processes.sh:47` pins `CHEMCLAW_CONNECTORS_REQUIRED=true` while `chem` and
`safety` are enabled and never started — `cli/connectors_dev.py:78` emits URLs only for bundles with
a local app, so those two keep loopback defaults and `check_connectors_at_startup` raises. Their
capability is `Chemclaw3-mcp`'s.

**Steps.**
1. Point the lane at a running `Chemclaw3-mcp` for those two: extend `infra/live/processes.sh` to
   start the fleet's `chem` and `safety` servers (the repo is already cloned by
   `infra/live/e2e-full-stack/up.sh`) and export `CHEMCLAW_CONNECTOR_URLS` for both.
2. Leave `CHEMCLAW_CONNECTORS_REQUIRED=true` alone. The pin is the point — LIVE-8's lesson is that a
   configuration only production sets is a configuration nothing tests, and relaxing it to get a
   green boot deletes the test.
3. Separately, and **not** in the same commit: `infra/live/e2e-full-stack/up.sh:185` puts
   `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR`, which `connectors/calc/connector.yaml:13`
   forbids and which survives only on `registry.py:124`'s first-dir-wins. That ordering *is* pinned
   (`tests/test_connector_registry.py:293`), so this is a latent-not-broken and gets its own change.

**Acceptance.** `make live-up && make live-status` shows the front door and four workers up, and
`make live-probes --suite grounded` returns transcripts under `tasks/live-test/`.

**Risk.** This is the row most likely to be bigger than it looks, because it is the only one whose
blocker is another repository's runtime. Timebox to three days; if it overruns, W2.1 proceeds
against the in-process agent with the caveat recorded, and W2.3/W2.4 slip rather than the rest.

---

## Wave 1 — Context economics (graded by W0.2)

### W1.1 — Give the lossless context edit its own trigger · [S]

*Row: § 5 #8. No dependencies. Do this first in the wave: it is the smallest real win in the plan.*

**Spec.** `context_compaction_middleware` hands both `ClearToolUsesEdit` (lossless) and
`KeepLastConversationGroupsEdit` (destructive) `trigger=settings.agent_context_token_budget`. One
knob at 100 000, so nothing reduces until 100 k and then both fire together.

1. Add `agent_tool_result_clear_trigger: int = Field(default=30_000, ge=1)` to
   `core/config/agent.py`, with the comment saying what it is *for*: a lossless edit is cheap enough
   to run early, and every token it reclaims early is a conversation group the destructive edit never
   has to reach for.
2. Pass it to `ClearToolUsesEdit`. Leave `KeepLastConversationGroupsEdit` on the budget.
3. Validate `agent_tool_result_clear_trigger <= agent_context_token_budget` at settings level — the
   inverted setting is silently equivalent to today's behaviour, which is the worst misconfiguration
   of the two because it looks like it took effect.

**Acceptance.** A third case in `tests/test_compaction.py` (which already drives both edits): a
thread over the clear trigger but under the budget has its old tool results placeholdered and
**every conversation group intact**. `chemclaw_context_compactions_total` ticks.

**Explicitly out of scope.** Turning the summarizer on. `disabled_summarizer`'s argument stands: a
summary is new model prose over content `agent/framing.py` marked untrusted, and the envelope does
not survive it. Anyone reopening that needs an ADR, not a config change.

---

### W1.2 — Defer the cold tool tail · [M]

*Row: § 5 #1, second half. Depends on W0.2 (nothing else can say whether it worked).*

**Step 1 is a measurement, not code.** Over the 232-probe corpus, rank every tool by how many probes
name it. Today's head is `gather_evidence` 116, `find_notes` 91, `expand_note` 60; the tail is thin.
Publish the ranking in the change. **A tool is "cold" if the corpus and the profile narrowing agree
it is** — a tool no probe names *and* that no profile makes central. Both, not either.

**Step 2 — the mechanism, and the decision inside it.** Three routes, in descending order of
preference:

- **(a) Profile narrowing, harder.** `_capability_tools(profile)` already attenuates. If the seven
  shipped profiles were narrow enough, most of the 8.6 k would go away *with no new machinery and no
  new failure mode*. Cost this first; it may be the whole answer, and it is the only route that adds
  nothing to maintain.
- **(b) A search tool over the cold tail.** One `find_tool(query)` returning name + schema for
  matches, with the cold tail unadvertised. This is a real behavioural change — the model cannot
  call what it cannot see — and it goes behind a setting, defaulting **off**, until the corpus says
  the same probes still reach the same tools.
- **(c) Provider-native deferred loading.** `langchain-anthropic` 1.5.6 is installed; whether it
  surfaces the `defer_loading` beta is a question to answer, not assume. If it does not, this route
  is a fork of the provider seam and it is the wrong trade for this repo — F0's whole point is that
  the provider is a seam, and an internal OpenAI-compatible endpoint will never have this beta.
  Record the finding either way; that is `tests/test_upstream_surface.py`'s job.

**Acceptance.** Floor under 8 k in W0.2's test **and** every probe's `expects_tools` still satisfied
in a live run (needs W0.4). If the second cannot be shown, the change does not ship — a cheaper
prompt that stops finding tools is a regression with a good-looking metric.

---

## Wave 2 — Evaluation (graded by W0.3)

### W2.1 — Write the seventeen missing probes · [M]

*Row: § 5 #2, second half. Depends on W0.3 for the target and W0.4 to run them.*

Order by risk, not by convenience — the newest surface first, because it is the least exercised:

1. **Scratchpad / filesystem (6)** — `ls`, `glob`, `grep`, `read_file`, `write_file`, `edit_file`.
   These are the context-management capability `D-2026-08-11` restored, and they currently have zero
   behavioural coverage. Probe the thing that matters: a turn that writes an intermediate finding to
   the scratchpad and reads it back after the window has moved.
2. **Memory / preferences (3)** — `remember_preference`, `recall_preferences`, `forget_preference`.
   A two-turn probe: state a preference, then check the next turn honours it without being told.
3. **Watches (2)** — `list_watches`, `stop_watching`. `watch_for` is already covered; its inverse is
   not, and "unsubscribe silently does nothing" is the classic shape here.
4. **Attachments (1)** — `read_attachment`. `list_attachments` is covered.
5. **Science (3)** — `compute_interaction_energy`, `run_conformer_refinement`, `render_structure`.
6. **Exempt with a pointer (2)** — `task` and `write_todos`, each naming the suite that drives it as
   a conversation rather than a turn.

**Every probe carries a *direction*, never a key** — the corpus's existing rule
(`docs/archive/vibe-test-2026-07.md`), and the reason bucket C exists.

**Acceptance.** W0.3 green with a two-entry exemption list. A live pass over the new probes with
transcripts on disk, because a finding whose reproduction is not on disk is a claim.

---

### W2.2 — Put cost on the eval axis · [S]

*Row: § 5 #9. Depends on nothing. The cheapest row in the plan.*

**Spec.** `agent/turn_cost.py` and `core/metrics.py` already produce the number; `evals/metric.py`'s
`@metric` registry already takes a float. Register `turn_cost` and score it per case.

**The design decision is what the metric's value is**, and `autonomy-plan-execute-utility` already
settled the general form: a metric's value is a single float, and an unbounded quantity denominated
in each case's own units cannot carry a drift band. So score **cost per case relative to the
baseline's cost for that case**, not dollars. Dollars go in the provenance string.

**Ungated at first, deliberately** — the same posture as `plan_execute_utility`. "What does this
answer cost" is a progress number until there is enough history to say what a regression looks like.

**Acceptance.** `make eval` prints a `turn_cost` row per case with dollars in the provenance;
`data/evals/baseline.json` carries it; `make eval-baseline-check` is unaffected while it is ungated.

---

### W2.3 — A bucket-C control arm · [M]

*Row: § 5 #3. Depends on W0.4.*

**Spec.** ChemToolAgent's finding is that tool augmentation does not consistently beat the base
model, and hurts on general chemistry questions. This repository cannot currently reproduce or
refute that about itself. `evals/ab.py::compare_tool_utility` is written and registered.

**Steps.** Run a sample of bucket-A and all bucket-C probes twice — full tool surface, and a
tools-free profile — and score the pair. Report the helped share, exactly as
`plan_execute_utility` does.

**What makes this worth doing rather than interesting:** if the answer is that tools hurt on some
band of questions, that is a *profile* change with a measurement behind it, which is the only kind
this repository accepts. If the answer is that they always help, W1.2's route (b) has a much higher
bar to clear.

**Acceptance.** A committed run with the helped share and the per-probe deltas, and a paragraph in
the eval report saying which band, if any, is worse with tools.

---

### W2.4 — Run one external benchmark · [M→L]

*Row: § 5 #4. Depends on W0.4. The single highest-value row in the plan and the one most likely to
be humbling.*

**ChemRAG-Bench first, and the choice is not arbitrary.** It scores the retrieval half, which is
where this system's science actually lives; it is 1,932 expert-curated pairs against a 7-document
corpus and a 39-note graph, so it will produce a number rather than a tie; and it runs against an
OpenAI-compatible endpoint, which `agent/llm_provider.py` already is.

**Steps.**
1. A thin adapter under `evals/external/` that drives the front door and emits the benchmark's
   expected output shape. It calls the **front door**, not `build_langgraph_agent` — `evals/live.py`
   already argues this at length and the argument does not change per benchmark.
2. Run it. Record the number and the cost, and record what the run *cannot* say: this system is not
   a general chemistry QA system and a corpus mismatch is a finding about applicability, not a
   grade.
3. Only then consider ChemBench (knowledge, not retrieval) and AstaBench (whose controlled
   environment is the interesting part and also the largest integration).

**Acceptance.** One committed number with its cost, its date, its model, and one paragraph on what
it does and does not measure about this system.

**Risk.** The likeliest outcome is a low score for a legitimate reason — the corpus is not ours. The
plan's failure mode is treating that as a verdict rather than as a calibration. Write the "what this
does not measure" paragraph *before* seeing the number.

---

## Wave 3 — Two decisions

W3.1 is a spike whose deliverable may be "no". **W3.2 is no longer a spike**: it carries a
concrete proposal, so its ADR decides a design rather than a direction, and the build follows it in
`Chemclaw3-mcp`.

### W3.1 — Evaluate the Temporal LangGraph plugin · [L] → ADR

*Row: § 5 #5.*

**What is already known** and does not need re-deriving: the plugin reached public preview
2026-07-16, needs Temporal Python SDK ≥ 1.27 and Python ≥ 3.11, and **this repo already ships
`temporalio` 1.31.0 on Python 3.11** — so the version bar is met today. Graph runs as a workflow,
node as an activity, checkpoint per node, `interrupt()` durable and signal-resumed.

**The spike answers three questions and nothing else.** Timebox: five days.

1. Does `interrupt()`-as-signal close **"A decided approval hold can be reopened"**? That row's fix
   is to read the prior run's terminal outcome and refuse restart only on an actual decision —
   expiry deliberately *completes*. Does the plugin's own id-reuse posture make that easier, harder,
   or unchanged?
2. Does it close the queued **durable approval store** row, and what happens to
   `agent/plan_approval_store.py`'s two-backend design (which follows the session store precisely so
   the approval cannot outlive or be outlived by the mode it authorises)?
3. What does it cost against **`agent/checkpointer.py`'s three measured reasons for its own pool** —
   `CREATE INDEX CONCURRENTLY` outside a transaction, the per-saver `asyncio.Lock` that `alist`
   yields inside, and pipeline mode on a borrowed connection. If the plugin's durability replaces
   the saver, all three arguments are moot; if it sits beside it, there are now three layers.

**The line the ADR must not cross.** `D-2026-08-10 §3`: Temporal keeps every long or expensive job,
layer 1's checkpointer holds turn state and nothing else. A plugin that makes every model call a
Temporal activity is a much larger change than "durable interrupts" and needs to be argued as one.

**Deliverable.** An ADR, plus — if the answer is yes — a migration plan as its own document. Not
code in the same change.

---

### W3.2 — `pyexec`: a Python analysis sandbox, as an offline MCP server · [L] → ADR + build

*Row: § 5 #6. **This row now carries a concrete proposal**, and the proposal is what makes the ADR
answerable — the previous framing asked "should the agent execute code" in the abstract, which is
not a question anybody can close.*

**Restate the current position, because the proposal depends on it being narrow.**
`agent/scratchpad.py::scratchpad_tools` withholds `execute` and `delete`. The `execute` argument is
that deepagents 0.7 ships exactly one concrete sandbox (`LangSmithSandbox`, declined because it
egresses conversation content to a third party) and that `LocalShellBackend` is documented as
unrestricted. **That is a correct refusal of two specific sandboxes.** Neither sentence is an
argument against execution as such, and the consequence is much wider than the argument: numpy,
pandas, scipy and RDKit are all installed in the very process the agent runs in — measured, 2.4.6,
3.0.3, 1.17.1 and 2026.3.5 — and the agent can reach none of them. It cannot canonicalise an
unexpected SMILES, fit a kinetics curve, or aggregate a table a tool just returned.

#### The proposal

**Do not give the agent a shell. Give it one stateless MCP tool, in a server that already cannot
reach the network, and make the operating system the security boundary rather than Python.**

`Chemclaw3-mcp` is the right home and not a convenience: it already enforces no-egress at four
independent layers (a runtime `socket.connect` guard armed on import, an AST scan per server, the
whole suite run with the guard armed, and a default-deny `NetworkPolicy` asserted in both
directions), and `make offline-run` proves it by *removing the network* and checking every answer is
unchanged. That is a far stronger posture than anything reachable from inside this repository, and
it is already built, tested and shipped.

So: **`servers/pyexec/`, port 8899, one tool.**

```
run_python(code: str, data: dict | None = None) -> RunResult
```

It runs `code` in a throwaway child process with `numpy`, `pandas`, `scipy` and `rdkit` importable,
binds `data` into the namespace as a plain dict, and returns captured stdout plus whatever the code
assigned to `result`, JSON-serialised. No session, no persisted namespace, no files that outlive the
call — the fleet's statelessness rule, which is also what makes the sandbox disposable.

**Eight controls, and the honest statement of which ones are load-bearing.**

| | Control | What it stops |
| --- | --- | --- |
| 1 | Child process, never in-process `exec` | An escape reaches a disposable process, not the server |
| 2 | `start_new_session=True` + `killpg` on timeout | A run that spawns children still dies whole — the `calc` server's own `run_isolated` lesson, where a naive timeout killed one PID and left the rest burning CPU |
| 3 | `setrlimit` on CPU, address space, file size, process count, file descriptors — **hard limits, so they cannot be raised back** | Infinite loops, memory exhaustion, fork bombs, disk filling |
| 4 | Environment built from an **allowlist**, not by deleting | Bearer tokens, DSNs and `CHEMCLAW_*` settings never enter the child |
| 5 | `python -I -B`, fresh temp cwd, `HOME` pointed inside it | Ambient `PYTHON*` config, user site-packages, cwd imports |
| 6 | Import guard: a `sys.meta_path` finder plus a purge of `os`, `socket`, `subprocess`, `ctypes` and friends from `sys.modules`, after the heavy libraries are warmed | A casual reach for the filesystem or the network |
| 7 | `socket.socket.connect` patched to raise *before* the purge | A held reference to an already-imported module |
| 8 | Default-deny `NetworkPolicy`, no DNS | Everything above being wrong |

**Rows 6 and 7 are ergonomics and defence in depth. They are not the boundary.** A Python-level
sandbox is porous — `().__class__.__mro__` and its relatives are a research area, not a solved
problem — and a design that claims otherwise is the `map_to_hpc_identity` shape: a control that
exists in order to be pointed at. The boundary is rows 1–5 and 8. Even granting a complete escape
from the import guard, the escapee holds a scrubbed environment, a temp directory, hard resource
limits, a rootless read-only container and no route off the pod. **The README and the tool docstring
must say this in these words**, because the failure mode of a sandbox is a reviewer believing a
stronger claim than the one it can support.

**Classification: `read_only`.** It writes nothing, persists nothing, and has no effect outside a
directory deleted before it returns. That is not a technicality — `read_only` is what lets the agent
use it *while building the plan a human is asked to approve*, and an analysis that cannot run until
after approval is an analysis that cannot inform it.

**What this deliberately is not.** Not a shell. Not a notebook — no state survives a call. Not a
file-processing tool: `open` is removed from builtins, so the only way data gets in is `data` and the
only way out is `result`. Not a route to the knowledge graph, the ELN or any other tool. It is a
calculator with a scientific library on it.

#### Steps

1. `servers/pyexec/` following the `props` reference layout exactly: `engine/` (pure, no transport),
   `tools.py` (the FastMCP surface — the docstring is the prompt), a three-line `app.py` over
   `connector_app`, `Containerfile`, `deploy/networkpolicy.yaml`, `README.md`, and `connector.yaml`
   symlinked into `manifests/pyexec/`.
2. Tests, and the ones that matter are adversarial: a timeout is killed, a fork bomb is refused,
   `import socket` fails, `open` is gone, the environment carries no token, memory is bounded, output
   is truncated. Plus the fleet's standing set — `test_no_egress`, `test_deploy`, and
   `assert_manifest_matches` against a running server.
3. `MODULES.md`: a catalogue entry and the port registry row (8899, deliberately outside the
   thematic bands — this is not a chemistry data source).
4. Only then, on the Chemclaw3 side: nothing but a manifest directory and a URL. Zero core edits,
   which is D-118 and `D-2026-08-09-a-connector-we-do-not-run` working as designed — and is also the
   proof that this proposal does not need the `execute` verb it declines.
5. The ADR records the decision and, importantly, **what was declined**: a shell, a persistent
   namespace, and `LangSmithSandbox`.

#### Acceptance — met, and here is what each one returned

`make check` in `Chemclaw3-mcp`: **985 passed**, ruff clean, `mypy --strict` clean.
`make offline-run`: **985 passed with the network removed**. `assert_manifest_matches` green against
a running server. And from this checkout, with the server up and `CHEMCLAW_CONNECTORS_DIR` pointed
at the fleet's manifests: `run_python` appears on the agent surface, a live call returns
`{"ok": true, "result": {"mean": 3.0, "sd": 2.16}}`, `import os` is refused over the same wire, and
`git diff src/` is **empty**.

**What the build changed about the plan, both times because a measurement said so.** The import
guard was designed as a `sys.modules` purge plus a `meta_path` finder; it broke `scipy.optimize`'s
lazy `import sys` *and* did not hold, because `import` reads `sys.modules` before any finder. It is
a replaced `__import__` in the analysis namespace instead. And the runner was designed to warm the
scientific stack; measured at 1.2-1.9 s on every call against an 11 ms empty child, so nothing is
pre-imported. Both reversals are in `D-2026-08-25-a-sandbox-is-a-server-not-a-verb`.

**Not merged, deliberately.** It runs code a language model wrote. The gates are green and the
design is argued, but the control table in `servers/pyexec/README.md` — which half is a boundary and
which half is not — is a thing a person should read before this ships.

#### The risk to state plainly in the ADR

This is the first tool in the fleet whose *input is a program*. Every other server takes a SMILES or
a solvent name. Prompt injection that reaches the model reaches this tool, so the controls have to
hold against a hostile author rather than a careless one — which is why the boundary is the process
and the deployment, and why row 6 is written down as insufficient on its own.

## Wave 4 — Capability, mostly in the other repository

### W4.1 — Re-derive the MCP catalogue against 2026 releases · [S] · `Chemclaw3-mcp`

*Row: § 5 #10.* Three entries, in build order:

1. **`nomenclature`** — OPSIN, MIT, runs locally, zero licence risk. The catalogue already calls it
   "the best value-to-effort ratio"; nothing has changed except that it is still not built.
2. **`admet`** — re-derive against Boltz-2 (MIT, approaching FEP accuracy at ~1000× the efficiency).
   Note the shape mismatch honestly: Boltz-2 is protein–ligand affinity, and the catalogue's `admet`
   entry is a property panel. They may be two servers.
3. **`retro`** — adopted-not-built. RetroReasoner / Retro-R1 changed what a server would wrap.

**Its own PR in `Chemclaw3-mcp`**, per `CLAUDE.md`'s rule that a companion-repo change is never
proxied through this one.

### W4.2 — Give `deep-research` an index · [L] · `Chemclaw3-mcp` then here

*Row: § 5 #7.* Build `litsearch` — Europe PMC / OpenAlex / Crossref bulk, indexed at image build,
no outbound call at request time, same shape as every other server there. Then add it as a source to
`gather_evidence`'s fan-out here.

**The design input from ChemRAG that must not be skipped:** corpus choice is task-dependent —
reaction prediction benefits from literature, nomenclature from structured databases — and retrieval
gains flatten past ~5 documents, beyond which more retrieval adds noise. So this lands as *a source
in the existing fan-out with its own budget*, not as a new retrieval path, and
`D-2026-08-01-a-cap-that-starves-a-source` is the thing to re-read before choosing that budget.

**Two PRs, two repos**, and the second cannot merge before the first is deployable.

### W4.3 — Measure the memory→skill loop before building a generator · [M, measurement only]

*Row: § 5 #12.* The row asks for a number first and this plan does not overrule it. Over the
sessions on disk: how many recurring trajectories are there, and would a distilled playbook have
changed a later answer? Only that number decides whether a generator is worth building, and building
one first is the mistake `D-2026-08-15` already records.

### W4.4 — A dated upstream-capability register · [S]

*Row: § 5 #11, and the meta-row.* A section in `BACKLOG.md`, re-derived whenever a dependency is
bumped, listing what each pinned upstream now ships that this repository implements itself.
`make upstream-check` guards the *shapes* we borrow; this guards the *decisions*. It is prose, not a
test, and that is deliberate — the thing being watched is judgement.

---

## Sequencing, and what can run in parallel

```
Wave 0   W0.1 ─┐
         W0.2 ─┤  (independent; land together)
         W0.3 ─┤
         W0.4 ─┘  (timebox 3d; slips W2.1-verify, W2.3, W2.4 — nothing else)

Wave 1   W1.1        (independent, do immediately)
         W1.2 ← W0.2

Wave 2   W2.2        (independent)
         W2.1 ← W0.3, W0.4
         W2.3 ← W0.4
         W2.4 ← W0.4

Wave 3   W3.1, W3.2  (spikes; run in parallel with Wave 2, they block nothing)

Wave 4   W4.1, W4.4  (independent, other repo / prose)
         W4.2 ← W4.1 deployable
         W4.3        (independent measurement)
```

**Three could start today with no dependency at all:** W0.1, W0.2, W1.1. If only one thing gets
done, make it **W0.2** — it is the instrument the largest finding is graded by, and without it every
context-economy claim after this is prose.

---

## Definition of done, per row

A row is closed when **all four** hold, and it is deleted from `BACKLOG.md` in the same commit that
closes it — the file's own rule, and the one that kept it from reaching 4,717 lines again.

1. `make lint type test` green, with what it skipped stated.
2. The row's own acceptance check above passes, and its output is in the commit message.
3. Where the row changed a number, the before and after are both in the commit message. "Measure it,
   don't argue it" is not satisfied by an after-figure alone.
4. Where the row was a decision, the ADR is merged and its row in `docs/decisions/README.md` exists.

## Review

*(To be written when the plan is executed, not before. Nothing in this file has been implemented.)*
