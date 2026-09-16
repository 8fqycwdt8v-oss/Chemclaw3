"""What keeps the external benchmark a measurement of this system rather than of its scorer.

The corpus is vendored and keyed, so nothing here needs a model. What does need asserting is the
part that went wrong first: a scorer that reads a model's *reasoning* instead of its *answer*
scores the reasoning. Measured against a real gateway on the first five questions of this subset,
the whole-answer scan credited the option `"5"` as `"1"` and `"4"` as `"2"` — 1/5 where the answers
were 4/5 — because those digits appear inside sentences like "approximately 1.07%".
"""

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from chemclaw.agent.chemclaw_agent import (
    _capability_tools,
    instructions_for,
    skill_tool_names,
)
from chemclaw.agent.profile_discovery import _load
from chemclaw.agent.profiles import get_profile
from chemclaw.cli.live_benchmark import (
    Answered,
    BenchmarkQuestion,
    _chosen,
    _prompt,
    load_questions,
    render,
)
from chemclaw.core.config import settings


def test_the_vendored_corpus_matches_its_recorded_checksum() -> None:
    """A corpus with no verified checksum cannot be shown to be what the review approved.

    It is also what makes the score comparable between two runs: a benchmark whose questions
    changed under it reports a comparison between two different things.
    """
    questions = load_questions(settings.benchmark_dir)
    assert len(questions) == 100
    assert len({q.id for q in questions}) == len(questions), "ids are this corpus's key"
    assert all(q.answer in q.options for q in questions), "a key naming no option scores nothing"

    # **The per-category split is recorded once, in `dataset.json`, and checked against the
    # corpus.** The README's prose said "13 from each of eight categories" while the manifest
    # beside it already said the 100-question trim cut `toxicity_and_safety` short — a number in
    # prose being wrong about the file it sits next to. It is data now, so a re-sampled subset that
    # changes the shape fails here rather than leaving a sentence describing the old one.
    manifest = json.loads(
        (Path(settings.benchmark_dir) / "dataset.json").read_text(encoding="utf-8")
    )
    assert manifest["categories"] == dict(sorted(Counter(q.category for q in questions).items())), (
        "dataset.json's per-category split is not the corpus's"
    )


def test_the_corpus_records_its_licence_and_where_a_human_got_it() -> None:
    """The discipline the sibling fleet holds every vendored corpus to, applied here.

    A corpus with no recorded licence is a legal question nobody can answer a year later, and
    `retrieved_from` is the only record of where a human obtained the file. Nothing reads it as an
    address.
    """
    manifest = json.loads(
        (Path(settings.benchmark_dir) / "dataset.json").read_text(encoding="utf-8")
    )
    for field in ("name", "version", "licence", "retrieved_from", "description", "sha256"):
        assert manifest.get(field), f"{field} is missing from the benchmark manifest"


def test_a_corpus_that_does_not_hash_is_refused(tmp_path: Path) -> None:
    """The other direction, so the checksum check cannot be satisfied by not checking."""
    source = Path(settings.benchmark_dir)
    (tmp_path / "dataset.json").write_text(
        (source / "dataset.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    body = json.loads((source / "questions.json").read_text(encoding="utf-8"))
    body["questions"][0]["question"] += " (edited)"
    (tmp_path / "questions.json").write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ValueError, match="Re-record the checksum"):
        load_questions(str(tmp_path))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # The defect this scorer was rewritten for: every wrong option appears in the reasoning.
        ("The M+1 peak is about 1.07% per carbon, so 2 would be too few.\n\n4", "4"),
        # A bare answer, which is what the prompt asks for.
        ("5", "5"),
        # An answer that names the option inside a sentence — formatting, not chemistry.
        ("The answer is n-alkanes.", "n-alkanes"),
        # A refusal names no option, and that is not the same finding as a wrong one.
        ("This system holds no chromatographic model, so I cannot answer from evidence.", ""),
    ],
)
def test_the_scorer_reads_the_answer_and_not_the_reasoning(answer: str, expected: str) -> None:
    """Last line first, whole answer only as a fallback, word boundaries throughout."""
    assert _chosen(answer, ["1", "2", "4", "5", "n-alkanes"]) == expected


def test_a_longer_option_wins_over_the_shorter_one_it_contains() -> None:
    r"""One option routinely *contains* another, and shortest-first credits the wrong one.

    **The fixture this replaces had no containment in it.** `"Only Ag+ is present"` is not a
    substring of `"Ag+ is present, and Pb2+ may be present"` — it carries a leading `Only` — so
    neither order could match it and deleting `sorted(..., reverse=True)` left this test green. A
    test named for a property its fixture does not exhibit is worse than none: it reads as the
    guard's coverage.

    So the cases come from the shipped corpus, where the containment is real and the consequence is
    the opposite answer. Driven over all 100 questions, four option-answers across three questions
    score differently without the sort.
    """
    questions = {q.id: q for q in load_questions(settings.benchmark_dir)}

    # The key *is* the longer option, and the shorter one inside it reverses the physics.
    mass = questions["analytical_chemistry:molecular_structure#3"]
    assert _chosen(mass.answer, mass.options) == mass.answer
    assert "proportional to the square root of its mass" in mass.options, (
        "the corpus no longer carries the option contained in the key; pick another pair"
    )

    # A reactive-hazard item, where the contained option is the opposite hazard call.
    hazard = questions["toxicity_and_safety:ChemComp#1"]
    innocuous = "Innocuous and non-flammable gas generation"
    assert innocuous in hazard.options and "Flammable gas generation" in hazard.options
    assert _chosen(innocuous, hazard.options) == innocuous


@pytest.mark.parametrize(
    ("answer", "options", "expected", "guard"),
    [
        # `(?<!\w)` — an option must not match inside a longer word. Both of these are options of
        # one shipped question (`materials_science:reactive_groups_53`).
        ("The gas evolved is methane.", ["Ethane"], "", "no word character before"),
        # `(?!\w)` — nor with a longer word growing out of its end.
        ("The product is ethanolamine.", ["ethanol"], "", "no word character after"),
        # `(?<!\.)` — a digit after a decimal point is part of a number, not an option.
        ("The measured ratio was 0.5 throughout.", ["5"], "", "not the tail of a decimal"),
        # `(?!\.\d)` — nor is one before it. This is the case measured against a real gateway:
        # the option "1" was credited from "approximately 1.07%".
        ("Roughly 1.07% per carbon, so neither.", ["1"], "", "not the head of a decimal"),
        # And the guards must not refuse a legitimate match: a full stop is a boundary, a decimal
        # point is not, and that difference is the whole of why they are written this way.
        ("The answer is 5.", ["5"], "5", "a full stop still ends an option"),
    ],
)
def test_each_word_boundary_guard_refuses_a_match_it_alone_blocks(
    answer: str, options: list[str], expected: str, guard: str
) -> None:
    r"""One case per guard in `_chosen`'s pattern, because all four were deletable individually.

    Driven before this test existed: removing `(?<!\w)`, `(?<!\.)`, `(?!\w)` or `(?!\.\d)` from
    the pattern — one at a time — left the whole file green, including the corpus-wide scorability
    tests above. Those bound the scorer from one side only: every option must be *findable*, and a
    guard's job is to stop it being found where it is not.

    The last case is the other direction, and it is why the guards are lookarounds rather than
    `\b`: a full stop ends an option and a decimal point does not, so a rule that blocked both
    would score "The answer is 5." as an abstention.
    """
    assert _chosen(answer, options) == expected, f"the {guard} guard is not holding"


def test_the_prompt_carries_every_option_and_asks_for_one() -> None:
    """The prompt is deliberately plain: engineering it would make the number this file's."""
    question = BenchmarkQuestion(
        id="q", category="c", question="Which?", options=["alpha", "beta"], answer="alpha"
    )
    prompt = _prompt(question)
    assert "alpha" in prompt and "beta" in prompt and "exactly one" in prompt


def test_the_report_separates_an_abstention_from_a_wrong_answer() -> None:
    """A model that declines is not a model that guesses, and one number cannot say both.

    It matters more here than on an ordinary benchmark: this system's instructions tell it not to
    answer without evidence, so a closed-book chemistry question it declines is the guardrail
    working. A report that folded those into "wrong" would read as a science failure.
    """
    results = [
        Answered(question_id="a", category="x", chosen="alpha", correct=True),
        Answered(question_id="b", category="x", chosen="beta", correct=False),
        Answered(question_id="c", category="x", chosen="", correct=False, unparsed=True),
    ]
    report = render(results, None)
    assert "1/3 correct (33%)" in report
    assert "1 answer(s) named no option" in report


# The two eval-only profiles the benchmark's arms are, and the sentence that separates them.
_PROFILE_DIR = Path(__file__).resolve().parents[1] / "data/evals/profiles"
# A `PromptBlock` with **no `requires` gate**, so no amount of tool removal takes it out of the
# prompt. Quoted rather than imported: the claim is about what the model is sent, and importing the
# block would let the two agree with each other while the shipped prose said something else.
_NO_RECORD_SENTENCE = "you must never state a specific parameter as though it came from the record"


def _toolless_prompt(filename: str) -> str:
    """The system prompt an arm with no capability tools is actually sent.

    `available` is what `build_langgraph_agent` passes — the names the graph binds — and for a
    toolless profile that is the six `FilesystemMiddleware` verbs plus `task`, neither of which a
    profile can strip. Passing it matters: `instructions_for(profile)` with no surface is the
    *maximal* prompt, which is not what either arm sends.
    """
    return instructions_for(_load(_PROFILE_DIR / filename), skill_tool_names() | {"task"})


def test_the_arm_that_varies_the_tools_varies_only_the_tools() -> None:
    """`tools-removed` is the contrast a tools claim may rest on, and this is why it can.

    It declares `tool_names: []` and **no** `instructions:`, so the prose is the deployment's own —
    narrowed only by the blocks that name an absent tool, which is a consequence of the treatment
    rather than a second variable. The sentence no tool removal can remove survives in it, which is
    the property the published 62-against-74 pair did not have.
    """
    profile = _load(_PROFILE_DIR / "tools-removed.yaml")

    assert profile.name == "tools-removed"
    assert profile.tool_names == frozenset()
    assert profile.instructions is None, (
        "an arm that overrides the instructions varies the prompt as well as the tools, which is "
        "the confound D-2026-09-14-tools-were-never-the-variable exists to end"
    )
    assert _capability_tools(profile) == []
    assert _NO_RECORD_SENTENCE in _toolless_prompt("tools-removed.yaml")


def test_the_prompt_swapping_arm_cannot_be_read_as_a_tools_contrast() -> None:
    """The other direction, and the one that catches a relabelling.

    `no-tools.yaml` is kept — it is the arm `make live-ab`'s merged measurement ran on — but it
    replaces the whole default prose, so a document calling it a tools contrast is wrong about its
    own fixture. Asserted as a property and a magnitude rather than a digit: the exact character
    counts move whenever the prompt is edited, while "it replaces the prose, by thousands of
    characters, including the sentence tools cannot remove" is the thing that would have to stop
    being true for the label to become honest.
    """
    profile = _load(_PROFILE_DIR / "no-tools.yaml")
    swapped = _toolless_prompt("no-tools.yaml")

    assert profile.tool_names == frozenset()
    assert profile.instructions is not None
    assert _NO_RECORD_SENTENCE not in swapped
    assert len(instructions_for(get_profile(None))) - len(swapped) > 10_000, (
        "the two arms no longer differ by a large block of prompt; if the default prose shrank to "
        "meet the control, the published pair's attribution may be worth re-measuring"
    )


# How a chemist — or a model — writes an option that the corpus stores as ChemBench's raw markup.
# **Written the other way round from `_normalised` on purpose**: this spells the symbols out as
# Unicode where the scorer folds them onto words, so the two meet in the middle rather than
# agreeing by construction. An oracle derived from the implementation would pass whatever the
# implementation does, which is the failure the sibling fleet's own corpus tests are built to
# avoid — they check a vendored table against independently written numbers rather than against
# the loader that reads it.
_PLAIN_SPELLING = {
    "\\circ": "°",
    "\\Delta": "Δ",
    "\\times": "×",
    "\\propto": "∝",
    "\\alpha": "α",
    "\\beta": "β",
    "\\log": "log",
}


def _as_a_chemist_writes_it(option: str) -> str:
    """The option with its typography removed and its symbols spelled the way a model types them."""
    for command, char in _PLAIN_SPELLING.items():
        option = option.replace(command, char)
    option = re.sub(r"\\(?:ce|pu|text|mathrm)\s*", "", option)
    return re.sub(r"[{}$^_]", "", option).strip()


def test_every_option_is_scorable_in_the_spelling_a_model_would_use() -> None:
    r"""The scorer must not be measuring typography, and on a fifth of this corpus it was.

    21 of the 100 keys carry `\\ce{}`, `\\pu{}` or math mode. Answered in the plain spelling above,
    **88 of the corpus's 418 options** scored as something other than themselves before the
    normaliser — most as an abstention, and at least one as a different option outright. The
    measurement is the whole corpus rather than a sample, because the defect is per-item and a
    sample would hide the arm-asymmetry: the two arms do not write markup equally often, so a
    markup-blind scorer is not neutral between them.
    """
    questions = load_questions(settings.benchmark_dir)
    misscored = [
        (question.id, option)
        for question in questions
        for option in question.options
        if _chosen(_as_a_chemist_writes_it(option), question.options) != option
    ]

    assert not misscored, (
        f"{len(misscored)} of {sum(len(q.options) for q in questions)} options are unscorable in "
        f"the spelling a model uses; the scorer is measuring markup. First: {misscored[:3]}"
    )


def test_an_option_answered_verbatim_still_scores_as_itself() -> None:
    r"""The other direction: normalising must not lose a match that already worked.

    A normaliser is a lossy transform applied to both sides, so it can break a comparison it was
    supposed to leave alone — two options that differ only by a symbol command are the pair at
    risk, which is why `_normalised` maps `\\Delta` to a word instead of deleting it.
    """
    questions = load_questions(settings.benchmark_dir)
    misscored = [
        (question.id, option)
        for question in questions
        for option in question.options
        if _chosen(option, question.options) != option
    ]

    assert not misscored, f"an option no longer scores as itself: {misscored[:3]}"


def test_the_markup_defect_credited_a_wrong_option_for_a_right_answer() -> None:
    r"""The worked example, from the run that found it, because the failure is not an abstention.

    `materials_science:polymer_chemistry_19`'s key is `\\ce{FeSO4} + t-butyl hydroperoxide`. The
    model's last line was `FeSO4 + t-butyl hydroperoxide` — correct — the exact matcher missed the
    mhchem spelling, fell through to the whole-answer fallback, and credited the initiator the
    model had named only to rule it out. A wrong answer recorded for a right one moves the score,
    where an abstention only widens the gap.
    """
    question = next(
        q
        for q in load_questions(settings.benchmark_dir)
        if q.id == "materials_science:polymer_chemistry_19"
    )
    answer = (
        "Azobisisobutyronitrile and benzoyl peroxide are thermal initiators, not redox pairs.\n\n"
        "FeSO4 + t-butyl hydroperoxide"
    )

    assert _chosen(answer, question.options) == question.answer


def test_a_turn_that_failed_is_not_a_turn_that_declined() -> None:
    """Three outcomes, not two — and the third is the one only one arm can produce.

    `api/runner.py` turns every failure into an `ErrorEvent`: a loop cap, a spend cap, a connector
    outage, a degraded capability. Each left `answer=""`, and an empty answer books as "named no
    option" — the column this benchmark's central finding is read off. The tool-bearing arm binds
    every connector and can fail all of those ways; the toolless control binds none and
    structurally cannot, so the two were not being measured on the same scale.
    """
    results = [
        Answered(question_id="a", category="x", chosen="alpha", correct=True),
        Answered(question_id="b", category="x", chosen="", unparsed=True),
        Answered(question_id="c", category="x", chosen="", error_code="loop_cap_reached"),
    ]
    report = render(results, None)

    assert "1 answer(s) named no option" in report, "an errored turn was counted as an abstention"
    assert "1 turn(s) failed and answered nothing (loop_cap_reached x1)" in report


def test_a_decimal_is_not_a_choice_and_a_full_stop_is_not_a_decimal() -> None:
    r"""The word-boundary rule, in both directions, on the one external number this repo publishes.

    The change that fixed it shipped with no test at all: `(?<!\w)(?<!\.)`, `(?<!\w)(?<!\d\.)`
    and a bare `(?<!\w)` all left this file at 10 passed, so nothing in the suite distinguished
    the buggy form, the fixed form and no lookbehind whatever — on the scorer behind the only
    benchmark figure this repository reports to anyone outside it.

    The rule the docstring states is that a `.` blocks only where a digit is on the other side of
    it, which is the difference between a decimal and a full stop. Each arm below is one half of
    that, and each fails under at least one of the three forms.
    """
    digits = ["1", "3", "4", "5"]

    # A decimal: the option must not match inside a number, on either side of the point.
    assert _chosen("the yield was approximately 1.07 percent", digits) == ""
    assert _chosen("the ratio came out at 3.5 to one", digits) == ""

    # A full stop is not a decimal: an option after one still counts. This is the arm the buggy
    # leading `(?<!\.)` failed, scoring the answer as an abstention.
    words = ["n-alkanes", "aromatics"]
    assert _chosen("that rules out the others. n-alkanes", words) == "n-alkanes"
    assert _chosen("that rules them out.n-alkanes", words) == "n-alkanes"

    # And the ordinary case still scores, so a lookbehind tightened until nothing
    # matches is red rather than quietly conservative.
    assert _chosen("the answer is 4", digits) == "4"


def test_ask_reads_the_same_awkward_stream_the_probe_harness_does() -> None:
    """The third of the three readers, against the fixture the other two are held to.

    This one carried `line[5:].strip()` where the storm carried `line[6:]` and `evals/live` carried
    a third spelling — three readings of one wire format, none of which handled a `data:` field
    split over two lines. The cost here is specific: `_ask` collects the `answer` event and the
    `error` event and nothing else, so a dropped `answer` frame scores as the model declining to
    name an option, which `Answered.unparsed` reports as a fact about chemistry.

    The fixture is `tests/test_live_probes.AWKWARD_STREAM`, imported rather than copied, so the
    claim that the three agree is a shared object rather than three transcriptions of one.
    """
    import asyncio

    import httpx

    from chemclaw.cli.live_benchmark import _ask
    from tests.test_live_probes import AWKWARD_STREAM, SSE_HEADERS

    question = BenchmarkQuestion(
        id="q1",
        category="solvents",
        question="which solvent?",
        options=["ethanol", "toluene"],
        answer="ethanol",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=AWKWARD_STREAM, headers=SSE_HEADERS)

    async def go() -> tuple[str, str]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await _ask(client, question, None)

    answer, error_code = asyncio.run(go())
    assert answer == "the corpus says ethanol."
    assert error_code == ""
    # And the scorer gets what it needs out of it, which is the reason the frame mattering is not
    # an internal detail: a dropped answer is an abstention in the published table.
    assert _chosen(answer, question.options) == "ethanol"
