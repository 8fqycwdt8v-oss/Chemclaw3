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

**And the prose half went on being re-derived for another week, which is that sentence happening
inside the file that wrote it.** The tool half was read off the compiled graph; the prompt half was
`instructions_for(profile)` plus `_skills_listing(...)` — this repository's own two contributions to
a system message the deepagents middlewares also write into. Measured 2026-09-05 against the
`SystemMessage` a model is actually handed: **7,006 derived against 7,484 sent, short by 458
tokens**. Every one of those 458 is upstream's `SKILLS_SYSTEM_PROMPT` — the wrapper deepagents puts
*around* the listing this file did measure, explaining progressive disclosure and how to read a
`SKILL.md`. That is what a middleware section costs today; what matters is that a bump lengthening
it, or any other middleware adding one, grows what every deployment pays on every turn with
nothing here going red — and the ceiling had already been passed, silently: at this change's base
commit the real prefix measured **43,521** against a ceiling of 43,500, with every test here green.

`_observed_prefix` fixes it the same way `_bound_tools` fixed the other half: one model call against
a capturing fake model, and the system message taken off the wire. The two derived halves stay in
the breakdown as a *split* of that observed number rather than as the basis for it, so `_report`
still says which half grew, and what neither half explains is a named line rather than a silence.

**And the graph it was all measured against bound no connector at all**, which is the same sentence
a third time and the largest of the three. `build_langgraph_agent` has taken a `connectors=`
argument since M7 and this file's call omitted it, so every figure above described a turn nobody
runs: the fixture bound the in-process surface while a shipped turn binds that *plus* every enabled
bundle's endpoint tools. `_connector_tools` closes it by running this repository's own manifests
through its own narrowing and its own MCP loader — derived from the tree rather than transcribed —
and `SERVED_ELSEWHERE` names the bundles whose servers live in `Chemclaw3-mcp`, which this
interpreter cannot import at any price. The allowance test below measures them across a process
boundary instead — in that repository's own interpreter, over a checkout `infra/live/siblings.sh`
locates — and skips loudly where there is no checkout of it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

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
from chemclaw.agent.skill_manifest import MAX_SKILL_DESCRIPTION_CHARS
from chemclaw.connectors.registry import discovered, enabled, server_tools_module
from chemclaw.connectors.transport import _allowed
from chemclaw.core.config import Settings, settings
from tests.siblings import (
    SIBLING_SKIP,
    bundles_declared_here,
    fleet_published_bundles,
    sibling_python,
    sibling_root,
)

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
#: `record_knowledge_note` costs, so it cannot absorb another tool of that size unnoticed.
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
#: surface left, and now well under what a single tool of `record_knowledge_note`'s size costs. The
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
#: The headroom on that day was **995** tokens against 42,505 — under what `record_knowledge_note`
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
#: holds with less headroom than one `record_knowledge_note` costs, which is the property every
#: raise above was chosen for, and `_report` prints the day's figure so nobody has to trust this
#: comment for it.
#: **44,500 as of 2026-09-05, and nothing was added — the measurement got honest again.** The
#: paragraph above is about the tool half; the prompt half was still re-derived, and re-derived it
#: is 458 tokens short of the `SystemMessage` a model is handed (see the module docstring). On the
#: observed basis `default` measures **43,701**, and it measured **43,521** at the commit this
#: change branched from — over the 43,500 ceiling that was supposed to bound it, by 21 tokens, with
#: every test in this file green. So this raise buys nothing and hides nothing: it is the same
#: surface, counted where it is paid.
#:
#: The headroom is what a raise is judged by, and it must stay *under* what one
#: `record_knowledge_note` costs, so the ceiling cannot absorb another tool of that size unnoticed.
#: A ceiling set to today's measurement plus a fixed headroom is set against whatever else is in
#: flight, which is why the margin is deliberately thin.
#:
#: **The two figures this paragraph used to state were stale within four days, and one of them was
#: stale when it was written.** It said 799 tokens of headroom against a 1,126-token tool; measured
#: on 2026-09-05 the floor was 44,089 (headroom 411) against a 1,238-token tool, drifted by merges
#: this file never saw — and this commit's own prose edits to tool docstrings moved it to 44,145
#: (headroom 355), which is stated because a docstring is part of the bill and pretending otherwise
#: is how the floor gets away from a ratchet. No raise: the property holds, more tightly than
#: before. Both live figures come out of `_report`, so nobody has to trust this comment for them,
#: and per `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` the ones written here are a
#: claim about this commit and nothing later.
#:
#: **`agent_tool_result_clear_trigger` moves with it, by derivation rather than by retuning**
#: (`core/config/agent.py`): its default is `PREFIX_BOUND` — this ceiling plus the allowance below
#: it — plus the 30,000 of thread the setting has always meant, so raising the ceiling raises the
#: trigger with it and every deployment's lossless edit fires slightly later than it did. That
#: coupling is the reason to keep this number a *ceiling* rather than a measurement — see the
#: config comment, which says why.
#:
#: **65,000 as of 2026-09-05, and for the third time nothing was added — the fixture stopped
#: measuring a smaller system.** The two entries above are about the tool half's *objects* and the
#: prompt half's *text*; this one is about the graph. `_bound_tools` read the compiled graph's
#: `ToolNode` — the honest source — while the call that compiled it omitted the `connectors=`
#: argument `build_langgraph_agent` has taken since M7, so the ratchet gated a turn with **no
#: connector bound at all**. Measured both ways on this commit: **43,179 over 61 tools** with the
#: argument omitted, **64,099 over 92** with it passed — 31 endpoint tools and **20,920 tokens**
#: this deployment has paid on every model call since the first bundle shipped, sitting outside
#: the only ratchet that could price them. The docstring promising that a connector would "land
#: here the moment it is bound" was a claim about the *method* that its own call site made false,
#: which is the 2026-08-29 entry's own shape one boundary further out.
#:
#: **What this still does not cover is named rather than implied.** `SERVED_ELSEWHERE`'s three
#: bundles are `Chemclaw3-mcp`'s servers, so their schemas arrive at handshake from a process this
#: repository does not build: **9,864 tokens over 21 tools**, measured against the sibling checkout
#: on 2026-09-05, which puts the shipped `default` prefix at **73,963** over 113 tools and makes
#: this ceiling a gate on 87% of it. `SERVED_ELSEWHERE_ALLOWANCE` is the bound for the rest and
#: `PREFIX_BOUND` is the two added. That remainder is a measurement
#: (`chemclaw_connector_tool_schema_tokens`), not a gap in the ratchet, and it cannot become one
#: without this repository building somebody else's server.
#:
#: **The figure this paragraph shipped with was 9,538, and it was stale on the day it was
#: written** — the sibling repository's own merge landed a `Raises:` paragraph and eleven lines
#: into `chem` the same afternoon. That is not a transcription error, it is the property of a
#: number about a repository this one does not build, and it is the reason
#: `test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound` below now
#: measures the sibling rather than quoting it.
#:
#: The headroom this leaves is smaller than one `record_knowledge_note`'s schema costs, which is
#: the property every entry above was chosen for. Stated as a relation rather than as two figures:
#: `_floor("default")` measures both, and the pair drifts on every merge that touches a tool
#: schema — the paragraph above is about exactly that.
#: **The basis moved again on 2026-09-09, and this time the prompt is what changed shape**
#: (`chemclaw_agent.PromptBlock`). The default instructions are now assembled per graph from blocks
#: that declare the tools they name, so what a deployment is *sent* varies with what it binds, and
#: `_floor` charges `_maximal_instructions` — the worst of both trail variants, every block — rather
#: than what this fixture can observe. It has to: the fixture binds no `SERVED_ELSEWHERE` bundle, so
#: the prompt it sees is missing the `screen_hazards` and `resolve_compound` blocks a served fleet
#: is sent, and charging the observation would be this file measuring a smaller system for the
#: fourth time. That difference is **206 tokens**, carried as its own line so nobody has to trust
#: this sentence for it.
#:
#: Measured four ways in one commit, because the ceiling did **not** move and the reason matters:
#:
#: - merged `main`, old basis: **64,720** over 92 tools — under this ceiling by 280.
#: - this branch's working tree with the prompt change *reverted*, old basis: **65,755**. The whole
#:   +1,035 is tool-schema growth from other modules edited on the same branch
#:   (`agent/graph_tools.py`, `agent/durable_tools.py`, `connectors/calc/server/tools.py` and four
#:   more), which is this file's standing lesson arriving inside one afternoon: the floor moves on
#:   somebody else's diff, and the only honest form of the number is the measurement.
#: - this branch with the prompt change, old basis: **65,578** — the text actually sent is **177**
#:   tokens narrower (upstream's Deepagents/Agents source-label sentence, 44, plus the two blocks
#:   this fixture's surface drops, 174, less the 32 the log-only trail block costs over the durable
#:   one).
#: - this branch with the prompt change, this basis: **65,784**.
#:
#: The tool half moved **three times in that one session** — 57,137, then 58,172, then 57,580 — as
#: sibling waves merged into the same tree, which is why the durable claim here is the two *deltas*
#: this change is answerable for (−177 sent, +206 previously unbanked) rather than any total. Read
#: the totals as of their measurement and re-measure before quoting one.
#:
#: **So the ceiling is breached and raising it is not this file's decision to take alone.**
#: `PREFIX_BOUND` below is this ceiling plus `SERVED_ELSEWHERE_ALLOWANCE`, and
#: `tests/test_compaction.py` holds `agent_tool_result_clear_trigger` and
#: `agent_context_token_budget` at their claimed allowances above it, so raising this number is
#: never free: it moves `PREFIX_BOUND`, and every token of prefix is a token of thread the policy
#: no longer has. `core/config/agent.py` states the same rule the other way round — *"the
#: instrument for wanting more is a narrower prefix"*.
#:
#: **65,500, and what the 500 cost.** Wave 13 made eight record-surface reads able to say their
#: answer was only a page, which is prefix a chemist gets a truthful "have we done this before?"
#: for. Narrowing paid part of it back — the wave's prompt blocks give back 177, and a
#: `report_measurement` paragraph that was a correction to a *previous docstring's wording* rather
#: than anything about the tool gave back 75 more, in a schema the model pays for on every call.
#: The remainder is bought, not found:
#:
#: * `agent_tool_result_clear_trigger` rises 500 with it, so the lossless edit keeps the full
#:   `CLEAR_TRIGGER_THREAD_ALLOWANCE` it was derived to have. Nothing bounds that setting from
#:   above, so it costs nothing to move.
#: * `agent_context_token_budget` does **not** rise, because it is derived downwards from the 128k
#:   window and there is nothing above it to take from. So the budget's thread allowance falls
#:   43,000 → 42,500 — **1.16% of the thread**, and that is the price of this ceiling, paid where
#:   the constraint actually is rather than spread until nobody can see it.
#:
#: Set with headroom on purpose. A ceiling 23 tokens above a measurement is a tripwire that the
#: next unrelated merge trips; this left ~420 for ordinary drift when it was set, which is what
#: makes it a ratchet rather than a trap. **The live headroom is this ceiling minus what the test
#: below measures, and it moves in both directions** — it read 610 three waves later, because a
#: bound tool's schema *shrank*. That is why the figure is dated here rather than stated: a
#: headroom transcribed as current is a claim about a commit, which is the defect the paragraph
#: above spends fifteen lines on.
#:
#: **67,500 since D-2026-09-13, and this one was bought outright.** Turning `harness_enabled` on by
#: default puts `write_todos` and upstream's todo prompt into every profile's prefix: measured both
#: ways in one process, +1,372 for `tool:write_todos` and +490 for `prompt:middleware-sections`,
#: **1,862 on every profile except two**: `computation`, which already sets the flag itself and so
#: moved by 0, and `safety`, which moved 1,863. The odd token is in the ADR's own table and was
#: rounded away by every prose statement of it, this one included, until a review read the table.
#: Only `default` was near enough to matter — 64,907 → 66,769, over the old ceiling by 1,269.
#:
#: Nothing was narrowed to pay for it, and that is deliberate rather than lazy: the narrowing this
#: wants is §5's `default`-profile allow-list, worth a measured -5,787, and it is blocked on a live
#: lane that can show every probe still reaching its tool. Buying the ceiling now and narrowing
#: later is the right order; narrowing blind to buy a ceiling is how a cheaper prompt stops finding
#: tools.
#:
#: The price, by the rule stated above: `agent_tool_result_clear_trigger` rises 2,000 with it and
#: costs nothing, while `agent_context_token_budget` cannot follow, so the thread allowance falls
#: 42,500 → 40,500 — **4.7% of the thread**, an order of magnitude more than wave 13's 500 paid.
#: What it buys is the plan gate attached in the posture every supported deployment already runs,
#: which `D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest` names as
#: the only cover over 29 write tools.
#:
#: **67,200 since `D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising`**, which is the 300 this
#: ceiling should have fallen by when `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`
#: moved 309 tokens of developer rationale out of three tool descriptions. Two changes landed in one
#: wave — the harness default above, +2,000, and that lowering, -300 — and the merge that resolved
#: them took the first and dropped the second, leaving three documents asserting a lowering the tree
#: did not carry. The two are independent and both belong, so the ceiling is their sum.
#: **Two branches added tools on the same day and each raised this ceiling for its own pair; the
#: merged tree carries both, so neither number survived and this one is measured on the union.**
#:
#: `D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction` adds `compose_workflow`
#: and `run_composed_workflow` — **978 tokens**, 752 and 226. The split is its argument:
#: `run_composed_workflow` takes a name and a dict, while `compose_workflow` publishes the step and
#: input models a workflow is *written* in and cannot be smaller without the model guessing the
#: shape. What it buys is the only path by which a procedure this system works out becomes one it
#: can re-run.
#:
#: `D-2026-09-15-a-comparison-with-no-caller-is-a-promise-about-a-check-that-does-not-exist` adds
#: `check_against_specification` and `estimate_stability_trend` — **730 tokens**, 316 and 414.
#: **Those were trimmed before their ceiling moved**, which is the order this file's history sets:
#: the first description went from 1,266 characters to 844 and the prefix was still 316 over,
#: because a tool's description is counted twice — once as itself and once inside the schema that
#: embeds it — so trimming cannot close a 300-token overage without gutting the prompt the model
#: reads. All four are under `MAX_SINGLE_TOOL_TOKENS`.
#:
#: **The merge is the lesson, not the arithmetic.** Each branch measured honestly against a tree
#: that did not contain the other's tools, so each ceiling was right when written and wrong on
#: `main` — which is this file's own standing warning ("the floor moves on somebody else's diff")
#: arriving as a merge conflict rather than as a red build. The number below is re-measured on the
#: union and neither branch's: **68,908**, which is 67,200 plus both pairs exactly. 69,800 leaves
#: 892 of headroom — the ~856 the workflow branch argued for, restored on the merged basis, where
#: its own 69,000 leaves 92 and is the 34-token tripwire it was written to escape.
#: **Raised to 70,600 by the `task` roster**
#: (`D-2026-09-16-a-roster-varies-the-two-dimensions-that-carry-no-authority`). Measured on this
#: commit: 69,107 without the roster and **69,412** with it, +305, all of it the `task` tool's own
#: description growing 592 -> 897 as upstream folds each entry's `{name}: {description}` into
#: `{available_agents}`.
#:
#: The raise is 1,188 rather than 305, and the difference is stated because it is not slack.
#: **This ratchet under-charges the roster by construction**, for the reason
#: `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` gives one level out:
#: each entry's description names the tools that entry's helper *binds*, and what a helper binds
#: depends on which connector bundles a deployment enables. Here `safety` binds nothing and is not
#: offered at all, and `computation` lists what this repository serves rather than what the fleet
#: does. Measured against the full declared surface the `task` description is **902** tokens rather
#: than 710 — so a real deployment pays ~192 tokens this file cannot see, and `safety` alone is
#: +107 of them. That excess belongs to the `task` tool, which is first-party and inside this
#: ceiling, so `SERVED_ELSEWHERE_ALLOWANCE` cannot absorb it and was not moved.
#:
#: 388 tokens of headroom was the alternative, against a file whose own history records a
#: neighbouring merge drifting this floor by 326 on a `Raises:` paragraph
#: (`D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising`). That is a tripwire rather than a
#: bound. What the turn buys for it is in the ADR; what it costs every deployment is 1,188 tokens
#: of thread allowance, and `core/config/agent.py` derives both compaction defaults from
#: `PREFIX_BOUND`, so they move with it.
#:
#: **Not raised by the durable-memory flip, and the headroom is where it went**
#: (`D-2026-09-20-a-tier-every-prefix-pays-is-still-not-a-ceiling`). `agent_memory_enabled` going
#: True binds `propose_skill` on every request, and this file could not see it: `_observed_prefix`
#: built under the code default of `session_store="memory"` while the shipped chart pins
#: `postgres`, so `personal_skills_available()` was False here and True in the fleet — the shape
#: `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` names, one predicate
#: over. `_as_a_deployment_runs` is the correction. Measured on this commit with the corrected
#: basis: **69,872**, of which `propose_skill` is **462** — 728 of headroom left, so the number
#: below does not move and neither do the two compaction defaults derived from it.
#:
#: The two *stored* skills tiers are deliberately still outside this, each with its own allowance:
#: they are a deployment's bytes rather than this repository's, and folding a worst case nobody has
#: into `PREFIX_BOUND` costs every deployment on earth the same thread allowance. See
#: `LOCAL_SKILLS_ALLOWANCE` and `ORG_SKILLS_ALLOWANCE`.
#: **Raised to 71,400 when `rescale_experiment_protocol` landed. The figure this entry first gave
#: was wrong, and correcting it is the point of the correction.**
#:
#: It said the tool "wanted 948" and that every deployment pays "948 more tokens" for it. 948 was
#: that commit's whole-prefix delta (69,872 -> 70,820), not the tool's schema. Re-derived on
#: 2026-09-21 with this file's own counter: `rescale_experiment_protocol` is **300** and its skill's
#: listing entry (`protocol-scale-translation`) is **127** — 427 of the 948, and the remainder is
#: **not attributed here**, because the honest thing to write in this file is the number that was
#: measured rather than a plausible split for the rest.
#:
#: That is the mistake this file exists to prevent, made inside it:
#: `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` is about exactly this, and
#: `test_the_recorded_cost_of_a_known_oversized_tool_is_still_true` was written because the figures
#: beside `KNOWN_OVERSIZED` drifted unasserted. A per-tool figure in *this* comment has no such
#: assertion behind it — the ceiling is asserted, the attribution is prose — so a reader should
#: treat any per-tool number in these entries as a claim about the afternoon it was taken and
#: re-derive it before relying on it. The ceiling itself was measured correctly against the real
#: prefix total on each raise, which is why the constant is right and the sentence was not.
#:
#: What the turn buys is unchanged and is the reason the raise stands: taking a procedure from the
#: scale it was run at to the scale it will be run at is the defining kilo-lab task, and before this
#: the model could only do it by multiplying numbers in prose, where the failure is silent and
#: specific — everything gets multiplied, including the addition time and the filtration, and the
#: scaled document then prescribes a time-temperature history no experiment ever produced.
#:
#: **Why the prose was not trimmed further instead**, since that is the cheaper answer when it
#: works: it was, twice, and the second pass bought **22 tokens** (70,842 -> 70,820). The schema
#: wrapper, the name and two string arguments are the floor for any tool at all, and
#: `test_no_single_tool_schema_dominates_the_floor` passes, so this is an ordinary tool's price
#: rather than a badly-shaped one.
#: **And to 72,000 for six process-development skills, which is a different kind of raise.**
#: A skill costs the prompt its *name and description* — the `skills-listing` contributor — and
#: nothing else until a turn loads it, which is what makes layer 3 cheap. Six of them
#: (`crystallisation-design`, `solvent-swap-and-distillation`, `impurity-fate-and-purge`,
#: `analytical-readiness`, `scale-up-readiness-review`, `robustness-and-edge-of-failure`) measured
#: **594** together. Measured on this commit: **71,414**, so the raise is that plus a tripwire's
#: worth of headroom rather than the round number it looks like.
#:
#: The descriptions were trimmed twice first, because that is the answer that costs nothing when it
#: works: 830 tokens down to 594. It stopped there on purpose. A skill's description is the only
#: thing deciding whether the model loads it at all, so trimming past the trigger phrases buys
#: prefix by making the judgment unfindable — which is a worse outcome than the prefix, and an
#: invisible one.
#:
#: **Why these are global rather than bundled**, since a bundled skill would have cost nothing on a
#: deployment that binds no process-development bundle: four of the six span two or more bundles
#: (`crystallisation-design` reads `unitops` and `props`; `scale-up-readiness-review` reads
#: everything), and a bundled skill belongs to one capability. The other two name only
#: default-enabled tools. Splitting judgment across bundles to save prefix would put the same
#: skill in two places, which is the duplication `connectors/README.md`'s ownership rule exists to
#: prevent.
#:
#: The cost, stated: every deployment pays 594 more tokens on every model call and the thread
#: allowance drops by the same amount again — `tests/test_compaction.py`'s two allowances carry it,
#: for the reason the entry there gives about the window being the input.
#: **And to 73,100 for the plate-results loop — the third raise on this branch, and the one with
#: the best case.** `attach_plate_results` cost **606** (a nested `list[ArmResult]` argument) and
#: `read_plate_results` **257**. Measured on this commit: **72,641**. Re-measured after the
#: tool's own top-level `note` argument was removed (each arm keeps `ArmResult.note`):
#: `attach_plate_results` **592**, `read_plate_results` unchanged, prefix **72,384**.
#:
#: The case is better than the two above it for one reason worth stating rather than assuming: both
#: tools work in **every** deployment. They touch only core's design store, so unlike the six
#: process-development skills — whose judgment is about tools most deployments do not bind — and
#: unlike the template that was parked for exactly this, the prefix here buys something every turn
#: can actually use.
#:
#: What it buys is the loop that was open since `D-2026-08-28` built the prescriptive tier: a
#: design reached `executed` and nothing attached the outcome, so the round trip
#: `skills/hte-campaign-design` promises in its own closing section was a person retyping a table,
#: and the `DEFERRED.md` row on mining the agent-to-human protocol diff had no corpus because
#: nothing could tell which designs had ever been run.
#:
#: **The running total is the thing to look at, not this entry.** This branch has taken
#: 70,600 -> 73,100, which is **2,500 tokens of thread allowance from every deployment on earth**,
#: and `tests/test_compaction.py`'s two allowances carry all of it because the budget is pinned by
#: the window rather than the prefix. A fourth raise on one branch should be refused; what buys it
#: back is `D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`'s deferred schemas, or
#: profile routing, neither of which is a raise.
#: **Lowered to 72,850, which is the first entry here that gives something back.**
#:
#: Three of the six skills above turned out to be largely inert in a default deployment: their
#: *central* tools ship with the opt-in process-development bundles, so what a default turn paid
#: for was judgment about a path it cannot take. `solvent-swap-and-distillation` is the clearest —
#: six of its twelve tools are the `props` chain plus `shortcut_distillation`, so a default
#: deployment can execute one step of its five-step answer.
#:
#: `SkillManifest.requires` names the subset without which a skill is *misleading* rather than
#: merely narrower, and `ToolScopedSkills` hides the skill when one of them is absent. Measured:
#: `skills-listing` 3,785 -> 3,542, so **243 tokens come back on every model call** in every
#: deployment that does not enable those bundles, and nothing changes for one that does. Total on
#: this commit: **72,398**.
#:
#: The ceiling drops by 250 rather than by the whole branch's 2,500, and the difference is worth
#: being plain about: the other raises bought capability every deployment can use, this one bought
#: capability most of them cannot, and only that part is refundable.
CEILINGS: dict[str, int] = {"__default__": 72_850}

#: How much of the floor one tool may be. A schema above this is not expensive, it is *badly
#: shaped* — the fix is pagination, a narrower argument, or splitting a tool that does two things.
MAX_SINGLE_TOOL_TOKENS = 900

#: The tools already over it, with what they cost when they were measured.
#:
#: Recorded rather than hidden by a bigger bound, so the *next* one fails this test. Every entry is
#: real debt and none is a mystery: each takes a **domain document** as its argument, which
#: `convert_to_openai_tool` inlines model by model. `start_optimization_campaign` carries a BoFire
#: campaign declaration — objectives, constraints and parameter domains; `record_knowledge_note`
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
#:
#: **Four names arrived on 2026-09-05 and, again, nothing was added.** The fixture began binding
#: the connector surface a turn actually binds, and `bo`'s four `OptimizationProblem`-taking
#: endpoint tools turned out to be the **widest schemas in the entire prefix** — wider than every
#: in-process tool, and unseen for as long as the graph was compiled with no connector. They are
#: recorded here on the same terms as the six that arrived on 2026-08-29: this list is what makes
#: debt visible, and refusing to record debt that was already being paid would only mean not
#: measuring it. **The `MAX_SINGLE_TOOL_TOKENS` message's warning still stands for a tool that is
#: new**; these are eleven weeks old and were merely invisible.
#:
#: **All five `bo` figures then came down in the same commit that first recorded them**, which is
#: this file's own rule running the way it is supposed to. Pydantic renders a class docstring as
#: its JSON-schema `description` and `convert_to_openai_tool` inlines it per *use*, so the design
#: rationale on `CategoricalParameter`, `LinearConstraint`, `ExcludeConstraint`, `Observation`,
#: `OptimizationProblem` and `CampaignSpec` — ADR ids, BoFire internals, why the constraint union
#: has two members rather than five — travelled to the model inside all four `bo` endpoint tools
#: *and* inside `start_optimization_campaign`. **Nothing a caller can express changed**: every
#: field, every default and every validator is untouched, and `tests/test_bo_tools.py` asserts both
#: halves — that the published property set still covers `model_fields` for every model in the
#: closure, and that the *load-bearing* guidance is still in the served `inputSchema`. The
#: rationale is in `#` comments beside the fields, which is where
#: `D-2026-08-28`'s fourth cause already put fifteen of them. `start_optimization_campaign` fell
#: furthest in proportion because `CampaignSpec` carried its own D-157 paragraph *and* the whole
#: `OptimizationProblem` closure. All five stay on this list — a 900-token bound is not reachable
#: by a tool that takes a BoFire decision space, for the same reason `ProtocolBody` puts the
#: protocol writers here.
#:
#: **The `$defs` escape is still closed and is not what did this**, for the reason measured above:
#: one tool references `OptimizationProblem` once, so there is no duplication inside a schema for a
#: `$ref` to buy back. The four copies are four *tools*, and the OpenAI tools array has no
#: cross-tool sharing at any price. What multiplies is the model, so what pays back five times is
#: narrowing the model.
KNOWN_OVERSIZED: dict[str, int] = {
    # **The one entry here whose cost is not ours to narrow, recorded on 2026-09-13 when
    # `harness_enabled` became the default and bound it on every profile.** The rule above says
    # narrow the arguments or paginate the result, and neither is available: decomposed on the
    # bound object with this file's own counter, the 1,372 below is **973 of upstream's own tool
    # description**, 152 of `plan_scope._SCOPE_GUIDANCE`, and 252 of parameters and envelope. So
    # 71% of it is somebody else's prose, arriving through a middleware
    # `_apply_excluded_middleware` refuses to let a profile strip, and the first-party half is 404.
    #
    # **Those three sum to 1,377 against a whole of 1,372, and the +5 is the counter rather than
    # the arithmetic.** `_count` wraps its argument in a `HumanMessage`, and
    # `count_tokens_approximately` charges 4 tokens of per-message envelope — measured on the empty
    # string — so three fragments counted separately pay it three times where the whole pays once.
    # Named because the paragraph below closes on "a decomposition whose parts do not sum to its
    # whole is arithmetic nobody checked", and leaving a 5-token residual under that sentence is
    # the same defect at a smaller scale: the parts are each exact, and what does not sum is the
    # measurement, not the schema.
    #
    # **That split shipped wrong and the error is worth naming.** It read "1,115 of upstream's own
    # tool description, 206 of parameters and 147 of `_SCOPE_GUIDANCE`" against a whole it called
    # 1,367 — three mistakes in one sentence. 1,115 is the *scoped* description,
    # `self.tool_description` after `__init__` has appended the guidance to upstream's, so the
    # guidance was counted twice and the parts summed to 1,468 against a whole of 1,367. And 1,367
    # was chars/4 where the dict value beside it is this file's counter, which the `_count`
    # docstring argues at length must be the basis. A decomposition whose parts do not sum to its
    # whole is arithmetic nobody checked.
    #
    # **Forking that description to trim it was considered and rejected, and the argument is
    # already in the tree**: `_SCOPE_GUIDANCE` is appended to upstream's text rather than replacing
    # it because "everything upstream says about when to plan and how to keep the list current is
    # as true here as there, and a fork of that text is a paragraph that goes stale on the next
    # bump with nothing to notice". Trimming is the same fork with a smaller diff.
    #
    # This is therefore the case this dict's warning did not anticipate — debt taken on knowingly,
    # by adopting a required upstream middleware, rather than first-party bloat being hidden. The
    # lever that *is* available is the profile allow-list in § 5: `write_todos` is bound on every
    # profile that runs the harness, so a narrowed surface pays this back on each of them.
    "write_todos": 1_372,
    "suggest_next_experiment": 2_951,
    # **2,309 -> 2,673 on 2026-09-21, and the +364 bought a design family rather than drifting.**
    # `D-2026-09-21-a-design-is-a-criterion-not-a-second-tool` folded BoFire's `DoEStrategy` into
    # this tool as a `criterion` argument instead of shipping a second one. The alternative was
    # measured: a standalone `generate_optimal_design` cost **1,435**, of which **1,367 was a second
    # copy of the `OptimizationProblem` schema this entry is already paying for** — so the fold is
    # 364 against 1,435, and it needed no ceiling raise where the standalone needed 1,147.
    #
    # It stays on this list rather than joining it, which is the distinction the dict's own warning
    # draws: growing debt already taken on, re-recorded in the commit that moved it, is the
    # mechanism working. Narrowing is still unavailable for the reason the two entries above share —
    # the cost is a nested union of parameter and constraint types, not prose, so **no tool taking
    # an `OptimizationProblem` can clear the 900-token cap**. Two docstring trims on the standalone
    # took
    # it 1,733 -> 1,435 and could touch no more.
    "generate_screening_design": 2_673,
    "predict_outcome": 2_201,
    "campaign_progress": 2_087,
    # **2,307 on `main`, 1,532 here, and the difference is this branch rather than drift.** That
    # figure was re-measured on `main` against the un-narrowed `OptimizationProblem`; this branch
    # narrowed it, so the merge inherits the smaller schema and the ratchet measures the smaller
    # cost. Re-recorded in the commit that merged the two, which is what this dict asks for.
    "start_optimization_campaign": 1_532,
    # 1,126 → 1,227 on 2026-09-05, and the cause is prose rather than schema: the tool was renamed
    # from `propose_knowledge_note` and its docstring rewritten, because the old one told the model
    # the note went to a human for review and that stopped being true
    # (`D-2026-09-05-the-gate-is-deleted-not-dormant`). The replacement says what a reader now has
    # instead — provenance, citations, correction — and that is 101 tokens the model is sent on
    # every call. Recorded rather than trimmed: the sentence it buys is the one that stops the model
    # asserting an unreviewed note as established fact.
    #
    # 1,227 → 1,238 in the fix-forward that followed, same cause: the `Returns:` line still said
    # "the submitted PR reference". Re-recorded in the commit that moved it, which is what this
    # dict's own test asks for and the reason it is +11 rather than a mystery inside the tolerance.
    "record_knowledge_note": 1_238,
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


def _count(text: str | BaseMessage) -> int:
    """Tokens, by the same counter `agent/compaction.py` budgets the thread with.

    A message counts as itself rather than as its text: `count_tokens_approximately` charges a
    per-message overhead, and the system message this file now measures is counted by production
    (`context_budget.MeasureRequestPrefix`) exactly this way.
    """
    from langchain_core.messages.utils import count_tokens_approximately

    message = text if isinstance(text, BaseMessage) else HumanMessage(text)
    return int(count_tokens_approximately([message]))


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
#: cannot be measured *in this interpreter* — only across a process boundary, from a checkout of
#: the repository that builds them, which is what the allowance test below does.
#:
#: **This said "cannot be measured here at any price" and the paragraph forty lines down described
#: the test that measures them.** Two sentences in one comment, the first denying what the second
#: reports; the first was true when it was written and survived the commit that made it false,
#: which is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` on a claim rather than a
#: number. What is genuinely out of reach is importing `chemclaw_mcp_chem` from *this* workspace,
#: and that is the sentence the module docstring now carries.
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
#: **A fifth reason this set did not grow, and the first one that is not "we declare no bundle".**
#: `D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions` declares five of the
#: fleet's bundles here — `thermalsafety`, `kinetics`, `unitops`, `props`, `suitability` — so the
#: four entries below that say "this tree declares no such bundle" have stopped being true, and
#: the set they were protecting still must not move.
#:
#: What keeps it right is that the allowance was never really about *declaring*. It prices what a
#: turn is **sent**, and a turn is sent what `enabled()` binds. All five declare
#: `default_enabled: false`, so an empty `connectors_enabled` — a fresh checkout, `make test`, CI,
#: and every chart release that has not asked — binds none of them and pays for none of them.
#: Their 21,913 tokens are charged to whoever names them in `CHEMCLAW_CONNECTORS_ENABLED`, which
#: is a line in a values file rather than a property of this repository.
#:
#: So the membership rule is now two predicates rather than one: declared in **both** trees *and*
#: bound by silence. `test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_
#: allowance` asserts exactly that pair, and it is what would red if somebody flipped one of those
#: five to `default_enabled: true` without raising the allowance in the same commit — which is the
#: whole point of the flag being in the manifest rather than in a deployment's head.
SERVED_ELSEWHERE = frozenset({"chem", "rxnpredict", "safety"})

#: What to allow for `SERVED_ELSEWHERE`'s schemas when a *bound* on the whole prefix is needed.
#:
#: **A bound rather than a measurement, for the same reason `CEILINGS` is one**, and the two are
#: added wherever a caller needs the figure a deployment actually pays: `PREFIX_BOUND` below.
#:
#: Measured 2026-09-05 against the `Chemclaw3-mcp` checkout beside this one — every declared tool,
#: through this repository's own `convert_to_openai_tool` path — the three bundles cost **9,864
#: tokens over 21 tools** (`chem` 5,577 / 12, `rxnpredict` 2,655 / 6, `safety` 1,632 / 3). 11,000
#: carries **11.5%** over that, which is the headroom a surface this file cannot ratchet needs:
#: nothing here fails when one of those servers adds a tool, so the allowance has to absorb one.
#:
#: **That was 9,538 and "~15%" when it was written, and it was already wrong the same day.** The
#: sibling repository merged a `Raises:` paragraph and eleven lines into `chem` between the
#: measurement and the commit that recorded it — 326 tokens, a third of the headroom, moved by a
#: repository this one does not build and cannot watch. A figure like that does not go stale
#: eventually; it goes stale on somebody else's merge schedule, which is why the paragraph below
#: no longer says the allowance cannot be asserted.
#:
#: **It is asserted, and the honest form of the assertion is one that can skip.**
#: `test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound` runs the
#: sibling's own servers in the sibling's own interpreter, converts their `tools/list` exactly as
#: `_served_tools` converts a local one, and fails when the total passes this allowance. Where
#: there is no sibling checkout it **skips with the reason in the message**, because a check that
#: quietly shrinks is worse than one that says what it did not look at — the argument
#: `cli/validate_connectors.py::unverified_tool_surfaces` makes about the identical blind spot one
#: layer over. A skip is not a pass: CI without the sibling learns nothing here, and
#: `tests/conftest.py::_report_sibling_skips` is what makes that visible — and it did not exist
#: when this sentence was first written, which made this the same kind of claim about a control
#: that `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` is about. It was
#: made true rather than corrected: `-ra` prints the skip in a list nobody reads, and the epilogue
#: is the part of a run that says what the run is not evidence about.
SERVED_ELSEWHERE_ALLOWANCE = 11_000

#: What the fleet's **whole** published `manifests/` directory costs, as a second and looser bound.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` above covers only the bundles *this* repository also declares,
#: which is the right basis for `PREFIX_BOUND`: the shipped Helm chart's `connectors:` block has an
#: entry for every bundle in this tree and none for anything else, so a chart deployment binds no
#: fleet-only bundle at all, and charging it for one would tighten both compaction defaults for a
#: surface it never sends.
#:
#: **But one configuration in this tree does mount the whole directory.**
#: `infra/live/e2e-full-stack/up.sh` puts `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR` —
#: *after* this tree's own directory, and discovery is first-directory-wins, so every name this
#: tree declares resolves to this tree's manifest and its `default_enabled: false` holds there too.
#: What that lane binds from the fleet is therefore only the bundles this tree declares nothing
#: for — `pyexec` — and its prefix is over `PREFIX_BOUND` by roughly that bundle's schema, which
#: leaves the clear trigger's thread allowance short of the
#: 30,000 `core/config/agent.py` derives it to be. That is stated rather than absorbed: raising
#: `SERVED_ELSEWHERE_ALLOWANCE` to cover it would move both defaults for every deployment on
#: account of a lane that talks to `chemclaw.cli.mock_llm`.
#:
#: What this bounds instead is the *growth* of the half nothing else here watches. A bundle the
#: fleet adds to `manifests/` lands in this total and nowhere else in this repository, which is the
#: answer to "what goes red when the fleet adds a bundle?" — previously nothing, unless the fleet
#: also added its manifest here. Measured 2026-09-07 against the checkout beside this one: 13,942
#: tokens over 28 tools (`chem` 5,577 / 12, `props` 2,936 / 6, `pyexec` 1,142 / 1, `rxnpredict`
#: 2,655 / 6, `safety` 1,632 / 3). The headroom is the same 11.5% and for the same reason.
#:
#: **It did exactly that on 2026-09-15, which is the first time this bound has been the thing that
#: noticed.** The fleet gained a `thermalsafety` server — runaway arithmetic from calorimetry, seven
#: tools — and the directory went to **17,835 over 35 tools** (`chem` 5,577 / 12, `props` 2,936 / 6,
#: `pyexec` 1,142 / 1, `rxnpredict` 2,784 / 6, `safety` 1,632 / 3, `thermalsafety` 3,764 / 7),
#: 2,335 over the 15,500 that stood. Raised to 19,800, which is the same 11% headroom over the new
#: measurement.
#:
#: The cost is stated rather than absorbed, because raising a bound quietly is how one stops being
#: one: a deployment that mounts this directory and enables the bundle pays 3,764 more tokens on
#: every model call. (`infra/live/e2e-full-stack/up.sh` mounts it and enables none: this tree's
#: opt-in copy wins the name there, as the note above says.) At 538 tokens a tool `thermalsafety` is
#: in the band its siblings occupy (`safety` 544, `props` 489, `chem` 465) rather than an outlier,
#: and the length is the fleet's own rule about a tool docstring stating what the tool is *not* —
#: which for a server that answers "what happens if the cooling fails" is the paragraph that keeps a
#: Semenov estimate from being quoted as an SADT. Trimming to fit would have cost ~330 tokens a
#: tool, which is that paragraph.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` deliberately did **not** move with it: `thermalsafety` is not a
#: bundle this tree declares, so no chart deployment binds it, and charging `PREFIX_BOUND` for it
#: would tighten both compaction defaults everywhere on account of a lane that talks to
#: `chemclaw.cli.mock_llm`. That is the same argument this entry opens with, arriving for real.
#:
#: **It happened again the same day, which is what a bound that works looks like.** The fleet
#: gained a `suitability` server — USP <621> chromatographic system suitability, seven tools — and
#: the directory went to **22,306 over 42 tools** (`chem` 5,577 / 12, `props` 2,936 / 6, `pyexec`
#: 1,142 / 1, `rxnpredict` 2,784 / 6, `safety` 1,632 / 3, `suitability` 4,471 / 7, `thermalsafety`
#: 3,764 / 7), 2,506 over the 19,800 that stood. Raised to 24,800, the same ~11% headroom over the
#: new measurement.
#:
#: The cost again stated rather than absorbed: a deployment that mounts that directory and enables
#: the bundle pays 4,471 more tokens on every model call. At 639 tokens a tool `suitability` is
#: **above** the band its siblings occupy (`thermalsafety` 538, `safety` 544, `props` 489, `chem`
#: 465) — the first entry here where that is true — and the reason is one tool rather than a verbose
#: server. Six of its seven cost 458-614, inside the band; `system_suitability_report` costs 1,171,
#: being the only composite in the fleet that takes a nested model (a peak table) plus nine named
#: criteria. That server's README argues why it is kept at that price: the alternative is the model
#: decomposing a pasted table across the single-peak tools, which means pairing widths with
#: retention times and resolving *adjacent* pairs, and a resolution computed between the wrong two
#: peaks looks exactly like a correct one.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` again did not move, for the reason given directly above: this tree
#: declares no `suitability` bundle, so no chart deployment binds it.
#:
#: **And a third time the same day, which is what makes this a bound rather than a number.** The
#: fleet gained `kinetics` — isothermal rate and ideal-reactor arithmetic, six tools — and the
#: directory went to **25,695 over 48 tools** (`chem` 5,577 / 12, `kinetics` 3,389 / 6, `props`
#: 2,936 / 6, `pyexec` 1,142 / 1, `rxnpredict` 2,784 / 6, `safety` 1,632 / 3, `suitability`
#: 4,471 / 7, `thermalsafety` 3,764 / 7), 895 over the 24,800 that stood. Raised to 28,500, the
#: same ~11% headroom.
#:
#: Three consecutive fleet additions, each caught here rather than noticed later, is the argument
#: for the bound existing at all: nothing in this tree builds those servers or watches their
#: merges, so the only thing standing between a sibling's pull request and a silently larger
#: prefix on every model call is this assertion failing.
#:
#: The cost again stated rather than absorbed: 3,389 more tokens on every model call for a
#: deployment that mounts that directory and enables the bundle. At 565 tokens a tool `kinetics` is
#: **inside** the band its siblings occupy (`thermalsafety` 538, `safety` 544, `props` 489, `chem`
#: 465) — unlike `suitability` above, and for a reason worth keeping: its six tools are
#: single-purpose, where `suitability`'s seventh is a composite taking a nested model plus nine
#: named criteria.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` did not move for the third time, and for the third time because
#: this tree declares no such bundle.
#:
#: **A fourth, and this one is the most expensive server in the fleet per tool.** `unitops` —
#: scale-up and unit-operation sizing, seven tools — took the directory to **33,048 over 55 tools**
#: (`chem` 5,577 / 12, `kinetics` 3,389 / 6, `props` 2,936 / 6, `pyexec` 1,142 / 1,
#: `rxnpredict` 2,784 / 6, `safety` 1,632 / 3, `suitability` 4,471 / 7, `thermalsafety` 3,764 / 7,
#: `unitops` 7,353 / 7), 4,548 over the 28,500 that stood. Raised to 36,700, the same ~11% headroom.
#:
#: The cost stated rather than absorbed: 7,353 more tokens on every model call for a deployment that
#: mounts that directory and enables the bundle. At **1,050 tokens a tool** it is nearly double
#: `suitability`'s 639, which was itself the first entry here to sit above the band — and the shape
#: of the two is different in a way that decides whether to trim. `suitability`'s total is one
#: composite (`system_suitability_report`, 1,171) over six tools at 458-614. `unitops` is
#: **uniform**: measured per tool, 958 to 1,148 across all seven, with no outlier to remove.
#:
#: **So the price is the fleet's own two rules meeting a server whose subject is measurements.**
#: Every tool there takes several physical quantities, each required with no default — that is
#: deliberate, because a correlation handed a defaulted `U` or `alpha` returns a plausible number
#: nobody measured — and this fleet requires every argument to state its units and every docstring
#: to state what the tool is *not*. Seven tools x several required quantities x a sentence each is
#: 1,050 tokens, and none of the three factors is the one to drop. Trimming here would buy ~2,500
#: tokens by deleting the units from a scale-up correlation's arguments, which is the trade this
#: entry exists to make visible rather than take quietly.
#:
#: **And the six library adoptions that landed next door in the same week moved this by zero.**
#: Subtract `unitops` and the fleet is 25,695 — the exact figure the paragraph above recorded
#: before either merge. A review that replaced a periodic table, an optimizer and a set of physical
#: constants across seven servers changed no tool's schema, which is what a dependency swap behind
#: a stable surface is supposed to look like and is not something anybody could have asserted
#: without measuring it here. The whole breach is `unitops`.
#:
#: `SERVED_ELSEWHERE_ALLOWANCE` did not move for the fourth time, and for the fourth time because
#: this tree declares no such bundle.
FLEET_PUBLISHED_ALLOWANCE = 36_700

#: The whole static prefix a shipped `default` turn may cost, as a bound: this file's ceiling plus
#: the allowance for what it cannot see.
#:
#: This is the number `core/config/agent.py` derives both compaction thresholds from — the trigger
#: is `PREFIX_BOUND + 30,000` and the budget `PREFIX_BOUND + 43,000` — and
#: `tests/test_compaction.py` asserts that relation rather than restating either figure, as
#: `CLEAR_TRIGGER_THREAD_ALLOWANCE` and `BUDGET_THREAD_ALLOWANCE`; read those, not this sentence,
#: for the live allowances. This one said `+ 57,000` against a shipped `+ 43,000` for as long as
#: the budget's own derivation ran the other way: the *window* is the input now, and the thread
#: allowance is what is left over once the prefix is paid, so holding 57,000 fixed is exactly the
#: arithmetic `core/config/agent.py` reverted for permitting a request a 128k model rejects. Both
#: assertions are inequalities, so the shipped defaults passed under either sentence — which is why
#: this one drifted silently and why the constants, not the prose, are the claim. It is here
#: rather than in the config because the ceiling is here: two numbers that must move together
#: belong in one place, and the previous arrangement (a config comment quoting a ceiling by value)
#: is exactly how the connector-less ceiling propagated into two settings that were floored for
#: eleven weeks.
PREFIX_BOUND = CEILINGS["__default__"] + SERVED_ELSEWHERE_ALLOWANCE


@cache
def _served_tools(connector: str) -> tuple[Any, ...]:
    """Every tool one bundle's own MCP server advertises, as the `BaseTool`s a turn would bind.

    **Not a fixture and not a hand-written schema: the bundle's real `FastMCP` server, over a real
    MCP session, through `load_mcp_tools` — the same function
    `connectors/transport.py::HeldConnectorSession._hold` calls.** An in-memory transport is the
    only thing substituted, so what is measured is the `tools/list` payload a deployment's pod
    would answer with. A schema invented here would be a second declaration of somebody else's
    surface, which is the defect this whole file is about.

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


def _bound_tools(graph: Any) -> list[Any]:
    """The tools a compiled graph actually binds — every one, as the object it binds.

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
    being taught about it.

    **That sentence was true of the method and false of the call, and `connectors=` is the fix.**
    For as long as the paragraph above existed, the graph handed to this function was compiled
    without the argument `build_langgraph_agent` takes at line 148, so the ratchet measured a graph
    with **no connector bound at all**. A connector could not "land here the moment it is bound"
    because nothing here ever bound one, and the gap was the *whole* subject of the deferred-schema
    decision (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) sitting outside the only
    ratchet that could have priced it. `_observed_prefix` now passes `_connector_tools(profile)`,
    which derives that surface from this repository's own manifests rather than transcribing it,
    and `SERVED_ELSEWHERE` names what it still cannot reach.

    The backlog row that asked for this proposed spying on `bind_tools`
    instead, and `_observed_prefix` now does that too — for the system message, which no node holds.
    **The node is still what this ratchet charges, and the difference is measured rather than
    assumed**: on 2026-09-05 the two lists held the same 61 names and differed by **20 tokens on
    `grep` alone**, whose description `FilesystemMiddleware` trims before binding because this
    deployment withholds `execute`. The node's copy is the larger one, so charging it over-counts by
    20 — the safe direction for a ratchet, and the direction
    `test_the_ratchet_charges_at_least_what_the_model_is_sent` pins so that a flip is red rather
    than quiet. Reading `_tools_by_name` also keeps the three upstream shapes below with a subject:
    `tests/test_upstream_surface.py` names this function in all three failure messages.

    **Three upstream shapes are read below, and all three are pinned in
    `tests/test_upstream_surface.py`**: the node key `"tools"`, `PregelNode.bound`, and the private
    `ToolNode._tools_by_name`. Only the last was pinned when this function was written, which left
    two thirds of the read able to break on a bump with nothing in the upstream-surface file going
    red — loudly rather than silently, but in the wrong file.
    """
    return list(graph.nodes["tools"].bound._tools_by_name.values())


#: What the last `_CapturingModel` was sent, and what was bound to it. Module level rather than
#: instance state because a `BaseChatModel` is a pydantic model, so an annotated class attribute
#: would become a *field* with a mutable default rather than a place to keep a measurement. Same
#: shape, and for the same reason, as `tests/test_compaction.py`'s capturing model.
_RECEIVED: list[Any] = []
_BOUND: list[Any] = []


class _CapturingModel(GenericFakeChatModel):
    """A fake model that keeps what it was actually sent, so the prompt comes off the wire.

    `GenericFakeChatModel` alone cannot be used for this: `BaseChatModel.bind_tools` raises
    `NotImplementedError`, which a turn hits *after* the request is assembled — enough for a
    middleware to have measured the prefix, not enough for the model to receive it.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record the surface and stay unbound — this model has no tool-calling path."""
        _BOUND[:] = list(tools)
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        """Record the request, then answer as the fake model would."""
        _RECEIVED[:] = list(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


@contextmanager
def _as_a_deployment_runs() -> Iterator[None]:
    """Build under `session_store="postgres"`, which is what every real deployment sets.

    **Without this the ratchet measures a system no deployment runs**, and it is the same defect
    `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` names, one predicate
    over. `tests/conftest.py` sets no `session_store`, so the suite runs at the code default of
    `"memory"` — and `local_skills.personal_skills_available()` reads
    `agent_memory_enabled and session_store == "postgres"`, so `build_langgraph_agent` strips
    `propose_skill` from every graph this file compiles while the shipped chart
    (`deploy/helm/chemclaw/values.yaml`) pins `CHEMCLAW_SESSION_STORE: "postgres"` and pays for it.
    Measured: the tool's schema is ~462 tokens of prefix on every request, charged to nothing here.

    It is a pure predicate — no database is opened by setting it, because the store only ever
    arrives as an argument — so this costs nothing and buys the one thing this file exists for.
    """
    original = settings.session_store
    settings.session_store = "postgres"
    try:
        yield
    finally:
        settings.session_store = original


def _observed_prefix(profile: Any) -> tuple[SystemMessage, list[Any], list[Any]]:
    """One real model call: the system message as sent, the tools as bound, the node's own list.

    **Driven rather than derived, which is this file's whole rule applied to its other half.** The
    prompt a turn pays for is not `instructions_for(profile)` plus a skills listing: the deepagents
    middlewares write into the same system message, and on 2026-09-05 their sections were 458 of the
    7,484 tokens — all of it upstream's `SKILLS_SYSTEM_PROMPT`, the wrapper around the listing this
    file already measured. That is invisible to any assertion built from this repository's own two
    pieces, which is how the real prefix passed the ceiling by 21 tokens with this file green.

    The turn is a real one: a compiled graph, invoked, answering with an empty message so the loop
    ends after one model call. Nothing is stubbed between the profile and the wire, which is the
    point — a capture taken anywhere earlier is a claim about what the request *would* become.

    Returns:
        The `SystemMessage` the model received, the tools bound to it, and the tools its `ToolNode`
        holds. The last two are the same surface seen from two places, and
        `test_the_ratchet_charges_at_least_what_the_model_is_sent` is what keeps them honest.
    """
    with _as_a_deployment_runs():
        graph = build_langgraph_agent(
            model=_CapturingModel(messages=iter([AIMessage(content="")])),
            profile=profile,
            audit_sink=NullAuditSink(),
            connectors=_connector_tools(profile),
        )
    bound = _bound_tools(graph)
    _RECEIVED.clear()
    _BOUND.clear()
    graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
    system = [message for message in _RECEIVED if isinstance(message, SystemMessage)]
    assert system, (
        "the model was called with no system message, so there is no observed prompt to charge — "
        "check that `build_langgraph_agent` still passes its instructions as `system_message`"
    )
    return system[0], list(_BOUND), bound


def _skills_listing(profile: Any, tools: list[Any], available: Collection[str]) -> str:
    """The skills block exactly as `SkillsMiddleware` publishes it into the system prompt.

    Built through the real middleware rather than re-derived from the `SKILL.md` frontmatter,
    because a second implementation of upstream's formatting is a second thing to keep in step —
    and the number this file gates on has to be the number the model is actually sent.
    `before_agent` on an empty state is upstream's own load path: what a first turn runs.

    `available` is the surface the graph binds, passed for the same reason
    `build_langgraph_agent` passes it: the capability predicate that decides which skills are listed
    reads it, and a listing derived from the manifests instead would split the observed total by a
    number production does not produce.
    """
    labelled = _labelled(_skill_dirs())
    backend = skills_backend(profile, tools, labelled=labelled, available=available)
    middleware = _skills_middleware(backend, labelled, profile)
    loaded = middleware.before_agent({}, None, None) or {}
    return str(middleware._format_skills_list(loaded.get("skills_metadata", [])))


def _maximal_instructions(profile: Any) -> int:
    """The most expensive instruction text any deployment of this profile can be sent.

    Two axes make the prompt vary, and a bound has to take the worst of both. The blocks a graph
    binds nothing for are dropped, so `available=None` — every block — is one end of the first
    axis. The second is the audit trail: the two traceability blocks are alternatives rather than
    one block and its absence, and the log-only one is the longer of the two, so a fixture that
    only ever measured the durable prompt would under-charge every deployment that has not set
    `session_store="postgres"` — which is `.env.example`'s default.
    """
    return max(
        _count(instructions_for(profile, durable_trail=durable)) for durable in (True, False)
    )


def _floor(profile_name: str) -> tuple[int, dict[str, int]]:
    """The static prefix for one profile: its total, and the per-part breakdown behind it.

    **The total is observed and the breakdown is derived, and the distinction is load-bearing.**
    What is charged is the `SystemMessage` the model was handed plus every schema the graph's
    `ToolNode` holds — two measurements, neither re-derived from what this repository believes it
    writes. The three prompt lines below *split* that observed number: two are this repository's own
    contributions, measured the way `build_langgraph_agent` builds them, and the third is the
    remainder — every deepagents middleware's prompt section, named rather than uncounted.

    The skills listing is derived from the *capability* tools and narrowed by the *bound* names,
    which is exactly the pair `build_langgraph_agent` passes: it hands `skills_backend` the raw
    callables and, since 2026-09-10, the surface the graph binds — because the capability predicate
    that decides which skills are listed had been reading the manifests, which do not move when a
    server is unreachable. Deriving either half differently here would split the observed total by
    a number production does not produce. A negative remainder would mean these halves are no
    longer what production puts in the prompt — the split has gone wrong, not the total, which is
    still what the model was sent.

    **The instructions are the one part charged at more than this fixture observes, and that is
    deliberate — the alternative is the 2026-09-05 defect again.** Since the prompt became
    `PromptBlock`s the model is sent only the blocks whose tools this graph binds
    (`chemclaw_agent.PromptBlock`), and this fixture binds no `SERVED_ELSEWHERE` bundle: it cannot
    bind `screen_hazards` or `resolve_compound`, so the prompt it observes is *missing* two blocks
    that a deployment with the fleet is sent. Charging what was observed would make the ratchet
    measure a smaller system for the fourth time. `_maximal_instructions` is the bound instead —
    the most expensive prompt any deployment of this profile can be sent, over both trail variants
    — and the difference is a named line rather than a silent absorption into the remainder, which
    is what it would have been if `instructions` alone were left maximal.
    """
    profile = get_profile(profile_name)
    system, _sent, bound = _observed_prefix(profile)
    observed = _count(instructions_for(profile, {_tool_name(tool) for tool in bound}))
    maximal = _maximal_instructions(profile)
    listing = _count(
        _skills_listing(profile, _capability_tools(profile), {_tool_name(tool) for tool in bound})
    )
    parts = {
        "instructions": observed,
        "instructions:blocks-only-a-served-fleet-binds": maximal - observed,
        "skills-listing": listing,
        "prompt:middleware-sections": _count(system) - observed - listing,
    }
    for tool in bound:
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


def test_the_ratchet_charges_at_least_what_the_model_is_sent() -> None:
    """The basis may over-count what a turn costs; it may never under-count it.

    A ceiling is only a bound on spend while the number under it is at least the bill. This file has
    been on the wrong side of that twice — 8,126 tokens of tool schema in 2026-08-29, 458 tokens of
    middleware prompt until 2026-09-05 — and both times every assertion here was green, because the
    basis and the assertion were derived from the same belief.

    So the two surfaces are compared where they can disagree. `_bound_tools` reads the `ToolNode`,
    which is what a graph *runs*; `bind_tools` receives what the model is *told about*, and they are
    not the same objects: measured 2026-09-05, the same 61 names differ by 20 tokens on `grep`,
    trimmed by `FilesystemMiddleware` on its way to the model. The node's copy is larger, so the
    ratchet over-charges by 20 tokens — harmless, and asserted rather than assumed, because the day
    the sign flips this file starts under-counting a bill somebody pays.
    """
    system, sent, bound = _observed_prefix(get_profile("default"))
    charged = {_tool_name(tool): _count(_tool_schema(tool)) for tool in bound}
    on_the_wire = {_tool_name(tool): _count(_tool_schema(tool)) for tool in sent}

    uncharged = sorted(set(on_the_wire) - set(charged))
    assert not uncharged, (
        f"{uncharged} are bound to the model and are not in the surface this file charges, so the "
        "ratchet does not bound what a turn costs. `_bound_tools` reads the graph's ToolNode; "
        "whatever now puts a tool on the wire without putting it there has to be counted too."
    )
    prompt = _count(system)
    total_charged = prompt + sum(charged.values())
    total_sent = prompt + sum(on_the_wire.values())
    differing = {
        name: (charged[name], size) for name, size in on_the_wire.items() if charged[name] != size
    }
    assert total_charged >= total_sent, (
        f"the ratchet charges {total_charged} tokens against {total_sent} the model is actually "
        f"sent, so it under-counts by {total_sent - total_charged}. The tools the two bases "
        f"disagree about, as (charged, sent): {differing}. Charge the surface `bind_tools` "
        "receives instead, and move the upstream-surface pins that name `_bound_tools` with it."
    )


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


def _nested_descriptions(node: Any, path: str = "") -> list[tuple[int, str, str]]:
    """Every `description` below a tool's own, as (tokens, path, text).

    A tool's top-level description is its docstring and is deliberately excluded: that one is the
    prompt, argued for tool by tool. Everything under it is a *field* or a *model* description, and
    a model's is published once per use — which is the multiplier the test below exists to bound.
    """
    found: list[tuple[int, str, str]] = []
    if isinstance(node, dict):
        text = node.get("description")
        if isinstance(text, str):
            found.append((_count(text), path, text))
        for key, value in node.items():
            found.extend(_nested_descriptions(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_nested_descriptions(value, f"{path}[{index}]"))
    return found


#: How long one *nested* schema description may be, in tokens.
#:
#: **This is the ratchet that would have caught the 2026-09-05 narrowing eleven weeks earlier**, and
#: it is a different bound from `MAX_SINGLE_TOOL_TOKENS` rather than a finer one. That bound asks
#: whether a tool is badly shaped; this one asks whether a *model* is carrying developer prose,
#: which is invisible per tool because it is spread across every tool that references the model:
#: `LinearConstraint`'s 288-token rationale never made any one schema look wrong and cost 1,440
#: tokens a turn across five.
#:
#: **250 rather than the measured maximum.** The widest nested description in the `default` prefix
#: on 2026-09-05 is `record_knowledge_note`'s `relations` at **192** — a keyed list of relation
#: kinds a caller genuinely has to be given — so the bound has ~30% headroom for a field
#: explanation that gets clearer, and none for a design note. The three entries this test was
#: written after measured 288, 264 and 220.
#:
#: The remedy is never "shorten the sentence until it fits": it is the one
#: `D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not` established and this narrowing
#: repeated — the model-facing half stays in the docstring, the rationale moves into a `#` comment
#: beside the fields, where a reader of the module finds it and the model is not charged for it.
MAX_NESTED_DESCRIPTION_TOKENS = 250


def test_no_nested_schema_description_carries_a_design_note() -> None:
    """A model docstring is a prompt once per *use*, so a long one is paid for several times over.

    Pydantic publishes a class docstring as the JSON-schema `description`, and
    `convert_to_openai_tool` inlines rather than `$ref`s — so prose written for whoever opens the
    module is sent to the model inside every tool that names the model. The `bo` decision space was
    the worst case and nothing in this file could see it: five schemas, each individually explicable
    at 2,300-3,600 tokens, sharing ~680 tokens of ADR ids and BoFire internals apiece.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _, _, bound = _observed_prefix(get_profile("default"))
    over: dict[str, tuple[int, str]] = {}
    for tool in bound:
        function = convert_to_openai_tool(tool)["function"]
        for tokens, path, text in _nested_descriptions(function.get("parameters", {})):
            if tokens > MAX_NESTED_DESCRIPTION_TOKENS:
                over[f"{function['name']}{path}"] = (tokens, text[:80])
    assert bound and not over, (
        f"these schema descriptions are over {MAX_NESTED_DESCRIPTION_TOKENS} tokens: {over}. "
        "A description below a tool's own is a field or a model explanation, and a model's ships "
        "once per tool that references it — move the design rationale into a `#` comment beside "
        "the fields and leave the sentence a caller needs."
    )


def test_the_floor_measures_the_connector_surface_a_turn_actually_binds() -> None:
    """The ratchet's basis includes the endpoint tools, and this is what would have caught it.

    **This is the assertion whose absence made the whole connector surface invisible.**
    `_bound_tools` read the compiled graph's `ToolNode` — the honest source — and the call that
    compiled that graph omitted the `connectors=` argument production passes, so the ratchet gated
    a turn that does not exist. No test could see it: every figure was self-consistent, and the
    docstring promising a connector would "land here the moment it is bound" was about the read,
    not about the call.

    What is asserted is *derived from the manifests*, not transcribed: every tool the enabled
    bundles this repository serves declare must be in the bound set. A bundle added, a tool added
    to a manifest, or `connectors=` dropped again all fail here, and the last one fails loudly
    instead of shrinking the number in silence.
    """
    _, _, bound_tools = _observed_prefix(get_profile("default"))
    bound = {_tool_name(tool) for tool in bound_tools}
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
        "exact defect `_observed_prefix`'s `connectors=` argument exists to prevent."
    )


def test_the_bundles_this_floor_cannot_measure_are_exactly_the_ones_it_names() -> None:
    """`SERVED_ELSEWHERE` is a claim about which schemas are out of reach, so it is checked.

    A blind spot that drifts is worse than one that is declared: a bundle whose server moved into
    this tree would silently stay excluded from the ceiling, and a *new* bundle served elsewhere
    would silently widen the unmeasured half while the ceiling comment kept quoting a share of the
    prefix. Both directions fail here, which is the two-sidedness `OVERSIZED_TOLERANCE` is written
    for one level down.
    """
    endpoint_bundles = {m.name for m in enabled() if m.endpoint is not None}
    unmeasurable = {name for name in endpoint_bundles if not _served_tools(name)}
    assert unmeasurable == SERVED_ELSEWHERE & endpoint_bundles, (
        f"this file can measure the tool schemas of {sorted(endpoint_bundles - unmeasurable)} and "
        f"not of {sorted(unmeasurable)}, but SERVED_ELSEWHERE names {sorted(SERVED_ELSEWHERE)}. "
        "Update it and the ceiling comment's share-of-the-prefix figure in the same commit."
    )


# --------------------------------------------------------------------------------------------
# The half this ratchet cannot serve, measured rather than quoted.
#
# `SERVED_ELSEWHERE_ALLOWANCE` bounds three servers built in `Chemclaw3-mcp`, and `PREFIX_BOUND`
# — which both compaction defaults are derived from — is that allowance plus this file's ceiling.
# So a bound nobody checks is not a bound on the prefix, it is a bound on the part of the prefix
# this repository happens to author, and the other part moves on somebody else's merge schedule:
# the recorded 9,538 was 9,864 by the end of the day it was measured.
#
# The measurement runs the sibling's servers in the *sibling's* interpreter, because
# `chemclaw_mcp_chem` is not importable from this workspace at any price — the whole point of
# `D-2026-08-09-a-connector-we-do-not-run` is that this repository does not carry their closure.
# What crosses the process boundary is `tools/list` as JSON, which is exactly what a handshake
# delivers; the conversion and the counting happen here, through the same two functions every
# other figure in this file goes through.
# --------------------------------------------------------------------------------------------

#: The program run inside the sibling checkout's interpreter. Written here rather than committed
#: there because it is *this* file's measurement: the sibling owes the fleet a `tools/list`, not a
#: token count in this repository's estimator.
_SIBLING_DUMP = """
import asyncio, importlib, json, sys
from mcp.shared.memory import create_connected_server_and_client_session


async def dump(name):
    server = importlib.import_module("chemclaw_mcp_%s.tools" % name).server
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        return [t.model_dump(mode="json", exclude_none=True) for t in listed.tools]


print(json.dumps({name: asyncio.run(dump(name)) for name in sys.argv[1:]}))
"""


def _sibling_python() -> tuple[Path | None, str]:
    """The sibling checkout's own interpreter, or `None` and the reason there is not one.

    **The search is `infra/live/siblings.sh`'s, asked rather than reimplemented** — see
    `tests/siblings.py` for why a second copy of it was the defect this delegation ends, and
    `test_the_ratchet_finds_the_checkout_the_live_lane_finds` for the assertion that keeps the two
    from parting again. `CHEMCLAW_MCP_REPO` and `CHEMCLAW_MCP_CHECKOUT` both override it, so a CI
    job that clones the sibling somewhere else can still measure whichever name it already sets.
    """
    return sibling_python("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")


def test_the_ratchet_finds_the_checkout_the_live_lane_finds() -> None:
    """This file's sibling search and `infra/live/siblings.sh`'s must resolve the same tree.

    **The one assertion that would have caught the whole of this.** Both landed on 2026-09-06, in
    different pull requests: `infra/live/siblings.sh` unified the live lanes onto four candidate
    paths in two casings under two variables, with a header describing exactly this bug being
    fixed — and `_sibling_python`, merged the same day, searched one path in one casing under one
    variable. On the container this repository's own tooling provisions, with the fleet at
    `../8fqycwdt8v-oss/chemclaw3-mcp`, `make live-up` resolved it and the test below skipped. So
    the allowance, `PREFIX_BOUND`, and both compaction defaults derived from it had never been
    checked by a machine anywhere, and the skip that said so was indistinguishable from the skip
    a machine with no checkout prints.

    A skip here is honest — no checkout is no checkout — but a skip *beside* a live-lane hit is
    the defect, and that is the only case this fails on.
    """
    root, live_reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(f"{SIBLING_SKIP} neither search has one to find: {live_reason}")
    interpreter, ratchet_reason = _sibling_python()
    assert interpreter is not None or ".venv" in ratchet_reason, (
        f"`infra/live/siblings.sh` resolves Chemclaw3-mcp to {root} and this file does not: "
        f"{ratchet_reason}. Two searches for one checkout is what that script exists to have "
        "ended; the consequence here is not a wrong answer but a control that skips on a machine "
        "which could have run it."
    )


def test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_allowance() -> None:
    """`SERVED_ELSEWHERE` is a claim about the *sibling's* tree, so the sibling's tree answers it.

    The neighbouring completeness test iterates `enabled()`, which under `make test` and CI reads
    only this tree's `connectors/` — so a bundle whose manifest lives next door is structurally
    invisible to it, and the honest answer to "what goes red when the fleet adds a bundle?" was
    "nothing, unless the fleet also adds its manifest to *this* repository". This is the half that
    reads the other tree.

    **What it does not do is widen the allowance to cover the fleet's own bundles**, and that is a
    decision rather than an omission (`D-2026-09-07-a-claim-about-another-repository-is-checked-by-
    reading-it`). `pyexec` is declared only in `Chemclaw3-mcp`, and the five process-development
    bundles are declared here but `default_enabled: false`; no chart entry mounts the first and no
    default `enabled()` returns any of them, so charging them to `PREFIX_BOUND` would raise both
    compaction defaults for every deployment on account of bundles those deployments do not
    bind. What they cost is bounded by `FLEET_PUBLISHED_ALLOWANCE` below instead, which prices a
    deployment that mounts the fleet's directory *and enables them*. The e2e lane
    (`infra/live/e2e-full-stack/up.sh`) mounts it after this tree's own, so this tree's opt-in
    copies win there too and it binds none of the five; `pyexec` is the one fleet-only bundle
    that lane binds.

    Needs a checkout and not a built `.venv`: reading the fleet's manifests is a shallow clone's
    worth of work, which is the half of this file that could plausibly run in CI.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(
            f"{SIBLING_SKIP} the fleet's published manifests were NOT read: {reason}. Whether "
            f"SERVED_ELSEWHERE ({', '.join(sorted(SERVED_ELSEWHERE))}) is still what both "
            "repositories declare is unchecked in this run."
        )
    published = set(fleet_published_bundles(root))
    bound_by_silence = {m.name for _, m in discovered().values() if m.default_enabled}
    charged = published & set(bundles_declared_here()) & bound_by_silence
    assert charged == SERVED_ELSEWHERE, (
        f"the fleet publishes {sorted(published)}, this repository declares "
        f"{sorted(set(bundles_declared_here()))} and binds {sorted(bound_by_silence)} by silence; "
        f"the names in all three are {sorted(charged)} where SERVED_ELSEWHERE says "
        f"{sorted(SERVED_ELSEWHERE)}. A name in both trees that an empty `connectors_enabled` "
        "still binds is a bundle this repository declares, does not serve, and pays for on every "
        "model call — so its schemas are charged to SERVED_ELSEWHERE_ALLOWANCE and through it to "
        "PREFIX_BOUND and both compaction defaults. A bundle declaring `default_enabled: false` "
        "is declared and not charged; flipping one to true means raising the allowance in the "
        "same commit."
    )


def _one_sibling_dump(interpreter: Path, name: str) -> tuple[list[dict[str, Any]] | None, str]:
    """`tools/list` for one bundle, or `None` and the reason that bundle could not be measured.

    One subprocess per bundle, which is the whole shape of this function. `_SIBLING_DUMP` takes
    `*names` and imports them inside one dict comprehension, so the first `ImportError` kills the
    process before anything is printed — and the caller then had nothing for *any* bundle.
    Observed 2026-09-16: the sibling's `.venv` lacked `molmass`, which its own newest commit had
    just added to `servers/thermalsafety/pyproject.toml`, and the allowance bound went unchecked
    for all three.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _SIBLING_DUMP, name],
            capture_output=True,
            text=True,
            # Per bundle, where it used to bound the whole batch — so the helper's own worst case
            # is now this times the bundle count. Inert rather than dangerous: pytest's global
            # `timeout = 180` bites first, and a dump that takes even 30 s is a finding.
            timeout=60,
            cwd=str(interpreter.parents[2]),
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment
        return None, f"could not run the sibling's interpreter: {error}"
    if completed.returncode != 0:
        return None, f"its tools/list dump failed: {completed.stderr.strip()[-400:]}"
    try:
        listed = json.loads(completed.stdout)
    except ValueError as error:  # pragma: no cover - environment
        return None, f"its dump was not JSON: {error}"
    tools = listed.get(name)
    if not isinstance(tools, list):  # pragma: no cover - the program above prints one key
        return None, f"the dump printed {sorted(listed)} rather than {name!r}"
    return [dict(tool) for tool in tools], ""


def _sibling_tool_tokens(
    names: Iterable[str],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """Per-bundle `(tools, tokens)`, and per-bundle reasons for the ones that went unmeasured.

    Never raises for a missing or broken sibling: this file's job is to bound *this* repository's
    prefix, and a checkout somebody has not built is a fact about their laptop rather than a
    regression. It raises for nothing at all — a failure to run a dump is returned as a reason
    string, so the caller decides between skipping and failing.

    **The partition is per bundle, and that is what changed.** This used to pass every name to one
    subprocess and return `{}` on any non-zero exit, so one bundle that would not import took the
    bound for all of them — and `SERVED_ELSEWHERE_ALLOWANCE`, and therefore `PREFIX_BOUND`, and
    therefore both compaction defaults `core/config/agent.py` derives from it, went unchecked while
    the run said only that there was a sibling problem. This file's own header argues the rule it
    was breaking: a check that quietly shrinks is worse than one that says what it did not look at.

    The cost is one process spawn per bundle where there was one per call, and it is measured
    rather than called expensive: on the sibling's own `.venv`, three names in one process is
    1.37 s against 2.61 s in three, and nine names is 1.27 s against 6.48 s — about **0.7 s a
    spawn**. Over this file that is 3 subprocesses becoming 16 and roughly **+9.5 s** on a ~290 s
    file. What it buys is that a bundle's schemas stop being unwatched because a *different*
    bundle grew a dependency.
    """
    interpreter, reason = _sibling_python()
    wanted = sorted(names)
    if interpreter is None:
        return {}, dict.fromkeys(wanted, reason)

    from langchain_core.tools import StructuredTool

    def _unused(**kwargs: Any) -> None:
        """A body these tools never get: only their published schema is measured."""

    measured: dict[str, tuple[int, int]] = {}
    unmeasured: dict[str, str] = {}
    for name in wanted:
        tools, why = _one_sibling_dump(interpreter, name)
        if tools is None:
            unmeasured[name] = why
            continue
        total = 0
        for tool in tools:
            built = StructuredTool(
                name=str(tool["name"]),
                description=str(tool.get("description") or ""),
                args_schema=tool["inputSchema"],
                func=_unused,
            )
            total += _count(_tool_schema(built))
        measured[name] = (len(tools), total)
    return measured, unmeasured


def test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound() -> None:
    """`SERVED_ELSEWHERE_ALLOWANCE` has to be checked against the servers it stands in for.

    **Why this is not the same test as the ceiling above.** The ceiling bounds what this repository
    builds and a pull request here is what moves it. This allowance bounds three servers built in
    another repository, so nothing in *this* one's history moves it — and `PREFIX_BOUND`, which
    `core/config/agent.py` derives both compaction defaults from, is the two added. Left as a
    recorded figure it was wrong within hours of being recorded: 9,538 became 9,864 on the sibling's
    own merge, a third of the headroom, with every test here green. Roughly a thousand more tokens
    over there — two `chem` tools — would put the real prefix over the bound both defaults rest on,
    and this repository would have had no way to notice.

    **A skip, loudly, rather than a green line.** A test that needs somebody else's checkout cannot
    be a hard requirement of this suite; the failure mode `cli/validate_connectors.py::
    unverified_tool_surfaces` names is a check that quietly narrows to what it can reach. So the
    skip message says which bundles went unmeasured and why, and
    `tests/conftest.py::_report_sibling_skips` counts it — the run then states what it is not
    evidence about instead of implying it checked. That reporter is newer than this sentence,
    which asserted it for a day while `grep` for a sibling in `tests/conftest.py` found nothing.
    """
    measured, unmeasured = _sibling_tool_tokens(SERVED_ELSEWHERE)
    total = sum(tokens for _tools, tokens in measured.values())
    breakdown = ", ".join(
        f"{name} {tokens} / {tools}" for name, (tools, tokens) in sorted(measured.items())
    )
    # **Asserted before the skip, because what was measured is evidence whether or not the rest
    # was.** A partial total is a *lower* bound on the real one, so a partial run can still fail
    # this honestly — and a bundle whose schemas grew past the allowance on its own is exactly the
    # case a sibling problem in a *different* bundle used to hide.
    assert total <= SERVED_ELSEWHERE_ALLOWANCE, (
        f"the bundles this ratchet cannot serve now cost {total} tokens ({breakdown}) against an "
        f"allowance of {SERVED_ELSEWHERE_ALLOWANCE}. That allowance is half of PREFIX_BOUND "
        f"({PREFIX_BOUND}), which `core/config/agent.py` derives `agent_tool_result_clear_trigger` "
        "and `agent_context_token_budget` from — so raising it is a change to both defaults and to "
        "what every request may cost, not a bump. Raise all three together, or narrow a schema in "
        "Chemclaw3-mcp."
    )
    if unmeasured:
        pytest.skip(
            f"{SIBLING_SKIP} {len(unmeasured)} of the {len(SERVED_ELSEWHERE)} bundles served from "
            "Chemclaw3-mcp were NOT measured — "
            + "; ".join(f"{name}: {why}" for name, why in sorted(unmeasured.items()))
            + f". The {len(measured)} that were cost {total} tokens ({breakdown or 'none'}), "
            f"which is a lower bound. SERVED_ELSEWHERE_ALLOWANCE ({SERVED_ELSEWHERE_ALLOWANCE}) "
            f"and therefore PREFIX_BOUND ({PREFIX_BOUND}) are unchecked in this run, and both "
            "compaction defaults are derived from them."
        )


def test_one_unmeasurable_bundle_does_not_take_the_measurement_of_the_others() -> None:
    """The partition the two tests above rest on, driven rather than read off the helper.

    Observed 2026-09-16: the sibling's `.venv` lacked `molmass`, which its own newest commit had
    just added to `servers/thermalsafety/pyproject.toml` — a bundle neither test names — and
    `_sibling_tool_tokens` passed every name to one subprocess whose dict comprehension died on the
    first `ImportError`. So the allowance bound, `PREFIX_BOUND` and both compaction defaults went
    unchecked because a *different* bundle grew a dependency, and the run said only that there was
    a sibling problem.

    A name no module answers to reproduces that cause exactly — an import that raises inside the
    dump — without depending on which dependency the sibling's tree happens to be missing today.
    """
    interpreter, reason = _sibling_python()
    if interpreter is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}, so the partition cannot be driven")

    alone, _ = _sibling_tool_tokens(SERVED_ELSEWHERE)
    beside, unmeasured = _sibling_tool_tokens([*SERVED_ELSEWHERE, "notabundle"])

    # **The property is "a broken name costs its own measurement and no other", and it is stated
    # against what this checkout could measure rather than against `SERVED_ELSEWHERE`.** The first
    # spelling asserted `set(unmeasured) == {"notabundle"}`, which reds whenever a *real* bundle is
    # also unmeasurable — the 2026-09-16 case this whole change is about, had the missing
    # dependency been in one of these three rather than in `thermalsafety` — and reds with the
    # wrong diagnosis, saying the broken bundle took the other down when it failed on its own. It
    # would also have been the one cross-repository check in this file that fails rather than
    # skips, against the helper's own rule that somebody's unbuilt checkout is not a regression.
    assert set(beside) == set(alone), (
        f"measuring {sorted(SERVED_ELSEWHERE)} beside a bundle that cannot be imported returned "
        f"{sorted(beside)} where measuring them without it returned {sorted(alone)}: the broken "
        "name took another bundle down with it, which is the all-or-nothing behaviour this "
        "partition replaced"
    )
    assert "notabundle" in unmeasured, "the unimportable bundle was reported as measured"
    assert all(tokens > 0 for _tools, tokens in beside.values()), (
        "a bundle measured at zero tokens is a dump that returned nothing, which would satisfy "
        "the allowance bound by measuring nothing at all"
    )
    assert "notabundle" in unmeasured["notabundle"], (
        "the reason must name the bundle it is about, or a partial skip says less than the "
        "all-or-nothing one it replaced"
    )


def test_the_whole_directory_the_e2e_lane_mounts_is_bounded_too() -> None:
    """`FLEET_PUBLISHED_ALLOWANCE` bounds every bundle the fleet publishes, not only the shared.

    The test above bounds what `PREFIX_BOUND` is built from and therefore what a chart deployment
    pays. This one bounds the fleet's whole `manifests/` directory — what a deployment that mounts
    it and enables its bundles would pay — because nothing in this repository was watching those
    schemas grow at all. `infra/live/e2e-full-stack/up.sh` mounts that directory but after this
    tree's own, so of it the lane binds only what this tree declares nothing for (`pyexec`); the
    bound is an over-estimate of that lane, not a measurement of it.

    It is a bound on somebody else's tree and it can only skip or fail; it can never be the thing
    that *sets* a default here, which is why it is a second constant rather than a larger first
    one. Read `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it` for why the
    e2e lane's excess over `PREFIX_BOUND` is stated rather than absorbed.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    published = sorted(fleet_published_bundles(root)) if root is not None else []
    measured, unmeasured = (
        _sibling_tool_tokens(published)
        if published
        # `reason` is empty when the checkout resolved but publishes nothing this can read — a
        # manifests-only clone whose symlinks do not resolve. Saying so beats printing a skip whose
        # reason is a full stop, which is what naming `reason` unconditionally produced.
        else ({}, {"the fleet's published bundles": reason or "the checkout publishes none"})
    )
    # A bundle whose dump returns an empty tool list lands in `measured` at zero tokens and raises
    # no reason, so the allowance below would pass having measured nothing. The sibling test above
    # asserts this for `SERVED_ELSEWHERE`; this is the same guard over the wider set.
    hollow = sorted(name for name, (tools, _tokens) in measured.items() if not tools)
    assert not hollow, (
        f"{hollow} published no tools at all, so the allowance below would be satisfied by a dump "
        "that returned nothing rather than by a bundle that is small"
    )
    total = sum(tokens for _tools, tokens in measured.values())
    breakdown = ", ".join(
        f"{name} {tokens} / {tools}" for name, (tools, tokens) in sorted(measured.items())
    )
    assert total <= FLEET_PUBLISHED_ALLOWANCE, (
        f"the fleet's published manifests now cost {total} tokens ({breakdown}) against an "
        f"allowance of {FLEET_PUBLISHED_ALLOWANCE}. A deployment that points "
        "CHEMCLAW_CONNECTORS_DIR at that directory and enables its bundles pays this on every "
        "model call, on top of what this file's own ceiling bounds."
    )
    # A partial total compared against the *whole* allowance is the second hazard of the old
    # all-or-nothing helper, in the direction that reassures: the assertion above still holds
    # honestly on a lower bound, and this is what stops the run reading it as a verdict.
    if unmeasured:
        pytest.skip(
            f"{SIBLING_SKIP} {len(unmeasured)} of the fleet's published bundles were NOT "
            "measured — "
            + "; ".join(f"{name}: {why}" for name, why in sorted(unmeasured.items()))
            + f". The {len(measured)} that were cost {total} tokens ({breakdown or 'none'}), so "
            f"FLEET_PUBLISHED_ALLOWANCE ({FLEET_PUBLISHED_ALLOWANCE}) is unchecked in this run and "
            "nothing here is evidence about what the fleet's directory costs a deployment."
        )


# --------------------------------------------------------------------------------------------
# The only cache the shipped gateway has: a prefix whose bytes repeat.
#
# `cache_control` breakpoints left with the provider concept
# (`D-2026-09-04-a-gateway-is-the-only-provider`): every model call goes to one OpenAI-compatible
# endpoint, which has no such parameter. So the entire remedy for a prefix this size is the
# *serving* stack recognising a repeated one (vLLM's `--enable-prefix-caching` and its
# equivalents), which is a deployment decision — and this repository's half of it is that the
# bytes are worth caching. That half is testable here and nowhere else, because this is the only
# module that can obtain the request prefix at all.
# --------------------------------------------------------------------------------------------


def sent_prefix(actor: str, correlation_id: str) -> str:
    """The bytes a model call sends before the conversation, for one actor and correlation id.

    Public because the cross-process test below drives it in a subprocess; there is no second
    in-repo way to obtain this string, and re-deriving it there would compare two derivations
    rather than two processes.

    Built on the same `_CapturingModel` `_observed_prefix` uses, for the reason that function
    gives: a prefix assembled by this file would be a second implementation of what the graph
    assembles, and the two agreeing would prove nothing about what leaves the process. The tool
    schemas are serialised in the order they were bound, because order is part of the bytes.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    _RECEIVED.clear()
    _BOUND.clear()
    with _as_a_deployment_runs():
        graph = build_langgraph_agent(
            model=_CapturingModel(messages=iter([AIMessage(content="done")])),
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
    return json.dumps(
        {
            "tools": [convert_to_openai_tool(tool) for tool in _BOUND],
            "system": [
                message.content for message in _RECEIVED if isinstance(message, SystemMessage)
            ],
        }
    )


def test_the_prefix_two_sessions_are_sent_is_the_same_bytes() -> None:
    """A prefix cache can only hit on bytes that repeat, so the prefix must not carry a turn in it.

    **This is the precondition under the only prompt-caching remedy that still exists, and nothing
    asserted it.** There are no `cache_control` breakpoints to place on an OpenAI-compatible
    endpoint, so the entire saving depends on the serving stack recognising a repeated prefix, and
    that recognition is byte-exact: one timestamp, one correlation id, one session id or one
    reshuffled tool order anywhere in the prefix turns a fleet-wide cache hit into a full prefill,
    on every call, with nothing anywhere reporting it.

    Measured 2026-09-05: two turns for different actors, different correlation ids and different
    threads are **byte-identical**. So the request is shaped correctly today and this test is what
    keeps it that way — the failure it guards against is a one-line addition to a system prompt,
    and it would be invisible in every other test here.
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


#: What a child process prints: its envelope tag, and the prefix hashed with the nonce masked out.
#:
#: **Two renderings of one nonce, and both are masked.** The tag is the envelope's
#: (`<retrieved-note-…>`); `framing.SYSTEM_SPEECH_MARK` is the same value again in the sentence
#: that tells the model what marks a refusal as this system's. Masking only the first would make
#: the second read as "something else in the prefix is per-process", which is the finding this
#: test exists to make loud — so a deliberate second use of the *same* nonce must be masked with
#: it, and anything genuinely new still fails.
_CHILD = """
import hashlib, json, sys
sys.path.insert(0, "tests")
from chemclaw.agent.framing import ENVELOPE_TAG, SYSTEM_SPEECH_MARK
from chemclaw.core.config import settings
from test_context_floor import sent_prefix
prefix = sent_prefix("alice@example.com", "corr-a")
prefix = prefix.replace(SYSTEM_SPEECH_MARK, "<MARK>").replace(ENVELOPE_TAG, "<TAG>")
print("RESULT " + json.dumps(
    {"tag": ENVELOPE_TAG, "masked": hashlib.sha256(prefix.encode()).hexdigest()}
))
"""


def _child_prefix(**env: str) -> dict[str, str]:
    """Build the request prefix in a *fresh* process and report its tag and masked hash."""
    import os
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, **env},
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert completed.returncode == 0, f"child failed:\n{completed.stderr[-3000:]}"
    line = next(
        (ln for ln in completed.stdout.splitlines() if ln.startswith("RESULT ")),
        None,
    )
    assert line is not None, f"child printed no result:\n{completed.stdout[-3000:]}"
    return dict(json.loads(line.removeprefix("RESULT ")))


def test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce() -> None:
    """Across processes the prefix varies in exactly one place, and config decides whether it does.

    **This is the finding, and only a second process could have found it.** Within one process the
    prefix is byte-identical for any two sessions (the test above asserts that). Across processes
    it is not: `agent/framing.py::_envelope_nonce` falls back to `secrets.token_hex(8)` when
    `framing_envelope_secret` is unset, and that value is written into the system prompt — so every
    replica, and every restart, sends a *different* prefix. Measured 2026-09-05, two processes
    differed in that one 16-character token and in nothing else.

    A server-side prefix cache is the entire remedy for a prefix this size and it is byte keyed, so
    an unset `CHEMCLAW_FRAMING_ENVELOPE_SECRET` bounds that cache to one entry per pod-process: at
    `maxReplicas: 6` the fleet pays six cold prefills for the same bytes and pays them again on
    every rollout. The secret is already a chart secret and `Settings` already warns when a
    Postgres session store runs without it, for a *correctness* reason
    (`D-2026-08-27-a-warning-is-the-shape-a-guard-takes-when-raising-would-break-a-deployment`) —
    this is a second, independent reason to set the same variable.

    **Both halves are asserted rather than one**, because they fail differently: the masked
    comparison catches anything *else* that is per-process (a boot id, a build stamp, a
    hash-ordered set — the seeds differ deliberately), and the tag comparison catches the nonce
    itself losing its configured determinism.
    """
    configured = _child_prefix(CHEMCLAW_FRAMING_ENVELOPE_SECRET="probe-secret", PYTHONHASHSEED="0")
    random = _child_prefix(CHEMCLAW_FRAMING_ENVELOPE_SECRET="", PYTHONHASHSEED="999")

    assert configured["masked"] == random["masked"], (
        "two processes send different prefixes for a reason other than the envelope nonce, so no "
        "server-side prefix cache can hit across replicas or across a restart and every model "
        "call on every pod pays a full prefill of the whole static prefix"
    )
    assert configured["tag"] != random["tag"], (
        "the envelope tag did not vary with `framing_envelope_secret` — either the fallback "
        "stopped being per-process or the secret stopped reaching it; `agent/framing.py`"
    )
    from chemclaw.agent.framing import _envelope_nonce

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "chemclaw.agent.framing.settings",
            Settings(  # type: ignore[call-arg]
                _env_file=None, framing_envelope_secret=SecretStr("probe-secret")
            ),
        )
        assert configured["tag"].endswith(_envelope_nonce()), (
            "a configured envelope secret must give every process the same tag; that determinism "
            "is what makes one prefix-cache entry serve the whole fleet"
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


def test_a_helpers_prefix_is_bounded_by_the_one_this_file_already_ratchets() -> None:
    """The helper is a second graph with a second prefix, and since 2026-09-15 not a small one.

    `D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened` gave a helper its caller's
    *reading* connector tools, which is a real per-model-call cost this file did not previously
    bound: every ceiling here is the prefix of the graph a chemist talks to, and a helper's schemas
    are never on that wire. A fan-out of four helpers is four of these prefixes.

    **The answer is not a second ceiling, and the reason is why this is an assertion rather than a
    number.** A helper's surface is a *strict subset* of its caller's
    (`tests/test_subagents.py::test_a_helper_holds_no_tool_its_caller_does_not`), and its prompt is
    its caller's plus `HELPER_BRIEF` minus the harness block it is built without. So the caller's
    ceiling already bounds it — as an inequality, which holds under every future edit to either
    side, where a transcribed helper ceiling would be a second number to keep true. Measured here
    the day it was written: **26,626 against 66,316**, 40%.

    What this catches is the direction that would break the argument: a helper prompt that grows
    past what the harness block pays for, or a tool source that reaches a helper without reaching
    its caller. Either turns the inequality red and this file's single ceiling stops covering two
    graphs.
    """
    import inspect

    profile = get_profile("default")
    connectors = _connector_tools(profile)
    caller = build_langgraph_agent(
        model=_CapturingModel(messages=iter([AIMessage(content="") for _ in range(8)])),
        profile=profile,
        audit_sink=NullAuditSink(),
        connectors=connectors,
    )
    task = caller.nodes["tools"].bound.tools_by_name["task"]
    body = cast(Any, getattr(task, "coroutine", None) or getattr(task, "func", None))
    helper = inspect.getclosurevars(body).nonlocals["subagent_graphs"]["general-purpose"]

    def prefix(graph: Any) -> int:
        _RECEIVED.clear()
        _BOUND.clear()
        graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
        system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
        content = system.content if isinstance(system.content, str) else str(system.content)
        return _count(content) + sum(_count(_tool_schema(tool)) for tool in _BOUND)

    caller_prefix, helper_prefix = prefix(caller), prefix(helper)
    assert helper_prefix < caller_prefix, (
        f"a helper's static prefix is {helper_prefix} tokens against its caller's {caller_prefix}, "
        "so the ceilings in this file no longer bound it — and a fan-out pays that prefix once per "
        "helper. Either the helper's prompt has outgrown the harness block it is built without, or "
        "something now binds a tool to a helper that it does not bind to its caller"
    )


#: What a chemist's own skills may add to the prefix of every one of their model calls, in tokens.
#:
#: **A bound on a tier this ratchet's ceilings deliberately do not hold, and the distinction is the
#: reason it is a separate number.** `CEILINGS` bounds what *this repository* ships — the same
#: prefix for every deployment and every person. The personal-skills tier is neither: it is per
#: actor, it is empty on a fresh deployment, and the bytes in it were written by a chemist. Folding
#: its worst case into `PREFIX_BOUND` would take 5,600 tokens of thread allowance from every
#: deployment on earth for a tier almost all of them will never fill.
#:
#: Nothing is lost by keeping it out, because the *runtime* charges the real thing:
#: `agent/context_budget.prefix_tokens` reads the prefix of the call in flight, so a chemist who
#: fills their tier is compacted against what they actually send. What this number holds is the
#: other question — how large can that get — and it is derived rather than picked:
#: `agent_local_skills_max` rows, each contributing the name and the description that deepagents
#: truncates at its spec limit of 1,024 characters. Measured 2026-09-18 on a compiled graph with
#: the connector surface bound: an empty mounted tier costs **6** tokens, and a maximal one
#: **5,571**.
#:
#: Raising it means one of the two bounds moved, which is a decision about how much of a person's
#: own context their own judgment may take.
LOCAL_SKILLS_ALLOWANCE = 5_700

#: What the organisation's skills may add to the prefix of every model call **every chemist** makes.
#:
#: A separate number from `LOCAL_SKILLS_ALLOWANCE` because it bounds a different population, not a
#: different mechanism: a personal tier is one person's and usually empty, while whatever an
#: administrator publishes is in everybody's prefix and in every helper a turn spawns — a
#: four-helper fan-out sends it five times, since a helper is compiled through the same builder over
#: the same backend.
#:
#: **Outside `CEILINGS` for `LOCAL_SKILLS_ALLOWANCE`'s reason, and the "every user pays it"
#: difference does not flip it.** That difference makes the worst case uniform *within* a
#: deployment, which is a reason to bound the tier tightly — `agent_org_skills_max` is 12 rather
#: than the personal tier's 20 — and not a reason to charge every deployment on earth the same
#: thread allowance for a tier most of them will never publish to. The runtime already charges the
#: real thing: `agent/context_budget.prefix_tokens` reads the prefix of the call in flight.
#:
#: Derived, then measured on this tier's own mount rather than carried across from the neighbouring
#: one — the mount path differs (`/org/` against `/mine/`), so the listing's scaffolding does too,
#: and a number moved between two tiers is a claim about the wrong commit. The derivation said 3,345
#: (12 rows at the ~278 tokens `LOCAL_SKILLS_ALLOWANCE` measures per maximal row, plus the empty
#: mount); measured 2026-09-20 on a compiled graph with the connector surface bound, a maximal tier
#: costs **3,330** over an empty one. The allowance is the measurement plus ~3.6%, which is the
#: headroom `LOCAL_SKILLS_ALLOWANCE` leaves over its own.
ORG_SKILLS_ALLOWANCE = 3_450


def test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows() -> None:
    """The one part of the prefix a *person* writes, bounded and measured rather than assumed.

    **This ratchet was blind to it**, for the reason `_bound_tools` and `_observed_prefix` were
    blind to their own halves: `_observed_prefix` passes no `store=`, so the graph it measures
    mounts no personal tier and the figure it charges is a deployment with the feature off. That is
    the same shape as
    `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system`, one tier over.

    It is asserted here rather than folded into `CEILINGS` because the two bound different things —
    see `LOCAL_SKILLS_ALLOWANCE`. What would make this fail is a raised row cap, a longer permitted
    description, or a listing format that grew: each of those is a real change in what a chemist's
    own judgment costs them on every turn, and each would otherwise be invisible.
    """
    import asyncio

    from langgraph.store.memory import InMemoryStore

    from chemclaw.agent.local_skills import save_local_skill
    from chemclaw.core.config import settings
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    profile = get_profile("default")
    connectors = _connector_tools(profile)

    def prefix(store: Any) -> int:
        tokens = set_current_identity("a-chemist", frozenset())
        try:
            graph = build_langgraph_agent(
                model=_CapturingModel(messages=iter([AIMessage(content="")])),
                profile=profile,
                audit_sink=NullAuditSink(),
                connectors=connectors,
                store=store,
            )
            _RECEIVED.clear()
            graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
            system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
            return _count(
                system.content if isinstance(system.content, str) else str(system.content)
            )
        finally:
            reset_current_identity(tokens)

    # Maximal by the tier's own two bounds: every row the cap permits, each with the longest
    # description deepagents will publish rather than truncate.
    filled = InMemoryStore()
    description = "x" * MAX_SKILL_DESCRIPTION_CHARS
    for index in range(settings.agent_local_skills_max):
        name = f"local-skill-{index:03d}"
        asyncio.run(
            save_local_skill(
                filled,
                "a-chemist",
                name,
                f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
            )
        )

    empty, full = prefix(InMemoryStore()), prefix(filled)
    cost = full - empty

    assert cost <= LOCAL_SKILLS_ALLOWANCE, (
        f"a full personal skills tier adds {cost} tokens to every one of that chemist's model "
        f"calls, over the {LOCAL_SKILLS_ALLOWANCE} this file allows it. Either "
        "`agent_local_skills_max` rose, the permitted description grew, or upstream's listing "
        "format did — each is a real change in what a person's own judgment costs them per turn"
    )
    assert cost > 0, (
        "a full personal tier costs nothing, which means it is not reaching the system message at "
        "all — the feature is mounted and invisible to the model, so this asserts nothing"
    )


def test_the_organisations_skills_cost_no_more_prefix_than_the_cap_allows() -> None:
    """The part of the prefix an *administrator* writes, bounded and measured rather than assumed.

    The twin of `test_a_chemists_own_skills_cost_no_more_prefix_than_their_cap_allows`, and it needs
    to exist separately for the reason `ORG_SKILLS_ALLOWANCE` gives: this tier is paid by everybody
    rather than by its author, so its cap is the deployment's bill rather than one person's worst
    case.

    **This ratchet is blind to it by construction**, exactly as it was to the personal tier:
    `_observed_prefix` passes no `store=`, so the graph it measures mounts no stored tier at all.
    What would make this fail is a raised row cap, a longer permitted description, or a listing
    format that grew — each a real change in what every chemist pays on every turn, and each
    otherwise invisible.
    """
    import asyncio

    from langgraph.store.memory import InMemoryStore

    from chemclaw.agent.org_skills import save_org_skill

    profile = get_profile("default")
    connectors = _connector_tools(profile)

    def prefix(store: Any) -> int:
        # No ambient identity: the organisation's tier needs none, and measuring it without one is
        # also what keeps this figure free of the personal tier, which needs an actor to mount.
        with _as_a_deployment_runs():
            graph = build_langgraph_agent(
                model=_CapturingModel(messages=iter([AIMessage(content="")])),
                profile=profile,
                audit_sink=NullAuditSink(),
                connectors=connectors,
                store=store,
            )
        _RECEIVED.clear()
        graph.invoke({"messages": [HumanMessage("what does this turn cost?")]})
        system = [message for message in _RECEIVED if isinstance(message, SystemMessage)][0]
        return _count(system.content if isinstance(system.content, str) else str(system.content))

    # Maximal by the tier's own two bounds: every row the cap permits, each with the longest
    # description deepagents will publish rather than truncate.
    filled = InMemoryStore()
    description = "x" * MAX_SKILL_DESCRIPTION_CHARS
    for index in range(settings.agent_org_skills_max):
        name = f"org-skill-{index:03d}"
        asyncio.run(
            save_org_skill(
                filled,
                name,
                f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
                activated_by="an-admin",
            )
        )

    empty, full = prefix(InMemoryStore()), prefix(filled)
    cost = full - empty

    assert cost <= ORG_SKILLS_ALLOWANCE, (
        f"a full organisation skills tier adds {cost} tokens to every model call every chemist in "
        f"this deployment makes, over the {ORG_SKILLS_ALLOWANCE} this file allows it. Either "
        "`agent_org_skills_max` rose, the permitted description grew, or upstream's listing format "
        "did — and this one is paid by everybody, and again by every helper a turn spawns"
    )
    assert cost > 0, (
        "a full organisation tier costs nothing, which means it is not reaching the system message "
        "at all — the tier is mounted and invisible to the model, so this asserts nothing"
    )
