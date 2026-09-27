"""The two halves of `make kg-validate` that only a database can answer, driven end to end.

**Why this file exists.** Both arms are dead on every CI run, and it is not an oversight that a
corpus edit would fix. Measured on the shipped tree: `make kg-validate` prints
*"0 reaction citation(s) and 0 calc_ref(s) verified"*, and the calc half cannot ever be otherwise —
`tests/test_seed_corpus.py::test_the_seed_corpus_cites_no_calculation_the_store_cannot_back`
requires it, deliberately, because a seed `calc_ref` is a fabricated key and would fail this very
gate on every fresh database. The reaction half is the same shape one step out: a committed note
citing `[[reaction-EXP-1]]` needs a `reaction_records` row, and CI's database holds migrations and
nothing else.

So the arms owe the suite what `D-2026-09-14-a-gate-nothing-has-failed-is-a-gate-that-cannot-fail`
makes a gated metric owe the case-set: a case that makes them **fail**, and one that makes them
pass. The pieces below each arm were already unit-tested — `unresolved_citations` in
`test_reaction_records.py` and `unresolved_calc_refs` in `test_knowledge_gaps.py` — but nothing
drove `validate_kg.main` over a note carrying a `calc_ref` at all, in either direction, so the
whole calc branch of the entrypoint (its `try`, its store construction, its exit code) had never
executed anywhere.

Real Postgres, because the question these arms ask is "does this row exist" and an in-memory store
answers a different one. Skips where there is no database, and `tests/conftest.py` counts the skip.
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
    """A typo'd run id is what this arm exists to stop, and nothing had ever driven it stopping one.

    `kg.graph.dangling_links` ignores every `reaction-` target on purpose since D-2026-08-25 — the
    graph cannot see the store — so this is the only thing between a citation to a run that does
    not exist and a merge.
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
    """The branch of `validate_kg.main` that had never run anywhere.

    `_calc_ref_shape` checks a key's *form* and concedes in its own comment that existence "is a
    question only a database can answer". This is where it is answered, and until now the answering
    was unit-tested one layer down while the entrypoint's own branch — its store construction, its
    `try`, its contribution to the exit code — was reached by nothing.
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
    """A success line that reads like a whole gate is the failure this gate is named after.

    Not an error — a corpus with no external citations is legitimate — but the reader of a green
    `make kg-validate` is owed the fact that the half needing a database checked nothing, which is
    the shipped tree's state on every CI run.
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
    """The state this file compensates for, asserted rather than remembered.

    It is not a defect to be fixed by editing the corpus — a seed `calc_ref` is a fabricated key
    and `test_the_seed_corpus_cites_no_calculation_the_store_cannot_back` forbids one for that
    reason — so what a reader needs is for the zero to be *stated*, and for the day it stops being
    zero to be a visible change rather than a quiet one. If this fails because the corpus gained a
    real citation, delete it: the arms then have input and this file's premise is gone.
    """
    from chemclaw.core.config import settings
    from chemclaw.kg.graph import load_notes
    from chemclaw.kg.validate import calc_citations, external_citations

    notes = load_notes(Path(settings.knowledge_path))
    assert notes, "the shipped corpus is empty; this says nothing about the arms"
    assert external_citations(notes) == []
    assert calc_citations(notes) == []
