"""`python -m chemclaw.cli.live_benchmark` — score this system on a benchmark somebody else wrote.

**Why this exists.** `make eval` gates 23 metric values over 15 first-party case files, a
7-document retrieval corpus and a 39-note knowledge graph. Every one of those numbers was written
here, which makes them honest and makes them incomparable to anything: no number in this repository
could be put beside another system's, and a number a chemist can argue with is the only kind that
survives the argument.

**Keyed, not graded.** ChemBench items carry `target_scores` naming exactly one correct option, so
scoring is a comparison rather than a judgement. That is the whole reason a multiple-choice
benchmark is worth having beside `data/evals/probes/`: the probe corpus measures whether an answer
is *grounded*, which needs a model to grade it (`evals/live_judge.py`) and inherits that model's
noise; this measures whether an answer is *right*, and inherits none.

**What it does not measure, said here because the number will be read as if it did.** A
multiple-choice chemistry question is answered from what the model knows. This system's retrieval,
its knowledge graph and its calculations have nothing to add to "what compounds form when aniline
reacts with nitrous acid", so the score is a floor — what the deployment's model brings before this
system does anything. `make live-ab` over `data/evals/probes/` is where the tools are actually the
subject.

**Two control arms, and which one answers which question is the whole of
`D-2026-09-14-tools-were-never-the-variable`.** `--profile tools-removed` removes every capability
tool and changes nothing else, so a difference against the default arm is attributable to the
tools. `--profile no-tools` *also* replaces the system prompt wholesale — a profile's
`instructions:` are a replacement, not an addition — so a difference against it is a prompt result
whatever the arm is called. The published 62-against-74 pair was the second one read as the first;
with the prompt held fixed, removing every tool moved 62 to 58.

Exit codes: 0 when the run completed, 3 when the lane could not be reached (never counted as a
pass, the posture `live_probes` and `live_turn_cost` already take).
"""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.evals.live import open_session


class BenchmarkQuestion(BaseModel):
    """One keyed multiple-choice item, as the vendored subset carries it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: str
    subfield: str = ""
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    uuid: str = ""


class Answered(BaseModel):
    """What one question produced: the option this system chose, and whether it was the key's."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    category: str
    chosen: str = ""
    correct: bool = False
    # An answer that named no option at all. Kept apart from a wrong one because they are different
    # findings: a model that declines is not a model that guesses, and a benchmark reporting them as
    # one number cannot tell an abstention from an error.
    unparsed: bool = False
    # The `ErrorCode` of a turn that *failed*, or `""` for a turn that answered. **A third
    # outcome, and the arms cannot produce it equally.** `_ask` collects the `answer` event and
    # nothing else, so a turn that hit the loop cap, the spend cap, a connector failure or a
    # degraded capability arrived here as `answer=""` and booked as an abstention — which is a
    # claim about the model's judgement. The tool-bearing arm binds every connector and can reach
    # all of those; the toolless control binds none and structurally cannot, so folding the two
    # together moves exactly one arm's "it declined" column, in the direction that flatters the
    # control. An errored turn is `correct=False` — it did not answer — and is *not* `unparsed`.
    error_code: str = ""


def load_questions(directory: str) -> list[BenchmarkQuestion]:
    """The vendored subset, checked against the checksum its manifest records.

    Checked rather than trusted, for the reason `mcp_server_kit.load_dataset` gives one repository
    over: a corpus with no verified checksum cannot be shown to be what the review approved, and a
    benchmark whose questions changed under it reports a comparison between two different things.
    """
    root = Path(directory)
    manifest = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    raw = (root / "questions.json").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError(
            f"{root / 'questions.json'} hashes to {digest}, and {root / 'dataset.json'} records "
            f"{manifest['sha256']}. Re-record the checksum in the commit that changes the corpus."
        )
    return [BenchmarkQuestion.model_validate(q) for q in json.loads(raw)["questions"]]


def _prompt(question: BenchmarkQuestion) -> str:
    """The question as the system is asked it — options listed, one-option answer requested.

    Deliberately plain. Prompt engineering here would make the number a property of this file
    rather than of the deployment, and the point of an external benchmark is that somebody else can
    produce it.
    """
    options = "\n".join(f"- {option}" for option in question.options)
    return (
        f"{question.question}\n\nChoose exactly one of these options and reply with that option's "
        f"text and nothing else:\n{options}"
    )


#: The mhchem/`siunitx` wrappers whose *contents* are the chemistry: `\ce{FeSO4}` is the string a
#: chemist writes as `FeSO4`, and `\pu{280 nm}` is `280 nm`. The command goes, the argument stays.
_MARKUP_WRAPPERS = ("ce", "pu", "text", "mathrm", "mathit")
#: The symbol commands this corpus actually uses, measured over its 100 items rather than imagined
#: (`\ce` 155, `\pu` 36, `\Delta` 21, `\circ` 19, `\log` 7, `\propto` 5, `\alpha`/`\beta` 5,
#: `\times` 2). Each maps to a token its Unicode spelling maps to as well, so the key and an answer
#: that writes the symbol meet in the middle. Mapped rather than deleted: deleting `\Delta` would
#: fold "ΔH" and "ΔG" onto each other's neighbourhood, and two options that differ only by a symbol
#: are exactly the pair a scorer must keep apart.
_SYMBOL_WORDS = {
    "circ": " deg ",
    "delta": " delta ",
    "alpha": " alpha ",
    "beta": " beta ",
    "times": " x ",
    "propto": " propto ",
}
_SYMBOL_CHARS = {
    "°": " deg ",
    "Δ": " delta ",
    "δ": " delta ",
    "α": " alpha ",
    "β": " beta ",
    "×": " x ",
    "∝": " propto ",
}
#: Sub- and superscript digits and signs, folded onto the ASCII the key writes as `^{2+}`.
_SCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-0123456789+-")
_COMMAND = re.compile(r"\\([a-zA-Z]+)|\\(.)")
_WHITESPACE = re.compile(r"\s+")


def _normalised(text: str) -> str:
    r"""The same string as markup-free lower-case text, so a key and an answer can be compared.

    **The corpus is ChemBench's raw markup and a model's answer is not.** 21 of the 100 keys here
    contain `\ce{}`, `\pu{}` or math mode, so an exact comparison against the option string was
    scoring *typography*: `materials_science:polymer_chemistry_19`'s key is
    `\ce{FeSO4} + t-butyl hydroperoxide`, the model's last line was `FeSO4 + t-butyl hydroperoxide`
    — the right answer — and the matcher missed it, fell through to its whole-answer fallback and
    credited a different option. A wrong answer recorded for a right one is worse than an
    abstention, because it moves the score in the flattering direction on the arm that happens to
    write plainer prose.

    Measured over this corpus with every option answered in the plain spelling a chemist uses:
    **88 of 418 options** were mis-scored before this and 0 after, which is the whole of the
    argument — the digits live in `tests/test_live_benchmark.py`, not here.

    Deliberately not a chemistry parser. It undoes typography — wrappers, braces, math delimiters,
    script digits, a handful of symbol commands — and nothing else, because anything that
    *interprets* a formula would make the score a property of this file, the same objection
    `_prompt` records.
    """

    def replace(match: re.Match[str]) -> str:
        command, escaped = match.group(1), match.group(2)
        if escaped is not None:
            return escaped
        if command in _MARKUP_WRAPPERS:
            return ""
        return _SYMBOL_WORDS.get(command.lower(), f" {command.lower()} ")

    text = _COMMAND.sub(replace, text)
    for char, word in _SYMBOL_CHARS.items():
        text = text.replace(char, word)
    text = text.translate(_SCRIPT_DIGITS)
    text = re.sub(r"[{}$^_]", "", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def _needle(option: str) -> str:
    r"""One option as a pattern: normalised, escaped, and tolerant of how it is spaced.

    Whitespace becomes `\s*` rather than being deleted, which is the difference between tolerating
    `\pu{53.2 L}` answered as `53.2L` and deleting the boundaries the lookarounds below depend on.
    """
    return r"\s*".join(re.escape(part) for part in _normalised(option).split(" ") if part)


def _chosen(answer: str, options: list[str]) -> str:
    r"""Which option the answer names, or `''` when it names none.

    **The last line first, and the whole answer only as a fallback**, because a model that reasons
    before answering writes every wrong option into its own working. Measured on the first five
    questions of this subset against a real gateway: scanning the whole answer scored the option
    `"5"` as `"1"` and `"4"` as `"2"`, matching digits inside sentences like "approximately 1.07%".
    A scorer that reads the reasoning instead of the answer measures itself.

    **Longest option first**, because one option is routinely a prefix of another ("Only Ag+ is
    present" against "Ag+ is present, and Pb2+ may be present") and a shortest-first scan credits
    the wrong one.

    **Word-boundary matching**, and a decimal point is not a boundary: the option `"1"` must not
    match inside `"1.07"`, while `"n-alkanes"` must still match at the end of a sentence. So a `.`
    blocks only where a digit is on the other side of it — `(?<!\d\.)` before and `(?!\.\d)`
    after — which is the difference between a decimal and a full stop. The leading guard was
    `(?<!\.)`, blocking *any* preceding period, so an option after a full stop
    (`"…done.n-alkanes"`) scored as an abstention while this paragraph described the symmetric
    rule.
    Substring-with-boundaries rather than equality, because a model asked for an option's text
    routinely returns it inside a sentence, and scoring that as an abstention would measure
    formatting rather than chemistry.
    """
    lines = [line for line in answer.strip().splitlines() if line.strip()]
    for haystack in ([lines[-1]] if lines else []) + [answer]:
        normalised = _normalised(haystack)
        for option in sorted(options, key=lambda opt: len(_normalised(opt)), reverse=True):
            needle = _needle(option)
            if needle and re.search(rf"(?<!\w)(?<!\d\.){needle}(?!\w)(?!\.\d)", normalised):
                return option
    return ""


async def _ask(
    client: httpx.AsyncClient, question: BenchmarkQuestion, profile: str | None
) -> tuple[str, str]:
    """Ask one question on its own session; return `(answer text, error code)`.

    One session per question, unlike the probe corpus's scripted follow-ups: these items are
    independent, and a shared thread would let one question's answer condition the next — which is
    a different experiment and a contaminated one.

    **The error event is read because this reader used to drop it**, and dropping it is not
    neutral between the arms: `api/runner.py` turns every failure into an `ErrorEvent`, so a loop
    cap, a spend cap, a connector outage or a degraded capability left `answer=""` and was scored
    as the model declining to name an option. Only the arm that binds tools can produce most of
    those. The code is carried per question so the three outcomes can be told apart after the run
    rather than argued about.
    """
    session_id = await open_session(client, profile=profile)
    answer, error_code = "", ""
    async with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": _prompt(question)}
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if event.get("type") == "answer":
                answer = str(event.get("text", ""))
            elif event.get("type") == "error":
                error_code = str(event.get("code", "") or "internal")
    return answer, error_code


async def _run(
    base_url: str, questions: list[BenchmarkQuestion], profile: str | None
) -> list[Answered]:
    """Ask every question and score what came back."""
    timeout = httpx.Timeout(settings.live_probe_timeout_seconds)
    results: list[Answered] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
        for question in questions:
            answer, error_code = await _ask(client, question, profile)
            chosen = _chosen(answer, question.options)
            results.append(
                Answered(
                    question_id=question.id,
                    category=question.category,
                    chosen=chosen,
                    correct=chosen == question.answer,
                    # A turn that failed is not a turn that declined, so it does not book as one.
                    unparsed=not chosen and not error_code,
                    error_code=error_code,
                )
            )
    return results


def render(results: list[Answered], profile: str | None) -> str:
    """The citable report: one accuracy, and the per-category breakdown behind it."""
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    unparsed = sum(1 for r in results if r.unparsed)
    errored = [r for r in results if r.error_code]
    arm = profile or "default"
    lines = [
        f"# ChemBench subset — {total} questions, profile `{arm}`",
        "",
        f"**{correct}/{total} correct ({correct / max(total, 1):.0%})**, "
        f"{unparsed} answer(s) named no option.",
        "",
        "| category | correct | asked | accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    if errored:
        # Stated on its own line and never folded into the abstentions: a turn the system failed
        # to run is not evidence about chemistry, and only the arm with tools bound can fail most
        # of these ways. Codes rather than a bare count, because `loop_cap_reached` and
        # `connector_unavailable` are different repairs.
        codes = ", ".join(
            f"{code} x{n}" for code, n in sorted(Counter(r.error_code for r in errored).items())
        )
        lines[2] += f" {len(errored)} turn(s) failed and answered nothing ({codes})."
    asked = Counter(r.category for r in results)
    right = Counter(r.category for r in results if r.correct)
    for category in sorted(asked):
        lines.append(
            f"| {category} | {right[category]} | {asked[category]} | "
            f"{right[category] / asked[category]:.0%} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Ask the benchmark of a running front door and print the score."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=settings.live_probe_base_url)
    parser.add_argument("--benchmark-dir", default=settings.benchmark_dir)
    parser.add_argument(
        "--profile",
        default=None,
        help="the agent profile to ask. `tools-removed` varies only the tools; `skills-removed` "
        "varies only the skills; `no-tools` varies the tools and the whole system prompt, so it "
        "answers a different question. Omitted, the front door's default agent",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="ask only the first N questions (0 = all)"
    )
    parser.add_argument("--out", default="", help="also write the report to this path")
    args = parser.parse_args(argv)

    questions = load_questions(args.benchmark_dir)
    if args.limit:
        questions = questions[: args.limit]
    try:
        results = asyncio.run(_run(args.base_url, questions, args.profile))
    except (httpx.HTTPError, OSError) as exc:
        print(f"could not reach the live lane ({exc}); nothing was scored")
        return 3

    report = render(results, args.profile)
    print(report, end="")
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
