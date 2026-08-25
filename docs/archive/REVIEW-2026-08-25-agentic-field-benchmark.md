# ChemClaw3 against the agentic field — August 2026

**Point-in-time review. Not maintained.** Accurate as of 2026-08-25 against `bed7d69`. What it asks
for is in [`docs/planning/BACKLOG.md`](../planning/BACKLOG.md); this file is the record and the
measurement behind those rows.

## 0. What this is, and why it is not the August-13 review

[`REVIEW-2026-08-13-external-synthesis-and-gap-analysis.md`](REVIEW-2026-08-13-external-synthesis-and-gap-analysis.md)
surveyed the *scientific* landscape — retrosynthesis foundation models, interatomic potentials,
vector stores, SiLA2, hazard screening — and ranked its findings around a GxP audit trail that
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` has since
removed. Its top finding no longer exists in the form it was written.

This review asks a different question, on the axis that one barely touched: **as an agentic system,
what does ChemClaw3 do that the 2026 field now does better, cheaper, or not at all?** Regulatory
framing is out of scope by instruction, and that turns out to be clarifying rather than limiting:
several things this repository built for compliance reasons are, on the engineering merits alone,
either its strongest asset or its most expensive habit — and which is which is not what the GxP
framing would have predicted.

**Method.** Every number below is either measured against this checkout or quoted from a named
source. Finding 7 is stated in its *corrected* form: the first draft of this review claimed the
lossless tool-result edit was missing, and it is not — `ClearToolUsesEdit` has been wired in
`context_compaction_middleware` since the context-management restoration. What is actually wrong is
one trigger shared by both edits, which is a smaller and more fixable thing. The mistake is recorded
rather than quietly overwritten because it is the failure mode this repository files rows about:
a claim about the code read off a docstring instead of the call site. Token counts use the `chars / 4` estimator this repository already uses for
`agent_context_token_budget`, so they are comparable to the budget the code enforces and are
**estimates, not tokenizer output**. Scripts are throwaway; what they did is described precisely
enough to re-run.

**The one measurement that did not happen, and it is the important one.** A live in-process run over
the probe corpus — real turns, real token growth, real tool selection — was attempted and could not
be made to work: the `API-KEY` this environment carries is rejected with `401 authentication_error`
by `api.anthropic.com`, with or without the session's `ANTHROPIC_BASE_URL` cleared, and `make
live-up` is independently blocked by the open backlog row *"No live lane in this repo can start"*.
So **no claim below is about what a real turn costs or how a real model behaves**. Everything is the
static surface a turn is compiled against, plus the offline eval. That is a real limitation and it
is exactly the limitation §3.2 identifies as the system's largest.

---

## 1. Executive summary

Ranked by consequence.

1. **The evaluation that would catch a regression cannot run, and the one that runs is very small.**
   232 probes exist and need a live model; CI gates on 14 case files producing 23 metric values, over
   a **7-document** retrieval corpus and a **39-note** knowledge graph, with the entire science half
   of the score resting on one solubility prediction, one BO regret replay and two green-chemistry
   mass balances. Measured separately: **17 of the 67 agent-callable tools are named by no probe at
   all**, including the whole scratchpad/filesystem surface and `task`, and **116 of 232 probes
   (50%) expect the same tool, `gather_evidence`**. The field's answer to this in 2026 is not a
   bigger corpus — it is trajectory-level scoring with cost on the axis (AstaBench, HAL), which this
   repository is unusually well-placed to adopt because it already records the trajectory.

2. **~14.7k tokens of context are spent before the user says anything, and 2026 shipped three
   different ways not to spend them.** Measured: 3.3k for the instructions, **8.6k for 29 in-process
   tool schemas**, 2.8k for the skills listing. Anthropic's tool search with `defer_loading` removes
   most of the tool half; programmatic tool calling cut billed input tokens ~38% on a 75-tool agent
   and improved agentic-search accuracy 11% while using 24% fewer input tokens; code-execution-with-MCP
   reports far larger reductions on wide tool surfaces. None of the three is adopted here, and the
   surface is growing — the `Chemclaw3-mcp` catalogue has 19 servers of which 5 are built.

3. **The agent cannot execute code, and the chemistry-agent field has converged on the opposite.**
   `scratchpad.py` withholds `execute` deliberately and states a good reason (deepagents 0.7's only
   concrete sandbox is content-egress-bound; `LocalShellBackend` is unrestricted). But the
   consequence is that anything not pre-wrapped as a tool is unreachable: no ad-hoc unit conversion,
   no fit, no plot, no parse of an unexpected output file. **El Agente Gráfico** measured what the
   alternative buys on six quantum-chemistry exercises — 94.6% fewer LLM requests, 82.8% fewer
   tokens, 4.5× faster wall clock, *and* accuracy up from 88.25% to 90.94% — by moving orchestration
   into a typed execution runtime where large objects never enter the context. The reason to decline
   a shell is real; the reason to decline *any* execution substrate is not, and a sandbox is a
   procurement decision rather than an architectural one.

4. **Two durability layers are maintained where upstream now ships one.** Temporal's LangGraph
   plugin went to public preview on 2026-07-16: the graph runs as a workflow, each node as an
   activity, `interrupt()` pauses durably and resumes on a signal. ChemClaw3 hand-built the
   equivalent — `agent/checkpointer.py` on its own autocommit pool, `agent/plan_approval_store.py`,
   the job→session push-back — and carries two open defects in exactly that seam (*"A decided
   approval hold can be reopened"*, *"A rejoined durable run never reaches the second chemist"*),
   plus a `BACKLOG.md` row asking for the durable approval store the plugin makes free.

5. **There is no literature.** `gather_evidence` sweeps the knowledge graph, the ELN, the document
   share and the fingerprint store — all internal. The `deep-research` skill has no index behind it,
   and `litsearch` is *proposed* in the `Chemclaw3-mcp` catalogue. ChemRAG measured a **+17.4%
   average relative gain** from a chemistry corpus (literature, PubChem, PubMed, textbooks), and
   found that *which* corpus matters by task: reaction prediction wants literature, nomenclature
   wants structured databases. A process chemist asking "has anyone run this coupling on a
   deactivated aryl chloride" gets whatever 39 notes happen to say.

6. **Memory records; it does not learn.** `memory/` has campaign, interaction, failure, playbook,
   progression and observation tiers, and `record_failure` / `recall_observations` are real tools.
   What none of them do is change what the agent *does* next time without a human writing a
   `SKILL.md`. The 2026 line of work — SkillRL, SkillForge, the self-evolving surveys — is
   specifically about abstracting recurring trajectories into reusable procedure automatically. The
   PR-gate is the right control for that, not an argument against it: distilled-playbook proposals
   already flow through it.

7. **The lossless context edit and the destructive one share a single trigger, so the cheap lever
   can never act alone.** `context_compaction_middleware` composes exactly the right two edits —
   upstream's `ClearToolUsesEdit` (lossless: a re-fetchable tool result becomes a placeholder, the
   `tool_use` record survives) and a first-party `KeepLastConversationGroupsEdit` (destructive: older
   groups are deleted) — and hands **both** `trigger=settings.agent_context_token_budget`. One knob,
   100k, so nothing reduces until 100k and then both fire together. Anthropic's own composition sets
   them an order of magnitude apart deliberately (clearing at 30k, compaction at 180k in the
   cookbook's research agent) precisely because the lossless one is cheap enough to run early and
   often. Splitting the trigger is a second setting, not a redesign. **The summarizer being off is
   correct and is not this finding** — a summary is new model prose over content `agent/framing.py`
   marked untrusted, and the envelope does not survive it.

8. **Nothing in this repository tracks the field.** `BACKLOG.md` is 30-odd rows and every one is a
   defect with an anchor in the tree; `DEFERRED.md` is postponements with triggers. Both are
   excellent and both point inward. There is no register of "what did the field solve for us since we
   built this" — which is why item 4 above (a plugin that closes an open backlog row, in public
   preview five weeks ago) is not on any list here.

**And what is genuinely ahead of the field, on the engineering merits and not the compliance ones:**
the single-agent decision (§6.1), the D-011 calculation cache (§6.2), and the discipline of deleting
unreachable code and unfalsifiable claims (§6.3). Two of the three are things the 2026 literature is
currently arriving at from the other direction.

---

## 2. What was measured

### 2.1 Context economics of one turn

Measured by importing `chemclaw.agent.chemclaw_agent`, taking the default profile's tool list,
serialising each tool's name + description + JSON schema, and reading the skills' frontmatter the
way `SkillsMiddleware` publishes it.

| Component | Chars | Est. tokens | Notes |
| --- | ---: | ---: | --- |
| System instructions | 13,187 | ~3,297 | `_INSTRUCTIONS`, one profile |
| 29 in-process tool schemas | 34,303 | ~8,576 | name + description + `args_schema` |
| Skills listing (29 skills) | 11,156 | ~2,789 | what goes in the prompt, not the bodies |
| **Static floor per turn** | **58,646** | **~14,662** | before history, evidence or tool results |
| Skill bodies (on demand) | 154,291 | ~38,573 | correctly *not* in the floor |

The five widest tool schemas are `get_durable_job_status` (2,441 chars), `compute_reaction_energy`
(2,375), `record_failure` (2,297), `gather_evidence` (1,987) and `propose_knowledge_note` (1,935).
The docstrings are unusually good — several are genuinely load-bearing guidance — which is precisely
why they are expensive, and why deferring them rather than trimming them is the right lever.

Progressive disclosure is doing its job on skills (38.6k tokens held back for 2.8k spent) and doing
nothing at all on tools (8.6k spent unconditionally, on a surface headed for 19 servers).

The compaction budget these numbers live under: `agent_context_token_budget = 100_000`,
`agent_keep_last_tool_groups = 2`, `agent_keep_last_conversation_groups = 12`, summarizer off.

### 2.2 Evaluation coverage

`make eval` on this checkout: **23 scored metric values across 14 case files**, 4 gated failures —
all four the case-set's own deliberate failures (`autonomy-plan-quality-drops-a-step`,
`pharma-solvent-heavy` ×2, `retrieval-cross-coupling-literal-miss`) — and **0 regressions** against
`data/evals/baseline.json`. The suite works and it is honest about what it is.

What it is: `plan_execute_utility` 0.5, `plan_quality` {1.0, 0.5}, `runaway_rate` 0.0, `bo_regret`
3.4 on a Reizman replay, `e_factor`/`pmi` on two mass balances, `retrieval_recall`/`precision` over a
**7-document** corpus, `precision`/`recall`/`f1` on one 3-item set, and `prediction_error` 0.4869 on
**one** solubility value (benzene). The knowledge graph the retrieval metrics run against holds **39
notes**.

The real corpus is the 232 live probes (`data/evals/probes/`, buckets A=120, B=61, C=51), and it
cannot run in CI because it needs a model. Measured against the agent-callable surface:

- **67** agent-callable tool names; **50** appear in at least one probe's `expects_tools`.
- **17 appear in none**: `compute_interaction_energy`, `run_conformer_refinement`, `render_structure`,
  `read_attachment`, `recall_preferences`, `remember_preference`, `forget_preference`, `list_watches`,
  `stop_watching`, `task`, `write_todos`, and the six filesystem verbs (`ls`, `glob`, `grep`,
  `read_file`, `write_file`, `edit_file`).
- **`gather_evidence` is expected by 116 of 232 probes** (50%); `find_notes` by 91; `expand_note` by
  60. The tail is thin.

The uncovered set is not random. It is the *newest* surface — the scratchpad and memory tools the
M-phases added, and `task`, the subagent seam three ADRs argue about. The corpus was written against
the capability the system had when the corpus was written, and nothing re-derives it.

### 2.3 Capability surface, both repositories

- This repo: 7 connector bundles, of which `chem`, `safety`, `molfp`, `rxnfp` are manifests only —
  the capability is `Chemclaw3-mcp`'s. 29 in-process `@tool` functions. 29 `SKILL.md` files (25
  global, 4 bundled).
- `Chemclaw3-mcp`: **19 servers catalogued, 5 built** (`props`, `rxnpredict`, `chem`, `safety`,
  `calc`). Proposed and unbuilt: `retro` (adopted, not built), `admet`, `litsearch`, `patents`,
  `spectra`, `nomenclature`, `pubchem`, `chembl`, `solidform`, `ghs`, `reactivity`, `regdocs`,
  `thermalsafety`, `kinetics`, `unitops`, `rxnsearch`, `blocks`, `chromatography`.
- Scale of the core: 75,720 LOC under `src/`, 255 test files, 334 ADRs.

The catalogue is the right catalogue. What §4 argues is that three of its entries have been
overtaken by open-weight releases since it was written, which changes what "building" one means.

---

## 3. The field, August 2026

### 3.1 Chemistry and pharma agents

The count is the story: a survey posted this month
([arXiv 2608.18508](https://arxiv.org/abs/2608.18508)) puts chemistry agents at "half a dozen in
2024, a dozen in 2025, approaching fifty" now — and reports **"very limited adoption beyond their own
developers"**, with every surveyed system keeping a human in the loop. Read that as the field
validating ChemClaw3's posture (`harness_autonomy = "plan_only"` by default) and simultaneously
warning that a chemistry agent's hard problem is deployment, not capability.

What the notable systems do that this one does not:

- **El Agente Quntur** ([2602.04850](https://arxiv.org/abs/2602.04850)) — hierarchical multi-agent
  research collaborator over ORCA 6.0, explicitly reasoning *over the software's documentation and
  the literature* to plan a calculation, with per-agent semantic/episodic memory and procedural
  memory emerging from the hierarchy. ChemClaw3's QM path submits a declared job; Quntur decides
  what job to run by reading the manual.
- **El Agente Gráfico** ([2602.17902](https://arxiv.org/html/2602.17902v2)) — the successor, and the
  most directly instructive result for this repository. It replaces multi-agent chat with a **typed
  execution graph**: node schemas govern state transitions, edges delimit admissible next actions,
  large numerical objects stay native in memory rather than being serialised through the context, and
  selected results are persisted to a knowledge graph with IRIs so a later session reuses them without
  recomputing. Measured against its own predecessor: **168.4 → 9.1 LLM requests (−94.6%), 1.65M → 284k
  tokens (−82.8%), −80.3% cost, 1,827s → 404s (4.5×), rubric score 88.25% → 90.94%.**
- **OpenClaw + domain skills** ([2603.25522](https://arxiv.org/html/2603.25522v2)) — a general agent
  plus runtime-loaded computational-chemistry skills, with `uv`-isolated CLI execution and a
  DPDispatcher skill for Slurm/PBS/LSF. This is *architecturally the same bet as ChemClaw3's layer 3*,
  made independently and published; its argument — "tool access alone does not specify how those
  capabilities should be ordered" — is the skills-vs-tools argument this repository already won. It
  also reports the honest cost of the shape: **~US$7.6 of inference per methane-combustion workflow**
  (463k input, 76k output, 6.03M cache tokens).
- **Robin / Kosmos (FutureHouse → Edison Scientific)** — Robin coordinates Crow (literature), Falcon
  (experiment design) and Finch (data analysis) and produced a real repurposing hypothesis
  (ripasudil for dry AMD) end-to-end. The transferable part is not the biology: it is that the loop
  is *hypothesis → design → analyse → revise*, and literature search is a first-class member of it.
- **ether0** — a 24B open-weights chemistry reasoning model (Apache-2.0, RL-trained on 640k
  experimentally-grounded tasks) that beats general frontier models on open-answer chemistry and
  molecular design. A domain model is now something you can host, which changes ChemClaw3's
  `model_routes` from a cost knob into a capability knob.
- **ChemToolAgent** ([2411.07228](https://arxiv.org/pdf/2411.07228)) — the necessary corrective:
  tool-augmented chemistry agents **do not consistently beat their own base LLM**. Tools win on
  specialised tasks (synthesis prediction); on general chemistry questions reasoning wins and tools
  can hurt. This is a direct warning about the `gather_evidence`-for-everything concentration §2.2
  measured.
- **ChemAmp** (ACL 2026 Findings) — composes chemistry tools into task-specialised super-agents from
  ≤10 samples, reporting **94% inference-token reduction versus vanilla multi-agent**.

Benchmarks that now exist and that ChemClaw3 appears in none of: **ChemBench** (2,700+ tasks,
frontier models above most human experts), **ChemRAG-Bench** (1,932 expert-curated QA pairs),
**LAB-Bench / LABBench2**, **SupraBench**, **BixBench**, **ScienceAgentBench** (102 tasks from 44
papers), **AstaBench** (57 agents, 22 architectural classes, with a *standardised* environment and
search tools so comparisons are controlled), **CORE-Bench** (computational reproducibility).

### 3.2 General agentic engineering

- **Context.** Anthropic's shipped triad — `compact_20260112`, `clear_tool_uses_20250919` and the
  memory tool — composes to ~48–50% peak-context reduction in their own research-agent example
  (335k → 169–173k), with the *lossless* tool-result clearing doing most of it. Tool search with
  `defer_loading: true` keeps tool definitions discoverable without spending context at session
  start. Programmatic tool calling: **−38% billed input tokens on a 75-tool agent with no accuracy
  change**, and **+11% accuracy / −24% input tokens** on agentic search. Code-execution-with-MCP
  reports the extreme case (≈150k → ≈2k on a wide server set).
- **Context rot** is now measured rather than folklore: softmax attention dilutes as context grows,
  mid-context content is systematically underweighted, and agents' tool choices drift as constraints
  get buried. "Big window, therefore fine" is not a position any more.
- **Multi-agent.** MAST (1,600+ annotated traces, 7 frameworks, 14 failure modes, κ=0.88) attributes
  **41.8%** of multi-agent failures to specification/design, **36.9%** to inter-agent misalignment and
  **21.3%** to verification/termination. Together with ChemAmp's and El Agente Gráfico's token
  numbers, the 2026 consensus is that a second agent must be paid for in a measurement.
- **Durable execution.** Temporal × LangGraph plugin, public preview **2026-07-16** (Python SDK
  ≥1.27, Python ≥3.11): graph-as-workflow, node-as-activity, checkpoint at every node, `interrupt()`
  durable and signal-resumed, per-policy retry. Pydantic-AI and others ship comparable seams.
- **Evaluation.** The move is to trajectory-level scoring — plan, tool calls, retries, recovery —
  with rubric-anchored judges, and **cost on the axis**: HAL (21,730 rollouts, 9 models, 9
  benchmarks, ~$40k) found the most expensive model on the Pareto frontier in **only 1 of 9**
  benchmarks, and that higher reasoning effort *reduced* accuracy in a majority of runs. OTel +
  OpenInference are the interoperable trace format, with eval scores attached to spans as
  `gen_ai.evaluation.score`.
- **Memory.** LoCoMo / LongMemEval / BEAM are the reference set; the hard open problems are named as
  cross-session identity, temporal abstraction and staleness. Letta's result is worth holding beside
  the vendor numbers: a simple filesystem-backed agent scores competitively with graph memory
  systems.
- **Skills** are an open standard as of 2025-12-18 (agentskills.io), adopted across 40+ clients.
  ChemClaw3's `SKILL.md` tree is already the shape; interop is now a naming question, not a port.
- **Security.** Tool poisoning and MCP supply chain are the live attack surface — poisoned tool
  *descriptions* the user never sees, and a malicious npm MCP server found in the wild. The defense
  set is allowlisting, identity binding, runtime monitoring and human checkpoints — which is a fair
  description of what this repository already does, and §6.3 gives it the credit.

---

## 4. Scorecard

Verdicts: **ahead** / **at parity** / **behind** / **absent**. "Behind" means the field has a
measured better answer to the same problem, not that the current answer is wrong.

| # | Dimension | ChemClaw3 today | Field, Aug 2026 | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Orchestration engine | LangGraph via `create_deep_agent`, one compiled graph per turn | LangGraph is the production standard for stateful/auditable agents | **at parity** |
| 2 | Single vs multi agent | One agent, one `task` helper; panel and specialists deleted after measuring | MAST, ChemAmp, El Agente Gráfico all converge on "pay for the second agent with a number" | **ahead** |
| 3 | Static context cost | ~14.7k tokens/turn, 8.6k of it tool schemas, unconditional | tool search + `defer_loading`; programmatic tool calling (−38%); code-exec MCP | **behind** |
| 4 | Compaction | `ClearToolUsesEdit` + a conversation window, both non-destructive of state — but on one shared 100k trigger | Same two edits, triggers set an order of magnitude apart so the lossless one runs early | **at parity** (mistuned) |
| 5 | Code execution | None — `execute` withheld by design | El Agente, OpenClaw, Coscientist, ChemCrow all execute; sandboxes are commodity | **absent** |
| 6 | Durable orchestration | Two layers, hand-wired; 2 open defects in the seam | Temporal×LangGraph plugin, public preview 2026-07-16 | **behind** |
| 7 | Human-in-the-loop | `plan_only` default, plan gate, PR-gate, approval store | 2026 survey: every chemistry agent keeps a human in the loop | **ahead** |
| 8 | Result caching | D-011 store; persisted result never recomputed; key names inputs | El Agente Gráfico persists results with IRIs for cross-session reuse | **ahead** |
| 9 | Literature | None. `deep-research` skill has no index; `litsearch` proposed | ChemRAG +17.4%; PaperQA/Crow lineage; corpus choice is task-dependent | **absent** |
| 10 | Retrieval | Hybrid + graph + fingerprints, fan-out, cited | GraphRAG-Bench: graph wins on multi-hop (+10) and summarisation (+13), ties on single facts; hybrid beats either | **at parity** |
| 11 | Memory | 6 tiers, recorded, human-gated | LoCoMo/LongMemEval; self-evolving skill libraries (SkillRL, SkillForge) | **behind** |
| 12 | Evaluation | 23 gated metrics / 14 cases; 232 live probes that CI cannot run | AstaBench (controlled env), HAL (cost-Pareto), trajectory + rubric judges | **behind** |
| 13 | Observability | OTel + OpenInference, first-party, content off by default; Phoenix optional | Exactly this stack | **at parity** |
| 14 | Skills | 29 `SKILL.md`, progressive disclosure, role-narrowed at the backend | Open standard since 2025-12; 40+ clients; OpenClaw makes the same bet | **ahead** (on the security narrowing) |
| 15 | Domain model | Frontier general models, per-task routes | ether0 24B open-weights beats frontier on open-answer chemistry | **behind** |
| 16 | Science coverage | 5 built servers; no retro / ADMET / spectra / nomenclature | retro, ADMET, Boltz-2, spectra all have open implementations | **behind** |
| 17 | Tool-supply security | Bearer auth, no-egress servers, allowlists, framing, authz gate, audit | The recommended defense set, post-incident | **ahead** |
| 18 | Cost accounting | Token budget + `turn_cost.py` + metrics | HAL: cost belongs on the eval axis, not just the meter | **behind** (measured, not evaluated) |

---

## 5. Blind spots

A blind spot is not a known gap. `DEFERRED.md` and `BACKLOG.md` between them cover the known gaps
well. These are the things no artefact in the repository is watching.

1. **No upstream-capability register.** `make upstream-check` guards the *shapes* this repo borrows
   against a bump. Nothing guards its *decisions* against upstream shipping the thing. Finding 4 in
   §1 is five weeks old and would have been caught by a monthly list.
2. **Eval coverage is never derived from the tool surface.** `tests/test_repo_map.py` enforces that
   every directory is documented in both directions; nothing does the equivalent for probes. The
   17-tool hole in §2.2 opened silently and would keep opening.
3. **Nothing measures the static context cost, so it can only grow.** There is a runtime
   `agent_context_token_budget` and a compaction metric. There is no test asserting the *floor*, so
   every added tool is free at review time and paid for on every turn forever.
4. **Concentration in the probe corpus is invisible.** 50% of probes expecting one tool means the
   corpus mostly measures one retrieval path. ChemToolAgent's finding — that tools can *hurt* on
   general questions — cannot be reproduced here, because bucket C (51 no-tool probes) is scored for
   restraint but there is no arm that runs the same question tool-free for comparison.
5. **No external benchmark is ever run.** ChemBench, ChemRAG-Bench and AstaBench are all runnable
   against an OpenAI-compatible endpoint, which is the seam F0 built. A number that is comparable to
   somebody else's number is the only kind that survives an argument with a chemist.
6. **The `Chemclaw3-mcp` catalogue is not re-derived against releases.** `admet` is filed as
   "ADMET-AI / DeepChem"; `retro` as adopted-not-built. Boltz-2 (MIT, ~1000× cheaper than FEP) and
   ether0 (Apache-2.0) both landed after that catalogue's assumptions.

---

## 6. Where ChemClaw3 is ahead, and why it matters

Not consolation. Each of these is a position the 2026 literature is arriving at independently, which
means the work is done and the argument is now free.

### 6.1 One agent, deleted specialists, and a measurement behind it

`D-2026-08-15` removed the specialist team, the challenge panel, `reject_widening` and the routing
measurement built to justify them — 1,442 + ~400 + 1,506 lines — because the corpus could not settle
the delegation question (2/15 under one framing, 14/15 vs 14/15 with the old arm at ceiling, and two
probes spanning two specialists giving the accuracy figure an unpassable floor). MAST then attributed
78.7% of multi-agent failures to specification and misalignment; ChemAmp and El Agente Gráfico both
report ~80–95% token reductions from *removing* agent-to-agent chat. Deleting the panel was the
correct call and the reasoning — *a named partition is a routing hypothesis nobody has measured* —
is a better statement of the 2026 position than most of the papers make.

The constraint recorded alongside it is worth restating because it is a security property others
will rediscover: deepagents builds a bare `SubAgent` dict with only `spec["middleware"]`, so anything
not compiled by `build_langgraph_agent` runs with **no audit trail, no authz and no plan gate,
silently**. `agent/subagents.py` claims `GENERAL_PURPOSE_SUBAGENT["name"]` to displace it, having
measured that the supported suppression (`GeneralPurposeSubagentProfile(enabled=False)`) fails *open*
on a provider-key miss. That is a real finding about a widely-used library.

### 6.2 The cache is the asset, and the boundary it drew is right

`D-2026-08-16-the-physics-leaves-the-cache-stays` splits capability by **composability**: a primitive
whose identity is derivable from its inputs is a stateless MCP server elsewhere; orchestration and
the D-011 cache stay here; a composite whose key would name an *output* is decomposed rather than
shipped. The measurement that forced it — `compute_thermochemistry` would turn a 0.007s repeat into a
full recompute because its key names the geometry its refinement loop settles on — is exactly the
distinction El Agente Gráfico's IRI-keyed knowledge graph is reaching for from the other side. This
repository has the harder half already built.

### 6.3 Deleting claims

The habit that shows up everywhere in `docs/decisions/` — a guard with no caller is a *claim* that a
control exists, so delete it; `map_to_hpc_identity`, OBO and workload-identity federation went for
being 254 lines whose only callers were their own tests; three settings with no reader and a system
prompt sentence describing compaction that was not happening — is the single most transferable thing
in this codebase. The 2026 agent literature is full of architecture diagrams for mechanisms nobody
measured. This is the opposite failure mode, and it is rare.

---

## 7. What to do, ranked

Each item names an anchor and the measurement that would settle it. These are filed as
`BACKLOG.md` rows; the reasoning is here.

1. **Make the tool surface deferrable.** Anchor: `agent/chemclaw_agent.py::_capability_tools`,
   `agent/langgraph_agent.py::_middleware`. Measure the floor first (a test asserting it), then put
   the cold tail behind search. Success = the floor falls below ~8k with no probe losing its expected
   tool.
2. **Close the eval-coverage hole and keep it closed.** Anchor: `data/evals/probes/`,
   `tests/test_repo_map.py` as the pattern. A test that fails when an agent-callable tool is named by
   no probe. Then write the 17 missing probes — the scratchpad and memory surfaces first, since they
   are the newest and least exercised.
3. **Run one external benchmark.** Anchor: `evals/harness.py`, `agent/llm_provider.py` (the
   OpenAI-compatible seam). ChemRAG-Bench is the best first target: it scores the retrieval half,
   which is where this system's science actually lives, and it produces a number comparable to
   somebody else's.
4. **Evaluate the Temporal LangGraph plugin against the seam it would replace.** Anchor:
   `agent/checkpointer.py`, `agent/plan_approval_store.py`, `durable/`. This is an evaluation, not a
   migration — the question is whether `interrupt()`-as-signal closes *"A decided approval hold can
   be reopened"* and the durable-approval-store row together.
5. **Decide code execution deliberately, on its merits.** Anchor: `agent/scratchpad.py::scratchpad_tools`.
   The current refusal is a refusal of *deepagents 0.7's two sandboxes*, correctly. A separate
   decision is owed on whether an execution substrate belongs at all, and El Agente Gráfico's numbers
   are the argument to test it against. An ADR either way.
6. **Give `deep-research` an index.** Anchor: `Chemclaw3-mcp` `litsearch` (proposed),
   `skills/deep-research/SKILL.md`, `agent/research_tools.py::gather_evidence`. Europe PMC / OpenAlex
   bulk, vendored, no egress — the same shape as every other server there. ChemRAG's finding that
   corpus choice is task-dependent is the design input.
7. **Give the lossless context edit its own trigger.** Anchor: `agent/compaction.py::context_compaction_middleware`,
   `core/config/agent.py`. Both edits currently read `agent_context_token_budget`. Add
   `agent_tool_result_clear_trigger`, default it well below the budget, and the cheap edit starts
   doing the work before the expensive one has to.
8. **Re-derive the MCP catalogue against 2026 releases.** Anchor: `Chemclaw3-mcp/MODULES.md`.
   Specifically: `retro` against RetroReasoner/Retro-R1, `admet` against Boltz-2, and `nomenclature`
   (OPSIN, MIT, local — the catalogue already calls it "the best value-to-effort ratio").
9. **Put cost on the eval axis.** Anchor: `evals/metric.py`, `agent/turn_cost.py`. HAL's result — the
   most expensive model on the frontier in 1 of 9 benchmarks — is only actionable if a case set scores
   accuracy *and* cost. The meter exists; the metric does not.
10. **Add a bucket-C control arm.** Anchor: `data/evals/probes/*.yaml`, `evals/ab.py`. Run the same
    question with and without tools and compare. ChemToolAgent says the answer is not obvious, and
    `compare_tool_utility` is already written.

---

## 8. Limitations

- **No live turn was measured.** See §0. Every token figure is a static estimate at `chars / 4`; no
  claim is made about real conversation growth, real tool selection, or real accuracy.
- **Token estimates are estimates.** They match the estimator the code budgets against, which makes
  them internally comparable and externally approximate.
- **The probe-coverage analysis reads `expects_tools`, which is ANY-OF and declarative.** A probe may
  in practice exercise a tool it does not name. The 17-tool hole is a floor on the gap, not a proof
  that those tools are never exercised.
- **Third-party figures are as reported by their authors** and were not reproduced here. The
  El Agente Gráfico, ChemAmp, HAL, ChemRAG and Anthropic numbers are all self-reported by the
  respective sources.
- **`Chemclaw3-mcp` was read at its default branch on 2026-08-25**, shallow clone, catalogue only.

## Sources

Chemistry and pharma agents:
[AI agents in computational chemistry (2608.18508)](https://arxiv.org/abs/2608.18508) ·
[El Agente Quntur (2602.04850)](https://arxiv.org/abs/2602.04850) ·
[El Agente Gráfico (2602.17902)](https://arxiv.org/html/2602.17902v2) ·
[OpenClaw + domain skills (2603.25522)](https://arxiv.org/html/2603.25522v2) ·
[ChemToolAgent (2411.07228)](https://arxiv.org/pdf/2411.07228) ·
[ChemAmp (2505.21569)](https://arxiv.org/abs/2505.21569) ·
[ChemRAG (2505.07671)](https://arxiv.org/abs/2505.07671) ·
[ether0](https://futurehouse.org/research-announcements/ether0-a-scientific-reasoning-model-for-chemistry) ·
[Robin / FutureHouse](https://www.futurehouse.org/about) ·
[Boltz-2](https://boltz.bio/boltz2) ·
[ChemBench](https://chembench.lamalab.org/) ·
[Schema-gated agentic AI (2603.06394)](https://arxiv.org/html/2603.06394v1) ·
[Autonomous chemistry & materials agents (JACS Au)](https://pubs.acs.org/doi/10.1021/jacsau.6c00213)

General agentic engineering:
[Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) ·
[Context engineering cookbook](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) ·
[Programmatic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling) ·
[Code execution with MCP](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/1780) ·
[Temporal × LangGraph plugin](https://temporal.io/blog/temporal-langgraph-plugin-durable-execution) ·
[Why do multi-agent LLM systems fail (MAST)](https://arxiv.org/abs/2503.13657) ·
[Holistic Agent Leaderboard](https://hal.cs.princeton.edu/) ·
[AstaBench](https://allenai.org/asta/bench) ·
[ScienceAgentBench](https://github.com/OSU-NLP-Group/ScienceAgentBench) ·
[Agent Skills open standard](https://thenewstack.io/agent-skills-anthropics-next-bid-to-define-ai-standards/) ·
[MCP tool poisoning](https://thehackernews.com/2026/06/microsoft-warns-poisoned-mcp-tool.html) ·
[GraphRAG vs vector RAG](https://venturebeat.com/orchestration/stop-graphing-everything-when-graphrag-actually-beats-vector-rag) ·
[Agent memory benchmarks 2026](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
