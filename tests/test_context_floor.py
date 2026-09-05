"""What a turn costs before the user has said anything, pinned so it can only shrink on purpose.

Every tool this repository adds is free at review time and paid for on **every turn, forever**. The
2026-08-25 field benchmark measured the bill nobody was watching and, building this file, measured
it again properly: the default profile's prefix is **18,805 tokens** — 3,301 of instructions, 2,968
of the skills listing, and **12,536 of tool schemas** — against a compaction budget of 100,000 that
never sees it, because compaction acts on the *thread* and this is the prefix under it.

**The review's own figure was ~14,700 and it was low by 28%.** It estimated at `chars / 4` over a
hand-serialised name-plus-docstring-plus-schema; the number here is `convert_to_openai_tool`, which
is the function LangChain calls when it binds tools to a model, counted with the counter compaction
uses. Where the two disagree, this one is the payload.

**What it caught on its first encounter with somebody else's merge**, recorded because it is the
argument for the file existing: eighteen new tools and **+32% on the static floor**, in one change,
with nothing else in the repository saying so.

**This file is a ratchet, not a report.** It asserts a ceiling per profile. A change that grows the
floor fails here and the failure names the tool that grew, which is the whole point: the cost
becomes visible in the pull request that creates it rather than in a bill nobody reads.

**Why a ceiling and not the measured value.** An equality assertion fails on every docstring edit
and gets bumped without thought, which is a ratchet that turns freely in the wrong direction. A
ceiling with headroom is edited deliberately — and lowering one after a real reduction is the
commit that proves the reduction happened.

**Why `count_tokens_approximately` and not `chars / 4`.** The benchmark used `chars / 4` because
that is what `agent_context_token_budget` budgets against, which made its numbers comparable to the
code's own estimator. A *gate* wants the one function this repository can keep consistent, and
LangChain's counter is what the compaction middleware already uses. The two disagree by a few
percent; the ceilings here are set against this one.

**What this file measures is the tools the compiled graph binds, and getting there took two
corrections rather than one.** The first is in `_tool_schema`: a hand-serialised name and docstring
measured ~11 tokens per tool. The second was still standing on 2026-08-29 — converting the
*callables* out of `_capability_tools` rather than the `BaseTool`s `build_langgraph_agent` binds,
which under-measured the `default` profile by **8,126 tokens (24%)** while this file's own prose
called it "the payload rather than an approximation of it". `_bound_tools` reads the surface off the
graph's `ToolNode`, so the ratchet gates what a deployment pays. The lesson is the file's own: a
ratchet is only as honest as its basis, and a basis that is re-derived rather than observed will
agree with itself forever.
"""

from __future__ import annotations

import asyncio
import json
from functools import cache
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs, instructions_for
from chemclaw.agent.langgraph_agent import (
    _labelled,
    _skill_dirs,
    _skills_middleware,
    build_langgraph_agent,
    skills_backend,
)
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.connectors.registry import enabled, server_tools_module
from chemclaw.connectors.transport import _allowed

# Discovered at import, not in a fixture: `registered_profile_names()` parametrises the test below
# and parametrisation is evaluated at *collection*, before any fixture runs. With the load in a
# fixture this file silently collected one profile instead of seven — a green run that checked a
# seventh of what it claimed to, which is the shape of failure this whole file exists to prevent.
load_profiles()

#: Per-profile ceilings on the static prefix, in tokens.
#:
#: One number, with roughly 10% headroom over the widest profile — enough for a docstring that gets
#: clearer, not enough to hide a new tool. A per-profile entry may be added when one earns a
#: tighter bound; the narrow profiles are far below this and would each take a much lower one.
#:
#: **Raised once, on 2026-08-25, from 21,000 to 27,500, and here is the number that raised it.**
#: The GFN multi-step work added *eighteen* agent-callable tools in one merge and took the `default`
#: profile's prefix from **18,805 to 24,838 tokens — a 32% increase in what every turn costs before
#: the user says anything**. That is exactly the event this file was written to make visible, and it
#: landed while this file was still on a branch, so the first thing the ratchet did was report a
#: cost that had already been paid.
#:
#: The ceiling is raised rather than the change blocked because blocking would punish this branch
#: for somebody else's merge. What must not happen is the raise being quiet: the figure is here, the
#: cause is named, and `docs/planning/BACKLOG.md` carries the row for bringing it back down.
#: **Lowering a ceiling is the commit that proves a reduction happened**; raising one belongs in a
#: pull request description, not in a diff nobody reads.
#
#: **28,250 as of 2026-08-26**, raised from 27,500 by `profile_rotation`
#: (`D-2026-08-26-a-torsion-is-named-not-indexed`). Raising it is what this file asks for — the
#: cost of a new capability becomes visible in the pull request that creates it — and the number to
#: judge is what the tool costs *after* being narrowed, not before. That one measured 1,499 tokens
#: on arrival, over `MAX_SINGLE_TOOL_TOKENS` and second only to `start_optimization_campaign`;
#: trimming its manifest prose and moving two model docstrings into comments took it to ~870, since
#: pydantic publishes a model's docstring as its JSON-schema `description` and a nested pair of them
#: is bound to the model on every turn. The ~230 tokens that remain are what one more tool costs,
#: and that is the right thing to pay for it.
#:
#: **29,000 as of 2026-08-26**, raised from 28,250 by `rank_species_across_solvents`
#: (`D-2026-08-26-a-solvent-is-an-argument-not-a-job`), which measured 28,586 with it. The step is
#: the same size as the one above it and for the same reason, but the *cause* is worth recording
#: because it is not one tool: `profile_rotation` and this one were built on branches that did not
#: know about each other, each stayed under `MAX_SINGLE_TOOL_TOKENS` on its own, and the ceiling was
#: crossed only where they met. A per-tool bound cannot see that, which is exactly why this
#: whole-prefix ratchet exists beside it.
#:
#: This one arrived at 1,011 tokens — over `MAX_SINGLE_TOOL_TOKENS`, and that test refused it until
#: the manifest description and the spec's field descriptions were cut to what a model needs to
#: choose and call it, which took it to 857. The ~340 that remain are what one more durable job
#: costs on every turn.
#: **29,500 as of 2026-08-26**, raised from 29,000 by `predict_pka_ensemble`
#: (`D-2026-08-26-a-pka-is-a-macrostate-not-a-microstate`), which measured 29,225 with it — so this
#: is the third tool in a row to cross the ratchet where its branch met the ones before it, exactly
#: as the paragraph above predicted.
#:
#: It arrived at 926 tokens, over `MAX_SINGLE_TOOL_TOKENS`, and that test refused it twice. What
#: took it to 639: the manifest description cut to what decides a *choice* between this and
#: `predict_pka` — the detail moved to the `calculation-selection` skill, which is loaded on demand
#: — and the `structure_id` argument **removed rather than shortened**. Every other geometry-taking
#: spec here accepts one so a caller can carry a chosen conformer forward; this job's first act is
#: a metadynamics conformer search, which re-samples whatever it is handed, so the argument
#: advertised a control that controlled nothing and cost its explanation on every turn.
#:
#: The ~640 that remain are what a second pKa calculator costs, and the pull request says why the
#: turn is worth it: it answers a question the fast one cannot (which proton), rather than the same
#: question better — measured, the two are level on error.
#:
#: **Examined on 2026-08-27 and deliberately left at 29,500**
#: (`D-2026-08-27-eighteen-names-for-a-primitive-set`). That is the pass the 2026-08-25 paragraph
#: above asks for, and it did not produce a reduction, so it does not produce a lower number: the
#: `default` prefix measures **28,114** both before and after it. What the pass established is where
#: the reduction actually lives. Eleven of the eighteen names that raised this ceiling are what the
#: default profile pays for; the other six are `chem` *endpoint* tools, whose schemas come from a
#: running server and are therefore invisible to `_floor` — so the seventeen-name surface the
#: backlog row worries about is 5,787 tokens here, not the whole of it. Dropping those eleven from
#: `default` (they are already named by the `computation` profile) measures **22,327, −21%**, and
#: that is a `data/profiles` edit rather than a tool change. Two of the nine `run_*` templates are
#: single-job wrappers worth 681 tokens between them; `run_bond_strength_survey` and
#: `survey_bond_strengths`, the pair the row names, are two capabilities and stay two.
#:
#: **Lowering this constant is the commit that proves a reduction happened, so it is not lowered by
#: a commit that only measured one.**
#:
#: **33,000 as of 2026-08-28**, raised from 29,500 by the prescriptive protocol surface
#: (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`), which measures **32,184** with
#: it. Four tools: `structure_experiment_request`, `draft_experiment_protocol`,
#: `read_experiment_protocol` and `find_experiment_protocols`, at **961 / 2,419 / 165 / 128**.
#:
#: **It arrived at 35,035 and this is the narrowed figure**, which is the number the convention
#: above says to judge a new tool by. What the −2,851 was, measured rather than estimated, because
#: three of the four causes are reusable and one is not:
#:
#: 1. **`draft_experiment_protocol` stopped taking the ask back.** It took a whole
#:    `ExperimentDesign`, whose `request` half `structure_experiment_request` had already stored —
#:    so the largest single item in the schema was a copy of a document the design already held, and
#:    one that could disagree with the copy the chemist had corrected. It now takes `design_id` plus
#:    the protocol half only, which makes the documented two-phase flow structural instead of
#:    advisory. −1,600.
#: 2. **It stopped taking a `layout`.** A plate layout is computed from `plate_format` by
#:    `protocols.layout.place`; a model-supplied one could contradict the format it was asked for.
#:    An argument that should never be filled in was costing its own schema on every turn.
#: 3. **`SpeciesRole` shipped its class docstring once per field that named it.** Pydantic publishes
#:    a referenced enum's docstring as the field description and `convert_to_openai_tool` inlines
#:    rather than `$ref`s, so `science/labels/vocabulary`'s 180-token argument for why the derived
#:    vocabulary is not `Role` — the right docstring for a reader of that module — was in this
#:    schema three times. One shared `Field(description=…)` naming the values replaces it.
#: 4. **Fifteen model docstrings moved into `#` comments**, the fix that took `profile_rotation`
#:    from 1,499 to ~870 four paragraphs up. `RequestField`'s alone shipped **four times** in one
#:    request. −457 on `structure_experiment_request`.
#:
#: **Both writing tools remain over `MAX_SINGLE_TOOL_TOKENS` and are recorded in `KNOWN_OVERSIZED`
#: below rather than narrowed further, and that is a decision rather than an omission.** The
#: irreducible core is `base: ProtocolBody` at **922 tokens on its own** — setpoints, a charge
#: table, ordered steps, analytics and an expected outcome, each a small model with a one-line
#: description. A typed laboratory procedure is about 900 tokens of schema, so no narrowing gets
#: this tool under a 900-token bound; only deleting the schema does. The alternatives were measured
#: against and rejected: taking the payload as a JSON string or a scratchpad path drops the schema
#: to ~150 tokens and takes schema-guided generation with it, trading a reliability property for a
#: context one on the tool where a malformed call is most expensive; and splitting it three ways
#: leaves the sum unchanged, the first piece still over, and a protocol costing three round trips
#: against the loop cap.
#:
#: **316 of the total are not tools at all.** The skills listing measured 3,034 and then 3,350
#: across two runs on this branch as the two new skills' frontmatter was still being edited. Worth
#: recording because it is the second time this file has caught a cost arriving from beside the tool
#: surface rather than from it — a skill's `description` is published into the prompt on every turn
#: exactly as a tool's is.
#:
#: The headroom is ~816 tokens, tighter in proportion than the 29,500 it replaces: less than
#: `propose_knowledge_note` costs, so it cannot absorb another tool of that size unnoticed.
#:
#: **Raised again on 2026-08-29 by the eight infrastructure findings**, which added five tools to
#: the `default` surface: `review_activity` 585, `request_external_input` 533, `review_commitments`
#: 421, `assemble_evidence_pack` 350, `check_pending_requests` 281 — **2,170 between them**, after a
#: trimming pass took 225 out of the two largest by moving developer rationale out of the docstrings
#: a schema ships. On that branch alone the prefix went 28,210 → 30,390; **merged with the protocol
#: surface above it measures 34,379**, which is 32,184 + 2,170 to within 25 tokens — the two
#: surfaces are additive, as they should be, and neither absorbed the other's headroom.
#:
#: Four of the five are the reason a project leader can be answered at all — the operational read
#: model, the inbox over the durable wait, the commitment mirror and the evidence pack — so this is
#: capability rather than drift. It is also **exactly the growth § 5's row is about**, and two
#: independent surfaces raising this ceiling within two days is the argument *for* that row rather
#: than against it: the `default` allow-list measures **-5,787 tokens (-21%)**, more than twice what
#: this work added and more than the protocol surface's headroom, and it is a `data/profiles` edit
#: rather than a tool change. It stays blocked on the live lane for the reason it gives — a cheaper
#: prompt that stops finding tools is a regression with a good-looking metric.
#:
#: The headroom is ~620 tokens against a measured 34,379 — tighter again than the ~816 the protocol
#: surface left, and now well under what a single tool of `propose_knowledge_note`'s size costs. The
#: next surface to arrive here should expect to be asked for the allow-list first.
#:
#: **43,500 as of 2026-08-29, and no tool was added.** Every paragraph above measured the wrong
#: thing: the basis moved from `convert_to_openai_tool` over `_capability_tools` to the tools the
#: compiled graph actually binds (`_bound_tools`, which says what the two are and why they differ),
#: and the `default` profile measures **42,505** where the old basis reported 34,379. **The number
#: grew because the measurement got honest, not because the surface did** — nothing shipped, nothing
#: regressed, and a deployment was already paying every one of these 8,126 tokens on every turn
#: while this file called the smaller figure "the payload rather than an approximation of it".
#:
#: So every figure above is a *lower bound* on what its own change actually cost, and none of them
#: is restated here: they were each right about the delta they measured and wrong about the base,
#: and rewriting them would be inventing measurements nobody took.
#:
#: The headroom on that day was **995** tokens against 42,505 — under what `propose_knowledge_note`
#: costs on the honest basis (1,126), so it could not absorb another tool of that size unnoticed,
#: which is the property every raise above was chosen for.
#:
#: **That headroom figure is about a commit, not about `HEAD`, and this file no longer states a
#: current one.** The floor moves whenever any bound tool's schema changes, including on branches
#: that never touch this file: four days later `default` measured 42,549 — 951 of headroom — after
#: a merge that touched `agent/protocol_design_tools.py` and nothing else here. Two sessions in a
#: row have re-transcribed these numbers *in order to correct them* and been stale again within a
#: merge. The property that survives is the one the assertion below tests; the number is whatever
#: `_floor` returns when you run it, which is why the failure message prints it.
#:
#: **The three figures in the two paragraphs above were re-measured on 2026-08-29 and each moved,
#: and the reason is the same one they are about.** They were written on a branch and landed after
#: `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, which rewrote the `task` tool's
#: description — so the basis shifted underneath the commit that was correcting the basis, exactly
#: as the 2026-08-25 paragraph at the top describes happening to the ceiling itself. Re-derived at
#: `HEAD`: `default` is **42,505** (was written as 42,458), the old basis is **34,379** (written as
#: 34,399, which was a transcription error rather than a stale reading — nothing ever measured
#: 34,399), the gap between them is **8,126** (written as 8,059) and the seven middleware tools are
#: **2,636** (written as 2,569). **Nothing about the mechanism changed; the numbers moved because
#: they were taken again.**
#:
#: **And the same sentence then said \"995 to spare\" for six days, which is the defect one
#: paragraph up, committed by the paragraph that names it.** Re-measured 2026-09-04: `default` is
#: **42,717**, so the headroom was 783 rather than 995 — drifted by merges this file never saw.
#: The load-bearing property is what survives a re-measurement and the number is not: the ceiling
#: holds with less headroom than one `propose_knowledge_note` costs, which is the property every
#: raise above was chosen for, and `_report` prints the day's figure so nobody has to trust this
#: comment for it.
#: **67,000 as of 2026-09-05, and again no tool was added — the fixture stopped lying instead.**
#: `_bound_tools` compiled the graph with `connectors=None` while `build_langgraph_agent` has taken
#: that argument since M7, so every figure above measured a turn with **no connector bound**:
#: `default` binds **92** tools and measures **66,157**, where the connector-less fixture reported
#: 61 and 42,730. The 23,427-token difference is 31 endpoint tools this repository serves itself,
#: and a deployment has paid all of it on every model call since the first bundle shipped.
#:
#: **This is the same failure as the 2026-08-29 entry, one boundary further out**, and it is worth
#: saying twice: that one measured the wrong *object* (a callable instead of the bound tool), this
#: one measured the wrong *graph*. Both were invisible for the same reason — the assertion and the
#: thing asserted were derived from the same short read — and both were found only by someone
#: measuring a shipped turn rather than reading the test. The paragraph above promising that a
#: connector "lands here the moment it is bound" was a statement about `_bound_tools`'s method that
#: its own call site made false.
#:
#: **What this still does not cover is named rather than implied**: `SERVED_ELSEWHERE`'s three
#: bundles are `Chemclaw3-mcp`'s, ~8,600 tokens over 21 tools measured against the sibling checkout
#: on 2026-09-05, so the real shipped prefix is ~74,700 and this ceiling gates the ~87% of it that
#: is knowable from this tree alone. That remainder is a measurement
#: (`chemclaw_connector_tool_schema_tokens`), not a gap in the ratchet, and it cannot become one
#: without this repository building somebody else's server.
#:
#: The headroom is ~840 tokens against 66,157 — the same tightness the entries above were chosen
#: for, under what one `propose_knowledge_note` costs, so the next tool of that size cannot arrive
#: unnoticed on either side of the process boundary.
CEILINGS: dict[str, int] = {"__default__": 67_000}

#: How much of the floor one tool may be. A schema above this is not expensive, it is *badly
#: shaped* — the fix is pagination, a narrower argument, or splitting a tool that does two things.
MAX_SINGLE_TOOL_TOKENS = 900

#: The tools already over it, with what they cost when they were measured.
#:
#: Recorded rather than hidden by a bigger bound, so the *next* one fails this test. Every entry is
#: real debt and none is a mystery: each takes a **domain document** as its argument, which
#: `convert_to_openai_tool` inlines model by model. `start_optimization_campaign` carries a BoFire
#: campaign declaration — objectives, constraints and parameter domains; `propose_knowledge_note`
#: carries the note frontmatter contract; and the two protocol writers carry a structured ask and a
#: laboratory procedure (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`).
#:
#: **Adding to this list is not the way past this test**, and the two 2026-08-28 entries are here
#: only after the narrowing the ceiling comment above measures in four parts — 6,231 tokens to
#: 3,380, a 46% reduction — established that the remainder is the schema of the document itself.
#: `ProtocolBody` is 922 tokens with every description already one line, so a 900-token bound cannot
#: be met by a tool that authors a procedure; what would meet it is deleting the schema, which
#: trades constrained generation for context on the call where a malformed argument costs most.
#: **The escape this comment used to name — a conversion that `$ref`s a repeated model instead of
#: inlining it — was measured on 2026-09-04 and is closed against.** It is available: installed
#: `langchain_core` is 1.6.0 and has no switch (`_convert_json_schema_to_openai_function` calls
#: `dereference_refs` and pops `$defs` unconditionally), but `bind_tools` converts each tool with
#: `convert_to_openai_tool`, whose \"already in OpenAI function format\" branch copies a dict's
#: `parameters` verbatim — so a `$defs` schema is deliverable with no upstream patch. It costs
#: tokens rather than saving them. Over these ten, built the same way the shipped path is built:
#: inline **13,326**, `$defs`/`$ref` **13,438** — **+112, and every one of the ten worse**. Seven
#: of the ten reference each nested model exactly once, so the `$defs` wrapper buys back no
#: duplication at all; the three that do repeat one (`RequestField` 4x, `Setpoints` and
#: `SpeciesRole` 2x) still come out level or worse.
#:
#: **The mechanism is the reason, and it is the 2026-08-28 fix above running in reverse.**
#: `dereference_refs` merges a `$ref` with its siblings and lets the *field's* `description`
#: override the referenced model's, which is exactly what makes one shared `Field(description=…)`
#: suppress a nested class docstring: `SpeciesRole`'s ships **zero** times in what is sent today.
#: Under `$defs` there is no field to override it and it returns, once, at 223 tokens. The
#: isolated figure is wider still — +391 with titles stripped from both arms, because upstream's
#: `_rm_titles` does not recurse into lists, so hoisting a model out of an `anyOf` into `$defs`
#: also strips titles the inline arm keeps, a saving that has nothing to do with refs.
#:
#: **Re-measured 2026-08-29 on the bound basis, and six names arrived without anything being
#: added.** Every figure below grew (`draft_experiment_protocol` 2,419 → 2,568), and six tools that
#: read as under the bound were over it all along: `rank_species` measured 885 as a callable and
#: **1,094** as the object the model is sent. That is the ceiling comment's point at per-tool
#: resolution — these are not new debt, they are debt that was never visible, and the assertion
#: below is the reason six of them stayed invisible for eleven weeks while a test claimed to catch
#: exactly this. They are recorded rather than hidden by a bigger `MAX_SINGLE_TOOL_TOKENS`, which
#: is the same choice the four original entries were recorded under; the bound stays 900.
#: **Four names arrived on 2026-09-05 and, again, nothing was added.** `_bound_tools` began binding
#: the connector surface a turn actually binds, and `bo`'s four `OptimizationProblem`-taking
#: endpoint tools turned out to be the **widest schemas in the entire prefix** — wider than every
#: in-process tool, and unseen for as long as the fixture compiled its graph with no connector.
#: They are recorded here on the same terms as the six that arrived on 2026-08-29: this list is
#: what makes debt visible, and refusing to record debt that was already being paid would only mean
#: not measuring it. **The `MAX_SINGLE_TOOL_TOKENS` message's warning still stands for a tool that
#: is new**; these are eleven weeks old and were merely invisible.
#:
#: The cause is one model, inlined four times: `OptimizationProblem` is a discriminated union of
#: feature kinds, and `convert_to_openai_tool` inlines rather than `$ref`s (see above for why
#: `$defs` is *worse*, not better). It is the same document `start_optimization_campaign` carries
#: at 2,307. **12,055 tokens on every default turn for four copies of one decision space** is the
#: largest single narrowing left in this prefix, and it is a `connectors/bo/server/tools.py` change
#: rather than a core one — `docs/planning/BACKLOG.md` carries the row.
KNOWN_OVERSIZED: dict[str, int] = {
    "suggest_next_experiment": 3_590,
    "generate_screening_design": 2_900,
    "predict_outcome": 2_839,
    "campaign_progress": 2_726,
    "start_optimization_campaign": 2_307,
    "propose_knowledge_note": 1_126,
    # Both +22 against the re-measurement above, and it is the same 22 twice: they share the
    # `ExperimentDesign` schema, and the `max_length` ceilings
    # `D-2026-08-29-a-check-a-reader-never-sees-is-not-a-check` put on its six keyed lists render as
    # `maxItems`. `structure_experiment_request` also lost `source_text` and gained the sentence
    # that makes its `salt` docstring true, and those two cancel to nothing measurable here — the
    # ADR's "net +7" was taken on the raw-callable basis this file has since abandoned.
    # Re-derived 2026-09-04 and both had drifted with nothing saying so — 2,590 → 2,738 and
    # 1,075 → 1,095 — which is why `test_the_recorded_cost_of_a_known_oversized_tool_is_still_true`
    # below now exists. The +148 is a schema change on a branch that never touched this file; the
    # +20 is wording. Until that test, this dict was prose: a claim about somebody's afternoon.
    "draft_experiment_protocol": 2_738,
    "structure_experiment_request": 1_095,
    "rank_species": 1_094,
    "rank_species_across_solvents": 1_039,
    "compute_reaction_energy": 1_018,
    "survey_bond_strengths": 989,
    "refine_ensemble": 984,
    "profile_rotation": 936,
}

#: How far a `KNOWN_OVERSIZED` figure may drift before it has to be re-recorded.
#:
#: **Two-sided, because both directions are a lie of the same kind.** A figure that has grown is
#: debt nobody re-priced; one that has shrunk is a narrowing whose commit did not claim it, and the
#: file's own rule is that lowering a bound is what proves a reduction happened.
#:
#: **5% rather than an exact match**, because an exact match fails on every docstring edit and gets
#: bumped without thought — the same argument the ceiling comment makes one screen up. Every entry
#: here is over 900 tokens, so the band is never narrower than ~45 tokens: a clearer sentence
#: passes, and the two drifts this constant was written after (+148, +5.7% and +20, +1.9%) sit one
#: either side of it, which is the split intended. A whole-prefix growth that hides *between* the
#: bands is still caught by the ceiling above.
OVERSIZED_TOLERANCE = 0.05


def _count(text: str) -> int:
    """Tokens, by the same counter `agent/compaction.py` budgets the thread with."""
    from langchain_core.messages import HumanMessage
    from langchain_core.messages.utils import count_tokens_approximately

    return int(count_tokens_approximately([HumanMessage(text)]))


def _tool_name(tool: Any) -> str:
    """The name a provider sees, which for this repository's tools is the function's."""
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", tool))


def _tool_schema(tool: Any) -> str:
    """One tool exactly as a provider is sent it, via LangChain's own conversion.

    **Not hand-serialised, and the first version of this file got it wrong that way.**
    `core/tool_registry`'s `@tool` decorator is *identity* — it stores plain callables — so
    `_capability_tools` returns functions, not `StructuredTool`s. Reading `.name`, `.description`
    and `.args_schema` off one therefore finds a repr, an empty string and `None`, and the whole
    tool surface measured ~11 tokens per tool: a ratchet that would have held nothing.

    `convert_to_openai_tool` is the function LangChain itself calls when binding tools to a model.
    What it is handed is the half this file got wrong a second time, and `_bound_tools` is the fix:
    converting the *callable* and converting the `BaseTool` the graph actually binds are two
    different schemas, and the second is the one that gets sent.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return json.dumps(convert_to_openai_tool(tool))


#: The endpoint-bearing bundles this repository declares but does not serve, so their tool schemas
#: cannot be measured here at any price.
#:
#: `chem`, `rxnpredict` and `safety` are `Chemclaw3-mcp`'s servers (`D-2026-08-09-a-connector-we-do-
#: not-run`): this tree holds their `connector.yaml` and none of their code, so what their schemas
#: cost arrives at handshake from a process this repository does not build.
#: `connectors/transport.py::_record_schema_cost` publishes that half as
#: `chemclaw_connector_tool_schema_tokens`, a measurement rather than a ratchet, for this reason.
#:
#: Named rather than left implicit, and asserted below, for the reason
#: `cli/validate_connectors.py::unverified_tool_surfaces` gives about the identical blind spot one
#: layer over: a check that quietly shrinks is worse than one that says what it did not look at.
#: **Measured against the sibling checkout on 2026-09-05 they are ~8,600 tokens over 21 tools** —
#: so the figure this file gates is the ~87% of the shipped prefix that is knowable offline.
SERVED_ELSEWHERE = frozenset({"chem", "rxnpredict", "safety"})

#: What to allow for `SERVED_ELSEWHERE`'s schemas when a *bound* on the whole prefix is needed.
#:
#: **A bound rather than a measurement, for the same reason `CEILINGS` is one**, and the two are
#: added wherever a caller needs the figure a deployment actually pays: `PREFIX_BOUND` below.
#:
#: Measured 2026-09-05 against the `Chemclaw3-mcp` checkout beside this one — every declared tool,
#: through this repository's own `convert_to_openai_tool` path — the three bundles cost **9,538
#: tokens over 21 tools** (`chem` 5,380 / 12, `rxnpredict` 2,526 / 6, `safety` 1,632 / 3). 11,000
#: carries ~15% over that, which is the headroom a surface this file cannot ratchet needs: nothing
#: here fails when one of those servers adds a tool, so the allowance has to absorb one.
#:
#: **It is not asserted, and cannot be.** A test reading a sibling checkout would pass or fail on
#: whether somebody happens to have cloned it, which is the failure mode
#: `cli/validate_connectors.py::unverified_tool_surfaces` refuses for the same surface. What makes
#: the figure re-derivable instead of a claim is that the method is written down: dump
#: `tools/list` from each `chemclaw_mcp_<name>` server and convert it exactly as `_served_tools`
#: converts a local one.
SERVED_ELSEWHERE_ALLOWANCE = 11_000

#: The whole static prefix a shipped `default` turn may cost, as a bound: this file's ceiling plus
#: the allowance for what it cannot see.
#:
#: This is the number `core/config/agent.py` derives both compaction thresholds from — the trigger
#: is `PREFIX_BOUND + 30,000` and the budget `PREFIX_BOUND + 57,000` — and
#: `tests/test_compaction.py` asserts that relation rather than restating either figure. It is here
#: rather than in the config because the ceiling is here: two numbers that must move together
#: belong in one place, and the previous arrangement (a config comment quoting `43,500`) is exactly
#: how the connector-less ceiling propagated into two settings that were floored for eleven weeks.
PREFIX_BOUND = CEILINGS["__default__"] + SERVED_ELSEWHERE_ALLOWANCE


@cache
def _served_tools(connector: str) -> tuple[Any, ...]:
    """Every tool one bundle's own MCP server advertises, as the `BaseTool`s a turn would bind.

    **Not a fixture and not a hand-written schema: the bundle's real `FastMCP` server, over a real
    MCP session, through `load_mcp_tools` — the same function
    `connectors/transport.py::HeldConnectorSession._hold` calls.** An in-memory transport is the
    only thing substituted, so what is measured is the
    `tools/list` payload a deployment's pod would answer with. A schema invented here would be a
    second declaration of somebody else's surface, which is the defect this whole file is about.

    Cached per connector because the servers behind these imports are the heavy half of the tree
    (`bo` pulls BoFire, `calc` its whole spec surface) and `_floor` is called once per profile per
    test. One session per bundle for the module, not one per call.
    """
    module = server_tools_module(connector)
    server = getattr(module, "server", None) if module is not None else None
    if server is None:
        return ()

    async def load() -> list[Any]:
        async with create_connected_server_and_client_session(server) as session:
            return list(await load_mcp_tools(session))

    return tuple(asyncio.run(load()))


def _connector_tools(profile: Any) -> list[Any]:
    """This profile's connector surface, narrowed exactly as a turn narrows it.

    `connector_specs(profile)` is the production narrowing — `mcp_server_names` selects bundles and
    `tool_names` narrows each surviving allow-list — and `_allowed` is production's own manifest
    filter. Only the transport is replaced, so a bundle enabled, a tool added to a manifest, or a
    profile widened all land in the floor without this file being taught about them.
    """
    tools: list[Any] = []
    for spec in connector_specs(profile):
        tools.extend(_allowed(list(_served_tools(spec.name)), spec.allowed_tools))
    return tools


def _bound_tools(profile: Any) -> list[Any]:
    """The tools this profile's compiled graph actually binds — every one, as the object it binds.

    **Read off the graph rather than re-derived, because re-deriving is the defect.** For eleven
    weeks this file measured `convert_to_openai_tool` over `_capability_tools(profile)`, and its own
    docstring called that "the payload rather than an approximation of it". Measured against the
    compiled graph it was short by **8,126 tokens — 24% of what it reported** — in two structural
    ways, both invisible to any assertion built on the same callables:

    1. **A callable's schema is not its `BaseTool`'s schema.** `@tool` is identity, so this file
       converted raw functions while `build_langgraph_agent:247` binds `as_structured_tool(fn)`.
       Measured, **all 54** differ and every one is *larger* — `gather_evidence` 490 → 878,
       `get_durable_job_status` 274 → 662.
    2. **Seven tools were bound every turn and counted never.** `ls`, `read_file`, `write_file`,
       `edit_file`, `glob`, `grep` (this repository's `FilesystemMiddleware`) and `task`
       (`SubAgentMiddleware`) come from middleware rather than from the registry, so no walk of
       `_capability_tools` can ever see them. **2,636 tokens.**

    Reading the `ToolNode` is deliberate and is why this cannot drift again: any future tool source
    — a middleware, a connector, upstream — lands here the moment it is bound, without this file
    being taught about it. The backlog row that asked for this proposed spying on `bind_tools`
    instead; the node holds the same tools and needs no model call to say so, so the graph is
    built and never invoked.

    **That sentence was true of the method and false of this function, and `connectors=` is the
    fix.** For as long as the paragraph above existed this call omitted the argument
    `build_langgraph_agent` takes at line 148, so the ratchet measured a graph with **no connector
    bound at all** — 61 tools where a shipped turn binds 92, and 42,730 tokens where the honest
    figure is 66,157. A connector could not "land here the moment it is bound" because nothing here
    ever bound one: the surface the deployment pays for was 35% larger than the number this file
    gated, and the gap was the *whole* subject of the deferred-schema decision
    (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) sitting outside the only ratchet
    that could have priced it.

    What is bound is derived rather than invented — `_connector_tools` runs this repository's own
    manifests through its own narrowing and its own MCP loader — so it tracks the tree instead of
    a transcription of it. `SERVED_ELSEWHERE` names what that still cannot reach, and the test
    below fails if that set ever changes silently.

    **Three upstream shapes are read below, and all three are pinned in
    `tests/test_upstream_surface.py`**: the node key `"tools"`, `PregelNode.bound`, and the private
    `ToolNode._tools_by_name`. Only the last was pinned when this function was written, which left
    two thirds of the read able to break on a bump with nothing in the upstream-surface file going
    red — loudly rather than silently, but in the wrong file.
    """
    graph = build_langgraph_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="")])),
        profile=profile,
        audit_sink=NullAuditSink(),
        connectors=_connector_tools(profile),
    )
    return list(graph.nodes["tools"].bound._tools_by_name.values())


def _skills_listing(profile: Any, tools: list[Any]) -> str:
    """The skills block exactly as `SkillsMiddleware` publishes it into the system prompt.

    Built through the real middleware rather than re-derived from the `SKILL.md` frontmatter,
    because a second implementation of upstream's formatting is a second thing to keep in step —
    and the number this file gates on has to be the number the model is actually sent.
    `before_agent` on an empty state is upstream's own load path: what a first turn runs.
    """
    labelled = _labelled(_skill_dirs())
    middleware = _skills_middleware(skills_backend(profile, tools, labelled=labelled), labelled)
    loaded = middleware.before_agent({}, None, None) or {}
    return str(middleware._format_skills_list(loaded.get("skills_metadata", [])))


def _floor(profile_name: str) -> tuple[int, dict[str, int]]:
    """The static prefix for one profile: its total, and the per-part breakdown behind it.

    The two prose parts are measured from the *capability* tools deliberately, and only the tool
    schemas come from `_bound_tools`. `build_langgraph_agent:228` hands `skills_backend` the raw
    callables, so narrowing the skills listing by the bound list instead would measure a backend
    production never builds — a second implementation of upstream's narrowing, which is the
    mistake one layer over.
    """
    profile = get_profile(profile_name)
    parts = {
        "instructions": _count(instructions_for(profile)),
        "skills-listing": _count(_skills_listing(profile, _capability_tools(profile))),
    }
    for tool in _bound_tools(profile):
        parts[f"tool:{_tool_name(tool)}"] = _count(_tool_schema(tool))
    return sum(parts.values()), parts


def _report(total: int, parts: dict[str, int], ceiling: int) -> str:
    """The failure message, which is the deliverable.

    Whoever trips this needs to see *what they grew*, sorted, without going and measuring it
    themselves — otherwise the ratchet is an obstacle rather than a tool.
    """
    widest = sorted(parts.items(), key=lambda item: -item[1])[:12]
    lines = [f"  {tokens:>6}  {name}" for name, tokens in widest]
    return (
        f"static prefix is {total} tokens against a ceiling of {ceiling}.\n"
        "The twelve widest contributors:\n" + "\n".join(lines) + "\n"
        "Either make one of these narrower, or raise the ceiling in this file and say in the "
        "pull request why the turn is worth more."
    )


@pytest.mark.parametrize("profile_name", sorted(registered_profile_names()))
def test_the_static_prefix_stays_under_its_ceiling(profile_name: str) -> None:
    """Every profile's turn costs what it costs today, and no more, without somebody saying so."""
    total, parts = _floor(profile_name)
    ceiling = CEILINGS.get(profile_name, CEILINGS["__default__"])
    assert total <= ceiling, _report(total, parts, ceiling)


def test_no_single_tool_schema_dominates_the_floor() -> None:
    """A tool wider than `MAX_SINGLE_TOOL_TOKENS` is badly shaped, not merely expensive.

    Anthropic's own guidance for a tool that can return a lot is pagination, filtering and a
    sensible default — all of which shrink the *schema* as well as the result. A 900-token argument
    description is a sign the tool is doing two jobs.
    """
    _, parts = _floor("default")
    oversized = {
        name.removeprefix("tool:"): tokens
        for name, tokens in parts.items()
        if name.startswith("tool:") and tokens > MAX_SINGLE_TOOL_TOKENS
    }
    unexpected = {name: tokens for name, tokens in oversized.items() if name not in KNOWN_OVERSIZED}
    assert not unexpected, (
        f"these tool schemas are over {MAX_SINGLE_TOOL_TOKENS} tokens each: {unexpected}. "
        "Narrow the arguments or paginate the result; do not add them to KNOWN_OVERSIZED to make "
        "this pass — that list is debt already taken on, not a place to put more."
    )
    fixed = sorted(set(KNOWN_OVERSIZED) - set(oversized))
    assert not fixed, (
        f"{fixed} no longer exceed {MAX_SINGLE_TOOL_TOKENS} tokens — delete them from "
        "KNOWN_OVERSIZED. A debt list that outlives the debt reads as live state."
    )


def test_the_recorded_cost_of_a_known_oversized_tool_is_still_true() -> None:
    """`KNOWN_OVERSIZED`'s numbers are a measurement, and a measurement nobody repeats is prose.

    The sibling test above checks *membership* only, so for as long as this file has existed the
    figures beside each name were unasserted: re-derived on 2026-09-04, `draft_experiment_protocol`
    had drifted 2,590 → 2,738 and `structure_experiment_request` 1,075 → 1,095, with nothing red and
    nothing said. That is `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` happening
    inside the file that exists to prevent it, one level down from the ceiling it does assert.

    **What this asserts is a band, not equality** — see `OVERSIZED_TOLERANCE` for why, and why it is
    two-sided. Whoever trips it re-records the number in the same commit that moved it; that is the
    whole remedy, and the message carries the value to paste.
    """
    _, parts = _floor("default")
    drifted = {}
    for name, recorded in KNOWN_OVERSIZED.items():
        live = parts.get(f"tool:{name}")
        if live is None:
            continue  # No longer bound at all; the membership test above is what reports that.
        if abs(live - recorded) > recorded * OVERSIZED_TOLERANCE:
            drifted[name] = (recorded, live)
    assert not drifted, (
        "these KNOWN_OVERSIZED figures are no longer what the tool costs, by more than "
        f"{OVERSIZED_TOLERANCE:.0%}: "
        + ", ".join(
            f"{name} recorded {rec} but measures {live} ({live - rec:+})"
            for name, (rec, live) in sorted(drifted.items())
        )
        + ". Re-record them in the same commit that moved them, and say in the pull request what "
        "moved them — a figure nobody re-derives is a claim about the afternoon it was taken."
    )


class _CapturingModel(GenericFakeChatModel):
    """A model that records the prefix it is sent: the bound tool schemas and the system message.

    Observed rather than re-derived, for the reason `_bound_tools` gives one function up: a prefix
    assembled by this file is a second implementation of what the graph assembles, and the two
    agreeing proves nothing about what leaves the process.
    """

    def bind_tools(self, tools: Any, **kw: Any) -> Any:
        """Record the schemas exactly as bound, in the order bound — order is part of the bytes."""
        from langchain_core.utils.function_calling import convert_to_openai_tool

        _SENT.append({"tools": [convert_to_openai_tool(tool) for tool in tools]})
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        """Record the system message this call carries, then answer."""
        from langchain_core.messages import SystemMessage

        _SENT[-1]["system"] = [m.content for m in messages if isinstance(m, SystemMessage)]
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


#: What `_CapturingModel` recorded, newest last. Module level because the model is constructed by
#: the graph builder and there is nowhere else for a per-call observation to go.
_SENT: list[dict[str, Any]] = []


def sent_prefix(actor: str, correlation_id: str) -> str:
    """The bytes a model call sends before the conversation, for one actor and correlation id.

    Public because `tests/test_prompt_caching.py` drives it in a subprocess to compare two
    processes; there is no second in-repo way to obtain this string, and re-deriving it there
    would compare two derivations rather than two processes.
    """
    import asyncio
    import uuid

    from langchain_core.messages import HumanMessage

    _SENT.clear()
    model = _CapturingModel(messages=iter([AIMessage(content="done")]))
    graph = build_langgraph_agent(
        model=model,
        profile="default",
        actor=actor,
        correlation_id=correlation_id,
        audit_sink=NullAuditSink(),
        connectors=_connector_tools(get_profile("default")),
    )
    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]},
            {"configurable": {"thread_id": uuid.uuid4().hex}},
        )
    )
    return json.dumps(_SENT[-1], sort_keys=False)


def test_the_prefix_two_sessions_are_sent_is_the_same_bytes() -> None:
    """A prefix cache can only hit on bytes that repeat, so the prefix must not carry a turn in it.

    **This is the precondition under every prompt-caching remedy, and nothing asserted it.** The
    shipped provider is `openai_compatible` (`deploy/helm/chemclaw/values.yaml`), where
    `llm_provider.prompt_caching_middleware` returns `[]` — there are no `cache_control`
    breakpoints to place, so the entire saving depends on the *serving* stack recognising a
    repeated prefix (vLLM's `--enable-prefix-caching` and its equivalents). That recognition is
    byte-exact: one timestamp, one correlation id, one session id or one reshuffled tool order
    anywhere in the prefix turns a fleet-wide cache hit into a full prefill, on every call, with
    nothing anywhere reporting it.

    Measured 2026-09-05 at 321,856 characters: two turns for different actors, different
    correlation ids and different threads are **byte-identical**. So the request is shaped
    correctly today and this test is what keeps it that way — the failure it guards against is a
    one-line addition to a system prompt, and it would be invisible in every other test here.
    """
    first = sent_prefix("alice@example.com", "corr-a")
    second = sent_prefix("bob@example.com", "corr-b")
    at = next(
        (i for i, (a, b) in enumerate(zip(first, second, strict=False)) if a != b),
        min(len(first), len(second)),
    )
    assert first == second, (
        "the prefix two sessions are sent differs, so no server-side prefix cache can hit across "
        f"them and every model call pays a full prefill. First difference at character {at}:\n"
        f"  {first[at : at + 120]!r}\n  {second[at : at + 120]!r}"
    )


def test_the_floor_measures_the_connector_surface_a_turn_actually_binds() -> None:
    """The ratchet's basis includes the endpoint tools, and this is what would have caught it.

    **This is the assertion whose absence cost 23,427 tokens of blindness.** `_bound_tools` read
    the compiled graph's `ToolNode` — the honest source — and then compiled that graph without the
    `connectors=` argument production passes, so the ratchet gated a turn that does not exist. No
    test could see it: every figure was self-consistent, and the docstring promising a connector
    would "land here the moment it is bound" was about the read, not about the call.

    What is asserted is *derived from the manifests*, not transcribed: every tool the enabled
    bundles this repository serves declare must be in the bound set. A bundle added, a tool added
    to a manifest, or `connectors=` dropped again all fail here, and the last one fails loudly
    instead of shrinking the number in silence.
    """
    bound = {_tool_name(tool) for tool in _bound_tools(get_profile("default"))}
    declared = {
        tool
        for manifest in enabled()
        if manifest.endpoint is not None and manifest.name not in SERVED_ELSEWHERE
        for tool in manifest.endpoint.tools
    }
    assert declared, "no in-repo connector declares an endpoint tool; this test now checks nothing"
    assert declared <= bound, (
        f"these declared connector tools are not in the floor's basis: {sorted(declared - bound)}. "
        "The ratchet is measuring a turn with fewer tools than a deployment binds, which is the "
        "exact defect `_bound_tools`'s `connectors=` argument exists to prevent."
    )


def test_the_bundles_this_floor_cannot_measure_are_exactly_the_ones_it_names() -> None:
    """`SERVED_ELSEWHERE` is a claim about which schemas are out of reach, so it is checked.

    A blind spot that drifts is worse than one that is declared: a bundle whose server moved into
    this tree would silently stay excluded from the ceiling, and a *new* bundle served elsewhere
    would silently widen the unmeasured half while the ceiling comment kept quoting ~87%. Both
    directions fail here, which is the two-sidedness `OVERSIZED_TOLERANCE` is written for one
    level down.
    """
    endpoint_bundles = {m.name for m in enabled() if m.endpoint is not None}
    unmeasurable = {name for name in endpoint_bundles if not _served_tools(name)}
    assert unmeasurable == SERVED_ELSEWHERE & endpoint_bundles, (
        f"this file can measure the tool schemas of {sorted(endpoint_bundles - unmeasurable)} and "
        f"not of {sorted(unmeasurable)}, but SERVED_ELSEWHERE names {sorted(SERVED_ELSEWHERE)}. "
        "Update it and the ceiling comment's share-of-the-prefix figure in the same commit."
    )


def test_a_narrowing_profile_is_actually_cheaper_than_the_default() -> None:
    """A profile that narrows the surface but not the bill is not narrowing anything.

    This is the second defect the ratchet finds for free. `_capability_tools(profile)` attenuates
    what a turn may reach; the *reason* to do that is partly safety and partly cost, and a profile
    whose floor matches the default's has quietly stopped delivering the second half.
    """
    default_total, _ = _floor("default")
    cheaper = {name: _floor(name)[0] for name in registered_profile_names() if name != "default"}
    not_narrowing = {name: total for name, total in cheaper.items() if total >= default_total}
    assert not not_narrowing, (
        f"the default profile's prefix is {default_total} tokens and these are not below it: "
        f"{not_narrowing}. A profile that advertises fewer tools should cost fewer tokens."
    )
