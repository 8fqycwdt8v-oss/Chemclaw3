"""The skills backend is a gate, not a listing.

The model is handed skill paths and reads them with a filesystem tool, so a backend that
filtered only `ls` would hand a hidden skill over on request. Every test asks both: is it hidden,
and is it unreachable.
"""

import asyncio
import inspect
import logging
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.skill_backend import REFUSED, NarrowedSkillsBackend, SkillsReadOnlyRefusal

_SKILLS = ("alpha", "beta", "gamma")

# The verbs the gate refuses outright, sync and async. One test requires each to raise
# `SkillsReadOnlyRefusal`; another requires these plus the reach probes to cover every public
# method.
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
    """`aread`/`als` dispatch to the overridden sync methods.

    The gate depends on upstream implementing async twins via `asyncio.to_thread(self.read, ...)`;
    if that changes, async reads would bypass the narrowing and this fails.
    """
    backend = _backend(tree, _only_alpha)

    assert asyncio.run(backend.aread("/beta/SKILL.md")).error == REFUSED
    listed = asyncio.run(backend.als("/"))
    assert [str(e["path"]).strip("/") for e in listed.entries or []] == ["alpha"]


def test_path_traversal_is_refused(tree: str) -> None:
    """Path traversal with `..` cannot leave the skills tree; virtual mode is set explicitly."""
    backend = _backend(tree, lambda _: True)
    with pytest.raises(ValueError, match="traversal"):
        backend.read("/../../etc/hostname")


def test_the_skills_tree_is_read_only(tree: str) -> None:
    """The skills tree is read-only: every write verb, including `delete`, is refused."""
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
        assert _call(call) == "refused: SkillsReadOnlyRefusal", f"{name} did not refuse"

    # The type is the contract: as an `AuthorizationError`, the refusal is answered by
    # `surface_authorization_denials` rather than read as an unclassified crash.
    assert issubclass(SkillsReadOnlyRefusal, AuthorizationError)

    assert Path(tree, "alpha", "SKILL.md").exists(), "a refused write still changed the tree"


def test_every_method_the_backend_exposes_is_either_gated_or_refused(tree: str) -> None:
    """No method reaches a refused skill — enumerated from the backend, not from a written list.

    The reach probes and `_WRITE_METHODS` must together cover the public surface, so a method
    upstream adds must be classified before this passes.
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

    # The concrete class as well as the protocol: a method added to `FilesystemBackend` alone would
    # be inherited by `NarrowedSkillsBackend` and invisible to a protocol-only derivation.
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
    """`grep` accepts `max_count` and `context_lines`, which upstream introspects for."""
    accepted = set(inspect.signature(NarrowedSkillsBackend.grep).parameters)
    declared = set(inspect.signature(FilesystemBackend.grep).parameters)
    assert declared <= accepted, (
        f"the gate's `grep` drops argument(s) the backend accepts: {sorted(declared - accepted)}"
    )


def _call(probe: Callable[[], Any]) -> Any:
    """Run a probe, treating a raised error as a refusal rather than a leak.

    Awaitables are driven to completion, or the async half would pass by never running.
    """
    try:
        result = probe()
        return asyncio.run(_awaited(result)) if inspect.isawaitable(result) else result
    except (SkillsReadOnlyRefusal, ValueError, NotImplementedError) as exc:
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
    """`SKILL_READ_TOOL` is the `read_file` name the skills prompt tells the model to use.

    Otherwise every skill is advertised and unreadable, which looks like the model declining.
    """
    from deepagents.middleware.skills import SKILLS_SYSTEM_PROMPT

    from chemclaw.agent.skill_backend import SKILL_READ_TOOL

    assert f"`{SKILL_READ_TOOL}`" in SKILLS_SYSTEM_PROMPT


def _read(backend: BackendProtocol, path: str) -> str:
    """Read one path through upstream's `read_file`, bound to a narrowed backend.

    This is the tool that ships. The `runtime` is constructed with the fields the read path uses,
    since upstream injects it and the tool requires it.
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langgraph.prebuilt import ToolRuntime

    tool = {t.name: t for t in FilesystemMiddleware(backend=backend).tools}["read_file"]
    runtime: ToolRuntime[Any, Any] = ToolRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="test",
        store=None,
    )
    return str(asyncio.run(tool.ainvoke({"file_path": path, "runtime": runtime})))


def test_the_read_tool_reads_a_permitted_skill(tree: str) -> None:
    """The tool returns the body, so progressive disclosure actually completes."""
    assert "body of alpha" in _read(_backend(tree, _only_alpha), "/alpha/SKILL.md")


def test_the_read_tool_carries_no_authority_of_its_own(tree: str) -> None:
    """The read tool carries no authority of its own: a refused skill stays refused by name."""
    assert REFUSED in _read(_backend(tree, _only_alpha), "/beta/SKILL.md")


def test_a_refused_skills_write_reaches_the_model_as_a_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused skills write reaches the model as a refusal, not as an unclassified crash.

    Driven through a compiled graph, with the raising tool bound via `connectors=`, so the
    classification is shown reaching the model. Both the wording and the absence of an ERROR
    traceback are asserted.
    """
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.state import turn_config, turn_input
    from tests.fakes_langgraph import ScriptedChatModel

    @tool
    def rewrite_a_skill(path: str) -> str:
        """Stand in for the filesystem verb that reaches `NarrowedSkillsBackend.write`."""
        NarrowedSkillsBackend(root_dir=".", permits=lambda _: True).write(path, "mine now")
        raise AssertionError("the backend accepted a write to the skills tree")

    graph = build_langgraph_agent(
        model=ScriptedChatModel(
            [{"name": "rewrite_a_skill", "args": {"path": "/alpha/SKILL.md"}}, "done"]
        ),
        audit_sink=NullAuditSink(),
        connectors=[rewrite_a_skill],
    )

    with caplog.at_level(logging.ERROR):
        final = asyncio.run(graph.ainvoke(turn_input("rewrite that skill"), config=turn_config()))

    # Selected by type, not by `name`: `tool_authz._refusal_message` builds the `ToolMessage` from
    # the call id alone, so a refusal carries no tool name — which is itself worth pinning, since a
    # reader that filtered on the name would silently find nothing and pass.
    answered = next(message for message in final["messages"] if isinstance(message, ToolMessage))
    assert answered.text.startswith("Refused:"), (
        "the system prompt's own rule is that a result beginning `Refused:` is an access-control "
        f"decision rather than a fault; the model was told {answered.text!r}"
    )
    assert "read-only" in answered.text, "the refusal lost the sentence that says why"
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR], (
        "a refusal any model can trigger at will logged at ERROR: "
        f"{[record.getMessage() for record in caplog.records]}"
    )


def test_a_capped_grep_still_says_it_was_capped(tree: str) -> None:
    """A capped grep still says it was capped: the gate must preserve `GrepResult.truncated`.

    Driven with an all-permitting predicate, so the narrowing is the only possible difference.
    """
    base = FilesystemBackend(root_dir=tree, virtual_mode=True)
    narrowed = _backend(tree, lambda _name: True)
    assert base.grep("body", "/", None, max_count=2).truncated is True, (
        "the fixture no longer trips the cap"
    )
    assert narrowed.grep("body", "/", None, max_count=2).truncated is True
    # And an uncapped search is still reported as complete, so this is not a flag stuck on.
    assert narrowed.grep("body", "/", None).truncated is False


def test_a_skill_longer_than_the_read_default_says_so_rather_than_stopping_silently() -> None:
    """A skill longer than the read default says so rather than stopping silently.

    The tool always passes `limit`, so a backend default cannot be spent; upstream's partial-read
    notice discloses the window instead. Fails if a read-limit constant returns without a seam, or
    if upstream drops the disclosure.
    """
    from chemclaw.agent import skill_backend

    assert not hasattr(skill_backend, "_SKILL_READ_LIMIT"), (
        "a read-limit default is back in this module, and there is still nowhere to spend it: "
        "upstream binds `limit` on every call, so a backend signature default is never consulted"
    )

    with tempfile.TemporaryDirectory() as tmp:
        skill = Path(tmp) / "long-one"
        skill.mkdir()
        body = "\n".join(f"step {n}" for n in range(1, 213))
        (skill / "SKILL.md").write_text(body + "\n")

        read = _read(_backend(tmp, lambda _name: True), "/long-one/SKILL.md")

    assert "step 100" in read and "step 101" not in read, "the tool's default no longer caps at 100"
    assert "112 lines remaining from offset 100" in read
    assert "of 212 total" in read


def _empty_listing(tmp_path: Path) -> str:
    """The skills section a deployment with nothing to list actually sends."""
    from chemclaw.agent.langgraph_agent import _skills_middleware, skills_backend

    profile = get_profile("default")
    labelled = [("cold", str(tmp_path))]
    middleware = _skills_middleware(
        skills_backend(profile, _capability_tools(profile), labelled=labelled), labelled, profile
    )
    loaded = middleware.before_agent({}, None, None) or {}
    metadata = loaded.get("skills_metadata", [])
    assert metadata == [], "this fixture is about the empty case; the tree was not empty"
    return str(
        middleware.system_prompt_template.format(
            skills_locations=middleware._format_skills_locations(),
            skills_load_warnings="",
            skills_list=middleware._format_skills_list(metadata),
        )
    )


def test_an_empty_skills_listing_does_not_invite_the_model_to_write_one(tmp_path: Path) -> None:
    """An empty skills listing does not invite the model to write a skill."""
    section = _empty_listing(tmp_path)
    assert "You can create skills" not in section
    assert "read-only to every turn" in section


def test_the_skills_prompt_drops_a_source_distinction_this_deployment_has_no_sources_for(
    tmp_path: Path,
) -> None:
    """The skills prompt drops the source distinction this deployment has no sources for.

    Sources are labelled by directory; there is no machine-wide "Agents" tree.
    """
    section = _empty_listing(tmp_path)
    assert 'Sources labeled "Deepagents"' not in section
    # The rest of upstream's prompt still arrives from upstream rather than from a copy here.
    assert "progressive disclosure" in section


def test_the_skills_prompt_refuses_to_trim_a_sentence_upstream_no_longer_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trim is a substring, so upstream rewording it must be loud rather than a silent no-op.

    Without this the bump that changes that sentence leaves `_skills_prompt` returning upstream's
    template unchanged — the removed sentence quietly back — and nothing anywhere says so.
    """
    from chemclaw.agent import langgraph_agent

    monkeypatch.setattr(langgraph_agent, "SKILLS_SYSTEM_PROMPT", "{skills_locations}")
    with pytest.raises(RuntimeError, match="source-label sentence"):
        langgraph_agent._skills_prompt()
