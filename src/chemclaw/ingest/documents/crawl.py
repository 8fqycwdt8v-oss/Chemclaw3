"""Walk the mounted share and decide what is worth opening — without opening anything.

The whole cost model of a TB share lives in this module. A crawl of 500k files that reads nothing
is a `scandir` pass measured in minutes; a crawl that reads each file to find out what it is would
be measured in days and would parse a decade of scanned archives to discover they say nothing. So
every filter here — extension, exclusion, size — runs on the directory entry and its `stat`, and a
file's bytes are read only after the sync has confirmed its fingerprint moved.

The walk is **deterministic and totally ordered**: roots in sorted order, entries sorted within
each directory. That is what lets a bounded chunk resume from `after` while keeping no state at
all — the same "the cursor is a position in a total order" trick the ELN sync plays on timestamps.

**Nothing here writes.** The share is mounted read-only and treated as read-only anyway: there is
no code path in this package that opens a file for writing, creates one, or removes one.
"""

import fnmatch
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from chemclaw.ingest.documents.binding import DocumentShareBinding, DocumentShareError, RootBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRef:
    """One candidate document: where it is, what it weighs, and what its path says about it."""

    # Mount-relative, POSIX-separated. The index key, the citation, and the walk's sort order.
    path: str
    absolute: str
    size: int
    mtime_ns: int
    tags: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """The stat signature that decides whether this file must be read again.

        `mtime_ns:size` — the same shape `note_index` stores (D-2026-08-02-embed-only-what-changed).
        It is not a content hash and does not pretend to be: it costs no read, and the one thing it
        can miss (a rewrite that preserves both mtime and size) does not happen to documents a
        human edited.
        """
        return f"{self.mtime_ns}:{self.size}"


@dataclass
class CrawlResult:
    """One bounded pass over the share: what to consider, and what was skipped or unreachable."""

    files: list[FileRef] = field(default_factory=list)
    # More entries remain past `cursor` — the sync comes back with `after=cursor`.
    has_more: bool = False
    # The last entry this pass *examined*, accepted or not. The resume point, and deliberately not
    # "the last accepted file": everything between the last accepted file and where the chunk
    # stopped would then be re-examined next time and counted twice in the skip tallies. A counter
    # that inflates during a drain is worse than no counter, because it is read as a measurement.
    cursor: str = ""
    # Roots that could not be walked to completion. **Prune safety depends on this being honest**:
    # a share that failed to mount presents as an empty directory, and pruning on that would delete
    # the whole corpus. Anything in here means "delete nothing this run".
    failed_roots: list[str] = field(default_factory=list)
    skipped_oversized: int = 0
    # Per-extension counts of everything the allowlist turned away. Reported rather than dropped:
    # an operator has to be able to see that 40% of the share is `.doc` before concluding the
    # corpus is complete.
    skipped_unsupported: Counter[str] = field(default_factory=Counter)


def _is_excluded(relative: str, patterns: list[str]) -> bool:
    """Whether a mount-relative path matches any exclusion glob (also matched per basename)."""
    name = relative.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns
    )


def _extension_of(name: str) -> str:
    """The lowercased suffix of a file name, `""` when it has none."""
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


class _Walk:
    """One pass's mutable state, so the recursive walk stays a plain function of the binding."""

    def __init__(self, binding: DocumentShareBinding, after: str, limit: int) -> None:
        self.binding = binding
        self.mount = Path(binding.mount).resolve()
        self.after = after
        self.limit = limit
        self.result = CrawlResult()

    def _within_mount(self, path: Path) -> bool:
        """Whether a followed link still lands inside the mount (only asked when following)."""
        try:
            return path.resolve().is_relative_to(self.mount)
        except OSError:
            return False

    def _accept(self, entry: os.DirEntry[str], relative: str, root: RootBinding) -> bool:
        """Record one file if it passes every filter; return False once the chunk is full."""
        if len(self.result.files) >= self.limit:
            self.result.has_more = True
            return False
        self.result.cursor = relative
        extension = _extension_of(entry.name)
        if extension not in self.binding.extension_set:
            self.result.skipped_unsupported[extension or "(none)"] += 1
            return True
        try:
            stat = entry.stat(follow_symlinks=self.binding.follow_symlinks)
        except OSError:
            logger.warning("could not stat %s; skipping", relative)
            return True
        if stat.st_size > self.binding.max_file_bytes:
            self.result.skipped_oversized += 1
            return True
        tags = list(root.tags)
        if root.tag_from_path is not None:
            below = relative[len(root.path) + 1 :] if root.path != "." else relative
            derived = root.tag_from_path.extract(below)
            if derived:
                tags.append(derived)
        self.result.files.append(
            FileRef(
                path=relative,
                absolute=str(entry.path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                tags=tuple(dict.fromkeys(tags)),
            )
        )
        return True

    def descend(self, directory: Path, root: RootBinding) -> bool:
        """Walk one directory in sorted order; return False when the chunk filled up.

        Sorted rather than in filesystem order because the walk's position *is* the resume cursor:
        an unordered walk would have to remember every path it had already seen to make progress.

        Raises:
            OSError: The directory could not be listed — the caller records the root as failed so
                nothing is pruned from a share that may simply be half-mounted.
        """
        with os.scandir(directory) as entries:
            listing = sorted(entries, key=lambda item: item.name)
        for entry in listing:
            relative = PurePosixPath(entry.path).relative_to(self.mount).as_posix()
            if _is_excluded(relative, self.binding.exclude):
                continue
            if entry.is_symlink() and not self.binding.follow_symlinks:
                continue
            if entry.is_symlink() and not self._within_mount(Path(entry.path)):
                logger.warning("%s links outside the mount; skipping", relative)
                continue
            if entry.is_dir(follow_symlinks=self.binding.follow_symlinks):
                if not self.descend(Path(entry.path), root):
                    return False
                continue
            # Already-visited region of the total order: this is how a bounded chunk resumes.
            if self.after and relative <= self.after:
                continue
            if not self._accept(entry, relative, root):
                return False
        return True


def crawl_share(
    binding: DocumentShareBinding, *, after: str = "", limit: int = 1000
) -> CrawlResult:
    """Walk the share's roots in order, returning up to `limit` candidate documents past `after`.

    Args:
        binding: The share's declared layout.
        after: The mount-relative path the previous chunk stopped at; `""` starts from the top.
        limit: How many candidates one chunk may carry.

    Returns:
        The candidates, whether more remain, what was skipped, and which roots could not be walked.

    Raises:
        DocumentShareError: The mount itself is not there — an unmounted share, not an empty one.
            Loud on purpose: every other failure mode here degrades to "index less", and this one
            would otherwise degrade to "the share is empty", which is the one wrong answer.
    """
    walk = _Walk(binding, after, limit)
    if not walk.mount.is_dir():
        raise DocumentShareError(
            f"share mount {binding.mount!r} is not a directory — the volume is not mounted"
        )
    # Sorted, not in declaration order: the resume cursor is a position in one lexical order over
    # the whole share, and every path under a root begins with that root's own name. Walking roots
    # out of order would make the concatenated stream non-monotonic, and `after` would then skip
    # every file in a later-walked root that happens to sort earlier.
    for root in sorted(binding.roots, key=lambda item: item.path):
        directory = walk.mount if root.path == "." else walk.mount / root.path
        if not directory.is_dir():
            logger.error("root %r of share mount %s is missing", root.path, binding.mount)
            walk.result.failed_roots.append(root.path)
            continue
        try:
            if not walk.descend(directory, root):
                break
        except OSError:
            logger.error("root %r could not be walked; nothing will be pruned", root.path)
            logger.debug("walk failure detail for %r", root.path, exc_info=True)
            walk.result.failed_roots.append(root.path)
    return walk.result
