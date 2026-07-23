"""Behavioral tests for the NetworkX indexer and validation (plan steps 2.3, 2.4)."""

from pathlib import Path

import pytest

import kg.graph as graph
from chemclaw.config import settings
from kg.graph import build_graph, neighborhood
from kg.validate import validate


def _note(id_: str, links: list[str], type_: str = "compound") -> str:
    body = " ".join(f"[[{target}]]" for target in links)
    return f"---\nid: {id_}\ntype: {type_}\n---\n{body}\n"


def _make_graph_dir(tmp_path: Path) -> Path:
    # a -> b -> c ; a -> c. Plus a README that must be ignored.
    (tmp_path / "a.md").write_text(_note("a", ["b", "c"]), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", ["c"]), encoding="utf-8")
    (tmp_path / "c.md").write_text(_note("c", []), encoding="utf-8")
    (tmp_path / "README.md").write_text("# notes\nno frontmatter here\n", encoding="utf-8")
    return tmp_path


def test_build_graph_nodes_and_edges(tmp_path: Path) -> None:
    """The graph has one node per note (README ignored) and one edge per wikilink."""
    built = build_graph(_make_graph_dir(tmp_path))
    assert set(built.nodes) == {"a", "b", "c"}
    assert set(built.edges) == {("a", "b"), ("a", "c"), ("b", "c")}
    assert built.nodes["a"]["note"].id == "a"


def test_load_notes_skips_unreadable_file(tmp_path: Path) -> None:
    """One non-UTF-8 note file is skipped by the indexer, not a crashed graph load (G4)."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "bad.md").write_bytes("---\nid: b\ntype: t\n---\nl\xf6slich\n".encode("latin-1"))
    assert [note.id for note in graph.load_notes(tmp_path)] == ["a"]


def test_validate_reports_unreadable_note(tmp_path: Path) -> None:
    """An unreadable (non-UTF-8) note file is reported rather than aborting validation."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "bad.md").write_bytes("---\nid: b\ntype: t\n---\nl\xf6slich\n".encode("latin-1"))
    problems = validate(tmp_path)
    assert any("unreadable" in p for p in problems)


def test_load_notes_caches_parse_until_a_note_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat load is served from cache; a changed tree busts it and re-parses (KM-14)."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    parses = {"count": 0}
    real_parse = graph._parse_notes

    def _counting(notes_dir: Path) -> list:  # type: ignore[type-arg]
        parses["count"] += 1
        return real_parse(notes_dir)

    monkeypatch.setattr(graph, "_parse_notes", _counting)
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")

    first = graph.load_notes(tmp_path)
    second = graph.load_notes(tmp_path)
    assert parses["count"] == 1  # the second call hit the cache, no re-parse
    assert [n.id for n in first] == [n.id for n in second] == ["a"]

    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    third = graph.load_notes(tmp_path)
    assert parses["count"] == 2  # a changed tree busts the cache
    assert {n.id for n in third} == {"a", "b"}


def test_load_notes_cache_off_always_reparses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the cache disabled every load re-parses (the pre-cache behavior)."""
    monkeypatch.setattr(settings, "graph_cache_enabled", False)
    parses = {"count": 0}
    real_parse = graph._parse_notes

    def _counting(notes_dir: Path) -> list:  # type: ignore[type-arg]
        parses["count"] += 1
        return real_parse(notes_dir)

    monkeypatch.setattr(graph, "_parse_notes", _counting)
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    graph.load_notes(tmp_path)
    graph.load_notes(tmp_path)
    assert parses["count"] == 2


def test_dir_fingerprint_tolerates_a_vanished_file(tmp_path: Path) -> None:
    """A note that cannot be stat'd (e.g. deleted mid-query) is skipped, not a crashed load."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    dangling = tmp_path / "gone.md"
    dangling.symlink_to(tmp_path / "does-not-exist.md")  # rglob lists it; stat() raises
    fingerprint = graph._dir_fingerprint(tmp_path)
    assert [entry[0] for entry in fingerprint] == [str(tmp_path / "a.md")]


def test_neighborhood_expands_both_directions(tmp_path: Path) -> None:
    """1-hop from c finds its direct neighbors; 2-hop reaches the whole component."""
    graph = build_graph(_make_graph_dir(tmp_path))
    # c is linked from a and b (incoming); traversal is undirected.
    assert neighborhood(graph, "c", hops=1) == {"a", "b"}
    assert neighborhood(graph, "b", hops=2) == {"a", "c"}


def test_validate_clean_dir(tmp_path: Path) -> None:
    """A consistent graph reports no problems."""
    assert validate(_make_graph_dir(tmp_path)) == []


def test_validate_reports_broken_link(tmp_path: Path) -> None:
    """A wikilink to an unknown note is reported."""
    (tmp_path / "a.md").write_text(_note("a", ["ghost"]), encoding="utf-8")
    problems = validate(tmp_path)
    assert any("unknown note 'ghost'" in p for p in problems)


def test_validate_reports_duplicate_id(tmp_path: Path) -> None:
    """Two notes with the same id are reported."""
    (tmp_path / "a.md").write_text(_note("dup", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("dup", []), encoding="utf-8")
    problems = validate(tmp_path)
    assert any("duplicate id 'dup'" in p for p in problems)


def test_validate_reports_malformed_note(tmp_path: Path) -> None:
    """A malformed note file is reported rather than aborting validation."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "bad.md").write_text("---\nid: x\ntype: [oops\n---\n", encoding="utf-8")
    problems = validate(tmp_path)
    assert any("malformed frontmatter" in p for p in problems)
