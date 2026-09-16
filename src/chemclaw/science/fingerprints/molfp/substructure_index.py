"""A pre-parsed, pattern-screened substructure index over one corpus slice, cached across queries.

`find_substructure_matches` used to re-parse **every stored SMILES on every query** — one
`Chem.MolFromSmiles` per record per call — and then ask each molecule for a subgraph match. RDKit
has shipped `rdkit.Chem.rdSubstructLibrary` since 2017 for exactly that shape: molecules are held
pre-parsed in C++, and a `PatternHolder` screens each one with a pattern fingerprint before any
subgraph isomorphism is attempted. This module is that index, and the lifecycle that makes it
worth having.

**The lifecycle is the whole point, and this is said plainly because the obvious adoption is a
loss.** Measured here on the 4,999-row NCI `first_5K` corpus (4,991 parse, 8 do not), rdkit
2026.03.5, one thread:

| query | today's loop | this index | speed-up |
| --- | --- | --- | --- |
| amide `C(=O)N` (671 hits) | 325.8 ms | 12.9 ms | 25.4x |
| aryl amide `c1ccccc1C(=O)N` (108 hits) | 392.9 ms | 16.5 ms | 23.8x |
| `[Se]` (4 hits) | 364.6 ms | 16.3 ms | 22.3x |
| adversarial `C(~*)(~*)(~*)~*` (620 hits) | 347.4 ms | 41.4 ms | 8.4x |

and in the same run **building the index costs 1,155 ms** — parsing every record once, serialising
it into the holder, and computing 4,991 pattern fingerprints. A per-query rebuild is therefore
about three times *slower* than the loop it replaces.

Those milliseconds are one run on one shared 4-core container, and **the ratio is the claim rather
than the absolute figure**: re-measured on the same commit while the box was busy, every number
grew by about 1.8x — 670 ms against 25.9 ms for the amide, 2,195 ms to build — while the speed-ups
held to within noise (25.9x, 22.6x, 23.0x, 7.4x). Re-measuring here and reading a bigger number as
a regression is the mistake that invites.

Every millisecond above is a millisecond only a *cached* index can save, so the cache below is not
an optimisation on top of this adoption: it is the adoption.

**An optimisation that cannot be had must therefore be *skipped*, never fatal — and the first
version of this module got that exactly backwards.** The build was charged against
`substructure_match_timeout_seconds`, the bound on *matching*, and an abandoned build caches
nothing: measured on 19,996 NCI records at the shipped 5.0 s, the build ran out of that budget at
11,776, 13,440 and 14,976 molecules on three successive attempts and the search **failed every
time**, where the per-record loop it replaced answered the same query in 2.01 s with 2,684 hits. A
corpus past roughly 20,000 rows could never produce an index and so could never produce an answer,
and the code's own advice was how you got there: `search.py` logs "raise
CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS" when the cap truncates, and doing so turned a
truncated-but-useful answer into a permanent refusal.

Two changes, and they are one idea. **The build has its own budget**
(`substructure_index_build_timeout_seconds`), because how long an index may take to *build* and how
long a chemist may wait for an *answer* are different questions that were sharing one number. And
**`index_for` returns `None` rather than raising** when there is no index to be had, so the scan
falls back to the per-record loop this module replaced — which is the floor this adoption has to
beat and is therefore also the floor it must fall back to. The same call now answers the 19,996-row
corpus in ~2 s instead of failing, for the same reason the old code could: it matches record by
record.

The budget is spent as a *projection* rather than as a stopwatch run to exhaustion: every
`_BUILD_CHECK_STRIDE` records the build extrapolates its own measured rate over the whole corpus
and gives up the moment that projection exceeds the budget. So a corpus that cannot be indexed
costs the query tens of milliseconds to find that out, not the whole budget, and no memory of the
refusal is needed to keep the cost off later queries.

**The cache is keyed on the corpus it was built from, because the store offers no revision to key
it on.** `molecule_fingerprints` carries `created_at` and nothing else that moves — an upsert
rewrites `label` and `bits` in place (`store.py`'s `_upsert`), so neither `count()` nor a max id
nor a max `created_at` changes when a row's *structure string* changes, and a cache keyed on any
of them would answer a chemist from a corpus that no longer exists. A wrong substructure answer is
a chemist told a precedent does not exist, so the key is a digest of the labels themselves, in
order: 0.71 ms for 4,999 of them, against the 1,560 ms build it protects. That is sound by
construction rather than by a schema promise — two record lists with the same labels in the same
order *are* the same index — and it needs no `FingerprintStore` method, so no backend has a new
contract to satisfy.

It costs the read it digests: `find_substructure_matches` still fetches the capped corpus slice on
every query, and this module only removes the re-parse. On the durable backend that fetch is the
larger half (`store.all_records`'s own docstring measures 2,055 ms for 5,001 rows, most of it the
`bits` column the scan never reads), and skipping *it* would need the revision signal the schema
does not have. Named here so the next reader sees the remaining cost rather than assuming this
closed it.

**This is not `docs/planning/DEFERRED.md`'s "Substructure pattern-fingerprint prefilter" row, and
that row stays open.** That one is the Postgres `pattern_bits` GIN screen back-ported from
`corpus_molecules` to `molecule_fingerprints` — a screen inside the *database*, for the
multi-million-row corpus table, which is what raises `substructure_scan_max_records` itself. This
is an in-process index over the slice that cap already allows. They compose; neither is the other.
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

# The first chunk of a scan is one record, so the first deadline check happens after exactly the
# work the per-record loop this replaces did between its own checks. Everything after it is sized
# from what that one cost — see `CorpusIndex.labels_matching`.
_FIRST_CHUNK = 1

# How fast a chunk may grow from the one before it. The chunk is sized from the *observed* cost of
# the records already scanned, and a corpus is not uniform: 4,991 small molecules followed by one
# 121-atom dendrimer would let a single cheap sample propose a chunk covering the rest of the
# corpus, and the deadline would then not be consulted again. Quadrupling reaches the whole shipped
# 5,000-record cap in seven calls (~0.5 ms of per-call overhead, measured below) while keeping the
# scan's step size within 4x of a cost it has actually seen.
_CHUNK_GROWTH = 4

# How many records a build parses between checks on its own budget and the caller's deadline.
# See `_build`.
_BUILD_CHECK_STRIDE = 64


class CorpusIndex:
    """One corpus slice, parsed once and screened by pattern fingerprint on every later query.

    Holds three things a query needs and must not recompute: the RDKit `SubstructLibrary`, the
    stored labels in library-index order (a hit is reported as the label a chemist would cite,
    which is the `id`-ordered SMILES the store handed over), and how many rows of the slice could
    not be parsed at all.

    **`unreadable` is counted here, at build time, and that is a deliberate move.**
    `CachedTrustedSmilesMolHolder` skips sanitisation — that is where its speed comes from — so
    nothing inside the library would ever notice a malformed row; the parse has to happen
    somewhere, and the build is the one place that pays for it once instead of per query. The count
    still reaches the answer the same way (`ScanOutcome.unreadable`, folded into `scan_truncated`),
    because "a corpus whose one azide row carried a malformed label still answered 'this is a
    genuine negative result'" is the defect that counting fixed and it is unchanged by where the
    parse happens.

    One consequence is stated rather than left to be discovered: the count is now over the *whole*
    slice, where the per-record loop stopped counting wherever it stopped scanning — so a query
    that fills `fingerprint_max_top_k` early no longer hides an unreadable row further down. That
    is the conservative direction (`scan_truncated` can now be True where it was False), it makes
    the flag a property of the corpus rather than of the query's hit distribution, and
    `tests/test_molfp.py` pins it.

    **The holder is `CachedMolHolder` over `mol.ToBinary()`, not a trusted-SMILES holder**, for two
    reasons that both come out of this tree. The molecule the library matches is then bit-for-bit
    the molecule `Chem.MolFromSmiles` produced — the same object today's loop matched — so
    agreement is structural rather than a property of a SMILES round trip preserving aromaticity
    and stereo. And a trusted holder needs `Chem.MolToSmiles` per record, which
    `core/config/fingerprints.py` records as an *uncatchable* crash between 16,000 and 20,000 atoms
    (`molecule_max_atoms` guards the callers that go through `require_molecule`; a stored label does
    not). Writing the corpus back out through that writer would put every indexed row through a
    segfault the current code never touches. It is also the cheaper half: measured the same way
    on the corpus above, 1,560 ms of build against 1,664 ms.
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

        `limit` is the caller's result cap **plus one**, and it must stay that way: `maxResults=cap`
        would make "I returned cap hits" and "there were more than cap" the same observation, which
        is precisely the `len(matches) == cap` inference `_scan_for_matches` was written to refuse.
        One surplus hit is what turns the flag from an inference into an observation, and it is what
        `GetMatches`' own `maxResults` buys over scanning to the end: the search stops at the first
        match it cannot return instead of finding every remaining one.

        **The deadline reaches the worker thread, which is the property this must not lose.**
        `asyncio.wait_for` releases the caller and cannot stop a thread, so the scan checks the
        clock itself — see `find_substructure_matches` for the 343 ms-per-molecule measurement that
        makes an orphaned scan thread cost ~28 minutes of the loop's default executor, the same
        executor `chemclaw.api.auth` validates bearer tokens on.

        **What changes is the granularity, and the unit it is expressed in.** The loop checked
        before *each record*; a C++ `GetMatches` call cannot be interrupted, so the check can only
        happen between calls, and the scan is chunked to make that interval short. A chunk of a
        fixed number of records would be the wrong unit — the whole reason the deadline exists is
        that per-molecule cost spans five orders of magnitude here (~6 µs for a functional-group
        SMARTS on caffeine, ~117 ms for a 16-atom recursive pattern on a 121-atom dendrimer), so
        500 records is 7 ms on one corpus and 58 s on another. So a chunk is a **time slice**:
        `substructure_scan_deadline_slice_seconds` of work at the rate the preceding chunk actually
        ran at, starting from one record and growing by at most `_CHUNK_GROWTH` a step. On a
        pathological corpus that settles at one or two molecules — today's granularity — and on an
        ordinary one it reaches the whole corpus in about seven calls.

        Chunking at all is nearly free, which is what makes the choice available. Measured over the
        4,991-molecule index, whole-corpus call against this sliced scan: amide 19.3 ms / 15.6 ms,
        aryl amide 19.0 ms / 18.6 ms, `[Se]` 23.9 ms / 19.1 ms, adversarial 53.0 ms / 53.2 ms —
        within run-to-run noise. The fixed cost per `GetMatches` call is the query's own pattern
        fingerprint, ~69 µs, which is why a chunk of 1 over the whole corpus costs 346 ms (i.e. the
        loop's own price) and anything from ~128 upwards costs nothing measurable.

        **`useChirality=False` is passed explicitly and is not a detail.** `GetMatches` defaults it
        to True while `Mol.HasSubstructMatch` defaults it to False, so taking the default would
        have silently changed the answer: measured on this corpus, `[C@H](O)(C)C` matches **514**
        molecules through `HasSubstructMatch` and **0** through `GetMatches` at its default — a
        clean "no precedent exists" for 514 molecules that are on file.

        Args:
            pattern: The compiled query.
            limit: How many matches to collect before stopping — the result cap plus one.
            deadline: `time.monotonic()` value past which the scan stops.

        Returns:
            The matching labels in stored order, at most `limit` of them.

        Raises:
            TimeoutError: The deadline passed before the whole slice was scanned. The caller turns
                it into the same `FingerprintError` `asyncio.wait_for` produces.
        """
        found: list[str] = []
        total = len(self.labels)
        start = 0
        chunk = _FIRST_CHUNK
        slice_seconds = settings.substructure_scan_deadline_slice_seconds
        while start < total and len(found) < limit:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"substructure scan gave up after {start} of {total} molecule(s)"
                )
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

    Labels only, because the library and its hit labels are a pure function of them — two record
    lists spelling the same structures in the same order build the same index, and nothing else on
    a `FingerprintRecord` reaches the answer (the id is not returned, and the bits are never read
    by this search). Keying on more would evict a still-correct index for a change it does not see.

    blake2b at 16 bytes: this is a cache key, not a signature, and the short digest keeps the map
    small. 0.73 ms over 4,999 labels, against the 1,155 ms build it decides.
    """
    digest = hashlib.blake2b(digest_size=16)
    for label in labels:
        digest.update(label.encode())
        digest.update(b"\x00")  # so ["ab","c"] and ["a","bc"] cannot collide
    return digest.digest()


# Bounded by entry count alone, and the entry's own size is bounded by the caller: an index holds
# at most `substructure_scan_max_records` molecules, measured at **863 bytes each** (1.6 MB of
# binary molecules and 2.5 MB of pattern fingerprints for 4,991 rows), so the ceiling is
# `substructure_index_cache_entries x substructure_scan_max_records x ~863 B` — ~8.4 MB at the
# shipped defaults. That is why this passes no `weight`/`max_weight`: a second bound in bytes would
# restate a bound the record cap already holds.
#
# Capacity is read live from settings on every eviction pass (`BoundedLru` takes a callable for
# exactly this), so the bound stays ENV-overridable without this module re-reading config.
_INDEXES: BoundedLru[bytes, CorpusIndex] = BoundedLru(
    lambda: settings.substructure_index_cache_entries
)


class _BuildSlot:
    """The lock one corpus's build is single-flighted on, and how many callers hold a reference.

    **One lock per corpus rather than one for the module**, because the two things a caller waits
    for are not the same thing. "Is *this* corpus already being built" must make a second caller
    wait rather than repeat ~1.2 s of CPU on the executor that also validates every bearer token;
    "is *some other* corpus being built" must not make it wait at all. A single module-wide lock
    answered both with the second, and it was taken on every call, hit or miss: driven, a query
    whose own index was **already in the map** refused at its 1.0 s bound while an unrelated
    5,000-molecule corpus was being indexed, where before this module existed the two ran
    concurrently in separate `to_thread` workers. It is worse under ingest than that sounds,
    because the key is a digest of the labels — one new molecule invalidates it, so every
    concurrent query would serialize behind one build.

    `waiters` is what keeps the map bounded, and a digest is why it needs to be: a key is a corpus
    *generation*, so an ingest that rewrites one row mints a new one on every pass, and a lock per
    key kept forever is the unbounded-growth shape `core/bounded.py` exists for. The slot is
    dropped when the last caller holding it leaves, so the map holds one entry per build actually
    in flight.
    """

    def __init__(self) -> None:
        """A free lock nobody is waiting on yet."""
        self.lock = threading.Lock()
        self.waiters = 0


# `BoundedLru` documents itself as not thread-safe, and this map is reached from worker threads
# (`asyncio.to_thread`), so it needs a lock — this one, held for a map operation and never across a
# build. What a build is single-flighted on is `_BUILDS[key].lock`, for the reason `_BuildSlot`
# gives.
_GUARD = threading.Lock()
_BUILDS: dict[bytes, _BuildSlot] = {}


def index_for(records: list[FingerprintRecord], deadline: float) -> CorpusIndex | None:
    """Return the index for exactly these records, or None when the scan must do without one.

    Synchronous on purpose: every caller is already inside `asyncio.to_thread`, so the build —
    which is CPU, and the most expensive thing in this module — happens off the event loop without
    this function knowing anything about it. Running it in-loop would stall every streamed session
    for the duration of a rebuild, which is the same argument that put the matching in a thread.

    **`None` is a normal answer and never an error.** An index is an optimisation over a scan that
    works without one, so every way of not having one — the caller is already out of time, the
    corpus is too large to index inside `substructure_index_build_timeout_seconds`, another
    thread's build for this same corpus is still running when this caller's own bound expires —
    returns `None` and lets `_scan_for_matches` answer record by record. Raising instead is what
    made a corpus past ~20,000 rows unanswerable; the module docstring has the measurement.

    **Concurrent misses on one corpus build once.** The second caller waits on that corpus's own
    slot and then finds the entry rather than repeating the build. It waits only until its *own*
    deadline, and a caller that cannot afford the wait takes the loop instead of blocking a thread
    past the bound it was promised.

    Args:
        records: The capped corpus slice, in store order.
        deadline: `time.monotonic()` value past which this caller stops waiting and stops building.

    Returns:
        The cached or freshly built index over those records, or None if there is none to be had
        within this caller's bounds.
    """
    labels = [record.label for record in records]
    key = _corpus_digest(labels)
    with _GUARD:
        held = _INDEXES.get(key)
        if held is not None:
            return held
        if time.monotonic() >= deadline:
            # The caller has no time left to build *or* to scan. Let the scan say so: it is the one
            # that knows how much of the corpus it examined, which is what the refusal has to name.
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

    **The budget is `substructure_index_build_timeout_seconds` and not the caller's match bound**,
    which is the whole of `D-2026-09-16`'s first defect: charging a build to the budget for matching
    made a corpus that could not be indexed a corpus that could not be *searched*, permanently,
    because nothing is cached when a build is abandoned and every retry therefore started from
    zero. The caller's deadline still applies on top of it — a build must not outlive the query
    that wanted it — so the build stops at whichever comes first, and either way the caller gets
    `None` and scans without it.
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

    **The budget is spent as a projection, not as a stopwatch run to exhaustion.** Every
    `_BUILD_CHECK_STRIDE` records the build extrapolates its own measured rate over the whole
    corpus, and gives up the moment that projection exceeds `budget` — so a corpus too large to
    index costs the query that discovered it tens of milliseconds rather than the entire budget,
    and the caller can fall back to the per-record loop with almost all of its own bound intact.
    Measured on 19,996 NCI records with the shipped 3.0 s budget, that refusal lands in ~0.05 s and
    the loop then answers inside the 5.0 s match bound; run to exhaustion it would have cost 3.0 s
    of the 5.0 s before the loop even started. It also means an unbuildable corpus needs no
    remembering: repeating a refusal that cheap is cheaper than a second cache to avoid it.

    The projection subsumes a plain wall-clock check, since a projection over the whole corpus is
    never less than the time already spent.

    The caller's `deadline` is checked in the same place, for the reason it is checked during the
    scan: this runs in the loop's default executor, and an abandoned caller must not leave a thread
    indexing thousands of molecules behind it. Both are checked every `_BUILD_CHECK_STRIDE` records
    rather than every one, because a `time.monotonic()` per record would be a measurable share of a
    ~0.24 ms step — the stride is in records rather than in time because, unlike matching,
    per-molecule cost here varies by a factor of ten rather than of ten thousand.

    **The pattern fingerprints are computed in this loop rather than by `AddPatterns` afterwards,
    and that is what makes the check above cover the whole build.** `AddPatterns` is one
    uninterruptible C++ call over the finished library and it is the *larger* half — measured,
    1,563 ms of a 2,354 ms build, 66% — so with it the deadline bounded only the parse. Filling the
    `PatternHolder` alongside the mol holder puts every molecule's fingerprint inside the same
    strided check, and it costs nothing to do: 1,201 ms against 1,463 ms for the same corpus, with
    both forms returning identical matches on every query class
    `tests/test_molfp.py::test_the_index_returns_exactly_what_the_per_record_loop_returned` drives.
    Index alignment is what makes the two holders one library, and appending to both in one loop is
    what keeps them aligned.

    One thread throughout, deliberately: this runs in the async front door's default executor while
    other sessions are being served, and RDKit's `numThreads=-1` takes every core for one chemist's
    query. Measured, `-1` is worth ~1.5-2x on a scan (adversarial query 46.2 ms against 25.4 ms) —
    bought by making one search the machine's only work, which is not a trade a shared front door
    gets to make.

    An index is *not* cached when the build is abandoned: a half-built library would answer later
    queries over a fraction of the corpus with no flag saying so, which is the failure this whole
    module is arranged against.

    Args:
        labels: Every stored SMILES of the corpus slice, in store order.
        budget: How many seconds the whole build may take before it is not worth having.
        deadline: `time.monotonic()` value past which the build is abandoned whatever it projects.

    Returns:
        The built index over the labels that parsed.

    Raises:
        TimeoutError: The build projects past `budget`, or `deadline` passed. `_build_and_cache`
            turns either into a `None` index and a scan that runs without one.
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
