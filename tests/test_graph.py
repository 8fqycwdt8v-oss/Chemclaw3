"""Behavioral tests for the NetworkX indexer and validation (plan steps 2.3, 2.4)."""

import ast
import hashlib
import inspect
import logging
import os
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import networkx as nx
import pytest

import chemclaw.kg.graph as graph
import chemclaw.retrieval.vector_index as vector_index
from chemclaw.core.config import settings
from chemclaw.kg.graph import build_graph, neighborhood
from chemclaw.kg.validate import validate


def _note(id_: str, links: list[str], type_: str = "compound") -> str:
    body = " ".join(f"[[{target}]]" for target in links)
    return f"---\nid: {id_}\ntype: {type_}\n---\n{body}\n"


def _make_graph_dir(tmp_path: Path) -> Path:
    # a -> b -> c ; a -> c. Plus a README that must be ignored. Filed under the type directory,
    # because `validate` now checks the layout the PR-gate writes (`note_relative_path`).
    (tmp_path / "compound").mkdir(exist_ok=True)
    (tmp_path / "compound" / "a.md").write_text(_note("a", ["b", "c"]), encoding="utf-8")
    (tmp_path / "compound" / "b.md").write_text(_note("b", ["c"]), encoding="utf-8")
    (tmp_path / "compound" / "c.md").write_text(_note("c", []), encoding="utf-8")
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


def test_a_skipped_note_is_said_out_loud(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A skipped note is logged.

    `kg-validate` reports an unparseable note in CI; this reports it over the tree a pod serves,
    where a partial sync leaves retrieval short with no other signal.
    """
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "bad.md").write_bytes("---\nid: b\ntype: t\n---\nl\xf6slich\n".encode("latin-1"))
    graph.invalidate_cache()

    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.graph"):
        assert [note.id for note in graph.load_notes(tmp_path)] == ["a"]

    assert any("bad.md" in record.getMessage() for record in caplog.records)


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


def test_validate_reports_a_filename_that_disagrees_with_the_note_id(tmp_path: Path) -> None:
    """A note whose file is not `<id>.md` is refused: the note index keys on that filename."""
    (tmp_path / "renamed-file.md").write_text(_note("ethanol-facts", []), encoding="utf-8")
    problems = validate(tmp_path)
    assert any("'ethanol-facts'" in p and "'renamed-file'" in p for p in problems)


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


# --- note_file_fingerprints: the per-note content signal an incremental reindex diffs against ---


def test_note_file_fingerprints_keyed_by_id_and_stable_when_untouched(tmp_path: Path) -> None:
    """One entry per note, keyed by id; re-scanning an untouched file gives the same fingerprint."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    first = graph.note_file_fingerprints(tmp_path)
    assert set(first) == {"a", "b"}
    second = graph.note_file_fingerprints(tmp_path)
    assert second == first  # nothing touched the files between scans


def test_note_file_fingerprints_changes_when_a_note_is_edited(tmp_path: Path) -> None:
    """Editing one note's content changes only its own fingerprint, not its siblings'.

    The fingerprint hashes the bytes, so no `sleep` is needed: an edit inside one filesystem tick is
    still an edit.
    """
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    before = graph.note_file_fingerprints(tmp_path)

    (tmp_path / "a.md").write_text(_note("a", ["b"]), encoding="utf-8")
    after = graph.note_file_fingerprints(tmp_path)

    assert after["a"] != before["a"]
    assert after["b"] == before["b"]


def test_note_file_fingerprints_sees_an_edit_that_moves_neither_mtime_nor_size(
    tmp_path: Path,
) -> None:
    """The fingerprint sees an edit that moves neither mtime nor size.

    A same-length restore with the mtime preserved (a checkout over restored timestamps) would be a
    stale skip under `mtime_ns:size`. The stat pair is asserted equal alongside the fingerprint
    differing, so the old signal's blindness is shown.
    """
    note = tmp_path / "a.md"
    note.write_text(_note("a", ["b"]), encoding="utf-8")
    before_stat = note.stat()
    before = graph.note_file_fingerprints(tmp_path)

    rewritten = _note("a", ["c"])  # same relation count, so the same byte length
    assert len(rewritten.encode()) == before_stat.st_size, "the probe needs an equal-length edit"
    note.write_text(rewritten, encoding="utf-8")
    os.utime(note, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

    after_stat = note.stat()
    assert (after_stat.st_mtime_ns, after_stat.st_size) == (
        before_stat.st_mtime_ns,
        before_stat.st_size,
    ), "the stat pair moved, so this no longer probes what the old fingerprint was blind to"
    assert graph.note_file_fingerprints(tmp_path)["a"] != before["a"]


def test_note_file_fingerprints_drops_a_deleted_note(tmp_path: Path) -> None:
    """A note removed from disk simply has no entry — the diff a caller needs to see it as gone."""
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")
    assert set(graph.note_file_fingerprints(tmp_path)) == {"a", "b"}

    (tmp_path / "b.md").unlink()
    assert set(graph.note_file_fingerprints(tmp_path)) == {"a"}


def test_concurrent_cold_reads_parse_the_corpus_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads that miss the cache together wait for one parse instead of each doing their own.

    Concurrent cold `load_notes` calls (process start, after `invalidate_cache`, a sweep offloading
    per source) would otherwise duplicate the parse and contend on the GIL. Counted rather than
    timed.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph.invalidate_cache()
    for index in range(20):
        (tmp_path / f"n{index}.md").write_text(_note(f"n{index}", []), encoding="utf-8")

    parses = {"count": 0}
    real_parse = graph._parse_notes
    started = threading.Barrier(8)

    def _slow_counting(notes_dir: Path) -> list:  # type: ignore[type-arg]
        parses["count"] += 1
        time.sleep(0.05)  # widen the window a duplicate parse would slip into
        return real_parse(notes_dir)

    monkeypatch.setattr(graph, "_parse_notes", _slow_counting)

    def _read() -> int:
        started.wait()
        return len(graph.load_notes(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: _read(), range(8)))

    assert counts == [20] * 8  # every caller got the whole corpus
    assert parses["count"] == 1  # and exactly one of them paid for it


def test_concurrent_cold_builds_assemble_the_graph_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent cold builds assemble the graph once.

    `build_graph` holds the corpus lock across `cached_notes` and `_assemble_graph`, which is why
    the lock is re-entrant.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    graph.invalidate_cache()
    (tmp_path / "a.md").write_text(_note("a", ["b"]), encoding="utf-8")
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")

    assemblies = {"count": 0}
    real_assemble = graph._assemble_graph
    started = threading.Barrier(8)

    def _slow_counting(notes: list) -> object:  # type: ignore[type-arg]
        assemblies["count"] += 1
        time.sleep(0.05)
        return real_assemble(notes)

    monkeypatch.setattr(graph, "_assemble_graph", _slow_counting)

    def _build() -> int:
        started.wait()
        nodes: int = build_graph(tmp_path).number_of_nodes()
        return nodes

    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(pool.map(lambda _: _build(), range(8)))

    assert sizes == [2] * 8
    assert assemblies["count"] == 1


def test_cache_disabled_still_lets_every_caller_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With caching off the corpus lock is not taken.

    Taking it would turn "always re-parse" into "re-parse one at a time", a different contract from
    `graph_cache_enabled=false`.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", False)
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    parses = {"count": 0}
    real_parse = graph._parse_notes

    def _counting(notes_dir: Path) -> list:  # type: ignore[type-arg]
        parses["count"] += 1
        return real_parse(notes_dir)

    monkeypatch.setattr(graph, "_parse_notes", _counting)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: graph.load_notes(tmp_path), range(4)))
    assert parses["count"] == 4


def test_duplicate_note_id_keeps_the_first_file_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two files claiming one id resolve to the first in path order, with a warning.

    `kg-validate` fails a duplicate in the repository; this covers the served tree, where an rsync
    can land a rename before removing the old file.
    """
    (tmp_path / "compound").mkdir()
    (tmp_path / "reaction").mkdir()
    (tmp_path / "compound" / "x.md").write_text(_note("x", [], "compound"), encoding="utf-8")
    (tmp_path / "reaction" / "x.md").write_text(_note("x", [], "reaction"), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        notes = graph.load_notes(tmp_path)

    assert [note.id for note in notes] == ["x"]
    assert notes[0].type == "compound"  # first in path order, not last-writer-wins
    assert any("already defined by" in record.getMessage() for record in caplog.records)
    assert build_graph(tmp_path).nodes["x"]["note"].type == "compound"


def test_note_file_fingerprints_agrees_with_the_parse_on_a_duplicate(tmp_path: Path) -> None:
    """The fingerprint scan and the parse name the same file when two claim one id.

    `reindex_notes` diffs one against the other, so a disagreement would embed one file's text under
    the other's id. The files differ by bytes, so the assertion is which file won.
    """
    (tmp_path / "compound").mkdir()
    (tmp_path / "reaction").mkdir()
    (tmp_path / "compound" / "x.md").write_text(_note("x", [], "compound"), encoding="utf-8")
    (tmp_path / "reaction" / "x.md").write_text(_note("x", [], "reaction"), encoding="utf-8")

    fingerprints = graph.note_file_fingerprints(tmp_path)
    first = (tmp_path / "compound" / "x.md").read_bytes()
    second = (tmp_path / "reaction" / "x.md").read_bytes()
    assert fingerprints["x"] == f"sha256:{hashlib.sha256(first).hexdigest()}"
    assert fingerprints["x"] != f"sha256:{hashlib.sha256(second).hexdigest()}"


def test_one_note_changed_re_reads_one_file_and_not_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One changed note re-reads one file, not the corpus.

    `kg/git_writer.py` calls `invalidate_cache()` on every note write, so it must keep the per-file
    parse cache. Counted at `read_note` rather than timed.
    """
    directory = _make_graph_dir(tmp_path)
    graph.invalidate_cache()
    reads: list[str] = []
    # Reached by string through `MonkeyPatch` rather than by attribute: `read_note` is imported by
    # `kg.graph` rather than re-exported from it, so a direct attribute access is not an export
    # mypy follows — and the patch has to land on the name the scanner looks up at call time.
    real = graph.__dict__["read_note"]

    def counting(path: Path) -> object:
        reads.append(path.name)
        return real(path)

    monkeypatch.setattr(graph, "read_note", counting)
    assert len(graph.load_notes(directory)) == 3
    cold = len(reads)
    assert cold == 4, f"the cold parse read {cold} files, not the four in the fixture"

    reads.clear()
    (directory / "compound" / "b.md").write_text(_note("b", ["a", "c"]), encoding="utf-8")
    graph.invalidate_cache()
    notes = {note.id: note for note in graph.load_notes(directory)}

    assert reads == ["b.md"], (
        f"one note changed and {len(reads)} files were re-read ({reads}); the per-file parse cache "
        "is not being reused, so every note write costs the next reader the whole corpus"
    )
    assert sorted(notes["b"].outgoing_links()) == ["a", "c"], (
        "the changed note was served from the cache rather than re-read, which is the failure in "
        "the other direction and worse"
    )


def test_the_parse_cache_and_the_content_fingerprint_disagree_and_reparse_is_what_settles_it(
    tmp_path: Path,
) -> None:
    """The parse cache and the content fingerprint can disagree, and `reparse` settles it.

    The parse cache keys on stat fields and the fingerprint on bytes, so a same-size edit with the
    mtime restored moves only the fingerprint; `reindex_notes` would then store the old body under
    the new digest and never heal. Both directions: a plain bust still keeps the parse (a note write
    stays cheap), and only a caller asking for `reparse` pays the read.
    """
    directory = tmp_path
    (directory / "compound").mkdir()
    note = directory / "compound" / "a.md"
    note.write_text(_note("a", ["b"]), encoding="utf-8")
    before = note.stat()

    graph.invalidate_cache(reparse=True)
    first_fingerprint = graph.note_file_fingerprints(directory)["a"]
    assert sorted(graph.load_notes(directory)[0].outgoing_links()) == ["b"]

    note.write_text(_note("a", ["c"]), encoding="utf-8")
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert note.stat().st_size == before.st_size, "the edit has to be the same size to be the case"
    assert note.stat().st_mtime_ns == before.st_mtime_ns

    assert graph.note_file_fingerprints(directory)["a"] != first_fingerprint, (
        "the fingerprint no longer sees a same-size edit, so it is a stat pair again and the whole "
        "disagreement this test is about has gone away — re-read note_file_fingerprints"
    )

    graph.invalidate_cache(directory)
    assert sorted(graph.load_notes(directory)[0].outgoing_links()) == ["b"], (
        "a plain bust now re-reads every file, which is the cost D-2026-09-06 removed; if this is "
        "deliberate, that decision needs superseding rather than this assertion loosening"
    )

    graph.invalidate_cache(directory, reparse=True)
    assert sorted(graph.load_notes(directory)[0].outgoing_links()) == ["c"], (
        "reparse=True left the stale parse in place, so reindex_notes still embeds the old body "
        "under the new digest and the row never heals"
    )


def test_the_reindex_job_asks_for_the_reparse_its_own_comparison_needs() -> None:
    """The reindex job asks for the reparse its own comparison needs.

    `reindex_notes` is the only function diffing `load_notes` against `note_file_fingerprints`. Read
    off the AST rather than the text, because the docstring beside the call mentions `reparse=True`
    too.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(vector_index.reindex_notes)))
    passed = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is True
    }
    assert "reparse" in passed, (
        "reindex_notes busts the caches without reparse=True, so its note list comes from the stat "
        "cache while its fingerprints come from the file's bytes — see "
        "test_the_parse_cache_and_the_content_fingerprint_disagree_and_reparse_is_what_settles_it"
    )


def test_a_file_that_is_deleted_leaves_no_entry_behind(tmp_path: Path) -> None:
    """A deleted file leaves no cache entry behind.

    `(mtime_ns, size)` is a weak signal for a path that has been away and recreated, so the entry is
    dropped when the scan stops naming the path.
    """
    directory = _make_graph_dir(tmp_path)
    graph.invalidate_cache()
    assert len(graph.load_notes(directory)) == 3
    (directory / "compound" / "b.md").unlink()
    graph.invalidate_cache()
    assert sorted(note.id for note in graph.load_notes(directory)) == ["a", "c"]
    assert "b.md" not in "".join(graph._PARSED_FILES.get(str(directory), {})), (
        "the deleted file's parse entry survived the scan that no longer names it"
    )


# --- Incremental assembly (D-2026-09-07-a-changed-note-is-not-a-changed-corpus) ----------------


def _linked(id_: str, links: tuple[str, ...] = (), retires: str | None = None) -> str:
    """A note whose body cites `links` (half of them through a typed edge), optionally retiring one.

    An edge carries a tuple of `Relation`s and a retirement carries `valid_to`, so a patch that
    rebuilt topology but lost metadata would still pass a node-and-edge comparison.
    """
    body = " ".join(
        f"[[precursor-of:{target}]]" if n % 2 else f"[[{target}]]" for n, target in enumerate(links)
    )
    retirement = (
        ""
        if retires is None
        else f"relations:\n  - rel: supersedes\n    to: {retires}\n    valid_to: 2026-01-01\n"
    )
    return f"---\nid: {id_}\ntype: compound\n{retirement}---\n{body}\n"


def _corpus(root: Path) -> None:
    """A corpus with every shape an incremental patch has to get right.

    `hub` is cited by ten notes; `mover` cites it; `doomed` is cited by `mourner`; `retiree` asserts
    a dated edge. Twelve notes is above the size at which `_MAX_PATCHED_FRACTION` lets a one-note
    change patch.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "hub.md").write_text(_linked("hub", ("doomed",)), encoding="utf-8")
    for n in range(10):
        (root / f"citer-{n}.md").write_text(_linked(f"citer-{n}", ("hub",)), encoding="utf-8")
    (root / "mover.md").write_text(_linked("mover", ("hub", "nowhere")), encoding="utf-8")
    (root / "doomed.md").write_text(_linked("doomed", ()), encoding="utf-8")
    (root / "mourner.md").write_text(_linked("mourner", ("doomed",)), encoding="utf-8")
    (root / "retiree.md").write_text(_linked("retiree", ("hub",), retires="hub"), encoding="utf-8")


def _rebuilt(root: Path) -> "nx.DiGraph[str]":
    """The graph a full reassembly of `root` produces — the only thing worth comparing against."""
    return graph._assemble_graph(graph._parse_notes(root))


def _assert_identical(patched: "nx.DiGraph[str]", rebuilt: "nx.DiGraph[str]") -> None:
    """Assert two graphs are the same graph: nodes, edges, and every attribute on both.

    A patch dropping one citation into a changed note answers every other query correctly, so only
    equality against the rebuild catches it.
    """
    assert set(patched.nodes) == set(rebuilt.nodes)
    assert {node: dict(data) for node, data in patched.nodes(data=True)} == {
        node: dict(data) for node, data in rebuilt.nodes(data=True)
    }
    assert set(patched.edges) == set(rebuilt.edges)
    assert {(u, v): dict(data) for u, v, data in patched.edges(data=True)} == {
        (u, v): dict(data) for u, v, data in rebuilt.edges(data=True)
    }


def _fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching on, TTL off — the patch path is about fingerprint moves, not about the TTL window."""
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    graph._GRAPH_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._PARSED_FILES.clear()
    graph._LAST_SCAN.clear()


def test_a_patched_graph_is_identical_to_the_rebuilt_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every corpus change, applied incrementally, gives the graph a full rebuild would give.

    Five shapes, one at a time: a note cited by others, a note citing one that changes, a deletion,
    an addition and a retirement, each compared against a fresh reassembly.
    """
    _fresh_caches(monkeypatch)
    _corpus(tmp_path)
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))

    # A note *cited by ten others* changes what it links to. `remove_node` would take those ten
    # in-edges with it; nothing else in this test would notice, which is why it is here.
    (tmp_path / "hub.md").write_text(_linked("hub", ("mourner", "retiree")), encoding="utf-8")
    graph.invalidate_cache()
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))

    # A note whose only link pointed at a dangling id stops doing so: the bare node it minted has
    # to go, or the graph keeps answering about an id no note cites.
    (tmp_path / "mover.md").write_text(_linked("mover", ("hub",)), encoding="utf-8")
    graph.invalidate_cache()
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))

    # A deletion, of a note something still cites — the node must survive as a bare, note-less one.
    (tmp_path / "doomed.md").unlink()
    graph.invalidate_cache()
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))

    # An addition that defines the id the deletion left dangling.
    (tmp_path / "doomed.md").write_text(_linked("doomed", ("hub",)), encoding="utf-8")
    graph.invalidate_cache()
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))

    # A retirement rewritten to a different target: the edge's `relations` tuple carries the
    # `valid_to`, so this fails a patch that reproduces topology and drops edge metadata.
    (tmp_path / "retiree.md").write_text(
        _linked("retiree", ("hub",), retires="mourner"), encoding="utf-8"
    )
    graph.invalidate_cache()
    _assert_identical(build_graph(tmp_path), _rebuilt(tmp_path))


def test_changing_a_note_keeps_the_citations_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing a note keeps the citations into it.

    `networkx.remove_node` takes a node's in-edges, which belong to other notes.
    """
    _fresh_caches(monkeypatch)
    _corpus(tmp_path)
    before = build_graph(tmp_path)
    assert set(before.in_edges("hub")) == {(f"citer-{n}", "hub") for n in range(10)} | {
        ("mover", "hub"),
        ("retiree", "hub"),
    }

    (tmp_path / "hub.md").write_text(_linked("hub", ("mourner",)), encoding="utf-8")
    graph.invalidate_cache()
    after = build_graph(tmp_path)
    assert set(after.in_edges("hub")) == set(before.in_edges("hub"))


def test_one_changed_note_does_not_reassemble_the_whole_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One changed note does not reassemble the whole graph.

    Counts `_assemble_graph` calls rather than timing. The `invalidate_cache()` between builds is
    what `kg/git_writer.py` does after every note write.
    """
    _fresh_caches(monkeypatch)
    assemblies = {"count": 0}
    real_assemble = graph._assemble_graph

    def _counting(notes: list) -> object:  # type: ignore[type-arg]
        assemblies["count"] += 1
        return real_assemble(notes)

    monkeypatch.setattr(graph, "_assemble_graph", _counting)
    _corpus(tmp_path)
    build_graph(tmp_path)
    assert assemblies["count"] == 1  # cold: there is nothing to patch from

    (tmp_path / "hub.md").write_text(_linked("hub", ("mourner",)), encoding="utf-8")
    graph.invalidate_cache()
    patched = build_graph(tmp_path)
    assert assemblies["count"] == 1  # the change was patched in
    _assert_identical(patched, _rebuilt(tmp_path))


def test_a_graph_already_handed_out_is_not_mutated_by_a_later_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph already handed out is not mutated by a later patch.

    Every caller shares one frozen instance, and a NetworkX mutation during iteration raises in
    whatever query is running, so patching happens on a copy.
    """
    _fresh_caches(monkeypatch)
    _corpus(tmp_path)
    held = build_graph(tmp_path)
    snapshot = (set(held.nodes), set(held.edges))

    (tmp_path / "hub.md").write_text(_linked("hub", ("mourner",)), encoding="utf-8")
    graph.invalidate_cache()
    fresh = build_graph(tmp_path)

    assert fresh is not held
    assert (set(held.nodes), set(held.edges)) == snapshot
    assert set(fresh.edges) != snapshot[1]


def test_a_wholesale_change_falls_back_to_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past `_MAX_PATCHED_FRACTION` the patch is declined, a rebuild being the cheaper one there.

    Declining is never wrong — it is what this module did before — so the bound is deliberately set
    below the measured break-even rather than at it.
    """
    _fresh_caches(monkeypatch)
    _corpus(tmp_path)
    build_graph(tmp_path)
    assemblies = {"count": 0}
    real_assemble = graph._assemble_graph

    def _counting(notes: list) -> object:  # type: ignore[type-arg]
        assemblies["count"] += 1
        return real_assemble(notes)

    monkeypatch.setattr(graph, "_assemble_graph", _counting)
    for n in range(10):
        (tmp_path / f"citer-{n}.md").write_text(
            _linked(f"citer-{n}", ("mourner",)), encoding="utf-8"
        )
    graph.invalidate_cache()
    rebuilt = build_graph(tmp_path)
    assert assemblies["count"] == 1
    _assert_identical(rebuilt, _rebuilt(tmp_path))


def test_a_note_that_will_not_open_keeps_its_entry_rather_than_vanishing(tmp_path: Path) -> None:
    """A note that will not open keeps its fingerprint entry rather than vanishing.

    `reindex_notes` prunes on that set, so an unreadable but present file must not read as deleted.
    The fault is a directory wearing a note's name, which raises `IsADirectoryError` at any
    privilege (a `chmod 0o000` file is readable as root). The entry must also be stable, or the note
    would be re-embedded on every pass.
    """
    (tmp_path / "a.md").mkdir()
    (tmp_path / "b.md").write_text(_note("b", []), encoding="utf-8")

    broken = graph.note_file_fingerprints(tmp_path)
    assert "a" in broken, "a note that would not open read as deleted and would be retired"
    assert broken["a"] == graph.UNREADABLE
    assert broken["b"].startswith("sha256:")
    assert graph.note_file_fingerprints(tmp_path) == broken, "the marker churns between passes"

    # And it re-embeds exactly once when the fault clears, rather than never.
    (tmp_path / "a.md").rmdir()
    (tmp_path / "a.md").write_text(_note("a", []), encoding="utf-8")
    healed = graph.note_file_fingerprints(tmp_path)
    assert healed["a"].startswith("sha256:")
    assert healed["a"] != broken["a"]
    assert graph.note_file_fingerprints(tmp_path) == healed
