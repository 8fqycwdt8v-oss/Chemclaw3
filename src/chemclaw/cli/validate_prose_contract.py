"""Check that the agent's and operator's prose only names things that actually exist.

Prose promising capability the code lacks is invisible to `mypy`, `pytest` and frontmatter checks.
Each rule is deliberately narrow and names the one namespace it resolves against.

Agent prose (every SKILL.md and the built-in instruction blocks):

1. Every `` `name(` `` call must be a registered tool, a connector tool, a template launcher, or an
   allowlisted helper.
2. Every bare `snake_case` identifier must satisfy the same rule — the instructions name tools
   bare, and English prose never contains an underscore.
3. A skill must not direct the agent at a `*Workflow` class; the agent can only call tools.
4. Every note type prose tells the agent to write (spelled ``type `x` ``) must be in
   `KNOWN_NOTE_TYPES`.

Operator prose (guides, reference docs, package READMEs, `Makefile` non-recipe lines,
`.env.example`; never `docs/decisions/` or `docs/archive/`, which are records):

5. Every backticked path containing a `/` must exist (from the repo root, `src/chemclaw/` or the
   Helm chart).
6. Every ADR id must name a decision file, or a sub-decision label an ADR heading defines.
7. Every `CHEMCLAW_*` key must be a `Settings` field (or a declared credential variable).
8. Every metric name written as a whole backticked span must be declared in `core/metrics.py`.
9. Every PromQL series selector (`chemclaw_x{…}`) must be declared too — over all of `docs/`
   except the archive, since a wrong selector renders an alert that never fires.

Plus: rule 0, the corpus must exist (`check_corpus_is_assembled`); rule 10, each `PromptBlock`
declares exactly the tools its text names (`check_instruction_blocks`); rule 11, every
`core/model_prose.ModelProse` marker sits where `marked_prose` can read it. Rules 1-4 do not run
over marked prose, whose templates name argument fields in `snake_case`.

Counts in prose are not checkable; assert them in a test instead. Run via `make prose-validate`.
"""

import argparse
import ast
import importlib
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypeGuard

from chemclaw.agent.chemclaw_agent import (
    _INSTRUCTION_BLOCKS,
    _SAFETY_BLOCKS,
    PromptBlock,
    declared_tool_names,
    harness_tool_names,
    skill_tool_names,
    subagent_tool_names,
)
from chemclaw.connectors.registry import skills_dirs as connector_skills_dirs
from chemclaw.core.config import Settings, settings
from chemclaw.core.metrics import declared_histogram_names, declared_metric_names
from chemclaw.core.model_prose import ModelProse
from chemclaw.kg.note import known_note_types

# Symbols a skill may name in call form that are not agent tools. Kept short: adding one is a
# review decision.
_ALLOWED_NON_TOOLS = frozenset(
    {
        "neighborhood",  # kg.graph traversal primitive, explained conceptually by the query skill
    }
)

_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\(")
_WORKFLOW = re.compile(r"`([A-Za-z][A-Za-z0-9]*Workflow)`")
# A bare snake_case identifier (at least one underscore). The lookbehind skips backticked spans,
# paths, dotted attributes and argument positions inside a call; the lookahead skips a trailing `(`
# (rule 1's form), so no name is reported twice.
_BARE = re.compile(r"(?<![\w`/.,(-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w(])")
# A whole backticked `snake_case` span, the form skill bodies use to name a tool. Not part of
# `referenced_tool_names`: many such spans are result fields, not tools. `taught_tool_names` uses it
# because there the known-tool set filters the matches.
_TICKED = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
# ``type `x` `` / ``types `x` ``: the one phrasing that means "write a note of this kind", anchored
# on the word so backticked tools and fields do not match. The convention is stated in
# `skills/README.md`.
_NOTE_TYPE = re.compile(r"\btypes?\s+`([a-z][a-z0-9-]*)`")


def referenced_note_types(text: str) -> set[str]:
    """Every note type `text` tells the model to write, in the one gated phrasing.

    Public so tests share this extractor rather than a second one that could disagree.
    """
    return set(_NOTE_TYPE.findall(text))


def referenced_tool_names(text: str) -> set[str]:
    """Every tool name `text` promises the model, in either form it can take.

    Public so `tests/test_langgraph_agent.py` shares this extractor. Allowlisted non-tools are
    excluded.
    """
    names = set(_CALL.findall(text)) | set(_BARE.findall(text))
    return names - _ALLOWED_NON_TOOLS


def taught_tool_names(text: str, known: Collection[str]) -> set[str]:
    """Every tool in `known` that `text` teaches, in any of the three forms prose names one.

    The counterpart to `referenced_tool_names`: that asks whether an invented name resolves, so its
    patterns must be strict; this asks whether a known tool is named, so a loose pattern is safe —
    non-tools are not in `known` and drop out. `known` is a required parameter so the filter cannot
    be forgotten.
    """
    ticked = set(_TICKED.findall(text))
    return (referenced_tool_names(text) | ticked) & set(known)


# Repo root, derived from this file rather than from the cwd so the check behaves the same under
# `make`, under pytest, and from a subdirectory.
_ROOT = Path(__file__).resolve().parents[3]

# The documents a human operates this system from. `docs/decisions/` and `docs/archive/` are
# excluded: merged ADRs and archived documents are records and are never edited. Package READMEs are
# included via the globs below; their paths are written from the repo root or `src/chemclaw/`, which
# is what `_path_resolves` tries.
_OPERATOR_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "CLAUDE.md",
    "deploy/README.md",
    "skills/README.md",
    "knowledge/README.md",
    "docs/README.md",
)
# `knowledge/README.md` and `skills/README.md` sit at the repository root (layers 4 and 3), outside
# the other globs. `docs/planning/` is not here yet: its tickets name since-deleted files that need
# rewording one by one, tracked as a backlog row.
_OPERATOR_DOC_GLOBS = ("docs/guides/*.md", "docs/reference/*.md", "src/chemclaw/**/README.md")

# Two operator documents read outside `_OPERATOR_DOCS`, because neither is prose the way a `.md`
# file is.
_MAKEFILE = "Makefile"
_ENV_EXAMPLE = ".env.example"
# A Makefile recipe command: the only line where a backtick is shell command substitution, so the
# only line excluded. A recipe line starts with a literal tab; a tab followed by `#`/`@#` is a
# comment, and target `## help` text is Make syntax, so both stay in the corpus.
_MAKEFILE_RECIPE = re.compile(r"^\t(?!@?#)")


def _makefile_prose(text: str) -> str:
    """The Makefile's non-recipe lines, joined — the part safe to scan as prose.

    Excludes only actual shell commands; comments, targets, `.PHONY` and variables pass through.
    """
    return "\n".join(line for line in text.splitlines() if not _MAKEFILE_RECIPE.match(line))


# A backticked path. Requires a `/`, so a bare filename used as a noun is not a reference;
# placeholder spellings (`sources/<name>/`, `knowledge/{id}.md`, `*/SKILL.md`) cannot match.
_PATH = re.compile(r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[a-z]{2,7})`")
# An ADR id: the frozen `D-NNN` form, the dated `D-YYYY-MM-DD-<slug>` form, or a `D-A5a`-style
# sub-decision label, which reads like a citation and must resolve to a defining ADR.
_ADR = re.compile(r"\b(D-(?:\d{4}-\d{2}-\d{2}-[a-z0-9-]+|\d{3}|A\d+[a-z]?))\b")
# A `CHEMCLAW_*` env key. The final character may not be `_`, so a prefix written in prose
# (`CHEMCLAW_SERVICE_*`) is not read as a key whose name happens to end there.
_SUB_DECISION = re.compile(r"\b(D-A\d+[a-z]?)\b")
_ENV_KEY = re.compile(r"\b(CHEMCLAW_[A-Z0-9_]*[A-Z0-9])\b")
# A metric name cited as a whole code span, optionally with a label matcher. The span must end at
# the name (or matcher), which keeps module paths out. Label names are not checked here;
# `tests/test_metric_declarations.py` pins them.
_METRIC = re.compile(r"`(chemclaw_[a-z0-9_]+)(?:\{[^`}]*\})?`")
# A PromQL series selector: a metric name immediately followed by a label matcher — unambiguously an
# instruction to query, so rule 9 may run it over a wider corpus.
_METRIC_SELECTOR = re.compile(r"\b(chemclaw_[a-z0-9_]+)\{")

# Metrics a merged ADR cites in selector form that the registry no longer declares. Rule 9 reads
# `docs/decisions/`, which is never edited, so retiring a quoted series lands here. An entry must
# name a metric no live document depends on, since rule 9 stops checking it everywhere.
_RETIRED_METRIC_NAMES: frozenset[str] = frozenset(
    {
        # Retired with the note-proposal gate; a merged ADR still quotes it in selector form.
        "chemclaw_note_proposals_total",
    }
)

# `chemclaw_`-prefixed names in the operator corpus that are not metrics: `chemclaw_app` is the
# Postgres role. Adding one is a review decision.
_NON_METRIC_NAMES = frozenset({"chemclaw_app"})

# Environment variables that are legitimately not `Settings` fields. Explicit and short, for the
# same reason `_ALLOWED_NON_TOOLS` is: adding one is a review decision.
_NON_SETTINGS_ENV = frozenset(
    {
        "CHEMCLAW_COMPONENT",  # read by deploy/entrypoint.sh to pick a role, never by Settings
        "CHEMCLAW_REVISION",  # a Containerfile build ARG, exported as CHEMCLAW_DEPLOYMENT_REVISION
        # Read only by `infra/live/processes.sh`: the issuer the live lane derives `entra_issuer`,
        # `entra_jwks_url` and `entra_audience` from. Not a Settings field because nothing in Python
        # reads it.
        "CHEMCLAW_LIVE_ENTRA_TOKEN_URL",
        # The knowledge-sync credential: a chart-required Secret key read by
        # `deploy/knowledge-sync.sh` (and redacted by `core/logging.py`), never by Settings.
        "CHEMCLAW_KNOWLEDGE_REPO_TOKEN",
        # The UI's backend address, set on the Chemclaw3_ui Deployment; the deployment guide has
        # to name it to wire the two together.
        "CHEMCLAW_API_URL",
        # Documented *as removed*, so the prose naming them is correct and must stay readable.
        "CHEMCLAW_ENTRA_CLIENT_ID",
        "CHEMCLAW_MCP_SERVERS",
    }
)


def _block_groups() -> tuple[tuple[str, tuple[PromptBlock, ...]], ...]:
    """Every group of `PromptBlock`s a prompt is assembled from, by the symbol that holds it.

    `_INSTRUCTIONS` and `_SAFETY_BLOCKS` (the floor appended to a profile that replaces the default
    prose); rule 10 must see both. Read at call time so a test can patch either group.
    """
    return (("_INSTRUCTION_BLOCKS", _INSTRUCTION_BLOCKS), ("_SAFETY_BLOCKS", _SAFETY_BLOCKS))


def _block_origin(symbol: str, index: int, blocks: tuple[PromptBlock, ...]) -> str:
    """How one prompt block is named in a problem line — the symbol, index and opening words.

    The index addresses a block and the opening words make it searchable; neither alone suffices.
    """
    opening = " ".join(blocks[index].text.split()[:6])
    return f"src/chemclaw/agent/chemclaw_agent.py::{symbol}[{index}] ({opening}…)"


def _prose_sources() -> dict[str, str]:
    """The agent-facing prose to check: every SKILL.md plus the built-in instructions.

    Block by block, so rules 1-4 and rule 10 see the same text, including blocks the maximal
    `_INSTRUCTIONS` assembly omits.
    """
    sources = {
        _block_origin(symbol, index, blocks): block.text
        for symbol, blocks in _block_groups()
        for index, block in enumerate(blocks)
    }
    for skills_dir in [*settings.skills_dirs, *connector_skills_dirs()]:
        for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
            sources[str(path)] = path.read_text()
    return sources


#: The package every marked constant lives under, which is also the tree `marked_prose` walks.
_PACKAGE = Path(__file__).resolve().parents[1]


def _is_marker_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Whether `node` is a `ModelProse(...)` call, the one spelling a marker takes."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == ModelProse.__name__
    )


def _marked_sites(path: Path) -> tuple[list[str], list[int]]:
    """The module-level names `path` marks, and the lines of any marker a loader cannot reach.

    A marker is reachable as the value of a module-level assignment or inside one (a mapping value,
    a tuple member). Anywhere else it is evaluated only when code runs, so it would guard nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    reachable: set[int] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        calls = [node for node in ast.walk(value) if _is_marker_call(node)]
        if not calls:
            continue
        reachable.update(id(call) for call in calls)
        names.extend(target.id for target in targets if isinstance(target, ast.Name))
    stray = [
        node.lineno
        for node in ast.walk(tree)
        if _is_marker_call(node) and id(node) not in reachable
    ]
    return names, stray


def _marked_files() -> list[Path]:
    """Every module under the package that spells the marker at all — a cheap text prefilter."""
    marker = f"{ModelProse.__name__}("
    return [
        path
        for path in sorted(_PACKAGE.rglob("*.py"))
        if marker in path.read_text(encoding="utf-8")
    ]


def marked_prose() -> dict[str, str]:
    """Every string a module marks as model-facing, by `prose:<module>:<name>[<key>]`.

    The loader `tests/test_prose_contract.py` reads marked prose through. Names come from parsing
    each module and values from importing it, so a template assembled at module scope is read as the
    text a model is sent. Mappings and tuples contribute one entry per member.
    """
    found: dict[str, str] = {}
    for path in _marked_files():
        names, _stray = _marked_sites(path)
        if not names:
            continue
        dotted = ".".join(path.relative_to(_PACKAGE.parent).with_suffix("").parts)
        module = importlib.import_module(dotted)
        for name in names:
            value = getattr(module, name)
            members: Iterable[tuple[object, object]]
            if isinstance(value, ModelProse):
                found[f"prose:{dotted}:{name}"] = str(value)
                continue
            if isinstance(value, Mapping):
                members = value.items()
            elif isinstance(value, tuple | list):
                members = enumerate(value)
            else:
                continue
            for key, member in members:
                if isinstance(member, ModelProse):
                    found[f"prose:{dotted}:{name}[{key}]"] = str(member)
    return found


def check_marked_prose_is_reachable() -> list[str]:
    """Rule 11: a `ModelProse` marker sits where `marked_prose` can read it, or it is refused.

    The loader reads module-level constants only; a marker inside a function body would read as a
    guard and check nothing.
    """
    problems: list[str] = []
    for path in _marked_files():
        _names, stray = _marked_sites(path)
        problems.extend(
            f"{path.relative_to(_ROOT)}:{line}: `{ModelProse.__name__}` is not a module-level "
            "constant, so no prose guard reads it — hoist it to module scope"
            for line in stray
        )
    return problems


def check_instruction_blocks() -> list[str]:
    """Rule 10: a block declares exactly the tools its own text names, from a bindable name space.

    A `PromptBlock` is dropped when the graph does not bind everything in its `requires`, so a block
    naming a tool it does not require never drops, and one requiring a tool it does not name drops
    for no visible reason — hence equality.

    Requirements must be bindable: middleware tools (`skill_tool_names`, `harness_tool_names`,
    `subagent_tool_names`) are attached after the prompt is narrowed, so requiring one would drop
    the block everywhere. Skill and subagent tools are always attached, so a block may name them
    without requiring them; harness tools are neither nameable-unrequired nor requirable.

    `absent_unless` names must also be bindable and disjoint from `requires` (otherwise the block is
    never shown). Whether a denial clause declares every tool that would refute it cannot be checked
    mechanically; `tests/test_prose_contract.py` asserts the shipped ones.
    """
    always_bound = skill_tool_names() | set(subagent_tool_names())
    # Declared, not bound: an opt-in bundle makes these bindable, so a block keyed on one has a
    # condition that can occur.
    bindable = declared_tool_names() - always_bound - harness_tool_names()
    problems: list[str] = []
    for symbol, blocks in _block_groups():
        for index, block in enumerate(blocks):
            origin = _block_origin(symbol, index, blocks)
            named = referenced_tool_names(block.text) - always_bound
            if named != block.requires:
                problems.append(
                    f"{origin}: names {sorted(named)} and requires "
                    f"{sorted(block.requires)}. A block must require exactly the tools it names — "
                    "one it names but does not require is never dropped, and one it requires but "
                    "does not name is dropped from deployments with no reason to lose it."
                )
            unreachable = sorted((block.requires | block.absent_unless) - bindable)
            if unreachable:
                problems.append(
                    f"{origin}: keys on {unreachable}, which `build_langgraph_agent` never binds — "
                    "a middleware's tool (a filesystem verb, `write_todos`, `task`) is attached "
                    "after the surface the prompt is narrowed against, so this block would be "
                    "dropped from every deployment (or, for `absent_unless`, from none)."
                )
            both = sorted(block.requires & block.absent_unless)
            if both:
                problems.append(
                    f"{origin}: requires {both} and is also declared false when they are bound, so "
                    "no deployment is ever sent it. A block is a promise or a denial, not both."
                )
    return problems


def _operator_sources() -> dict[str, str]:
    """The operator-facing documents: the ones a human runs this system from.

    `Makefile` contributes only its non-recipe lines (`_makefile_prose`); `.env.example` is read
    whole.
    """
    paths = [_ROOT / name for name in _OPERATOR_DOCS]
    for pattern in _OPERATOR_DOC_GLOBS:
        paths.extend(sorted(_ROOT.glob(pattern)))
    sources = {
        str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    }
    makefile = _ROOT / _MAKEFILE
    if makefile.is_file():
        sources[_MAKEFILE] = _makefile_prose(makefile.read_text(encoding="utf-8"))
    env_example = _ROOT / _ENV_EXAMPLE
    if env_example.is_file():
        sources[_ENV_EXAMPLE] = env_example.read_text(encoding="utf-8")
    return sources


def _selector_sources() -> dict[str, str]:
    """Rule 9's corpus: the operator documents plus all of `docs/`, minus the archive.

    A selector in a merged ADR is still what an operator builds an alert from. Enumerated roots
    rather than an `rglob` of the working directory, so build output such as `make mutants`' copy
    cannot join the corpus; a new documentation root is one line here.
    """
    archive = _ROOT / "docs" / "archive"
    paths = [_ROOT / name for name in _OPERATOR_DOCS] + sorted((_ROOT / "docs").rglob("*.md"))
    return {
        str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file() and archive not in path.parents
    }


def _decision_files() -> list[Path]:
    """The ADR files, which are the authority on which ids resolve."""
    return sorted((_ROOT / "docs" / "decisions").glob("D-*.md"))


def _sub_decision_labels() -> set[str]:
    """Sub-decision labels an ADR actually defines, e.g. `D-A5a` inside `D-048`.

    Derived from ADR title lines only: a label is defined in its ADR's heading, and scanning bodies
    would let any ADR that merely discusses a label license it.
    """
    labels: set[str] = set()
    for path in _decision_files():
        title = path.read_text(encoding="utf-8").split("\n", 1)[0]
        labels |= set(_SUB_DECISION.findall(title))
    return labels


def _adr_resolves(adr_id: str, stems: set[str], labels: set[str]) -> bool:
    """Whether `adr_id` names a shipped ADR — an exact stem, a `D-NNN` prefix, or a known label."""
    return (
        adr_id in stems or adr_id in labels or any(stem.startswith(f"{adr_id}-") for stem in stems)
    )


def _path_resolves(candidate: str) -> bool:
    """Whether a backticked path names something on disk.

    Tried from the repo root, `src/chemclaw/` and the Helm chart; each spelling is unambiguous.
    """
    return any(
        (_ROOT / base / candidate).exists() for base in ("", "src/chemclaw", "deploy/helm/chemclaw")
    )


def _connector_token_envs() -> set[str]:
    """The credential variable names this deployment declares, lowercased like a Settings field.

    Bearer variables are real, operator-set and not `Settings` fields: each connector manifest's
    `BearerAuth.token_env`, plus the names held in settings for servers reached without a mounted
    manifest. Derived rather than allowlisted, so each new external server is covered and a typo'd
    variable still fails.

    Returns:
        The declared names, prefix-stripped and lowercased to match how the caller compares.
    """
    from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint
    from chemclaw.connectors.registry import discovered

    # `HttpEndpoint` narrowly, not "has an endpoint": a `StdioEndpoint` carries no `auth` at all,
    # because a subprocess is reached across no network and has nothing to authenticate to.
    declared = {
        endpoint.auth.token_env.removeprefix("CHEMCLAW_").lower()
        for _, manifest in discovered().values()
        for endpoint in (manifest.endpoint,)
        if isinstance(endpoint, HttpEndpoint) and isinstance(endpoint.auth, BearerAuth)
    }
    # Bearers of the internal backend servers (`calc`, `rxnlabel`), whose names are settings' values
    # because their manifests are deliberately not mounted.
    declared.add(settings.calc_server_token_env.removeprefix("CHEMCLAW_").lower())
    declared.add(settings.rxnlabel_server_token_env.removeprefix("CHEMCLAW_").lower())
    # Core's own read-only MCP face: its bearer name is a setting's value because the face must
    # never be addressable as a connector.
    declared.add(settings.mcp_face_token_env.removeprefix("CHEMCLAW_").lower())
    return declared


def check_corpus_is_assembled() -> list[str]:
    """Refuse a corpus this module could not assemble, rather than checking it and finding it clean.

    Every corpus is built by filtering out missing paths, so running from an installed wheel or a
    relocated package — or renaming a shipped document — would silently check nothing. Each
    `_OPERATOR_DOCS` entry missing is named; globs may legitimately match nothing. Separate from
    `check_operator_prose` so tests can substitute a one-document corpus.
    """
    sources = _operator_sources()
    problems = [
        f"{name}: named in _OPERATOR_DOCS but not found under {_ROOT} — this gate reads its corpus "
        "by filtering out paths that do not exist, so a document it cannot find is one it silently "
        "stops checking"
        for name in _OPERATOR_DOCS
        if name not in sources
    ]
    if not _decision_files():
        # Rule 6 resolves every cited ADR id against this set. Empty, it cannot pass a cited id —
        # and with no corpus either, nothing is checked at all. Both are the gate not running.
        problems.append(
            f"no ADR files found under {_ROOT / 'docs' / 'decisions'} — every cited ADR id would "
            "be unverifiable, so rule 6 did not run"
        )
    if not _selector_sources():
        problems.append(
            f"no documents found under {_ROOT} for the selector corpus — rule 9 did not run, and "
            "an alert built from a name nothing declares matches nothing and never fires"
        )
    return problems


def check_operator_prose() -> list[str]:
    """Rules 5-7 over the operator documents: paths, ADR ids and config keys must resolve."""
    stems = {path.stem for path in _decision_files()}
    labels = _sub_decision_labels()
    fields = set(Settings.model_fields) | _connector_token_envs()
    problems: list[str] = []
    for origin, text in _operator_sources().items():
        for candidate in sorted(set(_PATH.findall(text))):
            if not _path_resolves(candidate):
                problems.append(f"{origin}: names `{candidate}`, which does not exist")
        for adr_id in sorted(set(_ADR.findall(text))):
            if not _adr_resolves(adr_id, stems, labels):
                problems.append(
                    f"{origin}: cites {adr_id}, which has no file in docs/decisions/ — a "
                    "sub-decision label inside another ADR must cite that ADR instead"
                )
        for key in sorted(set(_ENV_KEY.findall(text)) - _NON_SETTINGS_ENV):
            if key.removeprefix("CHEMCLAW_").lower() not in fields:
                problems.append(f"{origin}: names {key}, which is not a Settings field")
    return problems


def _declared_including_histogram_series() -> frozenset[str]:
    """Every declared name, plus the three series Prometheus derives from each histogram.

    Operators query `<name>_bucket` (e.g. in `histogram_quantile`), so those spellings must resolve.
    Only `declared_histogram_names()` get the suffixes, never a counter.
    """
    declared = declared_metric_names()
    derived = {
        f"{name}{suffix}"
        for name in declared_histogram_names()
        for suffix in ("_bucket", "_sum", "_count")
    }
    return declared | frozenset(derived)


def check_metric_citations() -> list[str]:
    """Rules 8-9: a metric name written down for an operator must be one the registry declares.

    A nonexistent series name does not fail visibly: the alert renders, matches nothing, and never
    fires.

    Rule 8 (a whole backticked span) runs over the operator corpus only: in ADRs, backticked
    `chemclaw_*` spans are often correctly something else (a module, the Postgres role, a log
    marker, or a stale name an ADR is about). Rule 9 (a `name{…}` selector) runs over all of `docs/`
    except the archive, because a label matcher only ever means "query this". That reach is why
    `_RETIRED_METRIC_NAMES` exists: retiring a series a merged ADR quotes lands there.
    """
    declared = _declared_including_histogram_series()
    problems: list[str] = []
    for origin, text in _operator_sources().items():
        for name in sorted(set(_METRIC.findall(text)) - _NON_METRIC_NAMES - declared):
            problems.append(f"{origin}: names the metric `{name}`, which no registry declares")
    for origin, text in _selector_sources().items():
        for name in sorted(set(_METRIC_SELECTOR.findall(text)) - declared - _RETIRED_METRIC_NAMES):
            problems.append(
                f"{origin}: queries {name}{{…}}, which no registry declares — an alert built "
                "from this reads an empty series forever and looks healthy"
            )
    return problems


def check_prose_contract() -> list[str]:
    """Return one problem string per violation; empty means the prose matches the tool surface."""
    # One definition of the tool union, shared with the other validators and the agent. Declared
    # rather than bound, so prose naming an opt-in bundle's tool validates where the bundle is off;
    # a deleted tool is in neither set.
    tools = declared_tool_names()
    problems: list[str] = []
    for origin, text in _prose_sources().items():
        for name in sorted(referenced_tool_names(text) - tools):
            problems.append(f"{origin}: names {name} but no such agent tool is registered")
        # The effective vocabulary — core's plus the enabled bundles' — because that is what
        # `kg-validate` accepts.
        for note_type in sorted(referenced_note_types(text) - known_note_types()):
            problems.append(
                f"{origin}: tells the agent to write a `{note_type}` note, which is not a known "
                "note type — the note lands in `knowledge/` and then fails `make kg-validate`"
            )
        for workflow_name in sorted(set(_WORKFLOW.findall(text))):
            problems.append(
                f"{origin}: directs the agent at `{workflow_name}`, which it cannot invoke — "
                "name the tool that starts it instead"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: report every prose/capability mismatch; non-zero exit fails the CI gate.

    Parses arguments though it declares none, so an unsupported argument is refused. The corpus is
    derived from the checkout and cannot be overridden.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_prose_contract",
        description="Check that the agent-facing prose only names capability that exists.",
    ).parse_args(argv)
    problems = (
        check_corpus_is_assembled()
        + check_prose_contract()
        + check_instruction_blocks()
        + check_marked_prose_is_reachable()
        + check_operator_prose()
        + check_metric_citations()
    )
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} prose/capability mismatch(es)", file=sys.stderr)
        return 1
    print(
        "prose contract OK: every named tool, note type, path, ADR id, config key and metric "
        "resolves"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
