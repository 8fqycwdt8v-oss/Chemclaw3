"""Package layering: every cross-package import is either an allowed edge or a declared exception.

**Derived, not enumerated.** The old version of this file hand-maintained three module lists (the
core modules, the retrieval modules, and the "siblings" a kernel module must not import) and
parametrized a subprocess check over their product. Those lists drifted from disk — `core` grew
`tracing`, `metrics_bridge` and `worker_http` after the list was last updated — and drift in an
*allow-list* is invisible: a module missing from the list is a module never checked, so the kernel
rule silently stopped covering three files, one of which (`worker_http`) had actually broken it
(`from chemclaw.api.metrics import ...` at module scope). This version AST-walks every `.py` file
under `src/chemclaw` to build the real package→package import graph and checks *that* against a
small, hand-authored policy of which package may depend on which — a policy is unavoidably
declared (that is what a layering rule *is*), but the graph it is checked against no longer is.

**Module scope vs. function scope, and why both are walked.** A static walk sees an import wherever
it is written, so a `def foo(): from chemclaw.x import y` inside a function reads the same as one at
the top of the file unless the walk distinguishes them. The codebase leans on that distinction
deliberately: `core.logging` lazily imports `agent.identity_context` and `connectors.registry`
*inside* two classes' `__init__`/`filter` methods specifically so `core.logging` — which every
entrypoint imports first — does not depend on those layers *at import time*, while still being able
to use them once the process has finished bootstrapping. A rule that only looked at module scope
would bless that pattern implicitly and then bless an accidental module-scope sibling import
identically, because both are "not in the module-scope list". So this file checks **both scopes**,
against **two different policies**: module-scope edges must be in `_ALLOWED_MODULE_EDGES` (the
package dependency graph the four-layer architecture actually has); function-scope edges may
additionally use `_ALLOWED_LAZY_EDGES`, each entry a documented, deliberate exception. An import
guarded by `if TYPE_CHECKING:` is excluded from both graphs — it is never executed and creates no
runtime edge, so it carries no layering risk (it is what `core.metrics_bridge` uses to name
`api.metrics.Metrics` in an annotation without importing it at runtime).

**Nine package-level cycles are real, not accidental**, each recorded in `_CYCLE_EDGES` with the
one-line reason a reader needs. Six are "data down, control up" pairs where a registry builds and
launches a durable job and the job's workflow module imports the registry's own manifest/template
types back: `templates↔durable`, `templates↔agent`, `connectors↔durable`, `agent↔durable`,
`agent↔connectors`, `agent↔kg`. **Three more turned up in the walk that no prior note named**:
`kg↔science` (the D-080 hazard gate needs `kg.Note` from `science`, and `kg.validate` needs the
hazard screen back from `science`), `api↔connectors` (the front door health-checks/discovers the
connector registry; a connector's own HTTP surface reuses the front door's stdlib-only metrics
registry rather than a second implementation), and `cli↔durable` (`cli.schedules` imports every
workflow class to register it; `durable.audit_verify`'s workflow imports `cli.verify_audit_chain`
so the chain check has exactly one implementation, shared by the manual command and the job). All
nine are declared, not hidden, and each direction is checked independently — the reason for A→B
does not excuse B→A.

**`chemclaw.cli` is not a special case.** A previous version of this file excluded `cli` from the
sibling list on the premise that "nothing imports it". That was false: `api.app` imports
`cli.schedules` and `durable.audit_verify` imports `cli.verify_audit_chain` at module scope, both
real edges the walk below finds and the policy declares. `cli` is simply one more package in the
graph.

**The kernel rule stays a runtime check, driven by the derived module list.** A static walk cannot
see a *transitive* import — module A importing module B which happens, at runtime, to import
module C — so `chemclaw.core imports no sibling` (the rule this file exists to protect; see
`core/README.md`) is additionally checked by importing each of `chemclaw.core`'s 15 modules
(computed from disk, not listed) in a clean interpreter and asserting no forbidden sibling shows up
in `sys.modules`. The former per-(module, sibling) parametrization spawned 12 × 11 = 132
subprocesses for this rule alone (138 with the retrieval rule); one subprocess per module, checking
every forbidden sibling in that single process, needs only 15 (+ 6 for retrieval = 21 total). See
`--durations=0` for the wall-clock this bought back.
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
    itself, a plain module resolves relative to its parent, and each extra dot beyond the first
    climbs one more package level.
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
    in_function: bool


class _ImportVisitor(ast.NodeVisitor):
    """Collect every first-party (`chemclaw.*`) import in one file, tagged by scope.

    `if TYPE_CHECKING:` blocks are skipped entirely (not just tagged): an import gated on it never
    runs, so it is not a dependency edge at all, only an annotation.
    """

    def __init__(self, module: str, path: Path) -> None:
        self.module = module
        self.path = path
        self.func_depth = 0
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
        # else: skip the guarded body outright (both branches - `orelse` runs when TYPE_CHECKING
        # is false, i.e. always at runtime, so it is walked like normal code by generic_visit above
        # only in the non-guarded case; here we deliberately visit neither branch's imports as
        # runtime edges for the `if` body, which is the only one that ever held the annotation-only
        # import).

    def _record(self, target: str, lineno: int) -> None:
        if target.startswith("chemclaw"):
            self.imports.append(_Import(self.path, lineno, target, self.func_depth > 0))

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


def _edges(*, in_function: bool) -> dict[Edge, list[_Import]]:
    """Cross-package import edges at the given scope, keyed by (source package, target package)."""
    out: dict[Edge, list[_Import]] = {}
    for imp in _IMPORTS:
        if imp.in_function != in_function:
            continue
        src = _package_of(_MODULE_NAMES[imp.file])
        dst = _package_of(imp.target)
        if src == dst or dst == "chemclaw":
            continue
        out.setdefault((src, dst), []).append(imp)
    return out


_MODULE_SCOPE_EDGES = _edges(in_function=False)
_FUNCTION_SCOPE_EDGES = _edges(in_function=True)

# ---------------------------------------------------------------------------------------------
# The declared policy: which package may depend on which. This is the one part of this file that
# is necessarily hand-authored — it *is* the layering rule — but it is now a graph over 13
# packages instead of a list of files that has to be kept in step with the filesystem.
# ---------------------------------------------------------------------------------------------

# The nine package-level cycles, each direction with the one-line reason it exists. Declaring them
# here (rather than only in the flat set below) is what makes them visible to a reader instead of
# indistinguishable from every other allowed edge.
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
        "the connector-job wrapper launches into a bundle's queue and registry"
    ),
    ("chemclaw.agent", "chemclaw.durable"): ("agent tools start durable jobs (interaction, tools)"),
    ("chemclaw.durable", "chemclaw.agent"): (
        "activities stamp identity and run agent turns using agent's own audit/authz/profile code"
    ),
    ("chemclaw.agent", "chemclaw.connectors"): (
        "the agent's tool surface is generated from the connector registry"
    ),
    ("chemclaw.connectors", "chemclaw.agent"): (
        "connector jobs and identity plumbing authorize against agent's authz/identity context"
    ),
    ("chemclaw.agent", "chemclaw.kg"): ("graph/memory tools read and propose notes via kg"),
    ("chemclaw.kg", "chemclaw.agent"): (
        "a proposal records the actor/session/correlation id of who proposed it"
    ),
    ("chemclaw.kg", "chemclaw.science"): (
        "the D-080 hazard gate (kg.validate) screens a note using science's hazard-screening logic"
    ),
    ("chemclaw.science", "chemclaw.kg"): ("the hazard-note helper parses kg's own Note type"),
    ("chemclaw.api", "chemclaw.connectors"): (
        "the front door health-checks and discovers tools from the connector registry"
    ),
    ("chemclaw.connectors", "chemclaw.api"): (
        "a connector's own HTTP surface reuses the front door's stdlib-only metrics registry"
    ),
    ("chemclaw.cli", "chemclaw.durable"): (
        "cli.schedules imports every workflow class in order to register its schedule"
    ),
    ("chemclaw.durable", "chemclaw.cli"): (
        "the audit-verify workflow shares cli.verify_audit_chain's implementation (DRY)"
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
    ("chemclaw.agent", "chemclaw.kg"),
    ("chemclaw.agent", "chemclaw.memory"),
    ("chemclaw.agent", "chemclaw.retrieval"),
    ("chemclaw.agent", "chemclaw.science"),
    ("chemclaw.agent", "chemclaw.templates"),
    ("chemclaw.api", "chemclaw.agent"),
    ("chemclaw.api", "chemclaw.cli"),
    ("chemclaw.api", "chemclaw.connectors"),
    ("chemclaw.api", "chemclaw.core"),
    ("chemclaw.api", "chemclaw.durable"),
    ("chemclaw.api", "chemclaw.kg"),
    ("chemclaw.cli", "chemclaw.agent"),
    ("chemclaw.cli", "chemclaw.connectors"),
    ("chemclaw.cli", "chemclaw.core"),
    ("chemclaw.cli", "chemclaw.durable"),
    ("chemclaw.cli", "chemclaw.evals"),
    ("chemclaw.cli", "chemclaw.ingest"),
    ("chemclaw.cli", "chemclaw.kg"),
    # `safety-validate` compiles the hazard and genotoxicity tables through the public screen, so
    # a bad SMARTS or a bad row fails at CI rather than on the first live hazard question. Same
    # shape as every other `*-validate` entrypoint reaching its own domain (kg, templates, eln).
    ("chemclaw.cli", "chemclaw.science"),
    ("chemclaw.cli", "chemclaw.templates"),
    ("chemclaw.connectors", "chemclaw.agent"),
    ("chemclaw.connectors", "chemclaw.api"),
    ("chemclaw.connectors", "chemclaw.core"),
    ("chemclaw.connectors", "chemclaw.durable"),
    ("chemclaw.connectors", "chemclaw.kg"),
    ("chemclaw.connectors", "chemclaw.science"),
    ("chemclaw.durable", "chemclaw.agent"),
    ("chemclaw.durable", "chemclaw.cli"),
    ("chemclaw.durable", "chemclaw.connectors"),
    ("chemclaw.durable", "chemclaw.core"),
    ("chemclaw.durable", "chemclaw.evals"),
    ("chemclaw.durable", "chemclaw.ingest"),
    ("chemclaw.durable", "chemclaw.kg"),
    ("chemclaw.durable", "chemclaw.memory"),
    ("chemclaw.durable", "chemclaw.retrieval"),
    ("chemclaw.durable", "chemclaw.science"),
    ("chemclaw.durable", "chemclaw.templates"),
    ("chemclaw.evals", "chemclaw.api"),
    ("chemclaw.evals", "chemclaw.core"),
    ("chemclaw.evals", "chemclaw.kg"),
    ("chemclaw.evals", "chemclaw.retrieval"),
    ("chemclaw.evals", "chemclaw.science"),
    ("chemclaw.ingest", "chemclaw.core"),
    ("chemclaw.ingest", "chemclaw.kg"),
    ("chemclaw.ingest", "chemclaw.retrieval"),
    ("chemclaw.ingest", "chemclaw.science"),
    ("chemclaw.kg", "chemclaw.agent"),
    ("chemclaw.kg", "chemclaw.core"),
    ("chemclaw.kg", "chemclaw.science"),
    ("chemclaw.memory", "chemclaw.core"),
    ("chemclaw.memory", "chemclaw.ingest"),
    ("chemclaw.memory", "chemclaw.kg"),
    ("chemclaw.memory", "chemclaw.science"),
    ("chemclaw.retrieval", "chemclaw.core"),
    ("chemclaw.retrieval", "chemclaw.kg"),
    ("chemclaw.retrieval", "chemclaw.science"),
    ("chemclaw.science", "chemclaw.core"),
    ("chemclaw.science", "chemclaw.kg"),
    ("chemclaw.templates", "chemclaw.agent"),
    ("chemclaw.templates", "chemclaw.core"),
    ("chemclaw.templates", "chemclaw.durable"),
} | set(_CYCLE_EDGES)

# Function-scope-only exceptions: a documented, deliberate lazy import of a package that may not be
# imported at module scope. Each is a real edge in `_FUNCTION_SCOPE_EDGES` that is *not* in
# `_ALLOWED_MODULE_EDGES` above - that asymmetry is the point, not a gap.
_ALLOWED_LAZY_EDGES: dict[Edge, str] = {
    ("chemclaw.core", "chemclaw.agent"): (
        "logging.ContextFilter binds identity getters lazily so core.logging - imported by every "
        "entrypoint first - does not depend on agent at import time (see its docstring)"
    ),
    ("chemclaw.core", "chemclaw.connectors"): (
        "logging's redaction filter resolves connector bearer-token env names lazily for the same "
        "reason: core.logging must not hard-depend on the connector registry at import time"
    ),
    ("chemclaw.core", "chemclaw.api"): (
        "metrics_bridge/worker_http record into the front door's stdlib-only metrics registry from "
        "any process; imported lazily so a process that never touches api still boots without it "
        "(api.metrics moves into core in a later work package, retiring this exception)"
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


def test_cycle_edges_are_all_still_real() -> None:
    """`_CYCLE_EDGES` documents cycles that exist; a stale entry needs pruning, not just review."""
    stale = [edge for edge in _CYCLE_EDGES if edge not in _MODULE_SCOPE_EDGES]
    assert not stale, f"declared cycle edge(s) no longer observed in the import graph: {stale}"


# ---------------------------------------------------------------------------------------------
# The runtime check: a static walk cannot see a transitive import, so `chemclaw.core imports no
# sibling` - the rule this file exists to protect - is also verified by actually importing each
# core module in a clean interpreter. Driven by the derived module/package lists, not a hand list.
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
    """`chemclaw.core` is what everything else builds on, so nothing it imports may reach back up.

    One subprocess per core module (15, derived from disk), each checked against every other
    top-level package at once - including `chemclaw.cli`, which a prior version of this test
    excluded on the false premise that nothing imports it.
    """
    _assert_no_forbidden_transitive_import(module, _CORE_FORBIDDEN_SIBLINGS)


@pytest.mark.parametrize("module", _RETRIEVAL_MODULES)
def test_retrieval_does_not_import_orchestration(module: str) -> None:
    """A retrieval module in a clean interpreter pulls in nothing from `chemclaw.agent`."""
    _assert_no_forbidden_transitive_import(module, _RETRIEVAL_FORBIDDEN)
