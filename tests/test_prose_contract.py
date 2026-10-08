"""The agent's prose must only name capability the agent has.

`mypy` cannot see prose and `make skill-validate` only checks frontmatter, so a skill or tool
description naming a tool, workflow or note type that does not exist would teach the model calls
that fail. These tests resolve every such name against the live surface.
"""

import re
from pathlib import Path

import pytest

import chemclaw
import chemclaw.cli.model_text_inventory as inventory
import chemclaw.cli.validate_prose_contract as prose
from chemclaw.agent.chemclaw_agent import (
    _INSTRUCTION_BLOCKS,
    _INSTRUCTIONS,
    _SAFETY_BLOCKS,
    PromptBlock,
    advertised_tool_names,
    available_tool_names,
    instructions_for,
    skill_tool_names,
    subagent_tool_names,
)
from chemclaw.agent.framing import ENVELOPE_TAG
from chemclaw.agent.profiles import get_profile
from chemclaw.cli.model_text_inventory import _marked_prose, model_facing_descriptions
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


#: The tier `D-2026-08-26-semiempirical-is-the-whole-tier` deleted, by the names it went under.
#: `conceptual[- ]DFT` is carved out: it names a shipped GFN2-xTB descriptor panel, a rule about a
#: word's meaning rather than an exemption for one sentence.
_A_REMOVED_TIER = re.compile(
    r"(?<!conceptual-)(?<!conceptual )\bDFT\b|\bHPC\b|Nextflow|Seqera|compute_dft_energy"
    r"|agents/job_status"
)

#: The deleted PR-gate, named by its machinery: PRs, the review queue, the knowledge gate.
_REVIEW_MACHINERY = re.compile(
    r"\bPR[- ]gate\b|pull requests?|\bPRs?\b|NoteProposal|review queue|knowledge gate",
    re.IGNORECASE,
)

#: A promise that something the model does waits for a person.
#:
#: `propose ... (what|it|them|the)` is not matched: most hits propose an experiment, which is wanted
#: behaviour, and the known defects are caught by `for review` and `pull requests?`. This is a
#: keyword guard with a recall limit: a paraphrase naming a "reviewer" passes, because widening to
#: reviewer/approve/sign-off vocabulary mostly matches correct text (including the real workflow
#: approval in `compose_workflow`).
_A_REVIEW_PROMISE = re.compile(
    r"for (?:review|approval|acceptance)|awaits? review|pending review", re.IGNORECASE
)

#: Shipped sentences that match a pattern above and are correct, each with why.
#:
#: Tense cannot discriminate: history about a removed tier is always past tense, including the
#: defect this guard was written for. So an exemption is a literal quote containing the match, keyed
#: by the one text it belongs to, and it must end at a clause boundary (end of text or punctuation)
#: so a span assembled across flattened line breaks cannot exempt anything.
#: `test_every_exemption_still_quotes_shipped_prose` fails when a quoted sentence is reworded.
_TRUE_ABOUT_WHAT_IS_GONE: dict[tuple[str, str], str] = {
    ("block:5", "never present one as if it were DFT"): (
        "The system prompt telling the model not to overclaim the method. Actionable, and the "
        "opposite of describing a tier as reachable."
    ),
    (
        "skill:computational-evidence",
        "There is no DFT tier and no cluster to send a calculation to",
    ): (
        "`skills/computational-evidence` saying the tier is absent, which is what "
        "`D-2026-08-26-semiempirical-is-the-whole-tier` asks prose to say."
    ),
    (
        "bundleskill:safety:safety-screening",
        "the PR gate over agent-written knowledge was deleted",
    ): (
        "`connectors/safety/skills/safety-screening` explaining why the screen is a tool and not "
        "a gate — the reader needs the removed control named to follow it."
    ),
    (
        "prose:chemclaw.durable.hypothesis_tournament:_DERIVE_CHECK",
        "There is no DFT and no cluster here",
    ): (
        "The discriminating-check prompt deciding `kind`: a check that would need a tier this "
        "system does not have is `physical`, which is the routing "
        "`D-2026-08-26-semiempirical-is-the-whole-tier` asks for rather than a tier described "
        "as reachable."
    ),
}

#: What may not follow an exempted quote: another word. Any punctuation ends a clause, including a
#: citation's opening bracket.
_A_WORD_CHARACTER = re.compile(r"\w")


def _exempt_spans(name: str, flat: str) -> list[tuple[int, int]]:
    """Where this text's own exemptions sit in it, clause boundary enforced."""
    spans = []
    for (owner, phrase), _ in _TRUE_ABOUT_WHAT_IS_GONE.items():
        if owner != name:
            continue
        for found in re.finditer(re.escape(phrase), flat):
            after = flat[found.end() :].lstrip()
            if after and _A_WORD_CHARACTER.match(after[0]):
                continue
            spans.append((found.start(), found.end()))
    return spans


def _first_offence(text: str, pattern: re.Pattern[str], name: str = "") -> str | None:
    """The first match in `name`'s text not covered by one of `name`'s exemptions, or `None`.

    Whitespace is flattened first so an exemption need not reproduce line wrapping; some shipped
    quotes span a line break.
    """
    flat = " ".join(text.split())
    exempt = _exempt_spans(name, flat)
    for match in pattern.finditer(flat):
        if any(start <= match.start() and match.end() <= end for start, end in exempt):
            continue
        return match.group(0)
    return None


#: The three classes the guards could not see before 2026-09-22, each with the loader that reaches
#: it. Parametrised rather than written out, so a fourth class is a row here and not a fourth copy
#: of the same driving.
_THE_CLASSES_THAT_WERE_OUTSIDE = (
    "_job_tool_docstrings",
    "_shipped_skill_bodies",
    "_instruction_block_texts",
    "_profile_prose",
    "_template_launcher_docstrings",
    "_marked_prose",
)


def test_the_universe_reaches_every_class_the_model_reads() -> None:
    """Each loader contributes, and the bundle that ships no served tools is in it.

    `results` is the case worth naming: it serves no tool module, so its durable job's
    assembled docstring is its *entire* model-facing surface and the old universe held none of it.
    """
    names = set(model_facing_descriptions())
    for prefix in (
        "job:",
        "bundleskill:",
        "skill:",
        "block:",
        "safety:",
        "profile:",
        "template:",
        "prose:",
    ):
        assert any(name.startswith(prefix) for name in names), f"no {prefix} text in the universe"
    assert any(name.startswith("job:results:") for name in names), (
        "the `results` bundle serves no tool module, so its job descriptions are the only "
        "model-facing text it has; the universe must reach them"
    )

    # Each class is counted against the tree, not a written number, so a loader that goes partly
    # silent fails here even though the poison tests below inject regardless of what the loader
    # returns.
    root = Path(__file__).resolve().parents[1]
    package = Path(chemclaw.__file__).parent
    expected = {
        "bundleskill:": len(list(package.glob("connectors/*/skills/*/SKILL.md"))),
        "skill:": len(list((root / "skills").glob("*/SKILL.md"))),
        "profile:": 2 * len(list((root / "data" / "profiles").glob("*.yaml"))),
        "block:": len(_INSTRUCTION_BLOCKS),
        "safety:": len(_SAFETY_BLOCKS),
    }
    short = {
        prefix: (sum(1 for name in names if name.startswith(prefix)), count)
        for prefix, count in expected.items()
        if sum(1 for name in names if name.startswith(prefix)) != count
    }
    assert not short, (
        f"{short} — a loader is returning fewer texts than the tree holds (got, expected). A class "
        "that goes partly silent scans green everywhere else in this file."
    )


@pytest.mark.parametrize("loader", _THE_CLASSES_THAT_WERE_OUTSIDE)
@pytest.mark.parametrize(
    "poison",
    [
        "Escalate to the DFT tier when GFN2 is not enough.",
        "The HPC launcher runs this through Nextflow on Seqera.",
        "It opens a pull request and the result waits for review.",
        "The finding is staged behind the knowledge gate until a reviewer accepts it.",
    ],
)
def test_a_forbidden_sentence_in_any_of_them_is_caught(
    monkeypatch: pytest.MonkeyPatch, loader: str, poison: str
) -> None:
    """A forbidden sentence injected into any model-facing class is caught.

    Poisoning each loader in turn means a class that silently stops being scanned fails here.
    """
    real = getattr(inventory, loader)
    guard = (
        test_no_tool_description_tells_the_model_about_a_tier_that_is_gone
        if _A_REMOVED_TIER.search(poison)
        else test_no_tool_description_tells_the_model_to_expect_a_review_gate
    )
    guard()  # the control: green before the poison, so the failure below is the poison's.
    monkeypatch.setattr(inventory, loader, lambda: dict(real(), poisoned=poison))
    with pytest.raises(AssertionError) as caught:
        guard()
    assert str(caught.value).startswith("{'poisoned':"), (
        f"the guard failed, but not solely on the poison: {caught.value}"
    )


def test_the_marked_class_reaches_its_first_carriers() -> None:
    """The two constants the row that opened this class named, read through the one loader.

    A prompt template is read as the string it evaluates to, and a mapping of markers contributes
    one text per member, so a finding names the hint that carried it.
    """
    found = _marked_prose()
    assert "prose:chemclaw.durable.hypothesis_tournament:_DERIVE_CHECK" in found
    assert (
        found["prose:chemclaw.durable.hypothesis_tournament:_TEMPLATE_HINTS[bond-strength-survey]"]
        == "which bond breaks first"
    )


def test_the_shipped_markers_are_all_where_the_loader_reads_them() -> None:
    """Rule 11 over the tree: every `ModelProse` is a module-level constant `marked_prose` sees."""
    assert prose.check_marked_prose_is_reachable() == []


def test_a_marker_inside_a_function_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A marker the loader cannot read looks applied and guards nothing, so the gate refuses it.

    The control is the same text at module scope, which the loader reads and the rule accepts.
    """
    package = tmp_path / "chemclaw"
    package.mkdir()
    (package / "stray.py").write_text(
        "from chemclaw.core.model_prose import ModelProse\n\n"
        "READ = ModelProse('Escalate to the DFT tier.')\n\n"
        "def prompt() -> str:\n"
        "    return ModelProse('Escalate to the DFT tier.')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prose, "_PACKAGE", package)
    monkeypatch.setattr(prose, "_ROOT", tmp_path)
    problems = prose.check_marked_prose_is_reachable()
    assert len(problems) == 1, problems
    assert problems[0].startswith("chemclaw/stray.py:6:"), problems


def test_every_exemption_still_quotes_shipped_prose() -> None:
    """Every exemption still quotes shipped prose in the text it is keyed to.

    A stale exemption could later match some other sentence; checking the owner stops one surviving
    a renamed text.
    """
    texts = {name: " ".join(text.split()) for name, text in model_facing_descriptions().items()}
    orphaned = sorted(
        f"{owner}: {phrase!r}"
        for owner, phrase in _TRUE_ABOUT_WHAT_IS_GONE
        if phrase not in texts.get(owner, "")
    )
    assert not orphaned, (
        f"{orphaned} are exempted but no longer appear in the text they name. Delete the entry: "
        "the prose it was written for has been reworded, renamed or removed."
    )


def test_every_exemption_is_needed() -> None:
    """Every exemption is needed: removing it must turn its text red.

    An exemption that guards nothing is noise with authority.
    """
    texts = model_facing_descriptions()
    assert texts, "no model-facing text found; this test is reading the wrong tree"
    for (owner, phrase), reason in _TRUE_ABOUT_WHAT_IS_GONE.items():
        assert reason.strip(), f"{phrase!r} is exempted without a reason"
        assert any(
            pattern.search(phrase)
            for pattern in (_A_REMOVED_TIER, _REVIEW_MACHINERY, _A_REVIEW_PROMISE)
        ), (
            f"{phrase!r} is exempted but matches none of the patterns, so it exempts nothing. "
            "Either a pattern was narrowed past it, or the entry was never needed."
        )
        without = {
            key: value for key, value in _TRUE_ABOUT_WHAT_IS_GONE.items() if key != (owner, phrase)
        }
        restore = dict(_TRUE_ABOUT_WHAT_IS_GONE)
        _TRUE_ABOUT_WHAT_IS_GONE.clear()
        _TRUE_ABOUT_WHAT_IS_GONE.update(without)
        try:
            still_clean = all(
                _first_offence(texts[owner], pattern, owner) is None
                for pattern in (_A_REMOVED_TIER, _REVIEW_MACHINERY, _A_REVIEW_PROMISE)
            )
        finally:
            _TRUE_ABOUT_WHAT_IS_GONE.clear()
            _TRUE_ABOUT_WHAT_IS_GONE.update(restore)
        assert not still_clean, (
            f"removing the exemption for {owner} changes nothing, so it guards nothing. Delete it."
        )


def test_an_exemption_cannot_form_across_a_line_break() -> None:
    """An exemption cannot form across a line break.

    The tail of one line plus the head of the next must not spell an exempted quote and swallow the
    offence that follows; the same offence one line later is the control.
    """
    owner = "block:5"
    swallowed = (
        "Report the method honestly and never present one as if it were\n"
        "DFT runs are dispatched to the cluster queue when GFN2 is not enough."
    )
    control = "Report the method honestly.\nDFT runs are dispatched to the cluster queue."
    assert _first_offence(swallowed, _A_REMOVED_TIER, owner) == "DFT", (
        "an exemption formed across a line break and exempted the sentence after it"
    )
    assert _first_offence(control, _A_REMOVED_TIER, owner) == "DFT"
    # And the shipped sentence it is written for is still exempt, line wrapping and all.
    assert _first_offence(model_facing_descriptions()[owner], _A_REMOVED_TIER, owner) is None


def test_an_exemption_reaches_only_the_text_it_names() -> None:
    """A quote that is correct in one skill is not a licence in another."""
    borrowed = "Anywhere else: never present one as if it were DFT."
    assert _first_offence(borrowed, _A_REMOVED_TIER, "block:5") is None
    assert _first_offence(borrowed, _A_REMOVED_TIER, "skill:deep-research") == "DFT"


def test_no_tool_description_tells_the_model_about_a_tier_that_is_gone() -> None:
    """No model-facing text describes the removed DFT/HPC tier.

    A tool docstring is re-sent on every model call, so prose about a system the model cannot reach
    costs tokens and misleads. Naming a removed tier is checkable; where rationale belongs in
    general stays a review rule.
    """
    offenders = {
        name: found
        for name, text in model_facing_descriptions().items()
        if (found := _first_offence(text, _A_REMOVED_TIER, name))
    }
    assert not offenders, (
        f"{offenders} name a removed tier in text the model is sent on every turn. Move the "
        "history to a `#` comment in the function body: the model cannot act on it and pays for "
        "it, and `D-2026-08-26-semiempirical-is-the-whole-tier` deleted what it describes."
    )


def test_no_tool_description_tells_the_model_to_expect_a_review_gate() -> None:
    """No model-facing text tells the model to expect a review gate.

    Knowledge lands the moment it is learned
    (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`); a model told its writes are reviewed
    reasons about a safety net nobody holds. `propose_skill` truthfully says its proposal waits for
    the chemist, and `_A_REVIEW_PROMISE` requires the deleted review object, so it does not match.
    """
    offenders: dict[str, str] = {}
    for name, text in model_facing_descriptions().items():
        if found := _first_offence(text, _REVIEW_MACHINERY, name):
            offenders[name] = found
        elif found := _first_offence(text, _A_REVIEW_PROMISE, name):
            offenders[name] = found
    assert not offenders, (
        f"{offenders} promise the model a review step that "
        "`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted. A note is recorded, not "
        "proposed; say what the tool does now."
    )


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
    """A note type the graph does not know is caught (D-164).

    A real tool told to write an unknown note type fails only at validation, which rule 1 cannot
    see.
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


# Built from a variable so no literal dangling path appears in this file.
_MISSING = "/".join(("vanished", "module.py"))
# A second one, for the Makefile tests below that need two distinct nonexistent paths in the same
# fixture (one in a comment, one in a recipe command) to tell which was scanned.
_MISSING_RECIPE = "/".join(("nonexistent", "recipe.py"))


def test_the_shipped_operator_documents_name_only_things_that_exist() -> None:
    """Rules 5-7: every module path, workflow file and ADR id in the operator docs resolves."""
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
    """A sub-decision label an ADR defines (`D-A5a` inside `D-048`) is accepted.

    Derived by scanning the ADRs, so an invented label is still caught.
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
    """The planning documents are deliberately out of scope.

    Their mismatches name files later decisions deleted; rewriting them to the nearest surviving
    module would falsify the build record they exist to be.
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
    """Rules 8-9 over the real tree: documents cite only declared metric names.

    A wrong series name renders as an alert that matches nothing and reads as healthy.
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
    """Rule 9 checks merged ADRs for metric selectors and leaves the archive alone.

    Bare backticked `chemclaw_*` spans in decisions are often correct prose (module names, roles,
    log markers), so only the selector spelling gets this reach.
    """
    origins = prose._selector_sources()
    assert any(origin.startswith("docs/decisions/") for origin in origins)
    assert not any(origin.startswith("docs/archive/") for origin in origins)


def test_rule_9s_corpus_cannot_be_widened_by_build_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rule 9's corpus is the repository's documents, not whatever the working directory holds.

    A walk from the root would include build output such as `make mutants`' gitignored copy and fail
    on paths no commit contains.
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
    """A retired metric quoted by a merged ADR has a legal remedy: `_RETIRED_METRIC_NAMES`.

    Merged ADRs cannot be edited, so a retired series they quote must be declared retired. Asserted
    by value so a new entry has to be argued here.
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
    """Every `src/chemclaw/*/README.md` is in the operator corpus, because readers navigate by them.

    Asserted as a corpus property, so narrowing the corpus fails here rather than passing the rule.
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
    """The prose gate refuses a corpus it could not assemble.

    Corpora drop absent files, so a wrong `_ROOT` (an installed wheel, a relocated package) would
    check zero documents and pass. The refusal names each missing document, since losing one is the
    realistic case.
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
    """A prompt block that names a tool it does not require is caught (rule 10).

    `_assemble` drops a block only when the graph lacks something in `requires`, so a block naming a
    tool with empty `requires` is sent to every deployment.
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
    """A block requiring a tool its text never mentions is caught.

    It would vanish where the tool is absent for no visible reason; equality makes both directions
    visible.
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
    """A block requiring a middleware-attached tool is caught.

    The prompt is narrowed against bound tools only; filesystem verbs, `write_todos` and `task` are
    attached afterwards, so such a block would be dropped from every deployment.
    """
    monkeypatch.setattr(
        prose,
        "_INSTRUCTION_BLOCKS",
        (PromptBlock("Use read_file on the path shown. ", frozenset({"read_file"})),),
    )
    problems = check_instruction_blocks()
    assert any("read_file" in problem and "middleware" in problem for problem in problems), problems


def test_the_prompt_a_graph_is_sent_names_no_tool_that_graph_cannot_call() -> None:
    """The prompt a graph is sent names no tool that graph cannot call, on two real surfaces.

    Asserts what `instructions_for` produces. Middleware-attached names (filesystem verbs, `task`)
    are always present and exempt, derived as `check_instruction_blocks` derives them.
    """
    profile = get_profile("default")
    always_bound = skill_tool_names() | set(subagent_tool_names())
    for available in (frozenset(registered_tool_names()), advertised_tool_names(profile)):
        text = instructions_for(profile, available)
        named = prose.referenced_tool_names(text) - always_bound
        assert named <= available, sorted(named - available)


def test_the_maximal_prompt_is_what_a_validator_and_a_caller_still_get() -> None:
    """`available=None` yields every block.

    So validators and callers without a graph see the maximal prose.
    """
    profile = get_profile("default")
    maximal = instructions_for(profile)
    assert maximal == _INSTRUCTIONS
    for block in _INSTRUCTION_BLOCKS:
        if block.trail in (None, "durable"):
            assert block.text in maximal, block.text[:60]


def test_a_deployment_with_no_durable_trail_is_not_told_it_has_one() -> None:
    """A deployment with no durable audit trail is not told it has one.

    `default_audit_sink()` is `NullAuditSink` unless `session_store="postgres"`, so the traceability
    paragraph has two variants and follows the sink.
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
    """The safety floor survives narrowing to a surface with no tools at all.

    A floor sentence sharing a block with a side-effecting tool instruction would be dropped from
    helpers, which lose those tools. The empty surface is the narrowest possible, so a floor that
    survives it survives every real narrowing.
    """
    profile = get_profile("default")
    floor = instructions_for(profile, frozenset())
    assert f"<{ENVELOPE_TAG}>" in floor, "the envelope rule is behind a tool this graph may lack"
    assert "'Refused:'" in floor
    assert "Earlier tool result dropped" in floor
    assert "What this system does not hold" in floor


def test_the_appended_safety_floor_names_no_tool_the_profile_cannot_call() -> None:
    """The safety floor appended to a replacing profile names no tool that profile cannot call.

    `instructions_for` appends the floor as one string to profiles that supply their own
    `instructions:`, bypassing block narrowing. Only the floor is asserted, minus always-bound
    names; a profile's own text is `data/profiles/`'s responsibility.
    """
    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.agent.profiles import registered_profile_names

    load_profiles()
    # The filesystem verbs are always bound (`FilesystemMiddleware` is on every agent), and the
    # floor is where the deny-rule around them must reach a profile that replaced the prose.
    always_bound = skill_tool_names() | set(subagent_tool_names())
    over_promised: dict[str, list[str]] = {}
    for name in sorted(registered_profile_names()):
        profile = get_profile(name)
        if profile.instructions is None:
            continue
        available = advertised_tool_names(profile)
        floor = instructions_for(profile, available).removeprefix(f"{profile.instructions}\n")
        named = prose.referenced_tool_names(floor) - always_bound
        if named - available:
            over_promised[name] = sorted(named - available)
    assert not over_promised, (
        "the safety floor names tools these profiles cannot call: "
        f"{over_promised}. The floor is blocks now — narrow it the way `_INSTRUCTION_BLOCKS` is."
    )


def test_the_prompt_does_not_claim_every_launcher_takes_a_rationale() -> None:
    """The prompt does not claim every launcher takes a rationale.

    Connector-job launchers take one; template launchers do not, since the template states its own
    purpose. The rule is about the argument.
    """
    import inspect

    from chemclaw.connectors.registry import job_tools
    from chemclaw.templates.registry import template_tools

    def takes_one(tool: object) -> bool:
        return "rationale" in inspect.signature(tool).parameters  # type: ignore[arg-type]

    templates = list(template_tools())
    jobs = list(job_tools())
    assert templates and jobs, "the fixture measured an empty surface"
    assert not any(takes_one(tool) for tool in templates), "a template launcher grew a rationale"
    assert all(takes_one(tool) for tool in jobs), "a connector-job launcher lost its rationale"
    assert "every launcher takes a rationale" not in _INSTRUCTIONS, (
        f"{len(templates)} of {len(templates)} template launchers take no rationale, so the prompt "
        "must not promise the model that every launcher does"
    )


def test_the_prompt_does_not_call_every_marked_refusal_an_account_decision() -> None:
    """The prompt does not call every marked refusal an account decision.

    `DryRunRefusal`, `PlanNotApprovedError`, `UndeclaredWriteRefusal` and `SkillsReadOnlyRefusal`
    are marked `AuthorizationError`s about the deployment's mode or the agent's surface, not
    entitlements; sending a chemist to request access for them is wrong.
    """
    from chemclaw.agent.authz import AuthorizationError
    from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal  # noqa: F401  (registers it)

    def subclasses(cls: type) -> set[type]:
        found = set(cls.__subclasses__())
        return found | {sub for child in cls.__subclasses__() for sub in subclasses(child)}

    families = subclasses(AuthorizationError)
    assert len(families) >= 4, f"only {sorted(c.__name__ for c in families)} — re-check the claim"
    for prompt in (_INSTRUCTIONS, instructions_for(get_profile("safety"))):
        assert "decision this system made about the asking chemist's account" not in prompt, (
            f"{len(families)} refusal families reach the model marked "
            f"({sorted(c.__name__ for c in families)}) and only one is about an account"
        )
        assert "the reason the result states" in prompt, (
            "the model is told to relay a refusal without being told to read its reason"
        )


def test_the_prompt_does_not_call_recall_preferences_the_only_memory_there_is() -> None:
    """The prompt does not call `recall_preferences` the only memory there is.

    `/memories/**` is a durable per-actor store.
    """
    assert "the only memory of them you have" not in _INSTRUCTIONS


def test_the_trail_paragraph_says_the_arguments_it_records_are_truncated() -> None:
    """The trail paragraph says recorded arguments are truncated (`audit.bounded_repr`)."""
    durable = instructions_for(get_profile("default"), durable_trail=True)
    assert "truncated arguments" in durable


def test_a_capability_this_fleet_serves_is_not_denied_in_the_message_that_offers_it() -> None:
    """A capability the fleet serves is not denied in the message that offers it.

    A "we do not hold X" clause must drop when X's tool is bound, the inverse of `requires`.
    Asserted both ways: bound and the clause is gone, absent and it stands.
    """
    profile = get_profile("default")
    served = {"screen_genotoxic_alerts", "ich_impurity_limit"}
    base = advertised_tool_names(profile) - served

    for tool, clause in (
        ("screen_genotoxic_alerts", "genotoxicity (ICH M7)"),
        ("ich_impurity_limit", "elemental-impurity or residual-solvent limits"),
    ):
        assert clause in instructions_for(profile, base), (
            f"the limit on {tool} is not stated even when nothing binds it"
        )
        assert clause not in instructions_for(profile, base | {tool}), (
            f"{tool} is bound and the prompt still denies the capability it provides"
        )
    assert "What this system does not hold" in instructions_for(profile, base | served), (
        "the whole paragraph was dropped rather than the two clauses inside it"
    )
