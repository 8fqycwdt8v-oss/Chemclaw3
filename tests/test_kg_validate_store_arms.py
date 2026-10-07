"""The two halves of `make kg-validate` that only a database can answer, driven end to end.

In CI both arms check nothing: the seed corpus may cite no calculation
(`tests/test_seed_corpus.py::test_the_seed_corpus_cites_no_calculation_the_store_cannot_back`), and
CI's database has no `reaction_records`. So each arm gets a case that makes it fail and one that
makes it pass, through `validate_kg.main` itself. Real Postgres, because the question is whether a
row exists; skips without a database.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from chemclaw.cli.validate_kg import main as validate_kg_main
from chemclaw.ingest.eln.records import PostgresReactionRecordStore, ReactionRecord
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import CalculationKey, StoredResult
from tests.pg import migrated_db_or_skip


def _note_citing_reaction(corpus: Path, reaction_id: str) -> None:
    """A campaign note citing one transcription, as `memory.campaign` renders one."""
    directory = corpus / "campaign"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "campaign-arm.md").write_text(
        "---\nid: campaign-arm\ntype: campaign\ncreated_by: agent\n---\n\n"
        f"1. [[{note_id_for_reaction(reaction_id)}]]: `CCO.CC(=O)O>>CCOC(C)=O`\n",
        encoding="utf-8",
    )


def _note_citing_calculation(corpus: Path, key: str) -> None:
    """A job-result note whose `calc_refs` cites one calculation key."""
    directory = corpus / "job-result"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job-result-arm.md").write_text(
        "---\nid: job-result-arm\ntype: job-result\ncreated_by: agent\n"
        f"calc_refs:\n  - {key}\n---\n\nOne cited calculation.\n",
        encoding="utf-8",
    )


def _run(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """`make kg-validate` over `corpus`, exactly as the Makefile invokes it."""
    monkeypatch.setattr(sys, "argv", ["validate_kg", str(corpus)])
    return validate_kg_main()


def test_the_reaction_arm_fails_on_a_citation_no_record_backs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reaction arm fails on a citation no record backs.

    `kg.graph.dangling_links` ignores `reaction-` targets because the graph cannot see the store, so
    this arm is the only check on a mistyped run id.
    """
    asyncio.run(migrated_db_or_skip())
    _note_citing_reaction(tmp_path, "arm-no-such-run")

    assert _run(tmp_path, monkeypatch) == 1
    printed = capsys.readouterr().out
    assert "campaign-arm" in printed and "arm-no-such-run" in printed


def test_the_reaction_arm_passes_on_a_citation_a_record_backs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other direction, without which the test above is satisfied by the arm failing always.

    The success line is asserted to *count* the citation, not merely to say OK: the shipped corpus
    prints `0 reaction citation(s)` and that is the state this whole file exists because of.
    """
    asyncio.run(migrated_db_or_skip())
    reaction_id = "arm-real-run"
    asyncio.run(
        PostgresReactionRecordStore().record(
            [ReactionRecord(reaction_id=reaction_id, body="body", source="eln:arm")], "arm-eln"
        )
    )
    _note_citing_reaction(tmp_path, reaction_id)

    assert _run(tmp_path, monkeypatch) == 0
    assert "1 reaction citation(s)" in capsys.readouterr().out


def test_the_calc_arm_fails_on_a_ref_no_calculation_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The calc arm fails on a ref no calculation produced.

    `_calc_ref_shape` checks only a key's form; existence is answered here, including the
    entrypoint's store construction, `try` and exit code.
    """
    asyncio.run(migrated_db_or_skip())
    real = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
    typo = real.as_str()[:-1] + ("0" if not real.as_str().endswith("0") else "1")
    _note_citing_calculation(tmp_path, typo)

    assert _run(tmp_path, monkeypatch) == 1
    printed = capsys.readouterr().out
    assert "job-result-arm" in printed and "calc_refs" in printed


def test_the_calc_arm_passes_on_a_ref_the_cache_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """And the pass, counted — so the arm cannot be satisfied by refusing everything."""
    asyncio.run(migrated_db_or_skip())
    key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO", "arm": "pass"})
    asyncio.run(
        PostgresStore().put(StoredResult(key=key, result={"energy": -1.0}, provenance="computed"))
    )
    _note_citing_calculation(tmp_path, key.as_str())

    assert _run(tmp_path, monkeypatch) == 0
    assert "1 calc_ref(s)" in capsys.readouterr().out


def test_a_corpus_with_no_citations_says_the_store_halves_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corpus with no citations says the store halves did not run.

    Not an error, but a green `make kg-validate` must not read as if the database half checked
    something.
    """
    directory = tmp_path / "playbook"
    directory.mkdir(parents=True)
    (directory / "playbook-arm.md").write_text(
        "---\nid: playbook-arm\ntype: playbook\ncreated_by: agent\n---\n\nNo citations.\n",
        encoding="utf-8",
    )

    assert _run(tmp_path, monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "nothing to check" in printed
    assert "test_kg_validate_store_arms.py" in printed


def test_the_shipped_corpus_still_gives_both_arms_nothing() -> None:
    """The shipped corpus still gives both arms nothing.

    States the zero so the day it changes is visible. If the corpus gains a real citation, delete
    this
    test: the arms then have input.
    """
    from chemclaw.core.config import settings
    from chemclaw.kg.graph import load_notes
    from chemclaw.kg.validate import calc_citations, external_citations

    notes = load_notes(Path(settings.knowledge_path))
    assert notes, "the shipped corpus is empty; this says nothing about the arms"
    assert external_citations(notes) == []
    assert calc_citations(notes) == []
