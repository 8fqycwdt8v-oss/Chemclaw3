"""`python -m chemclaw.cli.model_text_inventory` — every string a model reads, and what it costs.

Writes `schema/model-text/inventory.json` (`make model-text`); `--check` fails when the committed
file is stale, which `tests/test_model_text_inventory.py` also asserts. One entry per text: owner
(`file:symbol`), kind, tokens, characters, a content hash, and where it is paid: in `every-request`
prefix, `conditional` on a profile or bound surface, or `on-demand` (a skill body, a result).

The universe is `model_facing_descriptions()`, the one enumeration the prose guards also use, plus
what only a compiled graph knows: the tool schemas exactly as the model is sent them, including the
middleware's own tools, and the schema classes behind them. An entry whose words are already inside
another entry's (a docstring inside its tool schema) names it in `also_counted_in` and is left out
of every total. Tokens use `count_tokens_approximately`, the counter `tests/test_context_floor.py`
and `agent/compaction.py` budget with, so the prefix figures here and the ratchet's are comparable.
"""

import argparse
import ast
import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import sys
import tomllib
import types
import typing
from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, get_type_hints

import yaml
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

import chemclaw
import chemclaw.cli.validate_prose_contract as prose
from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import (
    _INSTRUCTION_BLOCKS,
    _SAFETY_BLOCKS,
    PromptBlock,
    _assemble,
    _capability_tools,
    connector_specs,
    harness_tool_names,
    instructions_for,
    subagent_tool_names,
    withheld_tool_names,
)
from chemclaw.agent.framing import ENVELOPE_TAG
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.text_overlay import DIGEST_CHARS, active, block_text
from chemclaw.agent.turn_ambient import turn_caps
from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.connectors.registry import discovered, enabled, server_tools_module
from chemclaw.connectors.transport import _allowed
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tools
from chemclaw.templates.registry import discovered as templates
from chemclaw.templates.registry import tool_name

_ROOT = Path(__file__).resolve().parents[3]
INVENTORY_PATH = _ROOT / "schema" / "model-text" / "inventory.json"

#: Where a text is paid for. `every-request` is in the default profile's prefix on every model call.
EVERY_REQUEST, CONDITIONAL, ON_DEMAND = "every-request", "conditional", "on-demand"
RESIDENCES = (EVERY_REQUEST, CONDITIONAL, ON_DEMAND)


def target_python() -> str:
    """The interpreter minor the repository targets, read from `[tool.mypy] python_version`.

    The inventory is measured under it and no other: on 3.13 the compiler dedents docstrings, and a
    tool description is its raw docstring (`langchain_core` keeps it uncleaned), so the same tool
    costs fewer tokens than the image sends.
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["tool"]["mypy"]["python_version"])


def running_python() -> str:
    """This interpreter's `major.minor`."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


#: The settings that decide which tools, blocks and skills a default turn is built from. Two
#: inventories are comparable only when these agree, so each records them.
SURFACE_SETTINGS = (
    "connectors_dirs",
    "connectors_enabled",
    "connector_urls",
    "templates_dir",
    "templates_enabled",
    "skills_dir",
    "skills_enabled",
    "profiles_dir",
    "agent_exhibits_enabled",
    "agent_html_artefacts_enabled",
    "agent_helper_roster",
    "agent_peer_roster",
    "harness_autonomy",
)


def overlay_digest() -> str | None:
    """The digest of the model-text overlay this process runs under, or `None` for shipped text."""
    overlay = active()
    return None if overlay is None else overlay.digest[:DIGEST_CHARS]


def environment() -> dict[str, Any]:
    """The values of `SURFACE_SETTINGS` this process runs under, with paths made repo-relative.

    A path inside the repository is written relative to it, so a checkout elsewhere agrees; one
    outside stays absolute and disagrees, which is the point.
    """

    def relative(value: Any) -> Any:
        if isinstance(value, list):
            return [relative(one) for one in value]
        if isinstance(value, str) and value.startswith(str(_ROOT)):
            return Path(value).relative_to(_ROOT).as_posix()
        return value

    return {name: relative(getattr(settings, name)) for name in SURFACE_SETTINGS}


ESTIMATOR = (
    "langchain_core count_tokens_approximately (chars/4 plus 3 per message), the counter "
    "tests/test_context_floor.py and agent/compaction.py use"
)

#: Characters of a text hash kept per entry: enough to show any edit in a diff.
_HASH_CHARS = 16

#: The first-party modules whose classes are walked for schema descriptions.
_OWN_MODULES = "chemclaw."


@dataclass
class Entry:
    """One model-facing text. `text` is what is measured and hashed; it is not written out."""

    id: str
    kind: str
    owner: str
    residence: str
    text: str
    also_counted_in: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Hold the canonical text, so nothing downstream sees a process-specific nonce."""
        self.text = canonical(self.text)

    def serialised(self) -> dict[str, Any]:
        """The committed form: everything but the text itself, in a fixed key order."""
        row: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "owner": self.owner,
            "residence": self.residence,
            "tokens": tokens(self.text),
            "chars": len(self.text),
            "sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:_HASH_CHARS],
        }
        if self.also_counted_in:
            row["also_counted_in"] = sorted(self.also_counted_in)
        return row


def canonical(text: str) -> str:
    """`text` with this process's per-process framing nonce replaced by zeros of the same length.

    The envelope tag and the system-speech mark carry a random nonce unless a secret is configured
    (`agent/framing.py`), so the same prompt reads differently in every process. Same length, so the
    token estimate is untouched.
    """
    nonce = ENVELOPE_TAG.removeprefix("retrieved-note-")
    return text.replace(nonce, "0" * len(nonce))


def tokens(text: str) -> int:
    """Tokens of `text` counted as one message, exactly as `tests/test_context_floor.py` counts."""
    return int(count_tokens_approximately([HumanMessage(canonical(text))]))


# --------------------------------------------------------------------------- the shared universe
#
# The seven classes of text the prose guards read. `tests/test_prose_contract.py` imports these, so
# the guards and the inventory cannot disagree about what a model is sent.


def model_facing_descriptions() -> dict[str, str]:
    """Every text this system ships to a model, by name.

    Seven classes: in-process tool docstrings, connector bundles' served tool docstrings (read with
    `ast`, so no bundle's dependencies are imported), durable-job docstrings as assembled, every
    loadable `SKILL.md`, the system prompt's blocks (both `_INSTRUCTION_BLOCKS` and the
    `_SAFETY_BLOCKS` appended to every profile), deployment profiles' `instructions:` and
    `description:`, template launchers' docstrings, and module constants marked
    `core/model_prose.ModelProse`. Unmarked prompt text built inline in a function body is outside.
    `tests/test_prose_contract.py` asserts that each class contributes, and how much.
    """
    # The registry only grows, so it still holds a launcher an earlier build bound under another
    # configuration; the graph never binds a withheld tool, and neither does this reading.
    withheld = withheld_tool_names()
    described: dict[str, str] = {
        getattr(fn, "__name__", str(fn)): inspect.getdoc(fn) or ""
        for fn in registered_tools()
        if getattr(fn, "__name__", str(fn)) not in withheld
    }
    bundles = sorted(Path(chemclaw.__file__).parent.glob("connectors/*/server/tools.py"))
    if not bundles:
        raise ValueError("no connector bundle tool modules found; this is reading the wrong tree")
    for module in bundles:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            served = any(
                isinstance(one, ast.Call)
                and isinstance(one.func, ast.Attribute)
                and one.func.attr == "tool"
                for one in node.decorator_list
            )
            if served:
                where = f"{module.parent.parent.name}:{node.name}"
                described[where] = ast.get_docstring(node) or ""
    described.update(_job_tool_docstrings())
    described.update(_shipped_skill_bodies())
    described.update(_instruction_block_texts())
    described.update(_profile_prose())
    described.update(_template_launcher_docstrings())
    described.update(_marked_prose())
    return described


def _job_tool_docstrings() -> dict[str, str]:
    """The durable-job tools' descriptions, assembled as `connectors/jobs.py::_docstring` does.

    The summary, the `description:` and one line per declared parameter; the `results` bundle's
    whole model-facing surface is these entries.
    """
    from chemclaw.connectors.jobs import _docstring

    found: dict[str, str] = {}
    for connector, (_, manifest) in discovered().items():
        for job in manifest.jobs:
            found[f"job:{connector}:{job.name}"] = _docstring(job)
    if not found:
        raise ValueError("no connector jobs discovered; this is reading the wrong tree")
    return found


def _shipped_skill_bodies() -> dict[str, str]:
    """Every `SKILL.md` a turn can load, bundle-local and the repository's own.

    Bundle skills reach a turn through `registry.skills_dirs`.
    """
    package = Path(chemclaw.__file__).parent
    found = {
        f"bundleskill:{path.parent.parent.parent.name}:{path.parent.name}": path.read_text(
            encoding="utf-8"
        )
        for path in sorted(package.glob("connectors/*/skills/*/SKILL.md"))
    }
    found.update(
        {
            f"skill:{path.parent.name}": path.read_text(encoding="utf-8")
            for path in sorted((_ROOT / "skills").glob("*/SKILL.md"))
        }
    )
    if not found:
        raise ValueError("no SKILL.md files found; this is reading the wrong tree")
    return found


def _instruction_block_texts() -> dict[str, str]:
    """The system prompt's own blocks, sent on every turn.

    Both groups: `_SAFETY_BLOCKS` is appended to every profile, including those that replace
    `_INSTRUCTION_BLOCKS`. Indexed by position because a `PromptBlock` has no name. Read through the
    model-text overlay, as the prompt itself is, so a candidate's inventory shows the candidate.
    """
    found = {
        f"block:{index}": block_text("blocks", index, block.text)
        for index, block in enumerate(_INSTRUCTION_BLOCKS)
    }
    found.update(
        {
            f"safety:{index}": block_text("safety", index, block.text)
            for index, block in enumerate(_SAFETY_BLOCKS)
        }
    )
    return found


def _profile_prose() -> dict[str, str]:
    """Every deployment profile's `instructions:` and `description:`.

    The first is the system prompt; the second goes into the `task` helper's description. Read off
    `data/profiles/` directly so a profile that fails to load is still scanned.
    """
    found: dict[str, str] = {}
    for path in sorted((_ROOT / "data" / "profiles").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found[f"profile:{path.stem}:instructions"] = str(loaded.get("instructions") or "")
        found[f"profile:{path.stem}:description"] = str(loaded.get("description") or "")
    if not found:
        raise ValueError("no deployment profiles found; this is reading the wrong tree")
    return found


def _template_launcher_docstrings() -> dict[str, str]:
    """The fixed-procedure launchers' docstrings, as `templates/registry.py` assembles them.

    Every enabled launcher, bound or withheld: a withheld launcher is bound the day a deployment
    turns its capability on.
    """
    from chemclaw.templates.registry import template_tools

    found = {
        f"template:{fn.__name__}": inspect.getdoc(fn) or "" for fn in template_tools(declared=True)
    }
    if not found:
        raise ValueError("no template launchers found; this is reading the wrong tree")
    return found


def _marked_prose() -> dict[str, str]:
    """Every module-level constant marked `ModelProse`: prompt text outside the agent module.

    `core/model_prose.py` is the declaration and `validate_prose_contract.marked_prose` the one
    loader, shared with `make prose-validate`.
    """
    found = prose.marked_prose()
    if not found:
        raise ValueError("no marked prose found; this is reading the wrong tree")
    return found


# ------------------------------------------------------------------------- what a graph binds


@contextmanager
def _as_a_deployment_runs() -> Iterator[None]:
    """Build under `session_store="postgres"`, which every real deployment sets.

    The suite's default `"memory"` strips `propose_skill`, which the shipped chart binds and pays
    for. A pure predicate: no database is opened.
    """
    original = settings.session_store
    settings.session_store = "postgres"
    try:
        yield
    finally:
        settings.session_store = original


@cache
def _served_in_process(connector: str) -> tuple[list[BaseTool], list[Any]]:
    """One bundle's own MCP server over an in-memory session: its `BaseTool`s and `tools/list`.

    The real `FastMCP` server through `load_mcp_tools`, as a turn loads it; only the transport is
    replaced. Empty for a bundle whose server lives in `Chemclaw3-mcp`. Cached: it is imported and
    served once per process however many questions are asked of it.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp.shared.memory import create_connected_server_and_client_session

    module = server_tools_module(connector)
    server = getattr(module, "server", None) if module is not None else None
    if server is None:
        return [], []

    async def load() -> tuple[list[BaseTool], list[Any]]:
        async with create_connected_server_and_client_session(server) as session:
            listed = (await session.list_tools()).tools
            return list(await load_mcp_tools(session)), list(listed)

    return asyncio.run(load())


def _bound_surface() -> tuple[list[BaseTool], dict[str, str], dict[str, list[Any]]]:
    """The default profile's compiled graph: every tool it binds, as the model is sent them.

    Returns the tools, the connector each connector tool came from, and each in-repo bundle's raw
    `tools/list` (for output schemas). The graph is built exactly as the context floor builds it:
    this repository's own servers bound, the fleet-served bundles absent.
    """
    profile = get_profile(None)
    listed = {name: raw for name in discovered() if (raw := _served_in_process(name)[1])}
    connectors: list[BaseTool] = []
    origin: dict[str, str] = {}
    for spec in connector_specs(profile):
        for tool in _allowed(_served_in_process(spec.name)[0], spec.allowed_tools):
            connectors.append(tool)
            origin[tool.name] = spec.name
    with _as_a_deployment_runs(), turn_caps(TurnUsage()):
        graph = build_langgraph_agent(
            model=GenericFakeChatModel(messages=iter([AIMessage(content="")])),
            profile=profile,
            audit_sink=NullAuditSink(),
            connectors=connectors,
        )
    bound = list(graph.nodes["tools"].bound._tools_by_name.values())
    return bound, origin, listed


def _relative(path: str | Path) -> str:
    """`path` relative to the repository root, in posix form."""
    return Path(path).resolve().relative_to(_ROOT).as_posix()


def _symbol_owner(obj: Any) -> str:
    """`file:symbol` of a function or class."""
    target = inspect.unwrap(obj)
    source = inspect.getsourcefile(target)
    if source is None:
        raise ValueError(f"no source file for {obj!r}")
    return f"{_relative(source)}:{getattr(target, '__qualname__', target.__name__)}"


def _generated_owner(name: str) -> str | None:
    """Where a generated launcher's words are written: its job's manifest or template file."""
    for path, manifest in discovered().values():
        if any(job.name == name for job in manifest.jobs):
            return f"{_relative(path / 'connector.yaml')}:jobs.{name}"
    for template in templates().values():
        if tool_name(template) == name:
            return f"data/templates/{template.name}.yaml"
    return None


def _upstream_owner(name: str) -> str:
    """Who writes a middleware tool's words: this repository's plan scope, or the framework."""
    if name in harness_tool_names():
        return "src/chemclaw/agent/plan_scope.py:ScopedTodoListMiddleware"
    if name in subagent_tool_names():
        return "upstream:deepagents.SubAgentMiddleware"
    return "upstream:deepagents.FilesystemMiddleware"


def _tool_owner(
    tool: BaseTool, functions: dict[str, Any], origin: dict[str, str]
) -> tuple[str, str]:
    """`(kind, owner)` for one bound tool: first-party, a connector's, or the framework's own."""
    if tool.name in origin:
        bundle = origin[tool.name]
        return "mcp-tool-schema", f"src/chemclaw/connectors/{bundle}/server/tools.py:{tool.name}"
    if tool.name in functions:
        generated = _generated_owner(tool.name)
        return "tool-schema", generated or _symbol_owner(functions[tool.name])
    return "upstream-tool-schema", _upstream_owner(tool.name)


def _tool_text(tool: BaseTool) -> str:
    """One tool exactly as a provider is sent it: LangChain's own conversion, compactly dumped."""
    return json.dumps(convert_to_openai_tool(tool))


def _tool_entries(
    bound: Sequence[BaseTool], origin: dict[str, str], functions: dict[str, Any]
) -> list[Entry]:
    """One `tool:<name>` entry per bound tool, in the default profile's every-request prefix."""
    entries = []
    for tool in sorted(bound, key=lambda one: one.name):
        kind, owner = _tool_owner(tool, functions, origin)
        entries.append(Entry(f"tool:{tool.name}", kind, owner, EVERY_REQUEST, _tool_text(tool)))
    return entries


# ------------------------------------------------------------------------------ schema classes


def _classes_in(annotation: Any) -> Iterator[type]:
    """Every class an annotation names, through unions, generics and `Annotated`."""
    if isinstance(annotation, type):
        yield annotation
    for argument in typing.get_args(annotation):
        yield from _classes_in(argument)


def _own(cls: type) -> bool:
    """Whether this repository owns `cls`'s words: a pydantic model, TypedDict or Enum of ours."""
    kind = issubclass(cls, BaseModel | Enum) or typing.is_typeddict(cls)
    return kind and cls.__module__.startswith(_OWN_MODULES)


def _members(cls: type) -> Iterator[type]:
    """The own schema classes `cls`'s fields refer to."""
    if issubclass(cls, BaseModel):
        annotations = [info.annotation for info in cls.model_fields.values()]
    elif typing.is_typeddict(cls):
        annotations = list(get_type_hints(cls, include_extras=True).values())
    else:
        return
    for annotation in annotations:
        yield from (one for one in _classes_in(annotation) if _own(one))


def _class_text(cls: type) -> str:
    """What the model reads of one class: its docstring, then each field's description."""
    lines = [inspect.cleandoc(cls.__doc__ or "")]
    if issubclass(cls, BaseModel):
        lines += [
            f"{name}: {info.description}"
            for name, info in cls.model_fields.items()
            if info.description
        ]
    elif issubclass(cls, Enum):
        lines += [f"{member.name} = {member.value!r}" for member in cls]
    return "\n".join(lines)


def _reachable(roots: Iterable[type]) -> dict[type, set[type]]:
    """Every own schema class reachable from `roots`, each with the roots that reach it."""
    reached: dict[type, set[type]] = {}
    for root in roots:
        pending = [root]
        while pending:
            cls = pending.pop()
            if root in reached.get(cls, set()):
                continue
            reached.setdefault(cls, set()).add(root)
            pending.extend(_members(cls))
    return reached


def _response_formats() -> list[type[BaseModel]]:
    """The classes a structured-output call is made with, found at its call sites.

    Read from source (`with_structured_output(X, ...)` and the tournament's `_structured(X, ...)`),
    so a new response format needs no entry here. Each must resolve to a pydantic model.
    """
    found: dict[str, type[BaseModel]] = {}
    for path in sorted(Path(chemclaw.__file__).parent.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "with_structured_output" not in text and "_structured(" not in text:
            continue
        module = _module_of(path)
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            callee = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            argument = node.args[0]
            if callee in {"with_structured_output", "_structured"} and isinstance(
                argument, ast.Name
            ):
                value = getattr(module, argument.id, None)
                if isinstance(value, type) and issubclass(value, BaseModel):
                    found[f"{value.__module__}.{value.__qualname__}"] = value
    return [found[key] for key in sorted(found)]


def _module_of(path: Path) -> types.ModuleType:
    """The imported module a source file under the package is."""
    dotted = ".".join(path.relative_to(Path(chemclaw.__file__).parent.parent).with_suffix("").parts)
    return importlib.import_module(dotted)


def _class_owner(cls: type, fallback: str) -> str:
    """`file:Class`, or `fallback` for a class generated at import (a template's inputs model)."""
    module = sys.modules.get(cls.__module__)
    if getattr(module, cls.__qualname__, None) is cls:
        return _symbol_owner(cls)
    return fallback


def _schema_entries(
    bound: Sequence[BaseTool], origin: dict[str, str], owners: dict[str, str]
) -> list[Entry]:
    """The own pydantic, TypedDict and Enum classes behind tool arguments and response formats.

    Their words are inside the tool schema (or response-format schema) that uses them, so each is
    `also_counted_in` those entries and adds nothing to a total; it exists to say who owns the text.
    """
    tool_roots: dict[type, list[str]] = {}
    for tool in bound:
        schema = tool.args_schema
        if tool.name in origin or not isinstance(schema, type) or not issubclass(schema, BaseModel):
            continue
        for cls in _members(schema):
            tool_roots.setdefault(cls, []).append(f"tool:{tool.name}")
    formats = {
        cls: f"response-format:{cls.__module__}.{cls.__qualname__}" for cls in _response_formats()
    }
    entries = [
        Entry(
            ident,
            "response-format-schema",
            _symbol_owner(cls),
            ON_DEMAND,
            json.dumps(cls.model_json_schema()),
        )
        for cls, ident in formats.items()
    ]
    owners = {**owners, **{entry.id: entry.owner for entry in entries}}
    users: dict[type, list[str]] = {}
    for cls, tools in tool_roots.items():
        for reached in _reachable([cls]):
            users.setdefault(reached, []).extend(tools)
    for cls in formats:
        for reached in _reachable(_members(cls)):
            users.setdefault(reached, []).append(formats[cls])
    for cls in sorted(users, key=lambda one: f"{one.__module__}.{one.__qualname__}"):
        used_by = sorted(set(users[cls]))
        residence = (
            EVERY_REQUEST if any(name.startswith("tool:") for name in used_by) else ON_DEMAND
        )
        entries.append(
            Entry(
                f"schema:{cls.__module__}.{cls.__qualname__}",
                "schema-class",
                _class_owner(cls, owners[used_by[0]]),
                residence,
                _class_text(cls),
                used_by,
            )
        )
    return entries


# --------------------------------------------------------------------------------- the rest


def _owner_of_key(key: str) -> str:
    """`file:symbol` for one `model_facing_descriptions()` key."""
    head, _, rest = key.partition(":")
    if head in {"block", "safety"}:
        group = "_INSTRUCTION_BLOCKS" if head == "block" else "_SAFETY_BLOCKS"
        return f"src/chemclaw/agent/chemclaw_agent.py:{group}[{rest}]"
    if head == "skill":
        return f"skills/{rest}/SKILL.md"
    if head == "bundleskill":
        bundle, _, name = rest.partition(":")
        return f"src/chemclaw/connectors/{bundle}/skills/{name}/SKILL.md"
    if head == "profile":
        name, _, which = rest.partition(":")
        return f"data/profiles/{name}.yaml:{which}"
    if head == "prose":
        module, _, symbol = rest.partition(":")
        return f"src/{module.replace('.', '/')}.py:{symbol}"
    if head == "job":
        connector, _, name = rest.partition(":")
        return f"src/chemclaw/connectors/{connector}/connector.yaml:jobs.{name}"
    if head == "template":
        return _generated_owner(rest) or f"data/templates/{rest}.yaml"
    if rest:
        return f"src/chemclaw/connectors/{head}/server/tools.py:{rest}"
    return key


def _block_residence(index_key: str, block: PromptBlock, bound: Collection[str]) -> str:
    """Whether the default prompt carries this block: it does exactly when `_assemble` keeps it."""
    group = "safety" if index_key.startswith("safety") else "blocks"
    kept = _assemble((block,), bound, durable_trail=True, group=group)  # type: ignore[arg-type]
    return EVERY_REQUEST if kept else CONDITIONAL


def _description_of(skill_text: str) -> str:
    """A skill's frontmatter `description:`, the part the listing in every prompt carries."""
    _, frontmatter, _body = skill_text.split("---", 2)
    return str(yaml.safe_load(frontmatter).get("description") or "").strip()


def _universe_entries(bound_names: set[str], tool_ids: set[str]) -> list[Entry]:
    """Every non-schema text of `model_facing_descriptions()`, classified.

    A docstring whose tool is bound is inside that tool's schema and is `also_counted_in` it.
    """
    entries: list[Entry] = []
    enabled_bundles = {manifest.name for manifest in enabled()}
    registry = {fn.__name__: fn for fn in registered_tools()}
    blocks = {f"block:{i}": b for i, b in enumerate(_INSTRUCTION_BLOCKS)}
    blocks.update({f"safety:{i}": b for i, b in enumerate(_SAFETY_BLOCKS)})
    for key, text in model_facing_descriptions().items():
        if not text.strip():
            continue  # a profile with no `instructions:` has no text to own
        head, _, rest = key.partition(":")
        owner = _symbol_owner(registry[key]) if key in registry else _owner_of_key(key)
        if key in blocks:
            residence = _block_residence(key, blocks[key], bound_names)
            entries.append(Entry(key, "prompt-block", owner, residence, text))
        elif head in {"skill", "bundleskill"}:
            bundle = rest.partition(":")[0] if head == "bundleskill" else ""
            name = rest.rpartition(":")[2]
            listed = EVERY_REQUEST if head == "skill" or bundle in enabled_bundles else CONDITIONAL
            entries.append(
                Entry(
                    f"skill-description:{name}",
                    "skill-description",
                    owner,
                    listed,
                    _description_of(text),
                )
            )
            entries.append(Entry(f"skill-body:{name}", "skill-body", owner, ON_DEMAND, text))
        elif head == "profile":
            kind = (
                "profile-instructions" if rest.endswith(":instructions") else "profile-description"
            )
            entries.append(Entry(key, kind, owner, CONDITIONAL, text))
        elif head == "prose":
            entries.append(Entry(key, "prose-constant", owner, ON_DEMAND, text))
        else:
            name = key.rpartition(":")[2]
            kind = "mcp-docstring" if rest and head not in {"job", "template"} else "tool-docstring"
            inside = [f"tool:{name}"] if f"tool:{name}" in tool_ids else []
            residence = EVERY_REQUEST if inside else CONDITIONAL
            entries.append(Entry(f"doc:{key}", kind, owner, residence, text, inside))
    return entries


def _mcp_output_entries(listed: dict[str, list[Any]]) -> list[Entry]:
    """The output schemas this repository's servers advertise (the model reads results)."""
    return [
        Entry(
            f"mcp-output:{bundle}:{tool.name}",
            "mcp-output-schema",
            f"src/chemclaw/connectors/{bundle}/server/tools.py:{tool.name}",
            ON_DEMAND,
            json.dumps(tool.outputSchema),
        )
        for bundle, tools in sorted(listed.items())
        for tool in sorted(tools, key=lambda one: one.name)
        if tool.outputSchema
    ]


def _unbound_server_entries(listed: dict[str, list[Any]], tool_ids: set[str]) -> list[Entry]:
    """Tool schemas of in-repo servers the default profile does not bind: paid where enabled."""
    entries = []
    for bundle, tools in sorted(listed.items()):
        for tool in sorted(tools, key=lambda one: one.name):
            if f"tool:{tool.name}" in tool_ids:
                continue
            body = {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema,
            }
            entries.append(
                Entry(
                    f"mcp-tool:{bundle}:{tool.name}",
                    "mcp-tool-schema",
                    f"src/chemclaw/connectors/{bundle}/server/tools.py:{tool.name}",
                    CONDITIONAL,
                    json.dumps(body),
                )
            )
    return entries


def _hoist_prose_in_the_prefix(entries: list[Entry]) -> None:
    """A `ModelProse` constant whose words sit inside a bound tool's schema is paid every request.

    `agent/plan_scope._SCOPE_GUIDANCE` is appended to `write_todos`'s description, for one. Matched
    on the constant's first line against each every-request tool, so no constant is named here.
    """
    prefix = {
        e.id: " ".join(e.text.replace("\\n", " ").split())
        for e in entries
        if e.id.startswith("tool:")
    }
    for entry in entries:
        if entry.kind != "prose-constant":
            continue
        head = " ".join(entry.text.split())[:80]
        inside = sorted(tool for tool, body in prefix.items() if head and head in body)
        if inside:
            entry.residence, entry.also_counted_in = EVERY_REQUEST, inside


def _prefix_section(entries: list[Entry], bound_names: set[str]) -> dict[str, int]:
    """The default profile's per-request prefix, in the parts the context floor charges.

    Tool schemas and the assembled instructions are counted as the floor counts them. Skill
    descriptions are the maximal listing, before the capability narrowing. The deepagents
    middleware's own prompt sections are not enumerable here and are in neither side of a comparison
    except through `tool_schemas`.
    """
    schemas = sum(tokens(e.text) for e in entries if e.id.startswith("tool:"))
    prompt = tokens(instructions_for(get_profile(None), bound_names))
    skills = sum(
        tokens(e.text)
        for e in entries
        if e.kind == "skill-description" and e.residence == EVERY_REQUEST
    )
    return {
        "tool_schemas": schemas,
        "instructions": prompt,
        "skill_descriptions": skills,
        "total": schemas + prompt + skills,
    }


def build_inventory() -> dict[str, Any]:
    """The whole inventory, as the JSON-ready dict `render` writes. Deterministic for one tree."""
    load_profiles()
    bound, origin, listed = _bound_surface()
    functions = {fn.__name__: fn for fn in _capability_tools(get_profile(None))}
    bound_names = {tool.name for tool in bound}
    entries = _tool_entries(bound, origin, functions)
    tool_ids = {entry.id for entry in entries}
    entries += _schema_entries(bound, origin, {e.id: e.owner for e in entries})
    entries += _universe_entries(bound_names, tool_ids)
    entries += _mcp_output_entries(listed)
    entries += _unbound_server_entries(listed, tool_ids)
    _hoist_prose_in_the_prefix(entries)
    entries.sort(key=lambda entry: entry.id)
    rows = [entry.serialised() for entry in entries]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate inventory ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    totals: dict[str, dict[str, int]] = {name: {"entries": 0, "tokens": 0} for name in RESIDENCES}
    for row in rows:
        if "also_counted_in" not in row:
            totals[row["residence"]]["entries"] += 1
            totals[row["residence"]]["tokens"] += row["tokens"]
    return {
        "estimator": ESTIMATOR,
        "python": running_python(),
        "environment": environment(),
        "overlay": overlay_digest(),
        "profile": "default",
        "prefix": _prefix_section(entries, bound_names),
        "totals": totals,
        "entries": rows,
    }


def render(inventory: dict[str, Any]) -> str:
    """The committed text: stable key order, one entry per block, trailing newline."""
    return json.dumps(inventory, indent=2, ensure_ascii=False) + "\n"


def diff(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """What changed between two inventories, one line per entry, for a failure message."""
    old = {row["id"]: row for row in committed.get("entries", [])}
    new = {row["id"]: row for row in current["entries"]}
    lines = [f"+ {name} ({new[name]['tokens']} tokens)" for name in sorted(new.keys() - old.keys())]
    lines += [
        f"- {name} ({old[name]['tokens']} tokens)" for name in sorted(old.keys() - new.keys())
    ]
    for name in sorted(old.keys() & new.keys()):
        if old[name] != new[name]:
            moved = (
                "text changed" if old[name]["sha256"] != new[name]["sha256"] else "metadata changed"
            )
            lines.append(
                f"~ {name}: {old[name]['tokens']} -> {new[name]['tokens']} tokens, {moved}"
            )
    if committed.get("prefix") != current["prefix"]:
        lines.append(f"~ prefix: {committed.get('prefix')} -> {current['prefix']}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Write the inventory, or with `--check` compare it; exit 1 when the file is stale."""
    parser = argparse.ArgumentParser(description="Dump every string the model reads.")
    parser.add_argument(
        "--check", action="store_true", help="fail when the committed file is stale"
    )
    parser.add_argument("--output", type=Path, default=INVENTORY_PATH, help="where to write")
    args = parser.parse_args(argv)
    logging.disable(
        logging.INFO
    )  # building the graph logs its wiring; the output here is the answer
    if running_python() != target_python():
        print(
            f"the inventory is measured under Python {target_python()} (the image's); this is "
            f"{running_python()}. Run it with `uv run --python {target_python()}`.",
            file=sys.stderr,
        )
        return 2
    if overlay_digest() is not None and args.output.resolve() == INVENTORY_PATH.resolve():
        print(
            f"CHEMCLAW_MODEL_TEXT_OVERLAY_DIR is set ({overlay_digest()}), so this process builds "
            "candidate text, and the shipped inventory is the shipped text's. Unset it, or write "
            "the candidate's inventory elsewhere with --output.",
            file=sys.stderr,
        )
        return 2
    current = build_inventory()
    text = render(current)
    if args.check:
        committed = (
            json.loads(args.output.read_text(encoding="utf-8")) if args.output.is_file() else {}
        )
        if committed == current:
            return 0
        print(f"{args.output} is stale; run `make model-text` and commit it:", file=sys.stderr)
        print("\n".join(diff(committed, current)), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    prefix = current["prefix"]
    print(f"wrote {args.output}: {len(current['entries'])} entries; per-request prefix {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
