"""The agent's prose must only name capability the agent has (gap IDEA-7).

This check exists because two shipped defects were the same shape and no gate saw either:
`skills/experiment-design/SKILL.md` pointed the agent at `BoCampaignWorkflow` (no tool exposed it),
and `skills/deep-research/SKILL.md` named `find_similar_reactions(...)` when the agent's actual MCP
tool is `similar_reactions` — so loading that skill taught the agent three tool names that would
fail at call time. `mypy` cannot see prose, `pytest` did not read it, and `make skill-validate` only
checks frontmatter.
"""

from pathlib import Path

import pytest

import chemclaw.cli.validate_prose_contract as prose
from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.cli.validate_prose_contract import (
    _ALLOWED_NON_TOOLS,
    check_operator_prose,
    check_prose_contract,
)
from chemclaw.kg.note import KNOWN_NOTE_TYPES


def test_shipped_prose_names_only_real_tools() -> None:
    """The committed skills + instructions pass — the regression guard itself."""
    assert check_prose_contract() == []


def test_mcp_tools_count_as_real() -> None:
    """MCP capability tools have no Python symbol, so they must come from the config allowlist."""
    names = available_tool_names()
    assert {"similar_molecules", "substructure_matches", "similar_reactions"} <= names


def test_in_process_tools_count_as_real() -> None:
    """The function tools registered on the agent are recognised too."""
    assert {"gather_evidence", "expand_note", "predict_pka"} <= available_tool_names()


def test_a_made_up_tool_is_caught(tmp_path: object, monkeypatch: object) -> None:
    """A skill naming a nonexistent tool fails the check — the `find_similar_reactions` case."""
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "Call `retrosynthesize(target)` to plan a route."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "retrosynthesize" in problems[0]


def test_pointing_the_agent_at_a_workflow_is_caught(monkeypatch: object) -> None:
    """The `BoCampaignWorkflow` case: the agent cannot invoke a workflow class, only a tool."""
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "For many rounds reach for the durable `BoCampaignWorkflow`."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "BoCampaignWorkflow" in problems[0]
    assert "cannot invoke" in problems[0]


def test_a_note_type_the_graph_does_not_know_is_caught(monkeypatch: object) -> None:
    """The `experiment-batch` case (D-164): reachable tool, unwritable artifact.

    Two shipped skills told the agent to propose a `protocol` / `experiment-batch` note. Both
    calls succeed and open a branch; `kg-validate` then rejects it on the PR the agent just
    created. Rule 1 could not see it — the tool name was real, only the type was not.
    """
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "Record it with `propose_knowledge_note`, type `field-trial`."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "field-trial" in problems[0]


def test_a_known_note_type_passes() -> None:
    """The rule must not fire on the types the graph does mint, or prose cannot name them."""
    import chemclaw.cli.validate_prose_contract as module

    for note_type in ("reaction", "experiment-proposal", "optimization-campaign"):
        assert module.referenced_note_types(f"write it as type `{note_type}`") <= KNOWN_NOTE_TYPES


def test_the_rule_reads_note_types_not_every_backticked_word() -> None:
    """Narrow on purpose: this prose is full of backticked tools, fields and chemistry."""
    import chemclaw.cli.validate_prose_contract as module

    prose = "filter on `type` or `tag`, then call `expand_note`"
    assert module.referenced_note_types(prose) == set()


def test_the_allowlist_is_small_and_deliberate() -> None:
    """The escape hatch must stay a review decision, not a dumping ground."""
    assert len(_ALLOWED_NON_TOOLS) <= 3


# Built from a variable rather than written inline: `tests/test_docstring_paths.py` scans this file
# too, and a literal backticked path that does not resolve is exactly what it fails on — which is
# the same rule, one corpus over.
_MISSING = "/".join(("vanished", "module.py"))
# A second one, for the Makefile tests below that need two distinct nonexistent paths in the same
# fixture (one in a comment, one in a recipe command) to tell which was scanned. Same reason as
# `_MISSING`: joined, not written contiguously, so `test_docstring_paths.py`'s whole-file scan
# does not read it as a dangling pointer of its own.
_MISSING_RECIPE = "/".join(("nonexistent", "recipe.py"))


def test_the_shipped_operator_documents_name_only_things_that_exist() -> None:
    """Rules 5-7 over the docs a human operates from — the state this PR had to reach.

    A verification pass found 40 mismatches here: module paths dead since the D-148 package move,
    a `.github/workflows/deploy.yml` that never existed, and ADR ids with no file. None was visible
    to any gate, which is why they had accumulated across five documents.
    """
    assert check_operator_prose() == []


def test_a_path_that_does_not_exist_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule that carries the whole class: a moved module leaves prose pointing at nothing."""
    monkeypatch.setattr(
        prose,
        "_operator_sources",
        lambda: {"fake.md": f"The audit sink lives in `{_MISSING}` today."},
    )
    problems = check_operator_prose()
    assert problems == [f"fake.md: names `{_MISSING}`, which does not exist"]


def test_a_real_path_passes_from_any_of_the_three_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repo root, `src/chemclaw/`, and the chart — all three are used and each is unambiguous."""
    monkeypatch.setattr(
        prose,
        "_operator_sources",
        lambda: {
            "fake.md": "See `deploy/README.md`, `agent/audit.py` and `templates/podmonitor.yaml`."
        },
    )
    assert check_operator_prose() == []


def test_a_bare_filename_is_not_read_as_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SKILL.md` and `connector.yaml` are nouns in this prose, not references to one file.

    Requiring a `/` is what keeps the rule from demanding that every filename-shaped word resolve —
    the difference between a check that is true and one that has to be argued with.
    """
    monkeypatch.setattr(
        prose,
        "_operator_sources",
        lambda: {"fake.md": "Each bundle ships a `connector.yaml` and one `SKILL.md` per skill."},
    )
    assert check_operator_prose() == []


def test_a_placeholder_path_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose has to be able to say "put it here" without naming a file that exists."""
    monkeypatch.setattr(
        prose,
        "_operator_sources",
        lambda: {"fake.md": "Add `ingest/sources/<name>/datasource.yaml` and `*/SKILL.md`."},
    )
    assert check_operator_prose() == []


def test_an_adr_id_with_no_file_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """A citation that resolves to nothing is worse than none: it looks like provenance."""
    monkeypatch.setattr(prose, "_operator_sources", lambda: {"fake.md": "As decided in D-999."})
    problems = check_operator_prose()
    assert len(problems) == 1
    assert "cites D-999" in problems[0]


def test_a_sub_decision_label_an_adr_defines_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`D-A5a` is a real label inside `D-048`, and prose should be able to name it.

    This is the clause that keeps the rule honest rather than merely strict. The first version
    rejected every `D-A*` token, which would have forced the docs to drop a label that says
    precisely which half of a two-part decision is meant. Derived by scanning the ADRs, so an
    invented label is still caught.
    """
    monkeypatch.setattr(
        prose, "_operator_sources", lambda: {"fake.md": "ADR **D-048** (Teilentscheidung D-A5a)."}
    )
    assert check_operator_prose() == []

    monkeypatch.setattr(prose, "_operator_sources", lambda: {"fake.md": "See ADR D-A77b."})
    assert len(check_operator_prose()) == 1


def test_a_config_key_that_is_not_a_setting_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prophylactic, and cheap: 60 keys across the docs are correct and nothing was holding them."""
    monkeypatch.setattr(
        prose, "_operator_sources", lambda: {"fake.md": "Set `CHEMCLAW_NOT_A_REAL_KEY=1`."}
    )
    problems = check_operator_prose()
    assert problems == ["fake.md: names CHEMCLAW_NOT_A_REAL_KEY, which is not a Settings field"]


def test_a_prefix_written_in_prose_is_not_read_as_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CHEMCLAW_SERVICE_*` names a family, and the trailing underscore is not a key ending."""
    monkeypatch.setattr(
        prose, "_operator_sources", lambda: {"fake.md": "The `CHEMCLAW_SERVICE_*` keys bound it."}
    )
    assert check_operator_prose() == []


def test_the_planning_documents_are_deliberately_out_of_scope() -> None:
    """Stated as a test because it is a decision that must not be undone by accident.

    Turning these rules on over the planning directory reports 175 further mismatches, and they
    are a different defect: a ticket that says "create the QM tools module" names a file D-118
    later deleted, so there is no path to correct it to — the sentence needs rewording.
    Mechanically rewriting each to the nearest surviving module would falsify the build record the
    tickets exist to be.
    """
    assert not any("docs/planning/" in origin for origin in prose._operator_sources())
    assert not any("docs/decisions/" in origin for origin in prose._operator_sources())
    assert not any("docs/archive/" in origin for origin in prose._operator_sources())


def test_makefile_and_env_example_join_the_operator_corpus() -> None:
    """F17: both were outside `_OPERATOR_DOCS`.

    Exactly how `.env.example:3` naming the pre-split, bare-filename `config.py` survived a gate
    built to catch precisely that.
    """
    sources = prose._operator_sources()
    assert "Makefile" in sources
    assert ".env.example" in sources


def test_a_makefile_recipe_command_is_not_read_as_prose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recipe command's backtick is shell command substitution, not a code span.

    Only the actual shell command line is excluded, so a recipe naming a path that does not
    exist — the shape a future `helm template` invocation could take — must not be reported.
    """
    (tmp_path / "Makefile").write_text(
        f"# comment mentioning `{_MISSING}`\nlint:\n\techo `{_MISSING_RECIPE}`\n"
    )
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    sources = prose._operator_sources()
    assert _MISSING in sources["Makefile"]
    assert _MISSING_RECIPE not in sources["Makefile"]


def test_a_makefile_atsign_comment_is_read_as_prose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recipe's `@#`-prefixed line is a real comment.

    Shell treats `#...` as inert too, so the prose after it — including the
    `helm-validate`/`deps-audit` rationale blocks — is scanned.
    """
    (tmp_path / "Makefile").write_text(f"target:\n\t@# see `{_MISSING}`\n")
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    sources = prose._operator_sources()
    assert _MISSING in sources["Makefile"]


def test_a_targets_trailing_help_text_is_read_as_prose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`target: ## help text (D-999)` is Make syntax, never handed to a shell.

    The case that makes scoping to bare `#`-only lines the wrong rule: `explain: ## ... (D-166)`
    is exactly this shape in the shipped `Makefile` and is a real citation, not a recipe.
    """
    (tmp_path / "Makefile").write_text(f"explain:  ## see `{_MISSING}`\n\tuv run true\n")
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    sources = prose._operator_sources()
    assert _MISSING in sources["Makefile"]


def test_env_example_is_read_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`.env.example` has no comment marker to strip.

    It is comments plus `KEY=VALUE` throughout, so every line, backticked or not, is scanned.
    """
    text = f"# maps to `{_MISSING}`\nCHEMCLAW_LOG_LEVEL=INFO\n"
    (tmp_path / ".env.example").write_text(text)
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    sources = prose._operator_sources()
    assert sources[".env.example"] == text
