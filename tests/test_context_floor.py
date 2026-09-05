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
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import _capability_tools, instructions_for
from chemclaw.agent.langgraph_agent import (
    _labelled,
    _skill_dirs,
    _skills_middleware,
    build_langgraph_agent,
    skills_backend,
)
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile, registered_profile_names

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
#: The headroom is 799 tokens, which is the property every raise above was chosen for — under what
#: one `record_knowledge_note` costs (1,126, re-derived the same day), so the ceiling cannot absorb
#: another tool of that size unnoticed. It is deliberately *not* the ~980 the branch point would
#: have given: this tree already carries a sibling branch's docstring edit worth 160 tokens, and a
#: ceiling set to today's measurement plus a fixed headroom is set against whatever else is in
#: flight. The figure to judge a raise by is the headroom, not the ceiling.
#:
#: **`agent_tool_result_clear_trigger` moves with it, by derivation rather than by retuning**
#: (`core/config/agent.py`): its default is this ceiling plus the 30,000 of thread the setting has
#: always meant, so raising the ceiling raises the trigger to 74,500 and every deployment's lossless
#: edit fires slightly later than it did. That coupling is the reason to keep this number a
#: *ceiling* rather than a measurement — see the config comment, which says why.
CEILINGS: dict[str, int] = {"__default__": 44_500}

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
KNOWN_OVERSIZED: dict[str, int] = {
    "start_optimization_campaign": 2_307,
    "record_knowledge_note": 1_126,
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
    being taught about it. The backlog row that asked for this proposed spying on `bind_tools`
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
    graph = build_langgraph_agent(
        model=_CapturingModel(messages=iter([AIMessage(content="")])),
        profile=profile,
        audit_sink=NullAuditSink(),
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

    **The total is observed and the breakdown is derived, and the distinction is load-bearing.**
    What is charged is the `SystemMessage` the model was handed plus every schema the graph's
    `ToolNode` holds — two measurements, neither re-derived from what this repository believes it
    writes. The three prompt lines below *split* that observed number: two are this repository's own
    contributions, measured the way `build_langgraph_agent` builds them, and the third is the
    remainder — every deepagents middleware's prompt section, named rather than uncounted.

    The two derived halves stay measured from the *capability* tools deliberately.
    `build_langgraph_agent` hands `skills_backend` the raw callables, so narrowing the skills
    listing by the bound list would measure a backend production never builds. A negative remainder
    would mean those two halves are no longer what production puts in the prompt — the split has
    gone wrong, not the total, which is still what the model was sent.
    """
    profile = get_profile(profile_name)
    system, _sent, bound = _observed_prefix(profile)
    instructions = _count(instructions_for(profile))
    listing = _count(_skills_listing(profile, _capability_tools(profile)))
    parts = {
        "instructions": instructions,
        "skills-listing": listing,
        "prompt:middleware-sections": _count(system) - instructions - listing,
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
