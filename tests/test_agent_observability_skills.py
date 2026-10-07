"""Skill offers, reads and refusals are logged and counted.

Decision: `D-2026-08-27-a-refusal-is-not-a-crash`. "Was the skill offered, and did the model read
it?" must be answerable. The role gate lives on the backend because deepagents publishes skill
paths into the prompt, and an enforcement point whose refusals are silent cannot be audited.
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

    INFO, because which procedure the model opened is reconstructible from nowhere else.
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
    """A refused read is counted and warned.

    The message to the model still does not say whether the skill exists (no enumeration oracle), so
    the operator record names the path rather than claiming a skill is there.
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

    DEBUG, because a graph is compiled per turn and per subagent; the line serves debugging one
    session.
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
    """`file_path` is the model's own tool argument, so it is bounded before it reaches a log line.

    Uses `audit.bounded_repr` over `agent_audit_max_arg_chars`, which also escapes newlines. Driven
    on the refusal branch, which an attacker reaches without a permitted skill.
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


def test_a_skill_the_model_reads_is_counted_by_name(tree: Path) -> None:
    """Which skill a turn actually used is counted by name.

    `chemclaw_skill_loads_total` is the evidence for ranking, promoting or retiring a skill.
    """
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    assert backend.read("/solvent-selection/SKILL.md").error is None

    assert METRICS.value("chemclaw_skill_loads_total") == before + 1
    assert 'chemclaw_skill_loads_total{skill="solvent-selection"}' in METRICS.render()


def test_a_refused_read_is_not_counted_as_a_load(tree: Path) -> None:
    """The two counters partition the *decided* reads; neither is the number of asks.

    A load counted beside the denial would make the series read as "asks", and the question it
    exists to answer is which procedure the model actually opened.
    """
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda name: name != "restricted-procedure")

    assert backend.read("/restricted-procedure/SKILL.md").error is not None

    assert METRICS.value("chemclaw_skill_loads_total") == before


def test_a_name_no_directory_backs_mints_no_series(tree: Path) -> None:
    """A name no skill directory backs mints no series.

    `skill` is the first segment of a model-written path, and `permits` returns True for anything
    when no gate is configured, so the label must be clamped. The tests below cover the cases a
    resolved-path check cannot see.
    """
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    assert backend.read("/not-a-skill-anybody-wrote/SKILL.md").error is not None

    assert METRICS.value("chemclaw_skill_loads_total") == before
    assert "not-a-skill-anybody-wrote" not in METRICS.render()


def test_a_file_beside_the_tree_is_not_a_skill(tree: Path) -> None:
    """A file beside the tree is not a skill.

    `skills/README.md` resolves inside `root_dir` but is not a skill directory; any top-level
    document would be the same, so the property is asserted rather than the one filename.
    """
    (tree / "README.md").write_text("what this tree holds\n", encoding="utf-8")
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    # It reads fine — this is a narrowing of what counts as evidence, not of what may be read.
    assert backend.read("/README.md").error is None

    assert METRICS.value("chemclaw_skill_loads_total") == before
    # The *series*, not the exposition: this counter's HELP text names `skills/README.md` as the
    # example it was written for, so a whole-render search matches the documentation and passes
    # nothing.
    assert 'chemclaw_skill_loads_total{skill="README.md"}' not in METRICS.render()


def test_a_read_that_asks_for_no_lines_is_not_a_load(tree: Path) -> None:
    """A read that asks for no lines is not a load.

    `limit=0` returns empty content with no error, so "it resolved" would over-count.
    """
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    result = backend.read("/solvent-selection/SKILL.md", limit=0)

    assert result.error is None and result.no_lines_requested
    assert METRICS.value("chemclaw_skill_loads_total") == before


def test_a_supporting_document_inside_a_skill_counts_for_that_skill(tree: Path) -> None:
    """The negative arm of the clamp: narrowing to `SKILL.md` alone would lose real reads.

    A skill is a *directory*, so a reference table or a worked example beside its `SKILL.md` is
    that skill being used. The clamp is "inside a skill directory", not "is the manifest".
    """
    (tree / "solvent-selection" / "hansen.md").write_text("a table\n", encoding="utf-8")
    before = METRICS.value("chemclaw_skill_loads_total")
    backend = NarrowedSkillsBackend(str(tree), lambda _name: True)

    assert backend.read("/solvent-selection/hansen.md").error is None

    assert METRICS.value("chemclaw_skill_loads_total") == before + 1
    assert 'chemclaw_skill_loads_total{skill="solvent-selection"}' in METRICS.render()
