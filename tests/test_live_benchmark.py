"""What keeps the external benchmark a measurement of this system rather than of its scorer.

The corpus is vendored and keyed, so nothing here needs a model. What does need asserting is the
part that went wrong first: a scorer that reads a model's *reasoning* instead of its *answer*
scores the reasoning. Measured against a real gateway on the first five questions of this subset,
the whole-answer scan credited the option `"5"` as `"1"` and `"4"` as `"2"` — 1/5 where the answers
were 4/5 — because those digits appear inside sentences like "approximately 1.07%".
"""

import json
from pathlib import Path

import pytest

from chemclaw.cli.live_benchmark import (
    BENCHMARK_DIR,
    Answered,
    BenchmarkQuestion,
    _chosen,
    _prompt,
    load_questions,
    render,
)


def test_the_vendored_corpus_matches_its_recorded_checksum() -> None:
    """A corpus with no verified checksum cannot be shown to be what the review approved.

    It is also what makes the score comparable between two runs: a benchmark whose questions
    changed under it reports a comparison between two different things.
    """
    questions = load_questions(BENCHMARK_DIR)
    assert len(questions) == 100
    assert len({q.id for q in questions}) == len(questions), "ids are this corpus's key"
    assert all(q.answer in q.options for q in questions), "a key naming no option scores nothing"


def test_the_corpus_records_its_licence_and_where_a_human_got_it() -> None:
    """The discipline the sibling fleet holds every vendored corpus to, applied here.

    A corpus with no recorded licence is a legal question nobody can answer a year later, and
    `retrieved_from` is the only record of where a human obtained the file. Nothing reads it as an
    address.
    """
    manifest = json.loads((Path(BENCHMARK_DIR) / "dataset.json").read_text(encoding="utf-8"))
    for field in ("name", "version", "licence", "retrieved_from", "description", "sha256"):
        assert manifest.get(field), f"{field} is missing from the benchmark manifest"


def test_a_corpus_that_does_not_hash_is_refused(tmp_path: Path) -> None:
    """The other direction, so the checksum check cannot be satisfied by not checking."""
    source = Path(BENCHMARK_DIR)
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
    """One option is routinely a prefix of another, and shortest-first credits the wrong one."""
    options = ["Only Ag+ is present", "Ag+ is present, and Pb2+ may be present"]
    assert _chosen("Ag+ is present, and Pb2+ may be present", options) == options[1]


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
