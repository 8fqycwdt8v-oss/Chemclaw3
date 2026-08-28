"""Skills were entirely uninstrumented, and the gate's own refusals were silent.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. `agent/skill_backend.py` and
`agent/skill_access.py` between them contained **zero** `logger.` calls and **zero** metric calls,
so
"the agent is not following the procedure" — a top-three support question — could not be answered
at its first step: *was the skill even offered, and did the model read it?*

The role gate lives on the backend because that is the enforcement point (deepagents publishes skill
*paths* into the system prompt, so filtering the listing alone leaves every hidden skill one guessed
path away). An enforcement point whose refusals are silent is a control nobody can audit.
"""

import logging
from pathlib import Path

import pytest

from chemclaw.agent.langgraph_agent import skills_backend
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.skill_backend import NarrowedSkillsBackend
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Two skills on disk — one the caller may reach and one it may not."""
    for name in ("solvent-selection", "restricted-procedure"):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: a skill\n---\n\nhow to do {name}\n"
        )
    return tmp_path


def test_a_skill_body_the_model_reads_is_named_in_the_log(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The second half of "was the skill even offered" — did the model actually read it.

    INFO because *which procedure the model opened* is the fact the support question turns on and
    it is reconstructible from nowhere else. The reason this docstring used to give — "a skill is
    read at most once per turn, not once per model call" — was a bound nothing enforces: `read` is
    a model-callable tool and a skill directory holds as many documents as its author put in it.
    """
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    with caplog.at_level(logging.INFO):
        result = backend.read("/solvent-selection/SKILL.md")

    assert result.error is None
    assert "skill.read" in caplog.text
    assert "solvent-selection" in caplog.text


def test_a_refused_read_is_counted_and_warned_where_it_used_to_be_silent(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate holds, and now says so.

    The message to the *model* still refuses to say whether the skill exists — distinguishing a
    gated skill from a typo would turn the gate into an enumeration oracle — so the operator-facing
    record names the path rather than claiming a skill by that name is there.
    """
    before = METRICS.value("chemclaw_skill_reads_denied_total")
    backend = NarrowedSkillsBackend(str(tree), lambda name: name != "restricted-procedure")

    with caplog.at_level(logging.WARNING):
        result = backend.read("/restricted-procedure/SKILL.md")

    assert result.file_data is None
    assert result.error is not None
    assert METRICS.value("chemclaw_skill_reads_denied_total") == before + 1
    assert "skill.read_denied" in caplog.text
    # The body never reaches the log, only the path that was asked for.
    assert "how to do restricted-procedure" not in caplog.text


def test_a_permitted_read_moves_no_denial_counter(tree: Path) -> None:
    """The negative case: a counter that also moved on success would report a permanent outage."""
    before = METRICS.value("chemclaw_skill_reads_denied_total")
    NarrowedSkillsBackend(str(tree), lambda _name: True).read("/solvent-selection/SKILL.md")
    assert METRICS.value("chemclaw_skill_reads_denied_total") == before


def test_the_build_records_which_skills_this_profile_offers(
    tree: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first half of the question: what the three predicates left, per profile.

    DEBUG because a graph is compiled per turn (M7) and per subagent, so at INFO this would be the
    loudest line in the process — and the question it answers is asked while debugging one session
    rather than while watching a fleet.
    """
    monkeypatch.setattr("chemclaw.agent.langgraph_agent._skill_dirs", lambda: [str(tree)])
    profile = AgentProfile(name="property-lookup", instructions="look things up")

    with caplog.at_level(logging.DEBUG, logger="chemclaw.agent.langgraph_agent"):
        skills_backend(profile, [])

    assert "skills.narrowed" in caplog.text
    assert "solvent-selection" in caplog.text


def test_a_model_authored_path_is_bounded_before_it_reaches_a_log_line(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`file_path` is the model's own tool argument, and nothing before this caps its length.

    So a megabyte-long or newline-laden path was written verbatim — at WARNING on the refusal and
    at INFO on the success — which is a model-authored string with unbounded reach into a log
    stack. This tree already has one answer for that (`audit.bounded_repr`, over
    `agent_audit_max_arg_chars`), and it reprs, so an embedded newline can no longer split one
    refusal record into two lines.

    Driven on the *refusal* branch because that is the one an attacker reaches without a permitted
    skill: the path need not exist for the gate to log it.
    """
    absurd = "/" + "A" * 5000 + "\ninjected-second-line: pretending to be a record\n/SKILL.md"
    backend = NarrowedSkillsBackend(str(tree), lambda _name: False)

    with caplog.at_level(logging.WARNING):
        backend.read(absurd)

    line = caplog.text
    assert absurd not in line, "the model's raw path reached the log unbounded"
    assert len(line) < 2 * settings.agent_audit_max_arg_chars + 500
    assert "\ninjected-second-line" not in line, "a newline in the path forged a second log record"
    # The path is still identifiable — bounding it must not turn the record into nothing.
    assert "AAAA" in line
