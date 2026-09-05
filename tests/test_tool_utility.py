"""The tool-utility A/B: what the control arm actually is, and what a paired score may not absorb.

Three separate claims, because each fails differently:

- The **scale** orders the judge's verdicts the way the comparison needs, `fabricated` below
  `unserved`. A scale that scored them level would report the failure mode this measurement exists
  to find (tools give a model something to invent with) as a tie.
- The **pairing** drops a grader failure and refuses a missing arm. Those are opposite decisions
  about superficially similar inputs, and getting the second one wrong silently compares different
  question sets.
- The **control arm** is a real narrowing rather than a name: the profile that stands for "no
  tools" must actually build an agent with none, and it must not be in the shipped profile set.
"""

from pathlib import Path
from typing import Literal

import pytest

from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.profile_discovery import _load
from chemclaw.core.config import settings
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe
from chemclaw.evals.tool_utility import (
    VERDICT_SCORES,
    UnpairedProbe,
    by_bucket,
    paired_tasks,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTROL_PROFILE = _REPO_ROOT / "data/evals/profiles/no-tools.yaml"


def _probe(probe_id: str, bucket: Literal["A", "B", "C"] = "A") -> Probe:
    return Probe(
        id=probe_id,
        section=1,
        persona="lab_technician",
        bucket=bucket,
        question="what is the melting point of benzoic acid?",
        direction="a number with its provenance",
    )


def _verdict(probe_id: str, verdict: str) -> Judgement:
    return Judgement(probe_id=probe_id, verdict=verdict)  # type: ignore[arg-type]


def test_a_fabricated_answer_scores_below_a_refusal() -> None:
    """The one ordering the comparison is *for*, not a general preference about verdicts.

    ChemToolAgent's finding is that tool augmentation introduces its own error class. If
    `fabricated` and `unserved` were both 0 the arm that invented a citation and the arm that
    declined would produce an identical delta, and the A/B would be unable to report the harm it
    was built to look for.
    """
    assert VERDICT_SCORES["fabricated"] < VERDICT_SCORES["unserved"]
    assert VERDICT_SCORES["unserved"] < VERDICT_SCORES["partial"] < VERDICT_SCORES["served"]


def test_an_ungraded_pair_is_dropped_and_named() -> None:
    """A grader failure is evidence about the grader, so it leaves the comparison — visibly."""
    probes = [_probe("p1"), _probe("p2")]
    tasks, dropped = paired_tasks(
        probes,
        augmented={"p1": _verdict("p1", "served"), "p2": _verdict("p2", "ungraded")},
        baseline={"p1": _verdict("p1", "unserved"), "p2": _verdict("p2", "served")},
    )
    assert [task.task_id for task in tasks] == ["p1"]
    assert dropped == ["p2"]


def test_a_probe_missing_from_one_arm_is_refused_rather_than_dropped() -> None:
    """Unlike an ungraded verdict: a missing id means the arms asked different sets."""
    with pytest.raises(UnpairedProbe, match="baseline"):
        paired_tasks(
            [_probe("p1")],
            augmented={"p1": _verdict("p1", "served")},
            baseline={},
        )


def test_buckets_are_summarised_apart_because_they_ask_opposite_questions() -> None:
    """A gain on bucket A must not cancel a loss on bucket C — that averaging is the whole risk."""
    probes = [_probe("a1", "A"), _probe("c1", "C")]
    tasks, _ = paired_tasks(
        probes,
        augmented={"a1": _verdict("a1", "served"), "c1": _verdict("c1", "fabricated")},
        baseline={"a1": _verdict("a1", "unserved"), "c1": _verdict("c1", "served")},
    )
    summaries = by_bucket(probes, tasks)

    assert summaries["A"].helped == ["a1"]
    assert summaries["C"].hurt == ["c1"]
    # The aggregate is the sum of the two, which is why it must never be the only thing a report
    # prints: +1 on the bucket where tools should win, -2 where they fabricated a capability that
    # does not exist, and one number that says "tools cost something" without saying where.
    assert summaries["all"].net_delta == pytest.approx(-1.0)
    assert "B" not in summaries


def test_the_control_arm_builds_an_agent_with_no_capability_tools() -> None:
    """`tool_names: []` is structural: the compiled graph never holds the tools.

    Asserted through `_capability_tools`, the function `build_langgraph_agent` passes to
    `create_agent`, rather than through the YAML — the file saying `[]` is a claim, and this is the
    thing the claim is about.
    """
    profile = _load(_CONTROL_PROFILE)

    assert profile.name == "no-tools"
    assert profile.tool_names == frozenset()
    assert _capability_tools(profile) == []
    assert _capability_tools(profile) != _capability_tools(
        _load(_REPO_ROOT / "data/profiles/evidence.yaml")
    )


def test_the_control_arm_is_not_in_the_shipped_profile_set() -> None:
    """It is a measurement instrument, so no deployment advertises it without asking.

    `data/profiles` is what `CHEMCLAW_PROFILES_DIR` defaults to and what every front door offers;
    a toolless agent reachable by name from a session request is not a capability anybody should
    get by picking it off a list.
    """
    shipped = {path.stem for path in (_REPO_ROOT / "data/profiles").glob("*.yaml")}

    assert "no-tools" not in shipped
    assert _CONTROL_PROFILE.exists()
    assert "data/evals/profiles" not in settings.profiles_dirs


def test_the_control_arm_asks_the_front_door_for_its_profile_by_name() -> None:
    """The wiring the whole comparison rests on: `run_probe(profile=…)` must reach `POST /sessions`.

    Driven through a mock front door that records the body, because everything else here is
    arithmetic over verdicts — if this one call sent `{}` the two arms would be the same agent and
    every number in the report would be a comparison of the default profile with itself. The
    control arm being *structurally* toolless (asserted above) is worth nothing if the request
    never names it.
    """
    import asyncio
    import json

    import httpx

    from chemclaw.evals.live import run_probe

    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            bodies.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=b'data: {"type": "answer", "text": "no."}\n\n')

    async def go() -> tuple[str, str]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            control = await run_probe(client, _probe("p1"), profile="no-tools")
            default = await run_probe(client, _probe("p2"))
        return control.profile, default.profile

    control_profile, default_profile = asyncio.run(go())

    assert bodies == [{"profile": "no-tools"}, {}]
    # And the transcript says which arm it came from, so the two halves stay tellable apart once
    # they are files rather than variables.
    assert (control_profile, default_profile) == ("no-tools", "")


def test_a_sample_is_spread_across_the_corpus_rather_than_its_first_n() -> None:
    r"""`--sample N` must draw across every section, at every N — including N above half.

    The claim is the point of the flag: the corpus loads in file order, which is section order, so
    a draw that is really `probes[:N]` asks one or two user stories' questions and reports them as
    a reading of the corpus. The first implementation was `probes[::len(probes) // n][:n]`, which
    is that exact anti-pattern for every `n > len/2` — an integer stride of 1 makes the slice a
    no-op — and dropped the tail sections below it, because truncating a strided list cuts from the
    end. Both ends and the degenerate middle are asserted here rather than the value that motivated
    the fix, per this repository's own lesson about testing a bound at one end only.
    """
    from chemclaw.cli.live_probes import _systematic_sample

    corpus = list(range(221))
    for n in (1, 2, 50, 110, 111, 150, 220):
        drawn = _systematic_sample(corpus, n)

        assert len(drawn) == n, f"n={n} drew {len(drawn)}"
        assert len(set(drawn)) == n, f"n={n} drew a duplicate"
        assert drawn == sorted(drawn), f"n={n} lost corpus order"
        # **Every stratum is represented, which is the property rather than a proxy for it.** Cut
        # the corpus into `n` equal-width bands and each must contribute exactly one probe — that
        # is what "spread across every section" means, and it is what the strided version broke at
        # both ends (stride 1 put every draw in the first bands; truncation emptied the last).
        # A first attempt asserted "the last draw is in the final tenth" instead and was simply
        # wrong at n=2, where the correct stratified draw is the first probe of each half.
        bands = [int(i * len(corpus) / n) for i in range(n + 1)]
        for low, high, picked in zip(bands[:-1], bands[1:], drawn, strict=True):
            assert low <= picked < high, f"n={n}: {picked} is outside its band [{low}, {high})"
        # And it is never literally the first n — the failure this test exists for.
        if 1 < n < len(corpus) - 1:
            assert drawn != corpus[:n], f"n={n} collapsed to the first {n} probes"


def test_a_sample_is_reproducible_and_larger_than_the_corpus_is_the_corpus() -> None:
    """A sample is reproducible, and one larger than the corpus is the corpus.

    Two runs of one `--sample` ask the same questions, which is what makes a re-run a comparison;
    and asking for more probes than exist is not an error, it is everything.
    """
    from chemclaw.cli.live_probes import _systematic_sample

    corpus = list(range(37))

    assert _systematic_sample(corpus, 9) == _systematic_sample(corpus, 9)
    assert _systematic_sample(corpus, 37) == corpus
    assert _systematic_sample(corpus, 99) == corpus
