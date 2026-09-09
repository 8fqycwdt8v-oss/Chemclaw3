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
from chemclaw.agent.chemclaw_agent import (
    _INSTRUCTION_BLOCKS,
    _INSTRUCTIONS,
    PromptBlock,
    advertised_tool_names,
    available_tool_names,
    instructions_for,
)
from chemclaw.agent.framing import ENVELOPE_TAG
from chemclaw.agent.profiles import get_profile
from chemclaw.cli.validate_prose_contract import (
    _ALLOWED_NON_TOOLS,
    check_instruction_blocks,
    check_metric_citations,
    check_operator_prose,
    check_prose_contract,
)
from chemclaw.core.tool_registry import registered_tool_names
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
        lambda: {"fake/SKILL.md": "Record it with `record_knowledge_note`, type `field-trial`."},
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


def test_the_shipped_documents_cite_only_declared_metric_names() -> None:
    """Rules 8-9 over the real tree: the regression guard for the defect that motivated them.

    `docs/decisions/D-2026-08-08-redaction-must-outlive-the-formatter.md` documented the only
    alert for the one *security* degradation in this codebase and named
    `chemclaw_degradations_total`, a counter that has never existed. Nothing failed, because a
    wrong series name renders as an alert that matches nothing and reads as healthy.
    """
    assert check_metric_citations() == []


def test_a_metric_name_no_registry_declares_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 8, over the operator corpus, in the backticked-span form."""
    monkeypatch.setattr(
        prose, "_operator_sources", lambda: {"fake.md": "Alert on `chemclaw_no_such_total`."}
    )
    monkeypatch.setattr(prose, "_selector_sources", dict)
    problems = check_metric_citations()
    assert len(problems) == 1
    assert "chemclaw_no_such_total" in problems[0]


def test_a_declared_metric_with_a_label_matcher_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The label matcher is part of the citation, not part of the name being resolved."""
    text = 'Alert on `chemclaw_degraded_total{subsystem="log_redaction"}`.'
    monkeypatch.setattr(prose, "_operator_sources", lambda: {"fake.md": text})
    monkeypatch.setattr(prose, "_selector_sources", lambda: {"fake.md": text})
    assert check_metric_citations() == []


def test_a_module_path_is_not_read_as_a_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """`chemclaw_agent.py` shares the prefix and is not a series.

    The span has to end at the name, which is what separates the two without an allow-list entry.
    """
    monkeypatch.setattr(
        prose, "_operator_sources", lambda: {"fake.md": "`chemclaw_agent.py` builds the agent."}
    )
    monkeypatch.setattr(prose, "_selector_sources", dict)
    assert check_metric_citations() == []


def test_a_selector_is_caught_in_a_document_rule_8_does_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 9's whole reason to exist: `docs/decisions/` is outside every other rule's corpus."""
    monkeypatch.setattr(prose, "_operator_sources", dict)
    monkeypatch.setattr(
        prose,
        "_selector_sources",
        lambda: {"docs/decisions/D-x.md": 'increments `chemclaw_gone_total{subsystem="x"}`'},
    )
    problems = check_metric_citations()
    assert len(problems) == 1
    assert "chemclaw_gone_total" in problems[0]


def test_rule_9_reads_the_decisions_and_leaves_the_archive_alone() -> None:
    """The corpus split, pinned: a merged ADR is checked for selectors, an archived document not.

    Measured on the tree this was written against: five backticked `chemclaw_*` spans in
    `docs/decisions/` are undeclared and four of them are correct prose — a module name, the
    Postgres role, a log marker, and an ADR quoting the stale metric name it exists to report.
    Which is why only the selector spelling gets this reach.
    """
    origins = prose._selector_sources()
    assert any(origin.startswith("docs/decisions/") for origin in origins)
    assert not any(origin.startswith("docs/archive/") for origin in origins)


def test_rule_9s_corpus_cannot_be_widened_by_build_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The corpus is the repository's documents, not whatever the working directory holds.

    It used to be `rglob("*.md")` from the root, so `make mutants` — which copies the whole tree
    into the gitignored `mutants/` — put a second copy of every document into the gate. Proven
    before the fix by dropping one probe file into `mutants/docs/guides/`:

        mutants/docs/guides/_probe.md: queries chemclaw_bogus_total{…}, which no registry declares
        1 prose/capability mismatch(es)

    `make ci` red on a path no commit contains, and the same hazard for any vendored checkout or
    tool cache at the root. The union costs no reach: 424 walked files became 290, and all 9
    documents that carry a selector are still in it.
    """
    (tmp_path / "docs" / "guides").mkdir(parents=True)
    (tmp_path / "docs" / "guides" / "real.md").write_text("real", encoding="utf-8")
    (tmp_path / "README.md").write_text("root", encoding="utf-8")
    (tmp_path / "mutants" / "docs" / "guides").mkdir(parents=True)
    (tmp_path / "mutants" / "docs" / "guides" / "real.md").write_text("copy", encoding="utf-8")
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    origins = prose._selector_sources()
    assert "docs/guides/real.md" in origins
    assert "README.md" in origins
    assert not any(origin.startswith("mutants/") for origin in origins), origins


def test_a_retired_metric_a_merged_adr_quotes_has_a_legal_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 9 reaches into `docs/decisions/`, where the fix rule 8 assumes is forbidden.

    Rule 8 stays out of that corpus precisely because "the fix would be editing a merged decision —
    which CLAUDE.md forbids". Rule 9 reaches in anyway, and six merged ADRs carry selectors today:
    simulated by dropping `chemclaw_repeated_tool_calls_total` from the declared set, the gate went
    red on D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice, with no edit anyone is allowed to
    make. `_RETIRED_METRIC_NAMES` is the remedy, and it was empty until a retirement needed it.

    **It has exactly one entry, and this assertion is what makes adding a second a reviewed act.**
    `chemclaw_note_proposals_total` is the PR-gate's by-state series, retired with the gate
    (`D-2026-09-05-the-gate-is-deleted-not-dormant`) and quoted in selector form by
    `D-2026-07-31-a-proposal-is-a-record-not-a-branch`, which is merged and therefore uneditable.
    The set is asserted by *value* rather than by size so a future entry has to be argued here.
    """
    assert prose._RETIRED_METRIC_NAMES == frozenset({"chemclaw_note_proposals_total"}), (
        "an entry here is a reviewed retirement"
    )
    text = 'the alert was `chemclaw_retired_total{subsystem="x"}`'
    monkeypatch.setattr(prose, "_operator_sources", dict)
    monkeypatch.setattr(prose, "_selector_sources", lambda: {"docs/decisions/D-x.md": text})
    assert len(check_metric_citations()) == 1, "the reach itself must stay"
    monkeypatch.setattr(prose, "_RETIRED_METRIC_NAMES", frozenset({"chemclaw_retired_total"}))
    assert check_metric_citations() == []


def test_the_non_metric_allowlist_is_small_and_deliberate() -> None:
    """One entry, and it is a namespace collision rather than a pardoned mistake."""
    assert prose._NON_METRIC_NAMES == frozenset({"chemclaw_app"})


def test_the_package_readmes_are_in_the_operator_corpus() -> None:
    """Every `src/chemclaw/*/README.md` is scanned, because a reader navigates by them.

    They were outside every gate until 2026-08-27. Scanning them for the first time found nine
    unresolvable pointers, including `agent/README.md`'s `workflows/` — a directory that has never
    existed under that package — beside a sentence advertising "the QM/DFT job" as a live connector
    bundle three weeks after `D-2026-08-26-semiempirical-is-the-whole-tier` deleted the tier.

    Asserted as a property of the corpus rather than only through `check_operator_prose() == []`,
    so narrowing the corpus fails here instead of quietly passing the rule that reads it.
    """
    sources = prose._operator_sources()
    readmes = sorted(
        str(path.relative_to(prose._ROOT))
        for path in (prose._ROOT / "src" / "chemclaw").rglob("README.md")
    )
    assert len(readmes) > 10, "no package READMEs found; the layout or the glob moved"
    assert set(readmes) <= set(sources), sorted(set(readmes) - set(sources))


def test_the_prose_gate_refuses_a_corpus_it_could_not_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules 5-9 filter out paths that do not exist, so a wrong `_ROOT` checks nothing and passes.

    Every corpus here is built by dropping entries whose file is absent (`if path.is_file()`), so
    an installed wheel, a vendored copy or a relocated package yields an empty corpus, zero ADR
    stems, and a green line reading "every named tool, note type, path, ADR id, config key and
    metric resolves" — over zero documents. The same silence applies one document at a time:
    renaming `SECURITY.md` simply stops it being checked. This module's own docstring warns against
    exactly that shape.

    The refusal names the missing documents rather than only the empty set, because losing one of
    `_OPERATOR_DOCS` is the realistic case and an aggregate count would not show it.
    """
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(module, "_ROOT", tmp_path)
    problems = module.check_corpus_is_assembled()
    assert problems, "a corpus of zero documents reported nothing wrong"
    assert any("README.md" in problem for problem in problems), problems
    assert any("docs/decisions" in problem for problem in problems), problems


def test_the_shipped_blocks_require_exactly_what_they_name() -> None:
    """Rule 10 over the committed blocks — the regression guard for the declaration itself."""
    assert check_instruction_blocks() == []


def test_a_block_that_names_a_tool_it_does_not_require_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect rule 10 exists for: a block that names a tool and declares nothing never drops.

    This is the original finding with one extra step. `_assemble` drops a block when the graph does
    not bind everything in `requires`, so a block whose `requires` is empty is sent to every
    deployment — and reads, in the declaration, exactly like one that would be dropped. Nothing
    else in this repository can see the difference: rules 1-2 are satisfied (`screen_hazards` is a
    real tool), `mypy` sees a `frozenset`, and the prompt is assembled at runtime from whatever is
    written here.
    """
    monkeypatch.setattr(
        prose,
        "_INSTRUCTION_BLOCKS",
        (_INSTRUCTION_BLOCKS[0], PromptBlock("Always call screen_hazards first. ")),
    )
    problems = check_instruction_blocks()
    assert problems, "a block naming a tool it does not require was reported as sound"
    assert "screen_hazards" in problems[0], problems


def test_a_block_requiring_a_tool_it_does_not_name_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, which is a defect too — and a silent one.

    A block requiring a tool its text never mentions vanishes from any deployment lacking that
    tool, and the prose gives a reader no reason why. Equality, not containment, is what makes both
    directions visible.
    """
    monkeypatch.setattr(
        prose,
        "_INSTRUCTION_BLOCKS",
        (PromptBlock("Cite the note id behind every claim. ", frozenset({"gather_evidence"})),),
    )
    problems = check_instruction_blocks()
    assert problems, "a block requiring a tool it never names was reported as sound"
    assert "gather_evidence" in problems[0], problems


def test_a_block_requiring_a_middleware_tool_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-117's shape from the other side: a name space the prompt is never narrowed against.

    `build_langgraph_agent` narrows against the tools it *binds* — registry, connectors, template
    launchers. The filesystem verbs, `write_todos` and `task` are attached by middleware
    afterwards, so a block requiring one of them passes rules 1-2 (they are real tools, and
    `available_tool_names` knows all six name spaces) and is then dropped from every deployment
    that exists. Loud rather than silent.
    """
    monkeypatch.setattr(
        prose,
        "_INSTRUCTION_BLOCKS",
        (PromptBlock("Use read_file on the path shown. ", frozenset({"read_file"})),),
    )
    problems = check_instruction_blocks()
    assert any("read_file" in problem and "middleware" in problem for problem in problems), problems


def test_the_prompt_a_graph_is_sent_names_no_tool_that_graph_cannot_call() -> None:
    """The property the blocks exist for, asserted against two real surfaces.

    Measured before the blocks: the default prompt named **16** tools a cold deployment binds
    nothing for — every calculator, the structure searches, `resolve_compound`, and
    `screen_hazards`, whose paragraph told the model to screen every proposed reagent against a
    hazard screen that was not there.

    **The bound half only.** This asserts what `instructions_for` produces, not the whole system
    message: `SkillsMiddleware` appends a skills listing carrying each skill's own declared tools,
    and that listing is narrowed by `skill_access`'s *advertised* names (the manifests) rather than
    by what a turn binds — so a deployment whose bundles are declared and unreachable is still
    offered the safety-screening skill. That is a second defect with the same shape and a different
    owner; naming it here is better than a test whose green line implies it was covered.
    """
    profile = get_profile("default")
    for available in (frozenset(registered_tool_names()), advertised_tool_names(profile)):
        text = instructions_for(profile, available)
        named = prose.referenced_tool_names(text)
        assert named <= available, sorted(named - available)


def test_the_maximal_prompt_is_what_a_validator_and_a_caller_still_get() -> None:
    """`available=None` is every block, so nothing that reads the prose sees a narrowed one.

    The validators, `tests/surface.py` and `AgentProfile`'s default all ask "what does this profile
    say" without a graph to ask about. A narrowed answer there would make the gate check less prose
    than a deployment is sent, which is this file's own failure mode.
    """
    profile = get_profile("default")
    maximal = instructions_for(profile)
    assert maximal == _INSTRUCTIONS
    for block in _INSTRUCTION_BLOCKS:
        if block.trail in (None, "durable"):
            assert block.text in maximal, block.text[:60]


def test_a_deployment_with_no_durable_trail_is_not_told_it_has_one() -> None:
    """F1's prompt half: the traceability paragraph follows the sink, not the wish.

    `default_audit_sink()` returns `NullAuditSink` on every deployment that has not set
    `session_store="postgres"` — the shipped `.env.example` — and the prompt asserted an
    append-only trail in the present tense regardless. The two texts are a pair rather than one
    text and its absence, because "how is this number defended" deserves an answer either way.
    """
    profile = get_profile("default")
    durable = instructions_for(profile, durable_trail=True)
    log_only = instructions_for(profile, durable_trail=False)
    assert "append-only audit trail" in durable
    assert "append-only audit trail" not in log_only
    assert "no durable audit trail here" in log_only
    assert "reproducibility, which is a different claim from integrity" in durable
    assert "reproducibility, which is a different claim from integrity" in log_only


def test_the_safety_floor_survives_narrowing_to_a_surface_with_no_tools_at_all() -> None:
    """A block carrying a floor sentence requires nothing — measured, not intended.

    **This caught a regression the blocks themselves introduced.** The envelope rule — half of the
    two-part prompt-injection defense (`agent/framing.py`) — sat in the same paragraph as the
    `record_knowledge_note` instruction, so the block required two side-effecting tools, and
    `agent/subagents.py` subtracts exactly those from the one helper this deployment builds.
    Measured on the compiled helper graph: 24 tools bound and **no envelope rule in its prompt**,
    while `tests/test_framing.py` stayed green because it reads the maximal text.

    The empty surface is the right probe: no real profile is that narrow, and a floor that survives
    it survives every narrowing that can occur. `_SAFETY_RULES` covers the profiles that replace
    the prose entirely; this covers the ones that keep it and lose tools.
    """
    profile = get_profile("default")
    floor = instructions_for(profile, frozenset())
    assert f"<{ENVELOPE_TAG}>" in floor, "the envelope rule is behind a tool this graph may lack"
    assert "'Refused:'" in floor
    assert "Earlier tool result dropped" in floor
    assert "What this system does not hold" in floor
