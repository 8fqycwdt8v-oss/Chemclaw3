"""Behavioral tests for the NetworkX indexer and validation (plan steps 2.3, 2.4)."""

import time
from pathlib import Path

import networkx as nx
import pytest

import chemclaw.kg.graph as graph
from chemclaw.core.config import settings
from chemclaw.kg.graph import build_graph, neighborhood
from chemclaw.kg.validate import validate


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
    # Fingerprint-based busting needs the stat scan to run on every call; the TTL window that
    # skips it has its own tests below.
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
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


def test_build_graph_caches_assembly_until_a_note_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat build reuses the assembled graph; a changed tree rebuilds it.

    The parse cache alone still re-added every node and edge per query, and the agent's
    `find_notes` → `expand_note` flow builds twice per turn.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    # This test is about fingerprint-based busting, which needs the stat scan to actually run on
    # every call; the TTL window that skips it is covered by its own tests below.
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    assemblies = {"count": 0}
    real_assemble = graph._assemble_graph

    def _counting(notes: list) -> object:  # type: ignore[type-arg]
        assemblies["count"] += 1
        return real_assemble(notes)

    monkeypatch.setattr(graph, "_assemble_graph", _counting)
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")

    first = build_graph(tmp_path)
    second = build_graph(tmp_path)
    assert assemblies["count"] == 1  # the second call hit the cache
    assert first is second  # and got the very same graph back

    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    third = build_graph(tmp_path)
    assert assemblies["count"] == 2  # a changed tree busts the cache
    assert set(third.nodes) == {"a", "b"}


def test_cached_graph_is_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared cached graph rejects mutation, so one reader cannot corrupt the next query."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    built = build_graph(tmp_path)
    with pytest.raises(nx.NetworkXError):
        built.add_node("injected")


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


def test_ttl_window_skips_the_stat_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the TTL window a warm query does no stat scan at all — the DA-5 latency win.

    The scan is O(notes) and was paid on *every* query, cache hit included; skipping it is the
    whole point, so the test counts scans rather than asserting on elapsed time.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    scans = {"count": 0}
    real_fingerprint = graph._dir_fingerprint

    def _counting(notes_dir: Path) -> object:
        scans["count"] += 1
        return real_fingerprint(notes_dir)

    monkeypatch.setattr(graph, "_dir_fingerprint", _counting)
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")

    graph.load_notes(tmp_path)
    assert scans["count"] == 1  # the cold read must scan
    graph.load_notes(tmp_path)
    build_graph(tmp_path)
    assert scans["count"] == 1  # every warm read inside the window skips it


def test_ttl_window_is_the_documented_staleness_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest trade-off: inside the window an externally-written note is not yet visible."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a"}

    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a"}  # still inside the window

    # Once the window lapses the next read scans again and picks the note up.
    graph._LAST_SCAN[str(tmp_path)] = time.monotonic() - 61.0
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a", "b"}


def test_invalidate_cache_bypasses_the_ttl_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local writer's own change is visible immediately — the authoring loop never waits."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a"}

    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    graph.invalidate_cache()  # what the PR-gate submitter calls after writing a note
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a", "b"}


def test_invalidate_cache_can_target_one_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Busting one directory leaves another directory's cache intact."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.md").write_text(_note("a", []), encoding="utf-8")
    (two / "c.md").write_text(_note("c", []), encoding="utf-8")
    graph.load_notes(one)
    graph.load_notes(two)

    (one / "b.md").write_text(_note("b", []), encoding="utf-8")
    (two / "d.md").write_text(_note("d", []), encoding="utf-8")
    graph.invalidate_cache(one)
    assert {n.id for n in graph.load_notes(one)} == {"a", "b"}  # busted, re-scanned
    assert {n.id for n in graph.load_notes(two)} == {"c"}  # untouched, still in its window


def test_ttl_zero_restores_scan_every_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`0` is the escape hatch for a deployment that cannot accept any staleness."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a"}
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    assert {n.id for n in graph.load_notes(tmp_path)} == {"a", "b"}  # visible at once


# --- note_file_fingerprints: the per-note stat signal an incremental reindex diffs against ------


def test_note_file_fingerprints_keyed_by_id_and_stable_when_untouched(tmp_path: Path) -> None:
    """One entry per note, keyed by id; re-scanning an untouched file gives the same fingerprint."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    first = graph.note_file_fingerprints(tmp_path)
    assert set(first) == {"a", "b"}
    second = graph.note_file_fingerprints(tmp_path)
    assert second == first  # nothing touched the files between scans


def test_note_file_fingerprints_changes_when_a_note_is_edited(tmp_path: Path) -> None:
    """Editing one note's content changes only its own fingerprint, not its siblings'."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    before = graph.note_file_fingerprints(tmp_path)

    time.sleep(0.01)  # guarantee a distinct mtime on filesystems with coarse resolution
    (tmp_path / "a.md").write_text(_note("a", ["b"]), encoding="utf-8")
    after = graph.note_file_fingerprints(tmp_path)

    assert after["a"] != before["a"]
    assert after["b"] == before["b"]


def test_note_file_fingerprints_drops_a_deleted_note(tmp_path: Path) -> None:
    """A note removed from disk simply has no entry — the diff a caller needs to see it as gone."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    assert set(graph.note_file_fingerprints(tmp_path)) == {"a", "b"}

    (tmp_path / "b.md").unlink()
    assert set(graph.note_file_fingerprints(tmp_path)) == {"a"}
