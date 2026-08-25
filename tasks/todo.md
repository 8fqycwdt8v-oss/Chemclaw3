# Implementation plan — closing the 2026-08-25 field benchmark

**Status, 2026-08-25.** Done: W0.1, W0.2, W0.3, W0.4, W1.1, W2.1, W2.2, W3.1 (declined, with an
ADR), W3.2 (**merged**, `Chemclaw3-mcp` #12), W4.1 (**merged**, `Chemclaw3-mcp` #13), W4.4. W1.2 and W4.3 were
*attempted and stopped by their own measurements* — both are written up where they stand rather than
left looking undone. **W2.3 and W2.4 are blocked on a working model credential**, and the mock
cannot stand in for either. W4.2 (`litsearch`) is not started: it needs a bulk-corpus build.

Two of the eleven §5 backlog rows were deleted outright (the live lane, the Temporal plugin), two
were replaced by better-aimed rows the measurements produced, and one — `admet` against Boltz-2 —
turned out to be a bad recommendation and is recorded as one.

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

**Acceptance — met.** `make live-up` now brings up `api`, `connectors`, `chem`, `safety`,
`mock-llm` and all four workers; `make live-status` shows nine processes up; and
`make live-probes --only ws-01,ws-03,ws-06` drove three of the new probes through the real front
door — real MCP sessions, real workers, transcripts on disk, 3/3 answered, 0 silent failures, 0
fabricated citations, median turn 4.4 s.

**Two things it needed that the row did not name.** The fleet's ports are read from
`$MCP_REPO/manifests/<name>/connector.yaml` rather than hard-coded, so a server that moves port
there moves here without an edit — the same "one reader for one shape" rule `connector_env`
follows. And the front door needs a *model* as well as connectors: it builds a chat client at
startup, so with none it does not fail at the first turn, it fails to boot. Pointing the lane at
`chemclaw.cli.mock_llm` (`CHEMCLAW_LLM_PROVIDER=openai_compatible`,
`CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1`, `CHEMCLAW_LLM_MODEL=mock`) brings it up with no
credential at all, which is what makes this lane runnable here.

**What the mock cannot do, and it bounds what W2.1, W2.3 and W2.4 can claim.** `expected tool
reached` came back 0/3, and that is the mock behaving as designed rather than a defect: it emits
scripted tool calls with argument names taken from the real tools, and it does not *choose* a tool
in response to a question. Every mechanical signal about the harness is real; nothing about the
model's judgement is. A real credential is still what those three rows need.

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

### W1.2 — Defer the cold tool tail · [M] · **step 1 done; the measurement says do not do step 2**

*Row: § 5 #1, second half. The plan said step 1 is a measurement, not code. It was, and it overturned
step 2.*

**What was measured.** Every tool in the `default` profile, ranked by how many probes name it and by
how many of the seven profiles advertise it — the plan's own definition of cold, which required
*both* ("a tool no probe names **and** that no profile makes central. Both, not either.").

```
default-profile tool schemas: 12,536 tokens over 29 tools
cold (no probe AND not in every profile): 0 tools, 0 tokens
```

**There is no cold tail.** W2.1 removed it: the fifteen tools that had no probe now have one, so the
intersection the plan asked for is empty by construction. Route (b) — a `find_tool` search over the
cold tail, behind a setting, defaulting off — has nothing left to defer, and building it would be
adding a mechanism to solve a condition that no longer holds.

**Route (a) is already implemented and already working.** Profile narrowing is the lever, and the
floors show it doing its job: `default` 18,805 tokens against `computation` 7,338, `reporting`
5,677, `evidence` 4,690, `design` 3,707, `property-lookup` 2,208, `safety` 1,937.
`tests/test_context_floor.py` now asserts that every narrow profile is genuinely cheaper than the
default, so that stays true. The open question route (a) leaves is not "narrow further" but
**whether a 29-tool `default` should be what an unprofiled turn gets at all** — a product decision
about the front door, not a context-engineering one.

**What the measurement did find, and it is a different row.** The cost is concentrated in prose, not
in breadth. Three tools are 32% of the tool budget, and the widest is mostly *developer rationale*
shipped to the model on every turn:

| tool | schema | of which description | of which elaboration after the first paragraph |
| --- | ---: | ---: | ---: |
| `start_optimization_campaign` | 8,063 ch | 4,392 | **3,047 (38% of the schema)** |
| `propose_knowledge_note` | 4,259 ch | 2,262 | 663 |
| `compute_reaction_energy` | 3,569 ch | 2,522 | 0 |

`science/bo/problem.py`'s nested models carry paragraphs like *"One `objectives` field rather than a
lead objective plus a sidecar list (W3). The sidecar shape guarantees that a lone objective
sometimes lands in the wrong one"* — an argument about why the API is shaped this way, which is
exactly what this repository asks a docstring to contain and exactly what a model filling in
arguments cannot use. Pydantic ships it because a class docstring becomes the schema `description`.

**Why that is not being done in this pass.** Separating caller guidance from developer rationale is
a judgment call per paragraph — some of the elaboration *is* for the caller (when to supply
categorical descriptors genuinely changes what the model should send), and a blanket cut would
delete guidance. The plan's own acceptance for W1.2 is the reason to wait: *"every probe's
`expects_tools` still satisfied in a live run… if the second cannot be shown, the change does not
ship — a cheaper prompt that stops finding tools is a regression with a good-looking metric."* The
live lane is W0.4 and it is not up. **This is exactly the situation that rule was written for.**

Filed as its own row rather than left here, since it is a different change from the one this row
described.

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

**Blocked on a working model credential, and the mock cannot stand in.** The whole comparison is
"did having tools change the answer", and `cli.mock_llm` emits scripted tool calls with argument
names taken from the real tools *without choosing them in response to the question*. Both arms of
the A/B would therefore be measuring the mock's script, not tool utility. Measured on 2026-08-25
during W0.4: three probes through the real lane, `expected tool reached` 0/3 — the harness is real
and the judgement is not.

**Acceptance, unchanged and still owed.** A committed run with the helped share and the per-probe
deltas, and a paragraph in the eval report saying which band, if any, is worse with tools.

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

**Blocked on the same credential.** ChemRAG-Bench is 1,932 chemistry QA pairs and the mock answers
none of them; there is no version of this that a scripted model can produce a real number for. The
adapter could be built unrun, and deliberately is not: this plan's first rule is that nothing which
cannot be graded gets built, and an eval harness with no run behind it is exactly that.

---

## Wave 3 — Two decisions

W3.1 is a spike whose deliverable may be "no". **W3.2 is no longer a spike**: it carries a
concrete proposal, so its ADR decides a design rather than a direction, and the build follows it in
`Chemclaw3-mcp`.

### W3.1 — Evaluate the Temporal LangGraph plugin · **done: declined**

*Row: § 5 #5, now deleted. ADR: `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use`.*

The spike was budgeted five days and took under an hour, because the first of its three questions
settles the other two.

**`interrupt()` is not used anywhere in this system.** `grep -rn "interrupt(" src/` returns two
hits and both are prose in `agent/checkpointer.py` explaining that a checkpointer would be needed
*if* one were used. The human gate is `agent/interaction_tools.py::start_approval` calling
`client.start_workflow` — it is **already a Temporal workflow**. So the plugin's headline feature is
a thing this repository arrived at from the other direction, and it closes neither defect the row
attributed to it: the reopened-hold bug is a missing `id_reuse_policy`, a few lines, entirely
independent of the plugin.

Two other facts worth carrying. The plugin **is already installed** —
`temporalio.contrib.langgraph` ships in the `temporalio` 1.31.0 this repo already pins, so the
version bar the row worried about was never the obstacle. (An earlier check looked for
`temporalio.contrib.langchain` and concluded it was absent; the wrong module name is exactly how a
spike reaches a confident wrong answer, and it is recorded in the ADR.) And adopting it would mean
annotating node metadata on a graph this repository does not author: measured, every node of a real
compiled agent has `metadata=None`, and those nodes are `create_deep_agent`'s.

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

**Merged** as `Chemclaw3-mcp` #12, on a green CI (`check`, `manifests`, `offline`). The backlog row
is deleted in the same commit, per that file's rule: until the PR merged the tree still could not do
this, and now it can.

#### The risk to state plainly in the ADR

This is the first tool in the fleet whose *input is a program*. Every other server takes a SMILES or
a solvent name. Prompt injection that reaches the model reaches this tool, so the controls have to
hold against a hostile author rather than a careless one — which is why the boundary is the process
and the deployment, and why row 6 is written down as insufficient on its own.

## Wave 4 — Capability, mostly in the other repository

### W4.1 — Re-derive the MCP catalogue · **done** · `Chemclaw3-mcp` #13

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

### W4.3 — Measure the memory→skill loop · **attempted; the corpus does not exist**

*Row: § 5 #12.* The row asks for a number before a mechanism, and this plan did not overrule it.
The number cannot be taken here. Against a live Postgres: `session_messages` 12 — all from this
day's own probe run — `session_turns` 0, `observations` 0, `note_proposals` 0, `audit_events` 3,
and the five `knowledge/playbook/` notes are committed examples rather than distillations. Blocked
on **deployment history**, not on effort, and the row now says so with its trigger.

### W4.4 — A dated upstream-capability register · **done**

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

**`make test`: 4,272 passed, 6 skipped, 0 failed** (8m04s), with Postgres up so the ~157 durable
tests actually ran. What it skipped, because a green line that does not say is worth less: three in
`test_migrations_are_additive.py` (this checkout is a shallow clone, so a migration compares against
itself), two in `test_prompt_caching.py` (the credential is present and stale — W0.1's whole point,
and the skip line now says which), and one in `test_retention.py` (a checkpointer exists in
`public`, so the absent-schema case cannot be produced without dropping tables that test does not
own). `make lint` green. `make type` has two errors, both in `test_bo_campaign_record.py` and
`test_step_handoff.py`, **both pre-dating this branch** — confirmed by stashing — and left alone
rather than widening the change.

`make eval`: 24 scored metrics, 4 gated failures (all four the case-set's own by-design ones),
0 regressions. `make eval-baseline` → `make eval-baseline-check` round-trips green, 13 metrics,
0 worsened.

### What the work actually taught, beyond the ledger above

**The plan's first rule paid for itself three times.** "Nothing that cannot be graded gets built"
was written to stop premature building, and what it actually did was stop *three* planned changes
that turned out to be wrong: W1.2's cold-tail deferral (the tail did not exist once the probes were
written), W3.1's plugin adoption (it solves an `interrupt()` this system does not use), and the
ChemRAG adapter (buildable unrun, and therefore not built).

**Three of my own claims were wrong and each is corrected where it was made**, not in a changelog:
the headline context figure was 28% low because it estimated at `chars / 4` instead of asking
LangChain what it sends; finding 7 described a missing edit that was already wired, read off a
docstring instead of the call site; and recommending Boltz-2 as an `admet` re-derivation was a
category error — it is a protein-ligand co-folding model.

**The instruments found defects the plan did not predict.** Building the context ratchet found that
`@tool` is identity here, so a first version measured 11 tokens per tool and would have held
nothing. Adding an eval case found that `make eval-baseline` and `make eval-baseline-check`
disagreed about the case-set version, so a regenerated baseline failed the check it was regenerated
for. And the full suite found that `evals` was importing `agent` — an edge the architecture
deliberately does not declare, because the scorer must not depend on the scored.

**Failed approach, recorded so it is not retried.** Driving `build_langgraph_agent()` in-process
against the live API to measure real turn economics: this environment's credential is rejected, and
clearing the session's `ANTHROPIC_BASE_URL` gives the same 401. `cli.mock_llm` makes the *harness*
runnable end to end but cannot stand in for judgement — it emits scripted tool calls without
choosing them, measured at expected-tool-reached 0/3. W2.3 and W2.4 need a real credential and
nothing else.
