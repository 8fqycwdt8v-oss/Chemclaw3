"""The share's layout as a document, because a site's folder tree is not knowable from here.

Same argument as `D-2026-08-04-the-schema-is-a-file`, one layer over: a warehouse's tables exist
before the adapter is written, and so does a file share's directory structure. Which folders hold
project work, which hold decade-old archives nobody wants indexed, which segment of a path is the
project code — none of that can be written into Python, because it is different at every site and
it changes without asking. So it is a binding in `datasource.yaml`, and attaching a real share is
editing that file (better: mounting a folder holding your own copy of it and putting that folder
first in `CHEMCLAW_DATA_SOURCES_DIR`, so the deployment's layout is not a change to this
repository at all).

Nothing in this package names a folder, an extension list or a project code. This module names
what a *shape* of those is, validates one at load, and refuses anything it cannot make sense of
before a single file is opened.
"""

import re
from pathlib import PurePosixPath
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.documents.formats import SUPPORTED_EXTENSIONS


class DocumentShareError(ChemclawError):
    """A share that cannot be read as declared: a bad binding, or a root that is not there.

    A `ValueError` by inheritance, and registered in `chemclaw.durable.publish` as non-retryable:
    a misspelled root or an unmounted share fails identically on every attempt, so retrying it
    only delays the log line that says what is wrong.
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

    # 0-based, counted *below the root* — so under root `Projects`, segment 0 of
    # `Projects/ACME-17/2024/report.pdf` is `ACME-17`. Relative to the root rather than to the
    # mount, because a deployment that later nests the root one level deeper must not have to
    # renumber every binding that reads from it.
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


class DocumentShareBinding(BaseModel):
    """Everything about one mounted share: where it is, what to read, and who may read it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The read-only mount point. A path, not a UNC or a URL: the share is mounted by the platform
    # (a CIFS/SMB PersistentVolume), so this package needs no SMB client and no credential.
    mount: str = Field(min_length=1)
    roots: list[RootBinding] = Field(min_length=1)

    # The entitlement a caller must hold for this source to return anything. Matched against the
    # turn's roles — which carry Entra app roles, plus group object-ids when
    # `entra_group_claims_as_roles` is on — so an AD group reaches this either way.
    #
    # **A manifest must state its intent: either this or `public`, never neither.** It used to
    # default to empty, and empty means ungated — so a hand-authored binding (which is the
    # documented way to attach a real share) that named `mount` and `roots` and simply forgot this
    # served the whole AD-gated drive to every authenticated user, with no warning and nothing to
    # distinguish it from a correctly gated one. A security model whose default is "off" and whose
    # failure is silent is not a security model.
    required_roles: list[str] = Field(default_factory=list)
    # The explicit opt-out, for a share every account holder may genuinely read. It exists so that
    # "ungated" is something a manifest *says* rather than something it omits — an author who means
    # it writes one word, and an author who forgot gets an error naming both choices.
    public: bool = False

    # Glob patterns matched against the mount-relative POSIX path. Office lock files (`~$...`),
    # archive folders and scratch directories are the usual population, and excluding them is
    # cheaper than parsing them.
    exclude: list[str] = Field(default_factory=list)
    # The formats to open, a subset of what this system can actually read. Narrowing it is a
    # legitimate cost control on a large share ("PDFs and decks only, for now").
    extensions: list[str] = Field(default_factory=lambda: sorted(SUPPORTED_EXTENSIONS))
    # A share holds files no document reader should be handed: a 2 GB scanned archive, a database
    # export named `.csv`. 50 MB covers real reports with room to spare.
    max_file_bytes: int = Field(default=52_428_800, ge=1024)

    # Chunking. Big enough that a chunk carries an argument rather than a sentence, small enough
    # that a citation points somewhere a reader can check.
    chunk_chars: int = Field(default=1800, ge=200, le=20000)
    chunk_overlap_chars: int = Field(default=200, ge=0)

    # Off by default: a symlink on a share is very often a loop or a pointer out of the mount, and
    # a crawler that follows one indexes a corpus nobody meant to publish.
    follow_symlinks: bool = False

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
                "a share must say who may read it: set `required_roles` to the Entra app role or "
                "AD group object-id that gates it, or `public: true` if every authenticated caller "
                "may read it. Omitting both used to mean ungated, which is a security decision no "
                "manifest should make by accident"
            )
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

    The single entry point both the retriever and the sync use, so a share is validated identically
    whichever half is being built.

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
