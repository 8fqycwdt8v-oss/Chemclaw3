"""The Markdown knowledge graph and the git-backed note writer behind it (plan Phase 2).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

import os
from pathlib import Path
from typing import Self

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

    # Directory of note files the indexer reads; retrieval is graph traversal over their
    # [[wikilinks]] (D-004).
    knowledge_dir: str = "knowledge"
    # Upper bound on `expand_note`'s link-expansion depth (SEC-4). The tool takes `hops` from
    # the model; an unbounded value would traverse the whole graph. 1–2 is typical; clamp to
    # this so a large value is bounded rather than rejected.
    graph_max_hops: int = Field(default=3, ge=1)
    # Upper bound on how many notes `find_notes` returns. It is a substring sweep over every
    # current note, so a broad needle (a single letter, a common element symbol) matches most of
    # the corpus and an uncapped hit list would flood the model context — the same failure mode
    # `fingerprint_max_top_k` bounds for substructure search. Hitting the cap logs a warning, so
    # a truncated result is never silent (D-066 #4).
    graph_max_results: int = Field(default=50, ge=1)
    # How many hub notes `find_knowledge_gaps` reports (`chemclaw.kg.analytics.analyze`). Same
    # argument as the two caps above and it was the one that stayed a literal: a number that
    # shapes a model-facing result is a knob, not a constant.
    graph_analytics_top_n: int = Field(default=5, ge=1)
    # Where a recorded note lands and where it is pushed (plan steps 2.7, 2.8): the writer commits
    # onto this branch in the clone below and pushes it to this remote. It refuses to run on any
    # other branch, so these two names are the whole of "where knowledge goes".
    note_base_branch: str = "main"
    git_remote: str = "origin"
    # The clone `GitNoteWriter` commits into. Its working tree is *written*, which is why
    # `_require_dedicated_checkout` refuses a checkout this process itself runs from and refuses a
    # linked worktree: committing under the running application is how a deployment loses a file
    # nobody wrote. (The private-worktree machinery that once made a submission never touch this
    # tree went with the PR gate — there is no second tree any more.) Point it at a dedicated clone
    # of the knowledge repo in production; the "." default only suits a dev checkout.
    note_repo_dir: str = "."
    # Publishing a QM result as a graph note is best-effort: bounded attempts + its own timeout
    # so a persistent failure gives up instead of retrying forever.
    note_write_timeout_seconds: float = Field(default=120.0, gt=0)
    note_write_max_attempts: int = Field(default=3, ge=1)
    # Wall-clock bound on a single git command in the note writer. A hung fetch/push (dead
    # remote, credential prompt) is killed after this, so it can never deadlock the process-wide
    # write lock; the failed activity then retries.
    git_command_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def knowledge_path(self) -> Path:
        """Where the notes actually live on disk: `note_repo_dir / knowledge_dir`.

        The writer (`chemclaw.kg.git_writer.GitNoteWriter`) writes into `note_repo_dir` — a
        dedicated clone in any real deployment, never the service's own checkout
        (`_require_dedicated_checkout`) — so a reader that resolved `knowledge_dir` alone
        (relative to the process CWD) would be looking at a different tree than the one
        notes are written to, and would see nothing the agent had ever recorded. Every reader
        (`chemclaw.kg.graph.load_notes`, the report retrievers, the note-index rebuild,
        `chemclaw.kg.validate`,
        the ELN sync, the memory-job synthesizers) resolves its default notes directory through
        this property instead of `knowledge_dir` raw, so read and write always agree on one
        location. `note_repo_dir`'s dev default (".") makes this identical to today's
        CWD-relative `Path(knowledge_dir)` — no behavior change until a deployment points
        `note_repo_dir` at a dedicated clone. An absolute `knowledge_dir` (as a test/demo may
        set directly, bypassing `_knowledge_dir_is_relative`) still wins outright: `Path.
        __truediv__` discards the left operand when the right is absolute.
        """
        return Path(self.note_repo_dir) / self.knowledge_dir

    @model_validator(mode="after")
    def _knowledge_dir_is_relative(self) -> Self:
        """`knowledge_dir` must be relative to the note repo, never an absolute path.

        The writer builds a note path as `Path(note_repo_dir) / knowledge_dir / …`. An absolute
        `knowledge_dir` would make `Path.__truediv__` discard `note_repo_dir`, so the write
        would land outside the repo — the containment check then fails the write, confusingly.
        Reject it at startup where the message is clear instead.
        """
        if os.path.isabs(self.knowledge_dir):
            raise ValueError(
                f"knowledge_dir must be relative to note_repo_dir, "
                f"got absolute {self.knowledge_dir!r}"
            )
        return self
