"""`python -m chemclaw.cli.live_benchmark` — score this system on a benchmark somebody else wrote.

Every `make eval` number is first-party; an external benchmark gives one that compares with other
systems. ChemBench items are keyed (`target_scores` name one correct option), so scoring is a
comparison, not a model-graded judgement, and inherits no judge noise.

It does not measure the tools: multiple-choice questions are answered from the model's knowledge, so
the score is a floor; `make live-ab` over `data/evals/probes/` measures the tools. Of the two
control arms, `--profile tools-removed` removes every capability tool and nothing else, so its
difference is attributable to the tools; `--profile no-tools` also replaces the system prompt, so
its difference is a prompt effect (`D-2026-09-14-tools-were-never-the-variable`).

Exit codes: 0 when the run completed, 3 when the lane could not be reached (never counted as a
pass).
"""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.markdown import render_table
from chemclaw.evals.live import decoded_events, open_session


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
    # An answer that named no option: an abstention, kept apart from a wrong guess.
    unparsed: bool = False
    # The `ErrorCode` of a turn that failed, or `""` for one that answered. A third outcome, not an
    # abstention: only the tool-bearing arm can hit most failures (caps, connector outages), so
    # folding them into abstentions would flatter the control. An errored turn is `correct=False`
    # and not `unparsed`.
    error_code: str = ""


def load_questions(directory: str) -> list[BenchmarkQuestion]:
    """The vendored subset, checked against the checksum its manifest records.

    A benchmark whose questions changed would compare two different things.
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

    Deliberately plain, so the number belongs to the deployment rather than to this file's prompt
    engineering.
    """
    options = "\n".join(f"- {option}" for option in question.options)
    return (
        f"{question.question}\n\nChoose exactly one of these options and reply with that option's "
        f"text and nothing else:\n{options}"
    )


# : The mhchem/`siunitx` wrappers whose contents are the chemistry (`\ce{FeSO4}` is `FeSO4`):
# : the command goes, the argument stays.
_MARKUP_WRAPPERS = ("ce", "pu", "text", "mathrm", "mathit")
# : The symbol commands this corpus uses, each mapped to the token its Unicode spelling also maps
# : to so a key and an answer meet. Mapped rather than deleted, so options that differ only by a
# : symbol (ΔH vs ΔG) stay apart.
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

    Corpus keys carry ChemBench's raw markup (`\ce{}`, `\pu{}`, math mode) and model answers do not,
    so an exact match would score typography and could credit the wrong option. This undoes
    typography only — wrappers, braces, math delimiters, script digits, a few symbol commands — and
    never interprets a formula. `tests/test_live_benchmark.py` holds the corpus-wide check.
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

    Whitespace becomes `\s*` rather than being deleted, keeping the boundaries the lookarounds need.
    """
    return r"\s*".join(re.escape(part) for part in _normalised(option).split(" ") if part)


def _chosen(answer: str, options: list[str]) -> str:
    r"""Which option the answer names, or `''` when it names none.

    The last line first, the whole answer only as a fallback, since reasoning mentions wrong
    options. Longest option first, since one option is often a prefix of another. Matching is
    substring-with-boundaries (a model returns the option inside a sentence), and a `.` blocks a
    match only when a digit is on its other side — `(?<!\d\.)` before and `(?!\.\d)` after — so
    `"1"` does not match inside `"1.07"` but an option after a full stop still matches.
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

    One session per question, so answers cannot condition each other. The error event is read
    because a failed turn (cap, connector outage) leaves no answer and must not be scored as an
    abstention.
    """
    session_id = await open_session(client, profile=profile)
    answer, error_code = "", ""
    # `evals.live.decoded_events` is the one reader of the wire format.
    async with aconnect_sse(
        client, "POST", f"/sessions/{session_id}/messages", json={"message": _prompt(question)}
    ) as source:
        source.response.raise_for_status()
        async for event in decoded_events(source):
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
    ]
    if errored:
        # On its own line, never folded into abstentions: a failed turn is not evidence about
        # chemistry. Codes, not a count, since each needs a different repair.
        codes = ", ".join(
            f"{code} x{n}" for code, n in sorted(Counter(r.error_code for r in errored).items())
        )
        lines[2] += f" {len(errored)} turn(s) failed and answered nothing ({codes})."
    asked = Counter(r.category for r in results)
    right = Counter(r.category for r in results if r.correct)
    lines.append(
        render_table(
            ["category", "correct", "asked", "accuracy"],
            [
                [
                    category,
                    str(right[category]),
                    str(asked[category]),
                    f"{right[category] / asked[category]:.0%}",
                ]
                for category in sorted(asked)
            ],
            align="lrrr",
        )
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
