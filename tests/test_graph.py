"""Behavioral tests for the NetworkX indexer and validation (plan steps 2.3, 2.4)."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    """Resilient is not the same as silent, and the indexer was both.

    `kg-validate` reports an unparseable note — over the repository, in CI. Nothing reported it
    over the tree a pod is actually serving, where a partial sync or a truncated write leaves the
    deployment retrieving less than it should with no signal anywhere. The skip is still the right
    behaviour; being unable to tell it happened was not.
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
    """A note whose file is not `<id>.md` is refused: the note index keys on that filename.

    `note_file_fingerprints` reads the id out of `path.stem` while `reindex_notes` looks it up by
    the frontmatter id, so a mismatch used to drop the note out of retrieval in silence. The
    indexer now re-embeds it instead, and this is what stops one merging in the first place.
    """
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


def test_concurrent_cold_reads_parse_the_corpus_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads that miss the cache together wait for one parse instead of each doing their own.

    The defect this pins was measured rather than reasoned about: on a 2,000-note corpus a single
    cold `load_notes` cost 198 ms, four concurrent ones 2,521 ms and eight 6,219 ms — 31x the work
    for 8x the callers, because eight parses of one tree contend on the GIL as well as duplicating
    each other. It is reachable on every process start and after every `invalidate_cache`, with a
    `gather_evidence` sweep offloading `load_notes` to a thread per source.

    Counted rather than timed: a wall-clock assertion would be a flaky machine-speed test, while
    "how many times was the corpus parsed" is the thing that was wrong.
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
    """The assembly is shared by the same lock as the parse, not merely the parse.

    `build_graph` holds the corpus lock across `cached_notes` *and* `_assemble_graph`, which is why
    that lock is re-entrant. Without the outer hold, eight cold builders would share one parse and
    then each assemble their own graph.
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
    """With caching off the corpus lock is not taken — nobody is filling a cache to share.

    Pinned because the lock is skipped by an explicit branch in `_corpus_lock`, and a branch that
    silently stopped skipping would turn "always re-parse" into "re-parse, one at a time" — a
    different contract from the one `graph_cache_enabled=false` states.
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
    """Two files claiming one id resolve to the first in path order, loudly.

    Both notes used to be returned and `add_node` then let whichever file sorted *last* replace the
    other — so one of two curated notes was unreachable by every query, the winner decided by a
    directory name, and nothing anywhere said so. `kg-validate` fails a duplicate in the repository;
    this is about the tree a pod is serving, where an rsync landing a rename before removing the old
    file produces exactly this.
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
    """The stat scan and the parse name the *same* file when two claim one id.

    They disagreed: the parse kept both notes and the graph kept the last, while this scan was a
    dict comprehension whose last entry won. `reindex_notes` diffs one against the other, so a
    disagreement meant embedding one file's text under the other's id.
    """
    (tmp_path / "compound").mkdir()
    (tmp_path / "reaction").mkdir()
    (tmp_path / "compound" / "x.md").write_text(_note("x", [], "compound"), encoding="utf-8")
    time.sleep(0.01)  # a distinct mtime, so the two files' fingerprints cannot coincide
    (tmp_path / "reaction" / "x.md").write_text(_note("x", [], "reaction"), encoding="utf-8")

    fingerprints = graph.note_file_fingerprints(tmp_path)
    first = (tmp_path / "compound" / "x.md").stat()
    assert fingerprints["x"] == f"{first.st_mtime_ns}:{first.st_size}"
