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
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from chemclaw.agent.chemclaw_agent import _capability_tools, instructions_for
from chemclaw.agent.langgraph_agent import (
    _labelled,
    _skill_dirs,
    _skills_middleware,
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
#: **Raised to 29,900 on 2026-08-29, and the reason is recorded rather than assumed.** F2 and F3
#: added three tools to the `default` surface — `review_activity`, `request_external_input` and
#: `check_pending_requests` — measured at 1,399 tokens between them *after* a trimming pass took
#: 225 off the first two by moving developer rationale out of the docstrings the schema ships. The
#: prefix went 28,210 → 29,609. Two of the three are the reason a project leader can be answered at
#: all (the operational read model and the inbox over the durable wait), so this is capability
#: rather than drift — but it is exactly the growth the § 5 row is about, and it makes the
#: `default` allow-list below the next thing worth doing rather than a nice-to-have.
CEILINGS: dict[str, int] = {"__default__": 29_900}

#: How much of the floor one tool may be. A schema above this is not expensive, it is *badly
#: shaped* — the fix is pagination, a narrower argument, or splitting a tool that does two things.
MAX_SINGLE_TOOL_TOKENS = 900

#: The two that are already over it, with what they cost on 2026-08-25.
#:
#: Recorded rather than hidden by a bigger bound, so the *third* one fails this test. Both are real
#: debt and neither is a mystery: `start_optimization_campaign` carries a BoFire campaign
#: declaration — objectives, constraints and parameter domains — as nested argument schema, and
#: `propose_knowledge_note` carries the note frontmatter contract. Each is a candidate for taking
#: its payload as one structured argument documented in a skill instead of as a wide signature.
KNOWN_OVERSIZED: dict[str, int] = {
    "start_optimization_campaign": 2_020,
    "propose_knowledge_note": 1_069,
}


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

    `convert_to_openai_tool` is the function LangChain itself calls when binding tools to a model,
    so this is the payload rather than an approximation of it. It derives the schema from the
    signature and the docstring, which is exactly why the docstrings are the expensive part.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return json.dumps(convert_to_openai_tool(tool))


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
    """The static prefix for one profile: its total, and the per-part breakdown behind it."""
    profile = get_profile(profile_name)
    tools = _capability_tools(profile)
    parts = {
        "instructions": _count(instructions_for(profile)),
        "skills-listing": _count(_skills_listing(profile, tools)),
    }
    for tool in tools:
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
