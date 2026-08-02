"""The Markdown knowledge graph and its PR-gate (plan Phase 2).

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
    """The Markdown knowledge graph and its PR-gate (plan Phase 2).

    Grouped because these knobs describe the one Git-backed note repository:
    where notes live, how the GitNoteSubmitter branches/pushes them through the PR-gate, and how
    long a human-approval hold may pend.
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
    # Edge length of a rendered structure depiction (gap TOOL-5). Config, not a magic number, so
    # a deployment whose surface renders larger cards can change it without a code edit.
    structure_render_size_px: int = Field(default=320, gt=0)
    # PR-gate git settings (plan steps 2.7, 2.8): agent notes branch off this base branch on
    # this remote before a human merges.
    note_base_branch: str = "main"
    git_remote: str = "origin"
    # The checkout the GitNoteSubmitter mutates (`git checkout -B` switches its whole working
    # tree). Point it at a dedicated clone of the knowledge repo in production; the "." default
    # only suits a dev checkout with nothing else running in it.
    note_repo_dir: str = "."
    # Publishing a QM result as a graph note is best-effort: bounded attempts + its own timeout
    # so a persistent failure gives up instead of retrying forever.
    note_write_timeout_seconds: float = Field(default=120.0, gt=0)
    note_write_max_attempts: int = Field(default=3, ge=1)
    # Wall-clock bound on a single git command in the PR-gate submitter. A hung fetch/push (dead
    # remote, credential prompt) is killed after this, so it can never deadlock the process-wide
    # submit lock; the failed activity then retries.
    git_command_timeout_seconds: float = Field(default=60.0, gt=0)
    # The shared secret a git host signs its post-merge webhook with (HMAC-SHA256 over the raw
    # body, sent as `X-Chemclaw-Signature: sha256=<hex>`). Empty means unsigned, which is what
    # `/events/knowledge-merged` accepted from any authenticated principal before it could close a
    # proposal — tolerable while it only kicked an idempotent reindex, not once it records a
    # decision. Set it in any deployment where the webhook may move a proposal to `merged`.
    note_webhook_secret: str = ""
    # Page size for `GET /proposals`. Bounded like every other listing: the review queue is
    # unbounded in principle, and a surface that asks for "all of it" should page rather than ask
    # the database for an unbounded scan.
    proposal_list_limit: int = Field(default=50, ge=1, le=500)

    # How long a confirmed-answer note is held pending a human Yes/No before the hold expires
    # unpublished (plan step 5.5, async approval seam). The button click is a Temporal signal
    # into `InteractionApprovalWorkflow`; this bounds the wait so an unanswered prompt cannot
    # pin a workflow forever. Default 7 days — generous for an out-of-band review, still finite.
    interaction_approval_timeout_seconds: float = Field(default=604800.0, gt=0)

    @property
    def knowledge_path(self) -> Path:
        """Where the notes actually live on disk: `note_repo_dir / knowledge_dir`.

        The PR-gate (`chemclaw.kg.git_submitter.GitNoteSubmitter`) writes into `note_repo_dir` — a
        dedicated clone in any real deployment, never the service's own checkout
        (`_require_dedicated_checkout`) — so a reader that resolved `knowledge_dir` alone
        (relative to the process CWD) would be looking at a different tree than the one
        notes are written to, and would see nothing an agent had ever proposed. Every reader
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

        The PR-gate builds a note path as `Path(note_repo_dir) / knowledge_dir / …`. An absolute
        `knowledge_dir` would make `Path.__truediv__` discard `note_repo_dir`, so the write
        would land outside the repo — the containment check then fails the submit, confusingly.
        Reject it at startup where the message is clear instead.
        """
        if os.path.isabs(self.knowledge_dir):
            raise ValueError(
                f"knowledge_dir must be relative to note_repo_dir, "
                f"got absolute {self.knowledge_dir!r}"
            )
        return self
