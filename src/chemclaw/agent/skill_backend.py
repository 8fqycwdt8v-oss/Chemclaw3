"""Skill visibility for the LangGraph engine: a backend that can only reach permitted skills.

`deepagents.SkillsMiddleware` publishes each skill's path in the system prompt and the model reads
bodies with ordinary filesystem tools over the same backend, so narrowing the listing is not enough:
a gated skill would be one guessed path away. The narrowing therefore lives in the backend, applying
`chemclaw.agent.skill_access`'s predicate on every reach path (`ls`, `read`, `glob`, `grep`,
`download_files`); the async twins dispatch through these overrides, pinned by
`tests/test_skill_backend.py`.

`virtual_mode=True` is written out rather than inherited: it roots every path at `/` and refuses
traversal, so a path's first segment is its skill.

The write half (`write`, `edit`, `upload_files`, `delete`) is refused outright, as
`SkillsReadOnlyRefusal`, so an agent can never rewrite its own instructions. The set of refused
methods is derived from upstream's protocol in the tests, so a new upstream verb cannot slip
through.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
)

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.refusal_route import routed
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.model_prose import ModelProse
from chemclaw.core.turn_signals import record_skill_loaded

logger = logging.getLogger(__name__)

# What a refused read returns: a readable result keeps the turn going. It does not say whether the
# skill exists, so the gate is not an enumeration oracle.
REFUSED = ModelProse("This path is not part of the skills available to you.")

# What a refused write says, worded for the model: nothing changed, and working notes belong under
# the scratch root.
_READ_ONLY = routed(
    "the skills tree is read-only — a skill is reviewed judgment, not something a turn may "
    "rewrite. Nothing was changed; keep working notes under /scratch/ instead.",
    code="skills_read_only",
    boundary="the skills tree, which is read-only to every turn in every deployment",
    who_can_act="nobody at run time — a skill changes only by a reviewed commit to skills/",
    sanctioned_path="write the same content under /scratch/, which this turn owns",
)


class SkillsReadOnlyRefusal(AuthorizationError):
    """A turn tried to change the skills tree, which no deployment permits.

    An `AuthorizationError`, so `surface_authorization_denials` relays it behind `Refused:` (an
    access-control decision, not a fault to retry) and the audit row records a refusal.
    """


class NarrowedSkillsBackend(FilesystemBackend):
    """A skills backend whose every read path is bounded by one `permits` predicate.

    Args:
        root_dir: The skills tree.
        permits: Whether a skill *name* is visible to this turn — `skill_access`'s composed
        narrowing. Called per reach, because the role gate reads the turn's ambient identity and one
        backend serves every concurrent turn.
    """

    def __init__(self, root_dir: str, permits: Callable[[str], bool]) -> None:
        """Hold the tree and the predicate; no filesystem access happens here."""
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._permits = permits

    def _allows(self, path: str) -> bool:
        """Whether this turn may reach `path` at all.

        A path's first segment names its skill. The tree root is neither permitted nor refused:
        listing it starts discovery, and `ls` filters what comes back.
        """
        skill = skill_of(path)
        return not skill or self._permits(skill)

    def ls(self, path: str) -> LsResult:
        """List the tree, keeping only entries belonging to a permitted skill."""
        result = super().ls(path)
        if not result.entries:
            return result
        kept = [entry for entry in result.entries if self._allows(str(entry.get("path", "")))]
        return LsResult(error=result.error, entries=kept)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a file, refusing anything outside a permitted skill — and say which, either way.

        The half that makes this a gate: the model could otherwise ask for a path it was never
        shown.

        A delivered body logs at INFO (which procedure the model actually opened) and increments
        `chemclaw_skill_loads_total`. The counter's label is model-authored, so it is booked only
        when the read resolved, `is_a_skill_body` holds, and lines were actually requested. A
        refusal logs a WARNING and a denial count; it names the path, since a refused path may name
        no real skill. The path is bounded with `audit.bounded_repr` before logging on both
        branches, since it arrives verbatim from the model.
        """
        skill = skill_of(file_path)
        recorded = bounded_repr(file_path)
        if not self._allows(file_path):
            record_metric(lambda m: m.increment("chemclaw_skill_reads_denied_total"))
            log_event(
                logger,
                "skill.read_denied",
                "refused a read of %s: it is outside the skills this turn may reach",
                recorded,
                level=logging.WARNING,
                path=recorded,
            )
            return ReadResult(error=REFUSED, file_data=None)
        log_event(logger, "skill.read", "the model read %s", recorded, skill=skill, path=recorded)
        result = super().read(file_path, offset, limit)
        if is_a_skill_body(file_path) and not result.error and not result.no_lines_requested:
            record_metric(
                lambda m: m.increment("chemclaw_skill_loads_total", labels={"skill": skill})
            )
            # Also record the load for this turn's cost row (a counter cannot say which turn), on
            # the same condition as the counter.
            record_skill_loaded(skill)
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Match files, dropping every hit outside a permitted skill.

        Narrowed with `replace` so upstream's `truncated` flag (and any future field) survives; this
        gate only removes matches.
        """
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return replace(result, matches=self._permitted(result.matches))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Search files, dropping every hit outside a permitted skill.

        The keyword-only arguments are forwarded because upstream introspects for them to decide
        where `max_count` applies. Narrowed with `replace` so `truncated` survives, as in `glob`.
        """
        result = super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        if not result.matches:
            return result
        return replace(result, matches=self._permitted(result.matches))

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(path_of(hit))]

    def download_files(self, paths: list[str]) -> Any:
        """Return the bodies of the permitted paths only.

        This returns full bytes, so it must be gated like `read`. Filtered rather than refused,
        since results are per path, as in `glob` and `grep`.
        """
        return super().download_files([path for path in paths if self._allows(path)])

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is reviewed judgment, never something a turn may rewrite."""
        raise SkillsReadOnlyRefusal(_READ_ONLY)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(_READ_ONLY)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        A turn that can remove a `SKILL.md` decides what judgment the next turn can load.
        """
        raise SkillsReadOnlyRefusal(_READ_ONLY)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(_READ_ONLY)


def skill_of(path: str) -> str:
    """The skill a path belongs to — its first segment, which is what `_allows` gates on.

    Empty for the tree root. One definition, so the logged name and the gated name agree; the stored
    tiers (`agent/skill_store.py`) import it too.
    """
    parts = PurePosixPath(path.strip("/")).parts
    return parts[0] if parts else ""


def is_a_skill_body(path: str) -> bool:
    """Whether `path` names a document *inside* a skill, rather than one at the tree root.

    The clamp on `chemclaw_skill_loads_total`'s label. A root-level file such as `README.md`
    resolves and is gated under its own name, but it is evidence about no skill. A path with a
    directory segment before its filename belongs to a skill; the read already resolved, so no
    `stat` is needed.
    """
    return len(PurePosixPath(path.strip("/")).parts) > 1


def path_of(hit: Any) -> str:
    """The path a glob or grep hit names — a `FileInfo` mapping, a `GrepMatch`, or a bare string."""
    if isinstance(hit, str):
        return hit
    if isinstance(hit, dict):
        return str(hit.get("path", ""))
    return str(getattr(hit, "path", ""))


# The tool the skills prompt tells the model to call: deepagents' `SKILLS_SYSTEM_PROMPT` says "use
# `read_file` on the path shown", so any other name would leave skills unloadable. Pinned against
# the prompt by `tests/test_skill_backend.py`.
SKILL_READ_TOOL = "read_file"
