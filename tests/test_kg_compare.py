"""The comparison of the tree and the Postgres store finds every kind of difference, and no other.

Each difference class has a negative control: a store pair that is equal, then one thing changed.
The comparison is on canonical notes and edges, so a file laid out by hand (another digest, the same
note) is not a difference. The committed `knowledge/` corpus, imported, compares clean.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import chemclaw.kg.compare as compare_module
from chemclaw.cli.validate_kg import main as validate_main
from chemclaw.core.config import settings
from chemclaw.core.db import connect, pooling
from chemclaw.core.metrics import METRICS
from chemclaw.kg.compare import (
    KINDS,
    Comparison,
    Difference,
    compare_once,
    compare_stores,
    publish_differences,
    repair_from_tree,
)
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.importer import import_notes
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.render import render_note
from chemclaw.kg.rows import content_sha
from chemclaw.kg.store import GitNoteStore, NoteUnit
from tests.kg_repo import write_notes
from tests.note_store_harness import corpus, empty_note_tables

_SHIPPED = Path(__file__).resolve().parents[1] / "knowledge"


@pytest.fixture
async def pair(tmp_path: Path) -> AsyncIterator[tuple[GitNoteStore, PostgresNoteStore, Path]]:
    """An empty notes directory and empty notes tables, with the stores over them."""
    await empty_note_tables()
    directory = tmp_path / "knowledge"
    directory.mkdir()
    async with pooling():
        yield GitNoteStore(directory), PostgresNoteStore(), directory


async def _sql(sql: str, *params: Any) -> None:
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(sql, params)
        await conn.commit()


def _note(note_id: str, body: str = "A note.", **fields: Any) -> Note:
    return Note(id=note_id, type="playbook", created_by="agent", body=body, **fields)


async def _fill(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], *notes: Note, only: str | None = None
) -> None:
    """The same notes in both stores, or in just one (`only` is `git` or `postgres`)."""
    _, postgres, directory = pair
    if only != "postgres":
        write_notes(directory, *notes)
        invalidate_cache()  # the tree's scan is cached for `graph_cache_ttl_seconds`
    if only != "git":
        for note in notes:
            await postgres.put(NoteUnit(note))


async def _differences(pair: tuple[GitNoteStore, PostgresNoteStore, Path]) -> dict[str, Any]:
    found = await compare_once(pair[0], pair[1])
    return {d.note_id: (d.kinds, d.fields) for d in found.differences}


async def test_equal_stores_have_no_differences(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    """The nine-note graph with windows, a cycle and a dangling target, in both: clean."""
    await _fill(pair, *corpus())
    found = await compare_once(pair[0], pair[1])
    assert found.clean and found.differences == ()
    assert (found.git_notes, found.postgres_notes) == (9, 9)
    assert found.counts() == dict.fromkeys(KINDS, 0)


async def test_a_file_laid_out_by_hand_is_the_same_note_with_another_digest(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    """Extra blank lines and CRLF change the file's bytes, not the note: not a difference."""
    note = _note("handmade", "Line one.\n\nLine two.")
    await _fill(pair, note, only="postgres")
    path = pair[2] / "playbook" / "handmade.md"
    path.parent.mkdir(parents=True)
    laid_out = render_note(note).replace("\n", "\r\n") + "\r\n\r\n\r\n"
    path.write_bytes(laid_out.encode())
    assert content_sha(note) != f"sha256:{hashlib.sha256(laid_out.encode()).hexdigest()}"
    assert await _differences(pair) == {}


async def test_a_note_in_the_tree_only_is_missing_in_postgres(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    await _fill(pair, _note("shared"))
    await _fill(pair, _note("tree-only"), only="git")
    assert await _differences(pair) == {"tree-only": (("missing_in_postgres",), ())}


async def test_a_note_in_postgres_only_is_missing_in_git(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    await _fill(pair, _note("shared"))
    await _fill(pair, _note("postgres-only"), only="postgres")
    assert await _differences(pair) == {"postgres-only": (("missing_in_git",), ())}


async def test_a_file_that_fails_the_schema_is_named_as_unparseable(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    """Postgres holds a note whose file no longer parses: missing in git, and why."""
    await _fill(pair, _note("broken"), only="postgres")
    path = pair[2] / "playbook" / "broken.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: not-a-valid: type\n  - [\n---\nbody\n", encoding="utf-8")
    assert await _differences(pair) == {"broken": (("missing_in_git",), ("unparseable",))}


async def test_different_content_is_a_canonical_difference_naming_the_fields(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    await _fill(pair, _note("same"))
    await _fill(pair, _note("edited", "The tree's body.", tags=["a"]), only="git")
    await _fill(pair, _note("edited", "Postgres's body.", tags=["b"]), only="postgres")
    assert await _differences(pair) == {"edited": (("canonical",), ("body", "tags"))}


async def test_a_different_window_is_a_validity_difference_alone(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    await _fill(pair, _note("windowed", "Same.", valid_to=date(2026, 3, 31)), only="git")
    await _fill(pair, _note("windowed", "Same.", valid_to=date(2026, 4, 30)), only="postgres")
    assert await _differences(pair) == {"windowed": (("validity",), ("valid_to",))}


async def test_a_note_can_differ_in_several_ways_at_once(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    await _fill(pair, _note("both", "Tree.", valid_from=date(2026, 1, 1)), only="git")
    await _fill(pair, _note("both", "Postgres."), only="postgres")
    kinds, fields = (await _differences(pair))["both"]
    assert kinds == ("validity", "canonical") and fields == ("body", "valid_from")


async def test_a_missing_or_altered_edge_is_an_edge_difference_alone(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    """The edges are compared as stored: tamper with `kg_edges` and the content still matches."""
    linked = _note("linked", "See [[a]] and [[b]].", relations=[Relation(rel="uses", to="c")])
    await _fill(pair, linked, _note("untouched", "See [[a]]."))
    assert await _differences(pair) == {}
    await _sql("DELETE FROM kg_edges WHERE src = 'linked' AND dst = 'b'")
    assert await _differences(pair) == {"linked": (("edges",), ())}
    await _sql("DELETE FROM kg_edges WHERE src = 'linked'")
    await PostgresNoteStore().put(NoteUnit(linked))  # identical content: repairs the edges
    assert await _differences(pair) == {}
    await _sql("UPDATE kg_edges SET valid_to = '2026-01-01' WHERE src = 'linked' AND dst = 'c'")
    assert await _differences(pair) == {"linked": (("edges",), ())}


async def test_the_committed_corpus_imported_compares_clean(tmp_path: Path) -> None:
    """The shipped `knowledge/` tree and its import into Postgres: zero differences."""
    await empty_note_tables()
    async with pooling():
        summary = await import_notes(_SHIPPED)
        assert summary.complete and summary.written >= 40
        found = await compare_once(GitNoteStore(_SHIPPED), PostgresNoteStore())
    assert found.clean, found.differences
    assert found.git_notes == found.postgres_notes == summary.written


async def test_a_difference_that_does_not_persist_is_not_reported(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write another pod has not synced looks different once and agrees after the pause."""
    git, postgres, _ = pair
    await _fill(pair, _note("late"), only="postgres")
    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def sync_arrives(seconds: float) -> None:
        slept.append(seconds)
        await _fill(pair, _note("late"), only="git")
        await real_sleep(0)

    monkeypatch.setattr(compare_module, "sleep", sync_arrives)
    assert (await compare_once(git, postgres)).differences != ()
    settled = await compare_stores(git, postgres, settle_seconds=7.0)
    assert settled.clean and slept == [7.0]


async def test_a_difference_that_persists_is_reported_with_the_second_looks_detail(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    git, postgres, _ = pair
    await _fill(pair, _note("stuck"), only="postgres")
    await _fill(pair, _note("fine"))

    async def nothing_changes(seconds: float) -> None:
        return None

    monkeypatch.setattr(compare_module, "sleep", nothing_changes)
    settled = await compare_stores(git, postgres, settle_seconds=1.0)
    assert [(d.note_id, d.kinds) for d in settled.differences] == [("stuck", ("missing_in_git",))]
    assert (await compare_stores(git, postgres, settle_seconds=0.0)).differences == (
        settled.differences
    )


async def test_repair_puts_the_trees_note_into_postgres_and_leaves_what_it_cannot_decide(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
) -> None:
    git, postgres, _ = pair
    await _fill(pair, _note("fine"))
    await _fill(pair, _note("absent"), only="git")
    await _fill(pair, _note("edited", "Tree."), only="git")
    await _fill(pair, _note("edited", "Stale."), only="postgres")
    await _fill(pair, _note("extra"), only="postgres")
    curated = Note(id="curated", type="playbook", created_by="human", body="A person's.")
    await postgres.put(NoteUnit(curated))
    await _fill(pair, _note("curated", "An agent's copy."), only="git")
    found = await compare_once(git, postgres)
    result = await repair_from_tree(git, postgres, found.differences)
    assert result.repaired == ("absent", "edited")
    reasons = dict(result.unrepaired)
    assert set(reasons) == {"curated", "extra"}
    assert "HumanNoteProtected" in reasons["curated"]
    assert "only Postgres holds it" in reasons["extra"]
    assert set(await _differences(pair)) == {"curated", "extra"}
    stored = await postgres.get("edited")
    assert stored is not None and stored.body == "Tree."
    # Repeating the repair changes nothing.
    again = await repair_from_tree(git, postgres, (await compare_once(git, postgres)).differences)
    assert again.repaired == ()


def test_a_comparison_reports_every_kind_and_serialises() -> None:
    found = Comparison(3, 2, (Difference("a", ("canonical", "edges"), ("body",)),))
    assert found.counts() == {
        "missing_in_postgres": 0,
        "missing_in_git": 0,
        "canonical": 1,
        "validity": 0,
        "edges": 1,
    }
    assert json.loads(json.dumps(found.as_json()))["differences"] == [
        {"id": "a", "kinds": ["canonical", "edges"], "fields": ["body"]}
    ]


def test_the_last_comparison_is_what_the_gauge_reads() -> None:
    publish_differences(Comparison(1, 1, (Difference("a", ("validity",)),)))
    text = METRICS.render()
    assert 'chemclaw_kg_compare_differences{kind="validity"} 1' in text
    assert 'chemclaw_kg_compare_differences{kind="edges"} 0' in text
    publish_differences(Comparison(1, 1, ()))
    assert 'chemclaw_kg_compare_differences{kind="validity"} 0' in METRICS.render()


# --- The command ---------------------------------------------------------------------------------


async def _run(*argv: str) -> int:
    """`validate_kg.main` on its own loop, as the command runs."""
    return await asyncio.to_thread(validate_main, list(argv))


async def test_the_command_exits_zero_when_clean_and_one_on_any_difference(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = str(pair[2])
    await _fill(pair, _note("one"), _note("two"))
    assert await _run("--compare", directory) == 0
    assert "OK: the stores serve the same notes" in capsys.readouterr().out
    await _fill(pair, _note("three"), only="git")
    assert await _run("--compare", directory) == 1
    out = capsys.readouterr().out
    assert "DIFFERENT three: missing_in_postgres" in out


async def test_the_command_prints_json(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    await _fill(pair, _note("one"))
    await _fill(pair, _note("two", "Tree."), only="git")
    await _fill(pair, _note("two", "Postgres."), only="postgres")
    assert await _run("--compare", "--json", str(pair[2])) == 1
    report = json.loads(capsys.readouterr().out)["comparison"]
    assert report["clean"] is False and report["counts"]["canonical"] == 1
    assert report["differences"] == [{"id": "two", "kinds": ["canonical"], "fields": ["body"]}]


async def test_repair_is_refused_outside_dual_mode(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    await _fill(pair, _note("one"), only="git")
    assert settings.kg_store == "git"
    assert await _run("--compare", "--repair", str(pair[2])) == 2
    assert "--repair is for kg_store=dual" in capsys.readouterr().err
    assert await _differences(pair) == {"one": (("missing_in_postgres",), ())}


async def test_repair_in_dual_mode_fixes_and_reports_the_state_after(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "kg_store", "dual")
    await _fill(pair, _note("one"), only="git")
    assert await _run("--compare", "--repair", "--json", str(pair[2])) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["repair"] == {"repaired": ["one"], "unrepaired": []}
    assert report["after_repair"]["clean"] is True
    await _fill(pair, _note("extra"), only="postgres")
    assert await _run("--compare", "--repair", str(pair[2])) == 1
    out = capsys.readouterr().out
    assert "NOT REPAIRED extra" in out and "after repair: 1 difference(s)" in out


async def test_the_command_exits_two_when_it_cannot_compare(
    pair: tuple[GitNoteStore, PostgresNoteStore, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://nobody@127.0.0.1:1/none")
    assert await _run("--compare", str(pair[2])) == 2
    assert "the comparison could not run" in capsys.readouterr().err
