"""The tool-utility A/B: what the control arm is, and what a paired score may not absorb.

- The **scale** orders `fabricated` below `unserved`, so inventing an answer cannot tie with
  declining.
- The **pairing** drops a grader failure and refuses a missing arm, so different question sets are
  never compared.
- The **control arm** really builds an agent with no tools, and is not in the shipped profile set.
"""

import subprocess
from pathlib import Path
from typing import Literal

import pytest

from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.profile_discovery import _load
from chemclaw.agent.profiles import DEFAULT_PROFILE
from chemclaw.agent.skill_access import skill_permits
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

#: The shipped skill names, read off the tree so a new skill is covered the day it lands.
_SHIPPED_SKILL_NAMES = sorted(
    path.parent.name for path in (_REPO_ROOT / "skills").glob("*/SKILL.md")
)


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
    """A fabricated answer scores below a refusal.

    Tool augmentation introduces its own error class; scoring both at 0 would hide it in the delta.
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

    Asserted through `_capability_tools`, which `build_langgraph_agent` passes to `create_agent`.
    """
    profile = _load(_CONTROL_PROFILE)

    assert profile.name == "no-tools"
    assert profile.tool_names == frozenset()
    assert _capability_tools(profile) == []
    assert _capability_tools(profile) != _capability_tools(
        _load(_REPO_ROOT / "data/profiles/evidence.yaml")
    )


def test_the_control_arm_is_not_in_the_shipped_profile_set() -> None:
    """The control arm is a measurement instrument, so no deployment offers it by default.

    `data/profiles` is what every front door offers.
    """
    shipped = {path.stem for path in (_REPO_ROOT / "data/profiles").glob("*.yaml")}

    assert "no-tools" not in shipped
    assert _CONTROL_PROFILE.exists()
    assert "data/evals/profiles" not in settings.profiles_dirs


def test_the_control_arm_asks_the_front_door_for_its_profile_by_name() -> None:
    """`run_probe(profile=…)` reaches `POST /sessions` with the profile name.

    If the request omitted it, both arms would be the default agent. Driven through a mock front
    door that records the body.
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
        return httpx.Response(
            200,
            content=b'data: {"type": "answer", "text": "no."}\n\n',
            headers={"content-type": "text/event-stream"},
        )

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
    r"""`--sample N` draws across every section, at every N, including N above half.

    The corpus loads in section order, so a draw that is effectively `probes[:N]` would report a few
    user stories as the corpus. Both ends and the middle are asserted.
    """
    from chemclaw.cli.live_probes import _systematic_sample

    corpus = list(range(221))
    for n in (1, 2, 50, 110, 111, 150, 220):
        drawn = _systematic_sample(corpus, n)

        assert len(drawn) == n, f"n={n} drew {len(drawn)}"
        assert len(set(drawn)) == n, f"n={n} drew a duplicate"
        assert drawn == sorted(drawn), f"n={n} lost corpus order"
        # Cut the corpus into `n` equal-width bands: each must contribute exactly one probe, which
        # is what "spread across every section" means.
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


# ---------------------------------------------------------------------------------------------
# The control arm is a prompt contrast, checked against the documents.
#
# `no-tools.yaml` swaps the system prompt as well as the tools, so no document may read it as a
# tools-only contrast. The fixture tests cannot see the documents; these do.

_AB_ANCHORS = ("no-tools", "_AB_BASELINE_PROFILE", "live-ab", "--suite")
"""A line that names the A/B or its control arm. The baseline *is* `no-tools`, so a sentence about
`make live-ab` or the `--suite ab` run is a sentence about this profile whether or not it spells
the name."""

_TOOLS_CONTRAST = (
    "without tools",
    "with and without tools",
    "tool utility",
    "tool-utility",
    "tool augmentation",
    "removes every capability tool",
)
"""Phrases that describe the pairing as a comparison *about the tools*. Deliberately about the
comparison rather than about the fixture: `tool_names: []` and "toolless" are true of this profile
and are not the claim at issue, so neither is here — a trigger that fired on them would fire on
`test_the_control_arm_is_not_in_the_shipped_profile_set`, which is right about its own subject."""

_PROMPT_NAMED = ("prompt", "instructions")
"""What makes such a sentence honest: it says the other variable moved too."""

_WINDOW = 8
"""Lines either side. A comment block or a docstring in this tree is smaller than this, so a caveat
anywhere in the paragraph counts and one three screens away does not."""

_SCAN_SKIPS = (
    "docs/decisions/",  # merged records, never edited
    "docs/archive/",  # what was believed then, kept as it was written
    "tasks/",  # recorded run output; restating a transcript would falsify it
    "uv.lock",
)


def _document_lines() -> list[tuple[str, int, str]]:
    """Every tracked line, with its path and 1-based number.

    Tracked rather than walked: `make mutants` copies the whole tree into a gitignored directory,
    and `tests/test_prose_contract.py` records what that did to a corpus built with `rglob`.
    """
    listing = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    out: list[tuple[str, int, str]] = []
    for name in listing.split("\0"):
        if not name or name.startswith(_SCAN_SKIPS):
            continue
        try:
            text = (_REPO_ROOT / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.extend((name, i, line) for i, line in enumerate(text.splitlines(), start=1))
    return out


def test_no_live_document_reads_the_control_arm_as_a_tools_contrast() -> None:
    """No live document reads the control arm as a tools contrast.

    A site may describe the A/B as about tools only if the same paragraph says the prompt moved as
    well. The scan covers the whole tracked tree minus the immutable records, so new documents
    inherit the rule.
    """
    lines = _document_lines()
    assert len(lines) > 10_000, "the corpus is too small to have been the repository"

    by_file: dict[str, list[str]] = {}
    for name, _, line in lines:
        by_file.setdefault(name, []).append(line)

    offenders: list[str] = []
    for name, body in by_file.items():
        for index, line in enumerate(body):
            if not any(anchor in line for anchor in _AB_ANCHORS):
                continue
            window = "\n".join(body[max(0, index - _WINDOW) : index + _WINDOW + 1]).lower()
            claimed = [phrase for phrase in _TOOLS_CONTRAST if phrase in window]
            if claimed and not any(word in window for word in _PROMPT_NAMED):
                offenders.append(f"{name}:{index + 1}: reads as {claimed} — {line.strip()[:70]}")

    assert not offenders, (
        "these sites describe the `no-tools` A/B as a result about the tools without saying the "
        "prompt moved too, which is the attribution D-2026-09-14-tools-were-never-the-variable "
        "withdrew:\n  " + "\n  ".join(offenders)
    )


def test_the_control_arm_labels_itself_as_a_prompt_contrast() -> None:
    """The control arm's profile labels itself as a prompt contrast.

    It must say what it is not, and its list of what it still carries must name the system prompt it
    replaces, the largest thing the arm changes.
    """
    header = "\n".join(
        line
        for line in _CONTROL_PROFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ).lower()

    assert "prompt contrast" in header
    assert "not a tools contrast" in header
    assert "d-2026-09-14-tools-were-never-the-variable" in header
    assert "tools-removed.yaml" in header, "the arm that can carry a tools claim is not named"
    carried = header.split("what it still carries", 1)
    assert len(carried) == 2, "the header no longer states what the arm still carries"
    assert "prose" in carried[1], (
        "the paragraph that exists so the control arm does not overstate itself still omits the "
        "system prompt, which is the largest thing this arm changes"
    )


_SKILLS_CONTROL_PROFILE = _REPO_ROOT / "data/evals/profiles/skills-removed.yaml"


def test_the_skills_arm_keeps_every_tool_and_reaches_no_skill() -> None:
    """The skills arm keeps every tool and reaches no skill.

    Keeping every tool is what makes the delta attributable to skills; the toolless arms cannot
    isolate skills, since `ToolScopedSkills` drops a skill whose tools are gone. Asserted against
    the permit predicate `skills_backend` composes, not the YAML.
    """
    profile = _load(_SKILLS_CONTROL_PROFILE)
    permits = skill_permits(
        enabled=None, declared={}, available=[], gates=None, names=profile.skill_names
    )
    unnarrowed = skill_permits(
        enabled=None, declared={}, available=[], gates=None, names=DEFAULT_PROFILE.skill_names
    )

    assert profile.name == "skills-removed"
    assert profile.skill_names == frozenset()
    # Every discovered skill is refused on both tiers; `ProfileScopedSkills` is in the stored
    # narrowing too, so no tier leaks a skill into this arm.
    assert [name for name in _SHIPPED_SKILL_NAMES if permits.filed(name)] == []
    assert [name for name in _SHIPPED_SKILL_NAMES if permits.stored(name)] == []
    # And the same predicate with `skill_names` unset refuses none of them — which is what makes
    # the empty set the narrowing rather than the default. `None` here would be `default` renamed.
    assert [name for name in _SHIPPED_SKILL_NAMES if not unnarrowed.filed(name)] == []
    # The tool surface is untouched: this arm narrows nothing a tool gate can see.
    assert profile.tool_names is None
    assert _capability_tools(profile) == _capability_tools(DEFAULT_PROFILE)


def test_the_skills_arm_is_not_in_the_shipped_profile_set() -> None:
    """A measurement instrument, like the two controls beside it — not a capability to pick."""
    shipped = {path.stem for path in (_REPO_ROOT / "data/profiles").glob("*.yaml")}

    assert "skills-removed" not in shipped
    assert _SKILLS_CONTROL_PROFILE.exists()
