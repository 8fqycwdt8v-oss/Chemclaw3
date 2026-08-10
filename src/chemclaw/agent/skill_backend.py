"""Skill visibility for the LangGraph engine: a backend that can only reach permitted skills.

**Why a backend and not a filter.** MAF's `SkillsProvider` owns skill loading end to end — it
advertises names and registers `load_skill`/`read_skill_resource` to fetch bodies — so narrowing
the advertised list was enough: there was no other way in. `deepagents.SkillsMiddleware` works
differently, and the difference is a security property rather than an API detail. It puts each
skill's *path* in the system prompt and expects the model to read the body with an ordinary
filesystem tool over the same backend. Listing is therefore only half the gate: a model told about
`/deep-research/SKILL.md` can ask for `/anything-else/SKILL.md`, and a role-gated skill would be
one guessed path away from the caller who was refused it.

So the narrowing moves to the backend, which is the only place both halves meet.
`chemclaw.agent.skill_access` still decides *which* skills survive — the same three predicates
under both engines, because a gate that answers differently per `agent_engine` is not a gate — and
this module makes those answers binding on every path that can reach a file.

**Every reach path, not the obvious one.** `BackendProtocol` exposes `ls`, `read`, `glob` and
`grep`, each with an async twin, plus a write half. Filtering `ls` alone would leave three
bypasses open. The async twins need no override: `FilesystemBackend` implements them as
`asyncio.to_thread(self.read, ...)`, so they dispatch through the subclass — measured, and pinned
by `tests/test_skill_backend.py` rather than assumed, because it is exactly the kind of upstream
detail that changes quietly.

**`virtual_mode=True` is not a default we accept, it is a decision.** deepagents' own deprecation
warning is explicit: *"leaving `virtual_mode=False` allows absolute paths and `'..'` to bypass
`root_dir`"*. Under the default, a model handed a filesystem tool could read any file the pod's
service account can — in a GxP system, over a tool surface whose whole point is that capability is
enumerated. Virtual mode roots every path at `/`, refuses traversal with a `ValueError`, and has
the useful side effect that a path's first segment *is* its skill, which is what makes the
predicate below a one-line lookup.

**The write half is refused outright.** A skill is judgment this system ships and a human reviews;
an agent that can edit `SKILL.md` can rewrite its own instructions, and D-038 disabled MAF's
file-write batteries for the same reason. Refusing is not a narrowing that could be configured
open — there is no deployment for which a writable skills tree is correct.
"""

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import GlobResult, GrepResult, LsResult, ReadResult

# What a refused read returns. A result rather than an exception, because these are model-facing
# tool results: a refusal the model can read keeps the turn going, where a raised error surfaces as
# a tool failure it may retry. It deliberately does not say whether the skill *exists* — "not
# available to you" is the same answer for a gated skill and for a typo, and distinguishing them
# would turn the gate into an enumeration oracle.
REFUSED = "This path is not part of the skills available to you."


class NarrowedSkillsBackend(FilesystemBackend):
    """A skills backend whose every read path is bounded by one `permits` predicate.

    Args:
        root_dir: The skills tree.
        permits: Whether a skill *name* is visible to this turn — `skill_access`'s composed
            narrowing. Called per reach rather than once at construction, because the role gate
            reads the turn's ambient identity and one backend serves every concurrent turn (the
            same lifetime rule that keeps connector MCP tools per-turn).
    """

    def __init__(self, root_dir: str, permits: Callable[[str], bool]) -> None:
        """Hold the tree and the predicate; no filesystem access happens here."""
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._permits = permits

    def _allows(self, path: str) -> bool:
        """Whether this turn may reach `path` at all.

        A skill is a directory holding `SKILL.md`, so everything belonging to one lives under that
        directory and the first segment names it — true of the `SKILL.md`, a helper script and a
        reference doc alike. A path with no segments is the tree root, which is neither permitted
        nor refused: listing it is how discovery starts, and `ls` filters what comes back.
        """
        parts = PurePosixPath(path.strip("/")).parts
        return not parts or self._permits(parts[0])

    def ls(self, path: str) -> LsResult:
        """List the tree, keeping only entries belonging to a permitted skill."""
        result = super().ls(path)
        if not result.entries:
            return result
        kept = [entry for entry in result.entries if self._allows(str(entry.get("path", "")))]
        return LsResult(error=result.error, entries=kept)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a file, refusing anything outside a permitted skill.

        The half that makes this a gate rather than a listing: the model is handed skill paths in
        its system prompt and could otherwise ask for one it was never shown.
        """
        if not self._allows(file_path):
            return ReadResult(error=REFUSED, file_data=None)
        return super().read(file_path, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Match files, dropping every hit outside a permitted skill."""
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return GlobResult(error=result.error, matches=self._permitted(result.matches))

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Search files, dropping every hit outside a permitted skill."""
        result = super().grep(pattern, path, glob)
        if not result.matches:
            return result
        return GrepResult(error=result.error, matches=self._permitted(result.matches))

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(_path_of(hit))]

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is reviewed judgment, never something a turn may rewrite."""
        raise PermissionError("the skills tree is read-only")

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise PermissionError("the skills tree is read-only")

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise PermissionError("the skills tree is read-only")


def _path_of(hit: Any) -> str:
    """The path a glob or grep hit names — a `FileInfo` mapping, a `GrepMatch`, or a bare string."""
    if isinstance(hit, str):
        return hit
    if isinstance(hit, dict):
        return str(hit.get("path", ""))
    return str(getattr(hit, "path", ""))
