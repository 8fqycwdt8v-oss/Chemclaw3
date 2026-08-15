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
`chemclaw.agent.skill_access` still decides *which* skills survive — the same three predicates,
because a gate that answers differently depending on which code path asked is not a gate — and this
module makes those answers binding on every path that can reach a file.

**Every reach path, not the obvious one.** `BackendProtocol` exposes `ls`, `read`, `glob` and
`grep`, each with an async twin, plus a write half. Filtering `ls` alone would leave three
bypasses open. The async twins need no override: `FilesystemBackend` implements them as
`asyncio.to_thread(self.read, ...)`, so they dispatch through the subclass — measured, and pinned
by `tests/test_skill_backend.py` rather than assumed, because it is exactly the kind of upstream
detail that changes quietly.

**`virtual_mode=True` is not a default we accept, it is a decision.** It was the *non*-default when
this was written, under a deprecation warning that said so outright: *"leaving `virtual_mode=False`
allows absolute paths and `'..'` to bypass `root_dir`"*. deepagents 0.7 made it the default and
dropped the warning, so the citation is gone and the argument is not: under `False`, a model handed
a filesystem tool could read any file the pod's service account can — over a tool
surface whose whole point is that capability is enumerated. It stays written out rather than
inherited, because a security property that arrives as somebody else's default can leave the same
way. Virtual mode roots every path at `/`, refuses traversal with a `ValueError`, and has the useful
side effect that a path's first segment *is* its skill, which is what makes the predicate below a
one-line lookup.

**The write half is refused outright.** A skill is judgment this system ships and a human reviews;
an agent that can edit `SKILL.md` can rewrite its own instructions, and D-038 disabled MAF's
file-write batteries for the same reason. Refusing is not a narrowing that could be configured
open — there is no deployment for which a writable skills tree is correct.

**That half grows, and it grew here.** deepagents 0.7 added `delete` to the protocol; nothing in
this class refused it, so a bump alone would have inherited a working delete into the one backend
whose reason to exist is that skills are read-only. It was caught by the derived enumeration in
`tests/test_skill_backend.py` rather than by review, which is the argument for deriving it: the
methods this class must answer for are whatever upstream declares this week, and a hand-written list
is a list of what upstream declared the week it was written.
"""

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
)

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

        The two keyword-only arguments are forwarded rather than accepted-and-ignored because
        upstream *introspects* for them: `protocol._method_accepts_max_count` decides whether to
        push the cap down to the backend or apply it itself, so an override that quietly dropped
        them would change how many matches a caller gets depending on which class is underneath.
        Filtering after the fact is still correct with a cap in play — `max_count` bounds what the
        tree returns, and this gate only ever removes from that.
        """
        result = super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        if not result.matches:
            return result
        return GrepResult(error=result.error, matches=self._permitted(result.matches))

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(_path_of(hit))]

    def download_files(self, paths: list[str]) -> Any:
        """Return the bodies of the permitted paths only — the reach path the gate had missed.

        `read` was gated and this was not, and it returns a file's **full bytes**: measured on the
        installed backend, a gated `SKILL.md` came back whole through here while `read` refused it
        and `ls` did not list it. Nothing binds it today — only `skill_read_tool` and the
        middleware's own listing reach the backend — so this was a latent hole rather than a live
        one, and it becomes live the moment upstream fetches a body this way. That is a patch
        release in a 0.x package, against the one narrowing that is a *security* property.

        Filtered rather than refused outright: this returns per-path results, so a caller asking for
        five paths of which one is gated should get the four, exactly as `glob` and `grep` do.
        """
        return super().download_files([path for path in paths if self._allows(path)])

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is reviewed judgment, never something a turn may rewrite."""
        raise PermissionError("the skills tree is read-only")

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise PermissionError("the skills tree is read-only")

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives — and it is the newest way in.

        `delete` arrived with deepagents 0.7. Unrefused it is worse than `write`, not milder: a
        turn that cannot rewrite a `SKILL.md` but can remove it still decides what judgment the
        next turn is able to load.
        """
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


# The tool the skills prompt tells the model to call. It is *not* a free choice: deepagents'
# `SKILLS_SYSTEM_PROMPT` publishes each skill's path and instructs the model to "use `read_file` on
# the path shown", so a differently-named tool would leave every skill advertised and unloadable.
# Pinned against the prompt by `tests/test_skill_backend.py` rather than trusted, for the reason
# D-117 gives: a name space that drifts silently is one every validator built on it then gets wrong.
SKILL_READ_TOOL = "read_file"

# What the prompt asks for, and the reason it does: SKILL.md bodies are long, and deepagents' own
# instruction is to "pass limit=1000 since the default of 100 lines is too small for most skill
# files". The default lives here rather than in the model's hands so a skill is not silently
# truncated when the model forgets.
_SKILL_READ_LIMIT = 1000
