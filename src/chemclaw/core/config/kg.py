"""Settings for the Markdown knowledge graph and the git-backed note writer behind it.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import os
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class KgSettings(BaseSettings):
    """The one Git-backed note repository and its query bounds.

    Where notes live and how `chemclaw.kg.git_writer.GitNoteWriter` commits them onto the base
    branch and pushes. Notes land directly, carrying `created_by: agent`.
    """

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
