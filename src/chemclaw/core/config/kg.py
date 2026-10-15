"""Settings for the Markdown knowledge graph and the git-backed note writer behind it.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class KgSettings(BaseSettings):
    """The Markdown knowledge graph and the git-backed note writer behind it (plan Phase 2).

    Grouped because these knobs describe the one Git-backed note repository: where notes live and
    how `chemclaw.kg.git_writer.GitNoteWriter` commits them onto its base branch and pushes.

    **These four git knobs no longer describe a review gate.**
    `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` ended the branch-per-note PR gate:
    knowledge is written straight into the graph carrying `created_by: agent`, and corrected rather
    than pre-approved. What survives is the same clone, the same base branch and the same remote —
    a note now lands on that branch instead of on `note/<id>` beside it.
    """

    # Where notes are stored. `git` is the Markdown tree and its writer. `dual` reads from the tree
    # and writes it first, then Postgres (a failed Postgres write is logged and counted, never the
    # chemist's error). `postgres` is refused at startup until the reads flip (the validator below).
    kg_store: Literal["git", "dual", "postgres"] = "git"
    # The nightly tree-versus-Postgres comparison under `dual` (`durable/kg_compare.py`). The settle
    # pause is how long a difference must persist before it is reported, so a note another pod just
    # wrote (whose clone has not synced yet) is not read as a divergence; keep it above the sync
    # interval. The timeout bounds the whole run, pause included.
    kg_compare_schedule_minutes: float = Field(default=1440.0, gt=0)
    kg_compare_settle_seconds: float = Field(default=330.0, ge=0)
    kg_compare_timeout_seconds: float = Field(default=900.0, gt=0)
    # Directory of note files the indexer reads; retrieval traverses their [[wikilinks]].
    knowledge_dir: str = "knowledge"
    # Upper bound on `expand_note`'s model-supplied `hops`; larger values are clamped, not rejected.
    graph_max_hops: int = Field(default=3, ge=1)
    # Upper bound on `find_notes` results; a broad substring matches most of the corpus. Hitting the
    # cap logs a warning, so truncation is never silent.
    graph_max_results: int = Field(default=50, ge=1)
    # How many hub notes `find_knowledge_gaps` reports (`chemclaw.kg.analytics.analyze`).
    graph_analytics_top_n: int = Field(default=5, ge=1)
    # Branch and remote a note is committed onto and pushed to; the writer refuses any other branch.
    note_base_branch: str = "main"
    git_remote: str = "origin"
    # The clone `GitNoteWriter` commits into. Its tree is written, so `_require_dedicated_checkout`
    # refuses the process's own checkout and linked worktrees. Use a dedicated clone in production;
    # "." only suits dev.
    note_repo_dir: str = "."
    # Identity for the writer's commits, passed to git as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`; git's
    # own fallback fails in a container. A service identity; `.invalid` (RFC 2606) claims no
    # mailbox.
    note_committer_name: str = Field(default="ChemClaw", min_length=1)
    note_committer_email: str = Field(default="chemclaw-notes@chemclaw.invalid", min_length=1)
    # Publishing a result as a graph note is best-effort: bounded attempts and its own timeout.
    note_write_timeout_seconds: float = Field(default=120.0, gt=0)
    note_write_max_attempts: int = Field(default=3, ge=1)
    # Wall-clock bound on one git command, so a hung fetch/push cannot hold the process-wide write
    # lock; the activity then retries.
    git_command_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def knowledge_path(self) -> Path:
        """Where the notes live on disk: `note_repo_dir / knowledge_dir`.

        Every reader resolves its notes directory through this property so reads see the same tree
        the writer commits into. An absolute `knowledge_dir` still wins outright
        (`Path.__truediv__`).
        """
        return Path(self.note_repo_dir) / self.knowledge_dir

    @model_validator(mode="after")
    def _postgres_reads_wait_for_the_flip(self) -> Self:
        """`kg_store="postgres"` is refused: readers still resolve some notes through the tree.

        Several helpers take a notes directory (`conflict_index`, the retrievers' `notes_dir`, the
        `exists()` guards), so reading from Postgres alone would answer some questions from an
        empty or stale tree. `dual` is the supported way to fill and compare the store; the pull
        request that flips the reads removes this check.
        """
        if self.kg_store == "postgres":
            raise ValueError(
                "kg_store='postgres' is not accepted yet: the reads have not been moved off the "
                "notes directory, so a Postgres-only deployment would answer some questions from "
                "an empty tree. Use kg_store='dual' (reads from the tree, writes both, compared "
                "nightly) until the read flip ships."
            )
        return self

    @model_validator(mode="after")
    def _knowledge_dir_is_relative(self) -> Self:
        """`knowledge_dir` must be relative to the note repo, never an absolute path.

        An absolute value would make `Path.__truediv__` discard `note_repo_dir` and send writes
        outside the repo; reject it at startup with a clear message.
        """
        if os.path.isabs(self.knowledge_dir):
            raise ValueError(
                f"knowledge_dir must be relative to note_repo_dir, "
                f"got absolute {self.knowledge_dir!r}"
            )
        return self
