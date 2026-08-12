"""The skills backend is a gate, not a listing (M4, D-2026-08-10).

This is the migration's load-bearing security test. Under MAF, narrowing the advertised skill list
*was* the gate, because `SkillsProvider` was the only way to a skill body. Under deepagents the
model is handed skill paths and reads them with a filesystem tool, so a backend that filtered only
`ls` would hide a role-gated skill from the listing and hand it over on request.

Every test here therefore asks the same question twice: is it hidden, and is it unreachable.
"""

import asyncio
import inspect
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from chemclaw.agent.skill_backend import REFUSED, NarrowedSkillsBackend, skill_read_tool

_SKILLS = ("alpha", "beta", "gamma")

# The verbs the gate refuses outright rather than narrowing, sync and async. Written down in one
# place because two tests need the same answer: one calls every name here and requires a
# `PermissionError`, the other requires that this set plus the reach probes account for every
# public method the backend exposes. Neither is a list of what upstream had when it was written —
# the second test fails if it becomes one.
_WRITE_METHODS = frozenset(
    {"write", "edit", "delete", "upload_files", "awrite", "aedit", "adelete", "aupload_files"}
)


@pytest.fixture
def tree() -> Iterator[str]:
    """A skills tree with three skills, each a directory holding a `SKILL.md` plus a helper."""
    with tempfile.TemporaryDirectory() as tmp:
        for name in _SKILLS:
            skill = Path(tmp) / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody of {name}\n")
            (skill / "helper.py").write_text(f"# helper for {name}\n")
        yield tmp


def _backend(tree: str, permits: Callable[[str], bool]) -> NarrowedSkillsBackend:
    return NarrowedSkillsBackend(root_dir=tree, permits=permits)


def _only_alpha(name: str) -> bool:
    return name == "alpha"


def test_a_refused_skill_is_absent_from_the_listing(tree: str) -> None:
    """The half MAF also had: a narrowed skill is not advertised."""
    listed = _backend(tree, _only_alpha).ls("/")
    assert [str(e["path"]).strip("/") for e in listed.entries or []] == ["alpha"]


def test_a_refused_skill_cannot_be_read_by_naming_its_path(tree: str) -> None:
    """The half MAF did not need, and the reason this module exists.

    `SkillsMiddleware` puts skill paths in the system prompt, so the model knows the shape of every
    path whether or not it was shown one. Guessing `/beta/SKILL.md` must not work.
    """
    backend = _backend(tree, _only_alpha)

    assert backend.read("/alpha/SKILL.md").error is None
    for hidden in ("/beta/SKILL.md", "/gamma/helper.py", "beta/SKILL.md"):
        result = backend.read(hidden)
        assert result.error == REFUSED, f"{hidden} was readable"
        assert result.file_data is None


def test_the_refusal_does_not_say_whether_the_skill_exists(tree: str) -> None:
    """A gated skill and a typo get the same answer, so the gate is not an enumeration oracle."""
    backend = _backend(tree, _only_alpha)
    assert backend.read("/beta/SKILL.md").error == backend.read("/nonexistent/SKILL.md").error


def test_glob_and_grep_cannot_reach_past_the_gate(tree: str) -> None:
    """The two bypasses a listing-only filter would leave open."""
    backend = _backend(tree, _only_alpha)

    globbed = [_p(m) for m in backend.glob("**/SKILL.md").matches or []]
    assert globbed and all("alpha" in path for path in globbed), globbed

    grepped = [_p(m) for m in backend.grep("body of").matches or []]
    assert all("alpha" in path for path in grepped), grepped


def test_the_async_twins_go_through_the_same_gate(tree: str) -> None:
    """`aread`/`als` dispatch to the overridden sync methods — measured, not assumed.

    `FilesystemBackend` implements each async twin as `asyncio.to_thread(self.read, ...)`, so a
    subclass override is honoured. That is an upstream implementation detail this gate depends on
    completely, so it is pinned here: if a release ever gives the async half its own body, every
    async reach would bypass the narrowing silently and this test is what fails.
    """
    backend = _backend(tree, _only_alpha)

    assert asyncio.run(backend.aread("/beta/SKILL.md")).error == REFUSED
    listed = asyncio.run(backend.als("/"))
    assert [str(e["path"]).strip("/") for e in listed.entries or []] == ["alpha"]


def test_path_traversal_is_refused(tree: str) -> None:
    """Virtual mode is on, so `..` cannot leave the skills tree.

    deepagents' warning on the old default (`virtual_mode=False`) said it "allows absolute paths and
    `'..'` to bypass `root_dir`". 0.7 flipped the default to `True` and dropped the warning, which
    makes this assertion more useful rather than less: the constructor still sets it explicitly, and
    what is checked here is the *behaviour*, which no longer depends on whose default is in force.
    """
    backend = _backend(tree, lambda _: True)
    with pytest.raises(ValueError, match="traversal"):
        backend.read("/../../etc/hostname")


def test_the_skills_tree_is_read_only(tree: str) -> None:
    """An agent that can edit — or delete — a `SKILL.md` decides what judgment the next turn loads.

    Every write verb is probed, not a chosen three: `delete` arrived in deepagents 0.7 and was
    inherited working, which is why the classification below is the thing under test rather than
    the three calls that used to be here.
    """
    backend = _backend(tree, lambda _: True)
    writes: dict[str, Callable[[], Any]] = {
        "write": lambda: backend.write("/alpha/SKILL.md", "rewritten"),
        "edit": lambda: backend.edit("/alpha/SKILL.md", "body", "rewritten"),
        "delete": lambda: backend.delete("/alpha/SKILL.md"),
        "upload_files": lambda: backend.upload_files([]),
        "awrite": lambda: backend.awrite("/alpha/SKILL.md", "rewritten"),
        "aedit": lambda: backend.aedit("/alpha/SKILL.md", "body", "rewritten"),
        "adelete": lambda: backend.adelete("/alpha/SKILL.md"),
        "aupload_files": lambda: backend.aupload_files([]),
    }
    assert set(writes) == _WRITE_METHODS, "every write verb the gate refuses must be called here"

    for name, call in writes.items():
        assert _call(call) == "refused: PermissionError", f"{name} did not refuse"

    assert Path(tree, "alpha", "SKILL.md").exists(), "a refused write still changed the tree"


def test_every_method_the_backend_exposes_is_either_gated_or_refused(tree: str) -> None:
    """No method reaches a refused skill — enumerated from the backend, not from a written list.

    The point of deriving the list is that it survives an upstream release adding a method. A new
    reach path that this class does not override shows up here as an unexpected mention of `beta`,
    rather than as a quiet hole a hand-maintained list would never have mentioned.

    **The classification is what is derived, and that is the change 0.7 forced.** This used to
    subtract a hand-written set of write methods and check only the remainder, so a name added to
    that set was exempted from every assertion in the file — a hole in the shape of the one it
    existed to close. Now the reach probes and `_WRITE_METHODS` must *together* cover the surface,
    so an upstream addition has to be triaged into one or the other before this file passes.
    `delete` is the case that proves it: 0.7 added it, and neither list mentioned it.
    """
    backend = _backend(tree, _only_alpha)
    probes: dict[str, Callable[[], Any]] = {
        "ls": lambda: backend.ls("/"),
        "read": lambda: backend.read("/beta/SKILL.md"),
        "glob": lambda: backend.glob("**/*"),
        "grep": lambda: backend.grep("body"),
        "download_files": lambda: backend.download_files(["/beta/SKILL.md"]),
        "als": lambda: backend.als("/"),
        "aread": lambda: backend.aread("/beta/SKILL.md"),
        "aglob": lambda: backend.aglob("**/*"),
        "agrep": lambda: backend.agrep("body"),
        "adownload_files": lambda: backend.adownload_files(["/beta/SKILL.md"]),
    }

    # The concrete class as well as the protocol: what a model can reach is what
    # `NarrowedSkillsBackend` inherits, and a public method upstream adds to `FilesystemBackend`
    # alone would be invisible to a protocol-only derivation. The two surfaces are identical today,
    # which is a fact worth failing on rather than an assumption worth resting on.
    surface = {m for m in (*dir(BackendProtocol), *dir(FilesystemBackend)) if not m.startswith("_")}
    unclassified = surface - set(probes) - _WRITE_METHODS
    assert not unclassified, (
        f"the backend exposes method(s) this file classifies as neither reach nor write: "
        f"{sorted(unclassified)} — probe them here, or add them to _WRITE_METHODS and refuse them"
    )

    leaked = {
        name: repr(result)
        for name, probe in probes.items()
        if "beta" in repr(result := _call(probe))
    }
    assert not leaked, f"reach path(s) returned a refused skill: {sorted(leaked)}"


def test_grep_forwards_the_arguments_upstream_introspects_for() -> None:
    """`max_count` and `context_lines` must be accepted, because upstream asks whether they are.

    `protocol._method_accepts_max_count` decides whether the cap is pushed down to the backend or
    applied above it, so an override that dropped the keyword would change how many matches a
    caller gets depending on which class is underneath — the quietest kind of difference.
    """
    accepted = set(inspect.signature(NarrowedSkillsBackend.grep).parameters)
    declared = set(inspect.signature(FilesystemBackend.grep).parameters)
    assert declared <= accepted, (
        f"the gate's `grep` drops argument(s) the backend accepts: {sorted(declared - accepted)}"
    )


def _call(probe: Callable[[], Any]) -> Any:
    """Run a probe, treating a raised error as a refusal rather than a leak.

    Awaitables are driven to completion: the protocol's async twins are half its reach surface, and
    a coroutine object's `repr` names no skill at all — so leaving them unawaited would make every
    one of them pass by never running.
    """
    try:
        result = probe()
        return asyncio.run(_awaited(result)) if inspect.isawaitable(result) else result
    except (PermissionError, ValueError, NotImplementedError) as exc:
        return f"refused: {type(exc).__name__}"


async def _awaited(result: Any) -> Any:
    """`await` whatever a probe returned — `asyncio.run` wants a coroutine, not any awaitable."""
    return await result


def _p(hit: Any) -> str:
    """The path a glob/grep hit names."""
    if isinstance(hit, dict):
        return str(hit.get("path", ""))
    return str(getattr(hit, "path", hit))


# --- the read tool -------------------------------------------------------------------------------


def test_the_read_tool_is_named_what_the_skills_prompt_tells_the_model_to_call() -> None:
    """`SKILL_READ_TOOL` must match deepagents' own prompt, or skills are unloadable.

    `SkillsMiddleware` publishes each skill's path and instructs the model to "use `read_file` on
    the path shown". A tool named anything else leaves every skill advertised and unreadable —
    a failure that would look exactly like a model declining to load skills, so it is pinned
    against the prompt rather than trusted to stay in step.
    """
    from deepagents.middleware.skills import SKILLS_SYSTEM_PROMPT

    from chemclaw.agent.skill_backend import SKILL_READ_TOOL

    assert f"`{SKILL_READ_TOOL}`" in SKILLS_SYSTEM_PROMPT


def test_the_read_tool_reads_a_permitted_skill(tree: str) -> None:
    """The tool returns the body, so progressive disclosure actually completes."""
    tool = skill_read_tool(_backend(tree, _only_alpha))
    assert "body of alpha" in asyncio.run(tool.ainvoke({"file_path": "/alpha/SKILL.md"}))


def test_the_read_tool_carries_no_authority_of_its_own(tree: str) -> None:
    """It reads through the narrowed backend, so it cannot reach what the listing hid.

    The tool is the model's only route to a skill body, so this is the assertion that the gate
    survives being given a way in: a refused skill stays refused when asked for by name.
    """
    tool = skill_read_tool(_backend(tree, _only_alpha))
    assert asyncio.run(tool.ainvoke({"file_path": "/beta/SKILL.md"})) == REFUSED
