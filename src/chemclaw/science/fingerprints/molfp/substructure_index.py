"""A pre-parsed, pattern-screened substructure index over one corpus slice, cached across queries.

Wraps `rdkit.Chem.rdSubstructLibrary`: molecules are held pre-parsed and a `PatternHolder` screens
each one before subgraph matching. Building costs about three times one per-record scan, so the
index pays only when cached; the cache is the point.

An index is an optimisation, never a precondition: `index_for` returns `None` when one cannot be had
(its own build budget, `substructure_index_build_timeout_seconds`, or the caller's deadline ran
out), and the scan falls back to the per-record loop. The build budget is spent as a projection, so
an unindexable corpus is detected in milliseconds.

The cache is keyed on a digest of the stored labels in order, because the store has no revision
signal: an upsert rewrites a row's label in place, so counts or max ids would not move.
"""

import hashlib
import logging
import threading
import time
from typing import Any

from rdkit import Chem
from rdkit.Chem import rdSubstructLibrary

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintRecord

log = logging.getLogger(__name__)


class ScanDeadlineExceeded(TimeoutError):
    """A substructure scan stopped on its deadline, carrying how far it actually got.

    A `TimeoutError` subclass, so every `except TimeoutError` upstream keeps working. `reached` lets
    a caller assert on records scanned rather than on wall-clock ratios.
    """

    def __init__(self, reached: int, total: int, because: str = "") -> None:
        """Record the cut-off point and which scan path ran out.

        `because` names the path, since a corpus too large to index and one too slow to match have
        different remedies.
        """
        detail = f", {because}" if because else ""
        super().__init__(
            f"substructure scan gave up after {reached} of {total} molecule(s){detail}"
        )
        self.reached = reached
        self.total = total


# The first chunk is one record, so the first deadline check comes after one record's work; later
# chunks are sized from what it cost.
_FIRST_CHUNK = 1

# Maximum growth of one chunk over the previous one. Corpora are not uniform, so a cheap sample must
# not propose a chunk covering the rest of the corpus and skip the deadline.
_CHUNK_GROWTH = 4

# Records a build parses between checks on its budget and the caller's deadline.
_BUILD_CHECK_STRIDE = 64


class CorpusIndex:
    """One corpus slice, parsed once and screened by pattern fingerprint on every later query.

    Holds the `SubstructLibrary`, the stored labels in library-index order (a hit is reported as the
    label a chemist cites), and how many rows did not parse. `unreadable` is counted over the whole
    slice at build time, so `scan_truncated` is a property of the corpus, not of where a query
    stopped.

    The holder is `CachedMolHolder` over `mol.ToBinary()`, so the matched molecule is exactly what
    `Chem.MolFromSmiles` produced, and no stored label goes through `Chem.MolToSmiles`, which can
    crash uncatchably on very large molecules.
    """

    def __init__(self, library: Any, labels: tuple[str, ...], unreadable: int) -> None:
        """Hold a built library with its labels and the rows that did not parse into it."""
        self.library = library
        self.labels = labels
        self.unreadable = unreadable

    def __len__(self) -> int:
        """How many molecules the library holds — the unreadable rows are not among them."""
        return len(self.labels)

    def labels_matching(self, pattern: Chem.Mol, limit: int, deadline: float) -> list[str]:
        """Return up to `limit` stored labels containing `pattern`, in stored order.

        `limit` is the result cap plus one, so "more than cap" is observed rather than inferred. The
        deadline (`time.monotonic()`) is checked in the worker thread, since `asyncio.wait_for`
        cannot stop it; `GetMatches` is uninterruptible, so the scan runs in time-sliced chunks
        (`substructure_scan_deadline_slice_seconds` at the previous chunk's rate), starting at one
        record and growing by at most `_CHUNK_GROWTH`, because per-molecule cost spans orders of
        magnitude. Raises `ScanDeadlineExceeded` when the deadline passes.

        `useChirality=False` is explicit: `GetMatches` defaults it to True while `HasSubstructMatch`
        defaults to False, and the default would silently drop stereo-specified matches.
        """
        found: list[str] = []
        total = len(self.labels)
        start = 0
        chunk = _FIRST_CHUNK
        slice_seconds = settings.substructure_scan_deadline_slice_seconds
        while start < total and len(found) < limit:
            if time.monotonic() >= deadline:
                raise ScanDeadlineExceeded(start, total)
            end = min(start + chunk, total)
            began = time.monotonic()
            hits = self.library.GetMatches(
                pattern,
                start,
                end,
                True,  # recursionPossible, as `HasSubstructMatch` defaults it
                False,  # useChirality — see the docstring; the default would change the answer
                False,  # useQueryQueryMatches
                1,  # numThreads: one scan is one worker thread — see `_build`
                limit - len(found),
            )
            found.extend(self.labels[index] for index in hits)
            per_record = (time.monotonic() - began) / (end - start)
            projected = total if per_record <= 0 else int(slice_seconds / per_record)
            chunk = max(1, min(projected, chunk * _CHUNK_GROWTH))
            start = end
        return found


def _corpus_digest(labels: list[str]) -> bytes:
    """The cache key: what the index would be built from, hashed in order.

    Labels only, because the index and its hit labels are a pure function of them; keying on more
    would evict a still-correct index. blake2b at 16 bytes, since this is a cache key, not a
    signature.
    """
    digest = hashlib.blake2b(digest_size=16)
    for label in labels:
        digest.update(label.encode())
        digest.update(b"\x00")  # so ["ab","c"] and ["a","bc"] cannot collide
    return digest.digest()


# Bounded by entry count; each entry is bounded by `substructure_scan_max_records`, so no byte
# weight is needed. Capacity is read live from settings so it stays ENV-overridable.
_INDEXES: BoundedLru[bytes, CorpusIndex] = BoundedLru(
    lambda: settings.substructure_index_cache_entries
)


class _BuildSlot:
    """The lock one corpus's build is single-flighted on, and how many callers hold a reference.

    One lock per corpus, not per module: a second caller for the same corpus waits instead of
    repeating the build, while a caller for a different corpus must not wait at all. The slot is
    dropped when its last waiter leaves, because a key is a corpus generation and a lock per key
    kept forever would grow without bound.
    """

    def __init__(self) -> None:
        """A free lock nobody is waiting on yet."""
        self.lock = threading.Lock()
        self.waiters = 0


# `BoundedLru` is not thread-safe and this map is reached from worker threads. Held for a map
# operation, never across a build; builds single-flight on `_BUILDS[key].lock`.
_GUARD = threading.Lock()
_BUILDS: dict[bytes, _BuildSlot] = {}


def index_for(records: list[FingerprintRecord], deadline: float) -> CorpusIndex | None:
    """Return the index for exactly these records, or None when the scan must do without one.

    Synchronous: every caller is already inside `asyncio.to_thread`, so the CPU-bound build stays
    off the event loop. `None` is a normal answer (caller out of time, corpus too large for the
    build budget, or another thread's build outlasting this caller's `deadline`), and the scan then
    answers record by record. Concurrent misses on one corpus build once; a waiter waits only until
    its own deadline.
    """
    labels = [record.label for record in records]
    key = _corpus_digest(labels)
    with _GUARD:
        held = _INDEXES.get(key)
        if held is not None:
            return held
        if time.monotonic() >= deadline:
            # No time to build or scan; the scan reports the refusal because it knows how much it
            # examined.
            return None
        slot = _BUILDS.setdefault(key, _BuildSlot())
        slot.waiters += 1
    try:
        if not slot.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            log.info(
                "another thread is still indexing these %d molecule(s); scanning without an index",
                len(labels),
            )
            return None
        try:
            with _GUARD:
                held = _INDEXES.get(key)
            return held if held is not None else _build_and_cache(key, labels, deadline)
        finally:
            slot.lock.release()
    finally:
        with _GUARD:
            slot.waiters -= 1
            if not slot.waiters:
                del _BUILDS[key]


def _build_and_cache(key: bytes, labels: list[str], deadline: float) -> CorpusIndex | None:
    """Build the index for `labels` under its own budget, cache it, and return it — or None.

    The budget is `substructure_index_build_timeout_seconds`, not the match bound, so an unindexable
    corpus stays searchable. The caller's deadline also applies; whichever comes first stops the
    build. Abandoned builds are not cached.
    """
    budget = settings.substructure_index_build_timeout_seconds
    try:
        built = _build(labels, budget, min(time.monotonic() + budget, deadline))
    except TimeoutError as exc:
        log.warning(
            "not indexing %d molecule(s): %s; scanning record by record instead "
            "(raise CHEMCLAW_SUBSTRUCTURE_INDEX_BUILD_TIMEOUT_SECONDS to index a corpus this "
            "large, or lower CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS)",
            len(labels),
            exc,
        )
        return None
    with _GUARD:
        _INDEXES.put(key, built)
    return built


def _build(labels: list[str], budget: float, deadline: float) -> CorpusIndex:
    """Parse every label once, hold it pre-parsed, and screen it with a pattern fingerprint.

    Every `_BUILD_CHECK_STRIDE` records the build projects its rate over the whole corpus and raises
    `TimeoutError` once that exceeds `budget`, or once the caller's `deadline` passes, so a refusal
    is cheap and an abandoned caller leaves no thread indexing. Pattern fingerprints are added in
    the same loop rather than by one uninterruptible `AddPatterns` call, so the check covers the
    whole build and the two holders stay index-aligned.

    Single-threaded: this runs in the shared default executor, and `numThreads=-1` would take every
    core for one query.
    """
    molecules = rdSubstructLibrary.CachedMolHolder()
    patterns = rdSubstructLibrary.PatternHolder()
    kept: list[str] = []
    unreadable = 0
    began = time.monotonic()
    for examined, label in enumerate(labels):
        if examined % _BUILD_CHECK_STRIDE == 0:
            elapsed = time.monotonic() - began
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"indexing gave up after {examined} of {len(labels)} molecule(s): the query "
                    "that asked for the index has run out of time"
                )
            projected = elapsed * len(labels) / examined if examined else 0.0
            if projected > budget:
                raise TimeoutError(
                    f"indexing {len(labels)} molecule(s) projects to {projected:.1f}s at the rate "
                    f"its first {examined} took, past the {budget}s build budget"
                )
        molecule = Chem.MolFromSmiles(label)
        if molecule is None:
            unreadable += 1
            continue
        molecules.AddBinary(molecule.ToBinary())
        patterns.AddMol(molecule)
        kept.append(label)
    log.info(
        "built substructure index over %d molecule(s) in %.0f ms (%d unreadable)",
        len(kept),
        (time.monotonic() - began) * 1000,
        unreadable,
    )
    return CorpusIndex(
        rdSubstructLibrary.SubstructLibrary(molecules, patterns), tuple(kept), unreadable
    )
