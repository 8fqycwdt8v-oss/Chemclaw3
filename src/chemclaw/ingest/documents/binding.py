"""The share's layout as a document, because a site's folder tree is not knowable from here.

Which folders hold project work, which are archives to skip, and which path segment is the project
code differ per site, so they are a binding in `datasource.yaml` rather than Python. This module
defines the shape of that binding, validates it at load, and refuses anything it cannot make sense
of before a file is opened.
"""

import re
from functools import cached_property
from pathlib import PurePosixPath
from typing import Any, Self

import pathspec
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import GROUP_ROLE_PREFIX
from chemclaw.ingest.documents.formats import SUPPORTED_EXTENSIONS


class DocumentShareError(ChemclawError):
    """A share that cannot be read as declared: a bad binding, or a root that is not there.

    A `ValueError`, registered in `chemclaw.durable.publish` as non-retryable: a misspelled root or
    an unmounted share fails identically on every attempt.
    """


# A tag the agent can filter on. Deliberately the same conservative charset the knowledge graph
# uses for its own tags, so a tag lifted from a folder name cannot become a tag no filter matches.
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PathSegmentTag(BaseModel):
    """Take a tag from one path segment below the root — a project code, a year, a site.

    The commonest thing a classical share encodes is exactly this: `Projects/ACME-17/report.pdf`
    means the report belongs to ACME-17, and there is nowhere else that fact is written down. One
    integer recovers it, and it costs the deployment one line instead of a re-organized share.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 0-based, counted below the root: under root `Projects`, segment 0 of
    # `Projects/ACME-17/2024/report.pdf` is `ACME-17`. Relative to the root so nesting the root
    # deeper does not renumber bindings.
    segment: int = Field(ge=0, description="Index of the path segment below the root, 0-based.")
    # Folder names are typed by humans over a decade; `ACME-17` and `acme-17` are one project.
    lowercase: bool = True

    def extract(self, relative: str) -> str:
        """Return the tag for a path relative to its root, or `""` when it has no such segment.

        Args:
            relative: The file's path relative to the root, POSIX-separated.

        Returns:
            The named segment (lowercased when configured), or `""` when the path is too shallow
            or the segment is not usable as a tag.
        """
        parts = PurePosixPath(relative).parts
        # The last part is the file itself, never a folder tag.
        if self.segment >= len(parts) - 1:
            return ""
        value = parts[self.segment]
        value = value.lower() if self.lowercase else value
        return value if _TAG.match(value) else ""


class RootBinding(BaseModel):
    """One subtree of the share to index, and what its paths mean."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Relative to the mount. `.` is the mount itself — allowed, but naming subtrees explicitly is
    # what makes a staged rollout possible on a share too large to index in one go.
    path: str = Field(min_length=1)
    # Applied to every document under this root, so a question can be scoped to SOPs or to reports.
    tags: list[str] = Field(default_factory=list)
    tag_from_path: PathSegmentTag | None = None

    @model_validator(mode="after")
    def _stays_inside_the_mount(self) -> Self:
        """Refuse an absolute or upward path: a root names a subtree, never an escape from it."""
        candidate = PurePosixPath(self.path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(
                f"root path {self.path!r} must be relative to the mount and may not contain '..'"
            )
        bad = [tag for tag in self.tags if not _TAG.match(tag)]
        if bad:
            raise ValueError(f"root {self.path!r} declares unusable tag(s): {bad}")
        return self


# Version of the rule deciding what a stored chunk's `content` holds, folded into `chunking_key` so
# a change re-reads and re-cuts every indexed document. Bump it whenever a fresh index would store
# different text than an existing row holds.
_CHUNK_TEXT_VERSION = "ctv2"


#: The document size the chart's per-parse memory coefficient was measured at, and therefore the
#: largest `max_file_bytes` a binding may declare.
#:
#: The parse budget bounds allocation beyond the document itself, so a pod's real per-parse charge
#: is the budget plus the document; a larger document needs the coefficient re-measured, so it is
#: refused at load rather than silently under-charged.
PARSE_COEFFICIENT_BASIS_BYTES = 52_428_800


class DocumentShareBinding(BaseModel):
    """Everything about one mounted share: where it is, what to read, and who may read it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The read-only mount point. A path, not a UNC or a URL: the share is mounted by the platform
    # (a CIFS/SMB PersistentVolume), so this package needs no SMB client and no credential.
    mount: str = Field(min_length=1)
    roots: list[RootBinding] = Field(min_length=1)

    # The entitlement a caller must hold for this source to return anything, matched against the
    # turn's roles. A group-gated share names `group:<claim value>` (the `GROUP_ROLE_PREFIX`
    # namespace), not the bare object id, which matches nothing.
    #
    # A manifest must state either this or `public`, never neither: an omitted gate would otherwise
    # serve the whole share to every authenticated user, silently.
    required_roles: list[str] = Field(default_factory=list)
    # The explicit opt-out for a share every account holder may read, so "ungated" is something a
    # manifest says rather than omits.
    public: bool = False

    # Gitignore patterns matched against the mount-relative POSIX path (lock files, archives,
    # scratch folders). Compiled by `exclude_spec`.
    exclude: list[str] = Field(default_factory=list)
    # The formats to open, a subset of what this system can actually read. Narrowing it is a
    # legitimate cost control on a large share ("PDFs and decks only, for now").
    extensions: list[str] = Field(default_factory=lambda: sorted(SUPPORTED_EXTENSIONS))
    # Files no reader should be handed (scanned archives, huge exports) are skipped. Bounded above
    # by `PARSE_COEFFICIENT_BASIS_BYTES` because this is a term of the pod's memory sizing.
    max_file_bytes: int = Field(default=52_428_800, ge=1024, le=PARSE_COEFFICIENT_BASIS_BYTES)

    # Chunking. Big enough that a chunk carries an argument rather than a sentence, small enough
    # that a citation points somewhere a reader can check.
    chunk_chars: int = Field(default=1800, ge=200, le=20000)
    chunk_overlap_chars: int = Field(default=200, ge=0)

    # Off by default: a symlink on a share is very often a loop or a pointer out of the mount, and
    # a crawler that follows one indexes a corpus nobody meant to publish.
    follow_symlinks: bool = False

    @property
    def chunking_key(self) -> str:
        """Which chunking produced a stored row: the settings that decide its boundaries and text.

        The counterpart of `embedding_config_key`: a stored chunk is reusable only for the chunking
        that cut it. Both crawl gates (`DocumentIndex.fingerprints`, `known_documents`) compare it,
        so a change re-reads and re-chunks the file. Includes `_CHUNK_TEXT_VERSION` so a change to
        what a row stores also forces a rewrite.
        """
        return f"{self.chunk_chars}:{self.chunk_overlap_chars}:{_CHUNK_TEXT_VERSION}"

    @cached_property
    def exclude_spec(self) -> pathspec.GitIgnoreSpec:
        """The `exclude:` patterns compiled once, under gitignore semantics rather than `fnmatch`'s.

        The patterns deployments write (`**/Archive/**`, `~$*`) are gitignore lines, and `fnmatch`
        gives `**` no meaning. `GitIgnoreSpec` rather than `PathSpec.from_lines("gitwildmatch",
        ...)`, which mishandles negation precedence and is deprecated in `pathspec` 1.x.

        Raises:
            ValueError: A pattern gitignore cannot parse; surfaced at load by `_is_coherent`.
        """
        return pathspec.GitIgnoreSpec.from_lines(self.exclude)

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        """Reject the bindings that would silently index nothing, or the wrong thing."""
        seen = [root.path for root in self.roots]
        duplicated = sorted({path for path in seen if seen.count(path) > 1})
        if duplicated:
            raise ValueError(f"roots must be distinct; duplicated: {duplicated}")
        # Overlapping roots would index the same file twice under two tag sets, and the second
        # write would silently win. `.` overlaps everything, so it may only stand alone.
        if "." in seen and len(seen) > 1:
            raise ValueError("root '.' covers the whole mount and cannot be combined with others")
        nested = sorted(
            f"{inner} inside {outer}"
            for outer in seen
            for inner in seen
            if inner != outer and PurePosixPath(inner).is_relative_to(outer)
        )
        if nested:
            raise ValueError(f"roots must not overlap; nested: {nested}")
        normalized = [extension.lower() for extension in self.extensions]
        unknown = sorted(set(normalized) - SUPPORTED_EXTENSIONS)
        if unknown:
            # The failure this prevents is the quiet one: `.pdff` matches no file, so the share
            # indexes cleanly and holds nothing, and the operator sees a working sync.
            raise ValueError(
                f"unreadable extension(s) {unknown}; supported: {sorted(SUPPORTED_EXTENSIONS)}"
            )
        if not normalized:
            raise ValueError("extensions must name at least one format, or nothing is indexed")
        if self.chunk_overlap_chars >= self.chunk_chars:
            raise ValueError(
                f"chunk_overlap_chars ({self.chunk_overlap_chars}) must be smaller than "
                f"chunk_chars ({self.chunk_chars}), or chunking never advances"
            )
        # Who may read this share is the one thing a manifest may not leave unsaid. Refused at
        # load, so `make datasource-validate` catches it rather than a chemist finding out later.
        if self.public and self.required_roles:
            raise ValueError(
                "a share cannot be both public and role-gated: `public: true` says every "
                "authenticated caller may read it, and `required_roles` says only these may. "
                f"Drop one — `required_roles: {self.required_roles}` is the gated choice"
            )
        if not self.public and not self.required_roles:
            raise ValueError(
                "a share must say who may read it: set `required_roles` to the Entra app role that "
                f"gates it (or to `{GROUP_ROLE_PREFIX}<claim value>` for an AD group — group "
                "claims are namespaced, so the bare object-id matches nothing), or `public: true` "
                "if every authenticated caller may read it. Omitting both used to mean ungated, "
                "which is a security decision no manifest should make by accident"
            )
        # Compiled at load: an unparseable exclusion is a manifest error, and failing mid-crawl
        # would raise outside `DocumentShareError` and be retried forever. Caught as `ValueError`
        # because `pathspec`'s error class differs between major versions and both subclass it.
        try:
            _ = self.exclude_spec
        except ValueError as exc:
            raise ValueError(f"exclude pattern is not a usable gitignore pattern: {exc}") from exc
        return self

    @property
    def extension_set(self) -> frozenset[str]:
        """The extensions to open, lowercased — what the walk filters on before reading."""
        return frozenset(extension.lower() for extension in self.extensions)

    @property
    def required_role_set(self) -> frozenset[str]:
        """The entitlement set a caller must intersect for this source to answer."""
        return frozenset(self.required_roles)


def load_binding(raw: Any) -> DocumentShareBinding:
    """Validate a manifest's `binding:` block, raising `DocumentShareError` if it is not one.

    The single entry point for both the retriever and the sync, so both validate a share
    identically.

    Args:
        raw: The `binding` value from a `datasource.yaml` `config:` block.

    Returns:
        The validated binding.

    Raises:
        DocumentShareError: The block is missing, is not a mapping, or does not validate.
    """
    if not isinstance(raw, dict):
        raise DocumentShareError(
            "a document share's 'binding' must be a mapping describing the mount and its roots; "
            f"got {type(raw).__name__}"
        )
    try:
        return DocumentShareBinding.model_validate(raw)
    except ValueError as exc:
        raise DocumentShareError(f"invalid document-share binding: {exc}") from exc
