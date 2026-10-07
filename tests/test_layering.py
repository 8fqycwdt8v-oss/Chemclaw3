"""Package layering: every cross-package import is either an allowed edge or a declared exception.

The import graph is derived by AST-walking every `.py` under `src/chemclaw` and checked against a
small hand-authored policy of which package may depend on which; only the policy is declared, so
a new module is covered without editing a list.

Imports are walked in three scopes against different policies: module-scope edges must be in
`_ALLOWED_MODULE_EDGES`; function-scope (lazy) edges may also use `_ALLOWED_LAZY_EDGES`, each a
documented exception; `if TYPE_CHECKING:` edges are checked against `_ALLOWED_AT_ANY_SCOPE`
rather than exempted, so the guard is not an escape hatch.

Package cycles are declared in `_CYCLE_EDGES`, each direction with its own reason, because the
reason for A->B does not excuse B->A. `cli` is checked like any other package.

A static walk cannot see transitive imports, so the kernel rule (`chemclaw.core` imports no
sibling) is also checked at runtime: each core module, computed from disk, is imported in a clean
interpreter and `sys.modules` is checked for forbidden siblings.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"


def _module_name_for(path: Path) -> str:
    """The dotted module name a file on disk corresponds to (`__init__.py` names its package)."""
    rel = path.relative_to(_SRC_ROOT.parent)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def _package_of(module: str) -> str:
    """The top-level `chemclaw.<layer>` package a dotted module name belongs to."""
    parts = module.split(".")
    return module if len(parts) <= 2 else ".".join(parts[:2])


_ALL_FILES = sorted(_SRC_ROOT.rglob("*.py"))
_MODULE_NAMES: dict[Path, str] = {f: _module_name_for(f) for f in _ALL_FILES}
_IS_PACKAGE: dict[str, bool] = {mod: f.name == "__init__.py" for f, mod in _MODULE_NAMES.items()}


def _resolve_relative(current_module: str, level: int, submodule: str | None) -> str:
    """Resolve `from .[.[...]][submodule] import x` written in `current_module` to a dotted name.

    Mirrors `importlib._bootstrap._resolve_name`: a package's `__init__.py` resolves relative to
    itself, a plain module relative to its parent, and each extra dot climbs one package level.
    """
    parts = current_module.split(".")
    base = parts if _IS_PACKAGE.get(current_module, False) else parts[:-1]
    if level > 1:
        base = base[: -(level - 1)] if (level - 1) < len(base) else []
    return ".".join(base + submodule.split(".")) if submodule else ".".join(base)


@dataclass(frozen=True)
class _Import:
    file: Path
    lineno: int
    target: str
    scope: str  # "module", "function" or "type_checking"


class _ImportVisitor(ast.NodeVisitor):
    """Collect every first-party (`chemclaw.*`) import in one file, tagged by scope.

    `if TYPE_CHECKING:` bodies get their own scope so an annotation-only edge is visible and
    declarable; the `orelse` branch is what runs, so it is walked as ordinary code.
    """

    def __init__(self, module: str, path: Path) -> None:
        self.module = module
        self.path = path
        self.func_depth = 0
        self.type_checking_depth = 0
        self.imports: list[_Import] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._descend(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._descend(node)

    def _descend(self, node: ast.AST) -> None:
        self.func_depth += 1
        self.generic_visit(node)
        self.func_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_type_checking:
            self.generic_visit(node)
            return
        self.type_checking_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.type_checking_depth -= 1
        # `orelse` runs when TYPE_CHECKING is false, i.e. always at runtime: ordinary code.
        for stmt in node.orelse:
            self.visit(stmt)

    def _record(self, target: str, lineno: int) -> None:
        if not target.startswith("chemclaw"):
            return
        scope = (
            "type_checking"
            if self.type_checking_depth
            else ("function" if self.func_depth else "module")
        )
        self.imports.append(_Import(self.path, lineno, target, scope))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = (
            _resolve_relative(self.module, node.level, node.module)
            if node.level
            else (node.module or "")
        )
        self._record(target, node.lineno)
        self.generic_visit(node)


def _collect_imports() -> list[_Import]:
    imports: list[_Import] = []
    for f in _ALL_FILES:
        tree = ast.parse(f.read_text(), filename=str(f))
        visitor = _ImportVisitor(_MODULE_NAMES[f], f)
        visitor.visit(tree)
        imports.extend(visitor.imports)
    return imports


_IMPORTS = _collect_imports()
_PACKAGES = sorted({_package_of(m) for m in _MODULE_NAMES.values()} - {"chemclaw"})

Edge = tuple[str, str]


def _edges(scope: str) -> dict[Edge, list[_Import]]:
    """Cross-package import edges at the given scope, keyed by (source package, target package)."""
    out: dict[Edge, list[_Import]] = {}
    for imp in _IMPORTS:
        if imp.scope != scope:
            continue
        src = _package_of(_MODULE_NAMES[imp.file])
        dst = _package_of(imp.target)
        if src == dst or dst == "chemclaw":
            continue
        out.setdefault((src, dst), []).append(imp)
    return out


_MODULE_SCOPE_EDGES = _edges("module")
_FUNCTION_SCOPE_EDGES = _edges("function")
_TYPE_CHECKING_EDGES = _edges("type_checking")

# ---------------------------------------------------------------------------------------------
# The declared policy: which package may depend on which. This is the layering rule itself, so it
# is necessarily hand-authored.
# ---------------------------------------------------------------------------------------------

# The package-level cycles, each direction with the one-line reason it exists. Declared here rather
# than only in the flat set below so they are visible to a reader.
_CYCLE_EDGES: dict[Edge, str] = {
    ("chemclaw.templates", "chemclaw.durable"): (
        "the template registry launches a durable TemplateWorkflow"
    ),
    ("chemclaw.durable", "chemclaw.templates"): (
        "the workflow substitutes steps using the registry's own manifest/resolve types"
    ),
    ("chemclaw.templates", "chemclaw.agent"): (
        "template tool registration needs authz/session/tool-registry helpers from agent"
    ),
    ("chemclaw.agent", "chemclaw.templates"): ("the agent exposes template tools it builds on"),
    ("chemclaw.connectors", "chemclaw.durable"): (
        "a connector bundle's own durable jobs live in durable.* (registry, workflows, activities)"
    ),
    ("chemclaw.durable", "chemclaw.connectors"): (
        "template activities resolve a connector's manifest to run a bundle's job as a step "
        "(durable/template_activities.py). NOT the connector-job wrapper, which this reason used "
        "to name: `durable/connector_job.py` imports nothing from any connector, and "
        "`test_the_connector_job_wrapper_imports_no_connector` below pins that separately, because "
        "this policy is package-granular and cannot express it"
    ),
    ("chemclaw.agent", "chemclaw.durable"): ("agent tools start durable jobs (durable_tools)"),
    ("chemclaw.durable", "chemclaw.agent"): (
        "activities stamp identity and run agent turns using agent's own audit/authz/profile code"
    ),
    ("chemclaw.agent", "chemclaw.connectors"): (
        "the agent's tool surface is generated from the connector registry"
    ),
    ("chemclaw.connectors", "chemclaw.agent"): (
        "connector jobs and identity plumbing authorize against agent's authz/identity context"
    ),
    ("chemclaw.connectors", "chemclaw.ingest"): (
        "a bundle serving structural hits asks the transcription store two questions no "
        "fingerprint index can answer about its own contents: whether the source has withdrawn the "
        "run a hit stands for (`connectors/rxnfp/server/tools.py::similar_reactions`, "
        "D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports), and how many citation-only records "
        "sit outside every structure index (`ReactionRecordStore.citation_only`, asked by the "
        "`rxnfp` and `molfp` bundles). The reverse edge stays undeclared: ingestion must not reach "
        "into a bundle"
    ),
}

# The full declared graph: every module-scope edge the codebase is allowed to have. `core` has no
# outgoing entry - that absence *is* the kernel rule, for the module-scope graph; the runtime
# subprocess check below covers the transitive case a static graph cannot see.
_ALLOWED_MODULE_EDGES: set[Edge] = {
    ("chemclaw.agent", "chemclaw.connectors"),
    ("chemclaw.agent", "chemclaw.core"),
    ("chemclaw.agent", "chemclaw.durable"),
    ("chemclaw.agent", "chemclaw.ingest"),
    # Half of an `agent <-> kg` cycle until R2: `kg.proposal` reached up for the ambient actor,
    # session and correlation id. Those are `core.identity_context`/`core.session_context` now, so
    # `kg -> agent` is gone from the graph and from this policy — kg may no longer import agent.
    ("chemclaw.agent", "chemclaw.kg"),
    ("chemclaw.agent", "chemclaw.memory"),
    # The operational read model (F3). `agent/operations_tools.py` is the tool over it, in the
    # same relationship `agent/memory_tools.py` has to `memory/`: the store is below, the
    # conversation plumbing is here.
    ("chemclaw.agent", "chemclaw.operations"),
    # The prescriptive-design layer. `agent` writes designs through it, `api` serves them.
    ("chemclaw.agent", "chemclaw.protocols"),
    ("chemclaw.agent", "chemclaw.retrieval"),
    ("chemclaw.agent", "chemclaw.science"),
    ("chemclaw.agent", "chemclaw.templates"),
    ("chemclaw.api", "chemclaw.agent"),
    # Half of an `api <-> connectors` cycle until R2, when the metrics registry a connector's own
    # HTTP surface reached back up for became `core.metrics`. What is left is an ordinary
    # downward edge, so it is declared here and no longer in `_CYCLE_EDGES`.
    ("chemclaw.api", "chemclaw.connectors"),
    ("chemclaw.api", "chemclaw.core"),
    ("chemclaw.api", "chemclaw.durable"),
    ("chemclaw.api", "chemclaw.kg"),
    ("chemclaw.api", "chemclaw.protocols"),
    # Like `protocols`: a front-door route decides on a stored document this layer owns (the
    # composed-workflow approval is a column on `composed_workflows`), so it reaches the store
    # directly.
    ("chemclaw.api", "chemclaw.templates"),
    ("chemclaw.cli", "chemclaw.agent"),
    # `cli.leak_probe` builds the real front door in its own process, since the leak it measures is
    # in what a turn retains. Nothing in `api` imports `cli`, so the edge stays one-way.
    ("chemclaw.cli", "chemclaw.api"),
    ("chemclaw.cli", "chemclaw.connectors"),
    ("chemclaw.cli", "chemclaw.core"),
    # `cli.schedules` is a thin `main()` shim over `durable.schedules`; only `cli` reaches into
    # `durable`, so this is not a cycle.
    ("chemclaw.cli", "chemclaw.durable"),
    ("chemclaw.cli", "chemclaw.evals"),
    ("chemclaw.cli", "chemclaw.ingest"),
    ("chemclaw.cli", "chemclaw.kg"),
    # `cli/rekey_compounds.py` is the terminal half of `memory.compound_rekey`, which supersedes
    # compound notes a `STANDARDIZATION_VERSION` bump moved via `memory.supersede.retire_note`. The
    # shim holds no logic.
    ("chemclaw.cli", "chemclaw.memory"),
    # `cli/propose_profile.py` mines `audit_events.tool`, the same model-written column
    # `operations.activity.safe_tool_name` bounds for its own readers — and a bound applied to one
    # reader of a column is not a bound, which that function's docstring argues.
    ("chemclaw.cli", "chemclaw.operations"),
    # `cli/verifier_margin.py` measures the judge's margin, and the judge's input type is
    # `retrieval.evidence.EvidenceChunk`; any other input would measure a different call.
    ("chemclaw.cli", "chemclaw.retrieval"),
    # `cli/rekey_campaigns.py` re-keys BO campaigns with
    # `science.bo.campaign_record.campaign_id_for`; a re-key must not carry a second copy of the
    # derivation it applies.
    ("chemclaw.cli", "chemclaw.science"),
    ("chemclaw.cli", "chemclaw.templates"),
    ("chemclaw.connectors", "chemclaw.agent"),
    ("chemclaw.connectors", "chemclaw.core"),
    ("chemclaw.connectors", "chemclaw.durable"),
    ("chemclaw.connectors", "chemclaw.kg"),
    ("chemclaw.connectors", "chemclaw.science"),
    ("chemclaw.durable", "chemclaw.agent"),
    ("chemclaw.durable", "chemclaw.cli"),
    ("chemclaw.durable", "chemclaw.connectors"),
    ("chemclaw.durable", "chemclaw.core"),
    ("chemclaw.durable", "chemclaw.evals"),
    ("chemclaw.durable", "chemclaw.hypotheses"),
    ("chemclaw.durable", "chemclaw.ingest"),
    ("chemclaw.durable", "chemclaw.kg"),
    ("chemclaw.durable", "chemclaw.memory"),
    ("chemclaw.durable", "chemclaw.retrieval"),
    ("chemclaw.durable", "chemclaw.science"),
    ("chemclaw.durable", "chemclaw.templates"),
    ("chemclaw.evals", "chemclaw.agent"),
    ("chemclaw.evals", "chemclaw.api"),
    ("chemclaw.evals", "chemclaw.core"),
    ("chemclaw.evals", "chemclaw.hypotheses"),
    ("chemclaw.evals", "chemclaw.kg"),
    ("chemclaw.evals", "chemclaw.retrieval"),
    ("chemclaw.evals", "chemclaw.science"),
    ("chemclaw.hypotheses", "chemclaw.kg"),
    ("chemclaw.ingest", "chemclaw.core"),
    ("chemclaw.ingest", "chemclaw.kg"),
    ("chemclaw.ingest", "chemclaw.retrieval"),
    ("chemclaw.ingest", "chemclaw.science"),
    ("chemclaw.kg", "chemclaw.core"),
    ("chemclaw.memory", "chemclaw.core"),
    ("chemclaw.memory", "chemclaw.ingest"),
    ("chemclaw.memory", "chemclaw.kg"),
    ("chemclaw.memory", "chemclaw.science"),
    # `operations` reads five of this system's own tables and nothing else. It is a leaf on
    # the kernel by construction: a reading of the record must not be able to reach the
    # capability that wrote it, or the trail would be able to describe itself.
    ("chemclaw.operations", "chemclaw.core"),
    # The outbound delivery seam: a leaf on the kernel, like `publish`. It reads config and the log
    # redaction filter; `durable` (the digest job) imports it, and it imports nothing back.
    ("chemclaw.deliver", "chemclaw.core"),
    ("chemclaw.durable", "chemclaw.deliver"),
    # The prescriptive-design layer: a leaf reading the kernel and `science.labels.vocabulary`, so a
    # design and a precedent share one species-role vocabulary. It imports neither `ingest` nor
    # `kg`: a design is prescriptive and their shapes are descriptive.
    ("chemclaw.protocols", "chemclaw.core"),
    ("chemclaw.protocols", "chemclaw.science"),
    # Artefacts: a leaf on the kernel that reuses `protocols`' `FieldChange` diff shape and CSV
    # formula-injection guard rather than copying them.
    ("chemclaw.exhibits", "chemclaw.core"),
    ("chemclaw.exhibits", "chemclaw.protocols"),
    # A `geometry` artefact cites a calculation by-product rather than copying it, so `sources.py`
    # reads the calc artifact store, for existence and bytes only.
    ("chemclaw.exhibits", "chemclaw.science"),
    # A development report requested from a conversation lands there as a `document` artefact,
    # written by the report's own activity (`durable/report_workflow.record_report_exhibit`)
    # through the one store every artefact writer uses.
    ("chemclaw.durable", "chemclaw.exhibits"),
    ("chemclaw.agent", "chemclaw.exhibits"),
    ("chemclaw.api", "chemclaw.exhibits"),
    # `analytical` reads only `core.units`: comparing a number to a limit needs no chemistry, and a
    # specification (prescriptive) must not share a shape with a `reaction_records` row
    # (descriptive).
    ("chemclaw.analytical", "chemclaw.core"),
    # The agent reaches the analytical tier the same way it reaches `protocols`: one tools module
    # over the models, with no logic of its own beyond parsing what the model wrote into the
    # `Measurement`s the tier takes.
    ("chemclaw.agent", "chemclaw.analytical"),
    ("chemclaw.publish", "chemclaw.core"),
    ("chemclaw.publish", "chemclaw.ingest"),
    ("chemclaw.durable", "chemclaw.publish"),
    ("chemclaw.cli", "chemclaw.publish"),
    # `cli/validate_channels.py` is `make channel-validate`, the same shape every other
    # validator entrypoint has: a terminal command that reads one seam's manifests and
    # binds each driver's signature. Nothing in `deliver` imports back.
    ("chemclaw.cli", "chemclaw.deliver"),
    # The `results` bundle's job re-queues stored calculations via `publish.backfill`, which lives
    # in `publish` rather than `cli` so a connector does not import a terminal entrypoint.
    ("chemclaw.connectors", "chemclaw.publish"),
    ("chemclaw.retrieval", "chemclaw.core"),
    ("chemclaw.retrieval", "chemclaw.kg"),
    ("chemclaw.retrieval", "chemclaw.science"),
    ("chemclaw.science", "chemclaw.core"),
    ("chemclaw.templates", "chemclaw.agent"),
    ("chemclaw.templates", "chemclaw.core"),
    ("chemclaw.templates", "chemclaw.durable"),
} | set(_CYCLE_EDGES)

# Function-scope-only exceptions: deliberate lazy imports of a package that may not be imported at
# module scope. Each is in `_FUNCTION_SCOPE_EDGES` and deliberately absent from
# `_ALLOWED_MODULE_EDGES`. Exactly one originates in `chemclaw.core` (the connector registry);
# `test_core_has_one_lazy_exception_and_the_dict_says_which` holds that.
_ALLOWED_LAZY_EDGES: dict[Edge, str] = {
    ("chemclaw.hypotheses", "chemclaw.core"): (
        "`dispatch.structure_of` validates a subject's SMILES with `core.chem`, which imports "
        "RDKit. Lazy rather than module-scope because `hypotheses` is imported inside Temporal's "
        "workflow sandbox: that is exactly where `hypotheses.rating`'s module-scope numpy reached "
        "`os.putenv` and was refused "
        "(`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate`), and a second heavy C "
        "extension at import time is the same bet twice. The call sites are "
        "all in activities, where the import is free"
    ),
    ("chemclaw.science", "chemclaw.publish"): (
        "cached_compute offers a freshly computed primitive to the external results store. Lazy "
        "for two reasons that both matter: `science` is the pure-computation layer and must not "
        "depend on an outbound seam at import time, and with no sink configured the projection "
        "machinery is never imported at all - so a deployment that does not publish pays nothing "
        "for the hook (see `science.calc.store._publish_best_effort`)"
    ),
    ("chemclaw.core", "chemclaw.connectors"): (
        "logging's redaction filter resolves connector bearer-token env names lazily so "
        "core.logging - imported by every entrypoint first - must not hard-depend on the connector "
        "registry at import time (see its docstring)"
    ),
    ("chemclaw.deliver", "chemclaw.connectors"): (
        "Message.redacted resolves the same connector bearer-token env names core.logging's filter "
        "does, through the one definition both share, so the log scrub and the outbound scrub "
        "cannot cover different sets - which they did: this file claimed 'the same filter runs "
        "here' and redact_secrets reaches connector tokens only through an argument nothing "
        "passed. "
        "Lazy for the reason the core.logging exception above is: the outbound seam must not "
        "hard-depend on the connector registry at import time"
    ),
    ("chemclaw.evals", "chemclaw.agent"): (
        "the live judge builds its gateway client through the same `_tls_http_clients` the agent "
        "does, so a private CA configured for one is not silently absent from the other - it was: "
        "`live_judge` read `llm_base_url` and ignored `llm_tls_ca_bundle`, so grading against "
        "exactly the internal gateway that setting exists for died at TLS. Lazy so that importing "
        "the eval package costs neither a model client nor a connection pool - and because a "
        "graded run is the only thing in `evals` that needs the agent's provider seam at all. "
        "A second use rides the same edge and is not the provider seam: `live._tool_expectation_"
        "applies` reads `available_tool_names()` to decide whether a probe's `expects_tools` could "
        "have been met, so a question about a tool the fleet serves and this tree does not declare "
        "scores as untested rather than as a miss"
    ),
    ("chemclaw.kg", "chemclaw.connectors"): (
        "known_note_types/known_relations union core's closed vocabulary with what the enabled "
        "bundles declare, because two shipped note types (job-result, bo-candidate) are minted by "
        "connectors and used to require a core edit to add. Lazy so layer 4 does not depend on the "
        "capability layer at import time for a set only the two validators ever ask for - the same "
        "shape as the core.logging exception above"
    ),
}

_ALLOWED_AT_ANY_SCOPE = _ALLOWED_MODULE_EDGES | set(_ALLOWED_LAZY_EDGES)


def _format_violations(edges: dict[Edge, list[_Import]]) -> str:
    lines = []
    for (src, dst), imports in sorted(edges.items()):
        sites = ", ".join(f"{imp.file.relative_to(_REPO_ROOT)}:{imp.lineno}" for imp in imports)
        lines.append(f"{src} -> {dst} (undeclared): {sites}")
    return "\n".join(lines)


def test_module_scope_imports_are_declared() -> None:
    """Every module-scope cross-package import is a policy edge in `_ALLOWED_MODULE_EDGES`."""
    violations = {
        edge: imports
        for edge, imports in _MODULE_SCOPE_EDGES.items()
        if edge not in _ALLOWED_MODULE_EDGES
    }
    assert not violations, "undeclared module-scope import(s):\n" + _format_violations(violations)


def test_function_scope_imports_are_declared() -> None:
    """Every function-scope import is a module-scope edge, or a declared lazy exception."""
    violations = {
        edge: imports
        for edge, imports in _FUNCTION_SCOPE_EDGES.items()
        if edge not in _ALLOWED_AT_ANY_SCOPE
    }
    assert not violations, "undeclared function-scope import(s):\n" + _format_violations(violations)


def test_type_checking_imports_are_declared() -> None:
    """An annotation-only cross-package import is declared like any other, not exempt.

    Zero such imports exist today — which is what makes stating the rule free, and what made the
    old outright skip dead code that nonetheless documented a way around every check above.
    """
    violations = {
        edge: imports
        for edge, imports in _TYPE_CHECKING_EDGES.items()
        if edge not in _ALLOWED_AT_ANY_SCOPE
    }
    assert not violations, "undeclared TYPE_CHECKING import(s):\n" + _format_violations(violations)


def test_cycle_edges_are_all_still_real() -> None:
    """`_CYCLE_EDGES` documents cycles that exist; a stale entry needs pruning, not just review."""
    stale = [edge for edge in _CYCLE_EDGES if edge not in _MODULE_SCOPE_EDGES]
    assert not stale, f"declared cycle edge(s) no longer observed in the import graph: {stale}"


# ---------------------------------------------------------------------------------------------
# The runtime check: a static walk cannot see a transitive import, so the kernel rule is also
# verified by importing each core module in a clean interpreter, driven by the derived lists.
# ---------------------------------------------------------------------------------------------

_CORE_MODULES = sorted(m for m in _MODULE_NAMES.values() if _package_of(m) == "chemclaw.core")
_CORE_FORBIDDEN_SIBLINGS = sorted(set(_PACKAGES) - {"chemclaw.core"})

_RETRIEVAL_MODULES = sorted(
    m for m in _MODULE_NAMES.values() if _package_of(m) == "chemclaw.retrieval"
)
# Retrieval's rule is narrower than core's: retrieval legitimately depends on core/kg/science, and
# only `agent` is the layer it must never see (the historical agent<->retrieval embedding cycle).
_RETRIEVAL_FORBIDDEN = ["chemclaw.agent"]

_CHECK = """
import importlib
import sys

target = sys.argv[1]
forbidden = sys.argv[2].split(",") if sys.argv[2] else []
importlib.import_module(target)
leaked = {
    f: sorted(name for name in sys.modules if name == f or name.startswith(f + "."))
    for f in forbidden
}
leaked = {f: names for f, names in leaked.items() if names}
if leaked:
    detail = "; ".join(f"{f}: {names}" for f, names in sorted(leaked.items()))
    raise SystemExit(f"{target} transitively imports forbidden sibling(s) - {detail}")
"""


def _assert_no_forbidden_transitive_import(module: str, forbidden: list[str]) -> None:
    """Import `module` fresh; fail if any `forbidden` package leaks into `sys.modules`."""
    result = subprocess.run(
        [sys.executable, "-c", _CHECK, module, ",".join(forbidden)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", _CORE_MODULES)
def test_the_kernel_imports_no_sibling(module: str) -> None:
    """`chemclaw.core` is what everything builds on, so nothing it imports may reach back up.

    One subprocess per core module (derived from disk), each checked against every other top-level
    package at once, `chemclaw.cli` included.
    """
    _assert_no_forbidden_transitive_import(module, _CORE_FORBIDDEN_SIBLINGS)


@pytest.mark.parametrize("module", _RETRIEVAL_MODULES)
def test_retrieval_does_not_import_orchestration(module: str) -> None:
    """A retrieval module in a clean interpreter pulls in nothing from `chemclaw.agent`."""
    _assert_no_forbidden_transitive_import(module, _RETRIEVAL_FORBIDDEN)


def test_the_connector_job_wrapper_imports_no_connector() -> None:
    """`durable/connector_job.py` imports nothing from any connector.

    A child job is addressed by workflow type name and task queue, so core needs no knowledge of any
    bundle. The package-granular policy cannot express this, because `durable -> connectors` is
    allowed for `template_activities`. Read from the AST, so only what the file declares counts.
    """
    source = (_SRC_ROOT / "durable" / "connector_job.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reaches = sorted(
        {
            name
            for node in ast.walk(tree)
            for name in (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if name.startswith("chemclaw.connectors")
        }
    )
    assert not reaches, (
        f"durable/connector_job.py imports {reaches}; the wrapper is the one module in this "
        "package that must name no connector, because that is what lets a bundle own its workflow"
    )


# The libraries that arrive *only* through a bundle, so seeing one in a process is proof a bundle
# loaded even when the module names do not say so. Same roots as
# `test_workflow_registry.py::test_cores_workers_import_no_bundle` checks for core's worker.
_BUNDLE_ONLY_DEPENDENCIES = ("bofire", "botorch", "gpytorch", "tblite", "xgboost")

# The agent-side modules that start durable work, plus the front door that hosts them — i.e. every
# way the conversation process comes to hold a workflow launcher.
_AGENT_LAUNCH_SURFACE = (
    "chemclaw.agent.durable_tools",
    "chemclaw.api.app",
)

_BUNDLE_CHECK = """
import importlib
import sys

target, bundles, heavy = sys.argv[1], sys.argv[2].split(","), sys.argv[3].split(",")
importlib.import_module(target)

prefixes = tuple(f"chemclaw.connectors.{b}." for b in bundles)
exact = {f"chemclaw.connectors.{b}" for b in bundles}
loaded_bundles = sorted(n for n in sys.modules if n in exact or n.startswith(prefixes))
loaded_heavy = sorted(n for n in sys.modules if n.split(".")[0] in heavy)
if loaded_bundles or loaded_heavy:
    # Roots and a count, never the full list: one bundle import pulls ~600 `bofire`/`botorch`
    # modules, and a failure a reader has to scroll past is a failure that hides its own cause.
    roots = sorted({n.split(".")[0] for n in loaded_heavy})
    raise SystemExit(
        f"{target} loaded bundle module(s) {loaded_bundles}, "
        f"pulling in {len(loaded_heavy)} modules from bundle-only dependency(ies) {roots}"
    )
"""


@pytest.mark.parametrize("module", _AGENT_LAUNCH_SURFACE)
def test_the_agent_layer_imports_no_bundle_workflow(module: str) -> None:
    """The agent may name a core-queue workflow type, never a bundle's.

    A bundle's workflow is reached by name across its queue, so `bofire`/`botorch`/`tblite` load
    only in the bundle's own worker. The package-granular policy cannot express this because `agent
    -> connectors` is allowed for the generated tool surface. Bundles are derived from the registry,
    and the check runs in a clean interpreter because the offending import is usually transitive.
    """
    from chemclaw.connectors.registry import discovered

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _BUNDLE_CHECK,
            module,
            ",".join(discovered()),
            ",".join(_BUNDLE_ONLY_DEPENDENCIES),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{result.stderr.strip()}\n\na bundle's durable work is launched by *name* across its own "
        "queue precisely so its heavy closure never loads here; import the workflow type only when "
        "this process already carries it"
    )


def test_core_has_one_lazy_exception_and_the_dict_says_which() -> None:
    """Exactly one lazy edge out of `chemclaw.core`, and `_ALLOWED_LAZY_EDGES` says which.

    `core` is imported first by every entrypoint, so each lazy edge out of it is the kernel reaching
    back into a layer above; `ARCHITECTURE.md` states the count as a property of the kernel.
    """
    from_core = sorted(edge for edge in _ALLOWED_LAZY_EDGES if edge[0] == "chemclaw.core")
    assert from_core == [("chemclaw.core", "chemclaw.connectors")], (
        f"the kernel's lazy exceptions are now {from_core}; ARCHITECTURE.md states there is "
        "exactly one, and a second is a decision rather than an entry"
    )
