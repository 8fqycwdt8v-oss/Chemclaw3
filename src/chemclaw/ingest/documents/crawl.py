"""Walk the mounted share and decide what is worth opening — without opening anything.

Every filter (extension, exclusion, size) runs on the directory entry and its `stat`; a file's bytes
are read only after the sync confirms its fingerprint moved. That is what keeps crawling a large
share cheap.

The walk is deterministic and totally ordered (roots sorted, entries sorted within each directory),
so a bounded chunk resumes from `after` with no other state. Nothing here writes: no code path opens
a file for writing, creates or removes one.
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pathspec

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

        `mtime_ns:size`, the same shape `note_index` stores. Not a content hash: it costs no read,
        and a rewrite preserving both mtime and size does not happen to human-edited documents.
        """
        return f"{self.mtime_ns}:{self.size}"


@dataclass
class CrawlResult:
    """One bounded pass over the share: what to consider, and what was skipped or unreachable."""

    files: list[FileRef] = field(default_factory=list)
    # More entries remain past `cursor` — the sync comes back with `after=cursor`.
    has_more: bool = False
    # The last entry this pass examined, accepted or not — the resume point. Not "the last accepted
    # file", which would re-examine and double-count skipped entries on the next chunk.
    cursor: str = ""
    # Roots that could not be walked to completion. Prune safety depends on this: an unmounted share
    # looks empty, so anything here means "delete nothing this run".
    failed_roots: list[str] = field(default_factory=list)
    # Entries `scandir` listed but that could not be stat'ed. The sync restamps them as seen so a
    # transient `EACCES` does not read as deletion; the mark means "observed to exist", never
    # "processed".
    unreadable: list[str] = field(default_factory=list)
    skipped_oversized: int = 0
    # Per-extension counts of everything the allowlist turned away, reported so an operator can see
    # what share of the corpus is unsupported.
    skipped_unsupported: Counter[str] = field(default_factory=Counter)


def _is_excluded(relative: str, spec: pathspec.GitIgnoreSpec, *, directory: bool = False) -> bool:
    """Whether a mount-relative path is excluded — asked of a directory as well as of a file.

    Gitignore semantics (`binding.exclude_spec`): a pattern without a separator matches at every
    depth, one with a separator is anchored at the mount, and a directory offered as `Archive/`
    matches `**/Archive/**` so its subtree is never listed. Everything under an excluded directory
    is excluded.

    Case-sensitive, which mismatches CIFS; case-folding patterns would silently widen exclusions
    deployments rely on, so it is left as is.

    Args:
        relative: The mount-relative POSIX path of the entry.
        spec: The binding's compiled exclusions.
        directory: True when the entry is a directory, so it is offered in the form gitignore
        recognises as one.
    """
    return spec.match_file(f"{relative}/" if directory else relative)


def _extension_of(name: str) -> str:
    """The lowercased suffix of a file name, `""` when it has none."""
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _within_mount(mount: Path, path: Path) -> bool:
    """Whether a path still lands inside the mount once every link in it is resolved.

    Used by `descend` for a symlink it is about to follow and by `crawl_share` for the root itself,
    which `descend` never sees.
    """
    try:
        return path.resolve().is_relative_to(mount)
    except OSError:
        return False


class _Walk:
    """One pass's mutable state, so the recursive walk stays a plain function of the binding."""

    def __init__(self, binding: DocumentShareBinding, after: str, limit: int) -> None:
        self.binding = binding
        self.mount = Path(binding.mount).resolve()
        self.after = after
        self.limit = limit
        self.result = CrawlResult()
        # The identity — `(st_dev, st_ino)` — of every directory this pass has already walked, so
        # a link back into the mount is followed once instead of forever. See `enter`.
        self.visited: set[tuple[int, int]] = set()
        # Entries whose type could not be read while sorting. Kept apart from
        # `CrawlResult.unreadable`, which the sync acts on by restamping; this is only "the sort key
        # was a guess". See `_order`.
        self.unstattable: list[str] = []

    def enter(self, directory: Path, relative: str) -> bool:
        """Claim `directory` for this walk; False when it is one already walked.

        `_within_mount` catches escape but not a cycle: a link resolving inside the mount (`current
        -> ..`) would otherwise recurse until `scandir` fails on path length, mark the root failed,
        and block pruning for good. Keyed on directory identity rather than path, which also stops
        two roots linked to one tree from indexing it twice; first walked wins, deterministically.

        A directory that cannot be `stat`ed is entered anyway, so the `scandir` failure that follows
        marks the root failed and stops the sweep rather than reading as "these files are gone".
        """
        try:
            stat = directory.stat()
        except OSError:
            return True
        identity = (stat.st_dev, stat.st_ino)
        if identity in self.visited:
            logger.warning(
                "%s is a link back to a directory this share has already walked; not descending "
                "again (the file it holds is indexed under the path that reached it first)",
                relative,
            )
            return False
        self.visited.add(identity)
        return True

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
            # DEBUG, with the path carried in `result.unreadable`: one changed ACL would otherwise
            # be one WARNING per file, and `sync._summarise_skips` reports the population in one
            # line.
            logger.debug("could not stat %s; skipping", relative)
            self.result.unreadable.append(relative)
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

    def _order(self, entry: os.DirEntry[str]) -> str:
        """The sort key that makes sibling order agree with joined-path order.

        A directory is keyed as `name + "/"` because the cursor is compared against joined paths,
        and `"."` sorts below `"/"`: on the bare name `Report` precedes `Report.txt` while
        `Docs/Report.txt` < `Docs/Report/a.txt`, so a resume would skip `Report.txt` for good.
        """
        try:
            is_dir = entry.is_dir(follow_symlinks=self.binding.follow_symlinks)
        except OSError:
            # An entry that cannot be stat'ed is sorted as a file. If it is really a directory the
            # stream stops being monotonic and the resume cursor may skip entries permanently, so it
            # is recorded and summarised once per pass by `crawl_share`.
            self.unstattable.append(entry.path)
            is_dir = False
        return entry.name + "/" if is_dir else entry.name

    def descend(self, directory: Path, root: RootBinding) -> bool:
        """Walk one directory in sorted order; return False when the chunk filled up.

        Sorted because the walk's position is the resume cursor.

        Raises:
            OSError: The directory could not be listed — the caller records the root as failed so
            nothing is pruned from a share that may be half-mounted.
        """
        with os.scandir(directory) as entries:
            listing = sorted(entries, key=self._order)
        spec = self.binding.exclude_spec
        for entry in listing:
            relative = PurePosixPath(entry.path).relative_to(self.mount).as_posix()
            if _is_excluded(relative, spec):
                continue
            if entry.is_symlink() and not self.binding.follow_symlinks:
                continue
            if entry.is_symlink() and not _within_mount(self.mount, Path(entry.path)):
                logger.warning("%s links outside the mount; skipping", relative)
                continue
            if entry.is_dir(follow_symlinks=self.binding.follow_symlinks):
                # Prune an excluded directory without listing it: a directory matches a pattern like
                # `**/Archive/**` only when offered as `Archive/`. A separate check so
                # `entry.is_dir` still raises `OSError` out of `descend`, which marks the root
                # failed and stops the sweep.
                if _is_excluded(relative, spec, directory=True):
                    continue
                if not self.enter(Path(entry.path), relative):
                    continue
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
    # Sorted (keyed with a trailing separator, see `_Walk._order`), not in declaration order: the
    # resume cursor is a position in one lexical order over the whole share, so roots must be walked
    # in that order.
    for root in sorted(binding.roots, key=lambda item: item.path + "/"):
        directory = walk.mount if root.path == "." else walk.mount / root.path
        if not directory.is_dir():
            logger.error("root %r of share mount %s is missing", root.path, binding.mount)
            walk.result.failed_roots.append(root.path)
            continue
        # The root itself is resolved, since `descend`'s per-entry symlink guard never sees it: a
        # root that is a link (`Projects -> /`) would otherwise index the container filesystem under
        # mount-relative paths. Neither `follow_symlinks: false` nor a lexical `..` check catches
        # this.
        if not _within_mount(walk.mount, directory):
            logger.error(
                "root %r of share mount %s resolves outside the mount; refusing to walk it",
                root.path,
                binding.mount,
            )
            walk.result.failed_roots.append(root.path)
            continue
        # The root is claimed too, so two roots linked to one tree on the share are not each walked
        # in full.
        if not walk.enter(directory, root.path):
            continue
        try:
            if not walk.descend(directory, root):
                break
        except OSError:
            logger.error("root %r could not be walked; nothing will be pruned", root.path)
            logger.debug("walk failure detail for %r", root.path, exc_info=True)
            walk.result.failed_roots.append(root.path)
    _report_unstattable(walk.unstattable)
    return walk.result


def _report_unstattable(paths: list[str]) -> None:
    """One line per pass for entries whose type the sort could not read; nothing when none were.

    The count distinguishes a file mid-write from a subtree whose permissions changed, where the
    resume cursor may be skipping files.
    """
    if not paths:
        return
    logger.warning(
        "%d entr(ies) could not be typed while sorting (e.g. %s); each was sorted as a file, so "
        "a directory among them shifts the resume cursor and the files it sorts past are skipped "
        "until the next full pass",
        len(paths),
        paths[0],
    )
