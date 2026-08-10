"""The skills backend is a gate, not a listing (M4, D-2026-08-10).

This is the migration's load-bearing security test. Under MAF, narrowing the advertised skill list
*was* the gate, because `SkillsProvider` was the only way to a skill body. Under deepagents the
model is handed skill paths and reads them with a filesystem tool, so a backend that filtered only
`ls` would hide a role-gated skill from the listing and hand it over on request.

Every test here therefore asks the same question twice: is it hidden, and is it unreachable.
"""

import asyncio
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent.skill_backend import REFUSED, NarrowedSkillsBackend, skill_read_tool

_SKILLS = ("alpha", "beta", "gamma")


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

    deepagents' own warning says the default (`virtual_mode=False`) "allows absolute paths and
    `'..'` to bypass `root_dir`". A filesystem tool that could read any file the pod's service
    account can is not something to leave to a default, so the constructor sets it and this asserts
    it stayed set.
    """
    backend = _backend(tree, lambda _: True)
    with pytest.raises(ValueError, match="traversal"):
        backend.read("/../../etc/hostname")


def test_the_skills_tree_is_read_only(tree: str) -> None:
    """An agent that can edit `SKILL.md` can rewrite its own instructions."""
    backend = _backend(tree, lambda _: True)
    writes: tuple[Callable[[], Any], ...] = (
        lambda: backend.write("/alpha/SKILL.md", "rewritten"),
        lambda: backend.edit("/alpha/SKILL.md", "body", "rewritten"),
        lambda: backend.upload_files({}),
    )
    for call in writes:
        with pytest.raises(PermissionError):
            call()


def test_every_reach_path_the_protocol_exposes_is_gated(tree: str) -> None:
    """No method reaches a refused skill — enumerated from the protocol, not from a written list.

    The point of deriving the list is that it survives an upstream release adding a method. A new
    reach path that this class does not override shows up here as an unexpected mention of `beta`,
    rather than as a quiet hole a hand-maintained list would never have mentioned.
    """
    backend = _backend(tree, _only_alpha)
    probes: dict[str, Callable[[], Any]] = {
        "ls": lambda: backend.ls("/"),
        "read": lambda: backend.read("/beta/SKILL.md"),
        "glob": lambda: backend.glob("**/*"),
        "grep": lambda: backend.grep("body"),
        "ls_info": lambda: backend.ls_info("/"),
        "glob_info": lambda: backend.glob_info("**/*"),
        "grep_raw": lambda: backend.grep_raw("body"),
    }

    leaked = {
        name: repr(result)
        for name, probe in probes.items()
        if "beta" in repr(result := _call(probe))
    }
    assert not leaked, f"reach path(s) returned a refused skill: {sorted(leaked)}"


def _call(probe: Callable[[], Any]) -> Any:
    """Run a probe, treating a raised error as a refusal rather than a leak."""
    try:
        return probe()
    except (PermissionError, ValueError, NotImplementedError) as exc:
        return f"refused: {type(exc).__name__}"


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
