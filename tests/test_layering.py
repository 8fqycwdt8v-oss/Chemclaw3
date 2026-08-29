"""Package layering: every cross-package import is either an allowed edge or a declared exception.

**Derived, not enumerated.** The old version of this file hand-maintained three module lists (the
core modules, the retrieval modules, and the "siblings" a kernel module must not import) and
parametrized a subprocess check over their product. Those lists drifted from disk — `core` grew
`tracing`, `metrics_bridge` and `worker_http` after the list was last updated — and drift in an
*allow-list* is invisible: a module missing from the list is a module never checked, so the kernel
rule silently stopped covering three files, one of which (`worker_http`) had actually broken it —
it imported the metrics registry from `chemclaw.api` at module scope, back when the registry lived
there. This version AST-walks every `.py` file
under `src/chemclaw` to build the real package→package import graph and checks *that* against a
small, hand-authored policy of which package may depend on which — a policy is unavoidably
declared (that is what a layering rule *is*), but the graph it is checked against no longer is.

**Module scope vs. function scope, and why both are walked.** A static walk sees an import wherever
it is written, so a `def foo(): from chemclaw.x import y` inside a function reads the same as one at
the top of the file unless the walk distinguishes them. The codebase leans on that distinction
deliberately: `core.logging` lazily imports `connectors.registry` *inside* a filter's `__init__`
specifically so `core.logging` — which every entrypoint imports first — does not depend on that
layer *at import time*, while still being able to use it once the process has finished
bootstrapping. A rule that only looked at module scope would bless that pattern implicitly and then
bless an accidental module-scope sibling import identically, because both are "not in the
module-scope list". So this file checks **both scopes**, against **two different policies**:
module-scope edges must be in `_ALLOWED_MODULE_EDGES` (the package dependency graph the four-layer
architecture actually has); function-scope edges may additionally use `_ALLOWED_LAZY_EDGES`, each
entry a documented, deliberate exception.

**`if TYPE_CHECKING:` is a third bucket, not an exemption.** It used to be skipped outright, on the
reasoning that such an import never executes and so creates no runtime edge. That is true and it is
not the whole story: a *skipped* import is an unchecked one, so the guard doubled as a working
escape hatch — `if TYPE_CHECKING: from chemclaw.agent import X` inside `core` would have passed
every test here. The measurement that makes this cheap to close is that the hatch guards **zero**
cross-package imports today, so the skip was dead code documenting a way around the rule. Those
imports are now walked into their own scope and checked against `_ALLOWED_AT_ANY_SCOPE`: an
annotation-only dependency is still a dependency a reader has to reason about, and declaring it
costs one row.

**Six package-level cycles are real, not accidental**, each recorded in `_CYCLE_EDGES` with the
one-line reason a reader needs. Five are "data down, control up" pairs where a registry builds and
launches a durable job and the job's workflow module imports the registry's own manifest/template
types back: `templates↔durable`, `templates↔agent`, `connectors↔durable`, `agent↔durable`,
`agent↔connectors`. The sixth turned up in the walk that no prior note named: `kg↔science` (the
D-080 hazard gate needs `kg.Note` from `science`, and `kg.validate` needs the hazard screen back
from `science`). All six are declared, not hidden, and each direction is checked independently —
the reason for A→B does not excuse B→A.

**Three of the nine cycles this file declared a phase ago were made of one or two imports each**,
and R2 deleted all three by moving the code rather than excusing the edge: `kg.proposal` reached
into `chemclaw.agent` for the turn's ambient actor/session/correlation id, `connectors.server`
reached into `chemclaw.api` for the metrics registry, and a durable workflow reached into
`chemclaw.cli` for a check's implementation. The primitives now live in `chemclaw.core` (which every
package already depends on) and the check in `chemclaw.durable`, so `kg -> agent`,
`connectors -> api` and `durable -> cli` are gone from the graph *and* from the policy — those
imports are now forbidden rather than merely unused. `cli -> durable` survives as an ordinary
downward edge and is declared in `_ALLOWED_MODULE_EDGES` rather than here.

**`chemclaw.cli` is not a special case**, even though it carries no cycle. A previous version of
this file excluded `cli` from the sibling list on the premise that "nothing imports it". That was
false while `cli.schedules` still held the library logic `api.app` and a durable workflow needed at
module scope — a front door and a Temporal workflow reaching into the entrypoint layer for library
functions, which is exactly backwards for a layer that is supposed to be the outermost one. It
moved to `durable/` (R2.B): `chemclaw.durable.schedules` holds the logic, its callers import it
directly (an ordinary same-or-lower-layer edge, no `cli` involved), and `cli.schedules` is left as a
thin `main()` shim that calls back down into `durable` to run — a plain `cli→durable` edge, declared
in the flat set below like every other package `cli` reaches into, not a cycle.

**The kernel rule stays a runtime check, driven by the derived module list.** A static walk cannot
see a *transitive* import — module A importing module B which happens, at runtime, to import
module C — so `chemclaw.core imports no sibling` (the rule this file exists to protect; see
`core/README.md`) is additionally checked by importing each of `chemclaw.core`'s modules (computed
from disk, not listed) in a clean interpreter and asserting no forbidden sibling shows up in
`sys.modules`. The former per-(module, sibling) parametrization spawned 12 × 11 = 132 subprocesses
for this rule alone; one subprocess per module, checking every forbidden sibling in that single
process, needs one per core module plus one per retrieval module. See `--durations=0` for the
wall-clock this bought back.
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
    scope: str  # "module", "function" or "type_checking"


class _ImportVisitor(ast.NodeVisitor):
    """Collect every first-party (`chemclaw.*`) import in one file, tagged by scope.

    `if TYPE_CHECKING:` bodies go into their own scope rather than being discarded, so an
    annotation-only edge is visible and declarable instead of silently exempt. The `orelse` branch
    is what actually runs, so it is walked as ordinary code.
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
# The declared policy: which package may depend on which. This is the one part of this file that
# is necessarily hand-authored — it *is* the layering rule — but it is now a graph over 13
# packages instead of a list of files that has to be kept in step with the filesystem.
# ---------------------------------------------------------------------------------------------

# The six package-level cycles, each direction with the one-line reason it exists. Declaring them
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
    ("chemclaw.cli", "chemclaw.agent"),
    # `cli.leak_probe` builds the *real* front door in its own process — that is the whole point:
    # the leak it measures is in what a turn retains, and an in-process repro that faked the app
    # measured zero. A CLI that drives the service it ships beside is the same shape as
    # `cli.connectors_dev` driving the connector servers, not a layering inversion: nothing in
    # `api` imports `cli`, so the edge stays one-way.
    ("chemclaw.cli", "chemclaw.api"),
    ("chemclaw.cli", "chemclaw.connectors"),
    ("chemclaw.cli", "chemclaw.core"),
    # `cli.schedules` is a thin `main()` shim that calls back down into its durable-layer
    # implementation (`durable.schedules`) — the library logic itself moved out of `cli` (R2.B)
    # because `api.app` needed it at module scope, which no longer makes this a cycle: only `cli`
    # reaches into `durable` now.
    ("chemclaw.cli", "chemclaw.durable"),
    ("chemclaw.cli", "chemclaw.evals"),
    ("chemclaw.cli", "chemclaw.ingest"),
    ("chemclaw.cli", "chemclaw.kg"),
    # `cli/verifier_margin.py` measures the judge's roll-to-roll margin
    # (D-2026-08-27-a-verdict-at-the-margin-is-a-coin-toss), and the judge's input type is
    # `retrieval.evidence.EvidenceChunk` — building the pairs from anything else would measure a
    # different call than the one the turn makes.
    ("chemclaw.cli", "chemclaw.retrieval"),
    # `cli/rekey_campaigns.py` re-keys recorded BO campaigns after a change to how a campaign id is
    # derived (D-2026-08-21). The derivation is `science.bo.campaign_record.campaign_id_for` over an
    # `OptimizationProblem`, so a re-key that did not import it would be a second copy of the rule
    # it exists to apply — which is the one thing a re-key must not have.
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
    ("chemclaw.kg", "chemclaw.core"),
    ("chemclaw.memory", "chemclaw.core"),
    ("chemclaw.memory", "chemclaw.ingest"),
    ("chemclaw.memory", "chemclaw.kg"),
    ("chemclaw.memory", "chemclaw.science"),
    # `operations` reads five of this system's own tables and nothing else. It is a leaf on
    # the kernel by construction: a reading of the record must not be able to reach the
    # capability that wrote it, or the trail would be able to describe itself.
    ("chemclaw.operations", "chemclaw.core"),
    # The result-publication seam (D-2026-08-25). It is a leaf that consumes what the system
    # produced: it reads the kernel and, for its SQL driver, the warehouse connection Protocol that
    # `ingest` already defines — reusing that rather than defining a second `module:callable`
    # driver seam with the same shape and the same credential discipline. Nothing imports back:
    # `publish` is imported *by* `durable` (the drain) and lazily by `science` (the enqueue hook),
    # and imports neither.
    # The outbound delivery seam (F7). A leaf on the kernel, like `publish`: it reads config
    # and the log redaction filter and nothing else. `durable` imports it (the digest job
    # is the caller); it imports nothing back, and nothing reads *from* a channel.
    ("chemclaw.deliver", "chemclaw.core"),
    ("chemclaw.durable", "chemclaw.deliver"),
    # The prescriptive-design layer (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`).
    # A leaf like `publish`, and narrower: it reads the kernel for SMILES arithmetic, ids and the
    # connection pool, and `science.labels.vocabulary` for the *one* species-role vocabulary the
    # precedent questions already use — so "the ligand a precedent used" and "the ligand this design
    # charges" are the same word rather than two enums that agree by accident. It deliberately
    # imports neither `ingest` nor `kg`: a design is prescriptive and their shapes are descriptive,
    # and reusing `OrdReaction.StepKind` or `ProcessConditions` here would have put an instruction
    # and a measurement in one model.
    ("chemclaw.protocols", "chemclaw.core"),
    ("chemclaw.protocols", "chemclaw.science"),
    ("chemclaw.publish", "chemclaw.core"),
    ("chemclaw.publish", "chemclaw.ingest"),
    ("chemclaw.durable", "chemclaw.publish"),
    ("chemclaw.cli", "chemclaw.publish"),
    # `cli/validate_channels.py` is `make channel-validate`, the same shape every other
    # validator entrypoint has: a terminal command that reads one seam's manifests and
    # binds each driver's signature. Nothing in `deliver` imports back.
    ("chemclaw.cli", "chemclaw.deliver"),
    # The `results` bundle's job re-queues stored calculations, and the walk it runs is
    # `publish.backfill`. That module is in the publish layer rather than in `cli/` *because* of
    # this edge: the walk began in the CLI, which made this a connector importing a terminal
    # entrypoint, and the gate caught it. A connector reaching down into publish is ordinary; the
    # inversion was not.
    ("chemclaw.connectors", "chemclaw.publish"),
    ("chemclaw.retrieval", "chemclaw.core"),
    ("chemclaw.retrieval", "chemclaw.kg"),
    ("chemclaw.retrieval", "chemclaw.science"),
    ("chemclaw.science", "chemclaw.core"),
    ("chemclaw.templates", "chemclaw.agent"),
    ("chemclaw.templates", "chemclaw.core"),
    ("chemclaw.templates", "chemclaw.durable"),
} | set(_CYCLE_EDGES)

# Function-scope-only exceptions: a documented, deliberate lazy import of a package that may not be
# imported at module scope. Each is a real edge in `_FUNCTION_SCOPE_EDGES` that is *not* in
# `_ALLOWED_MODULE_EDGES` above - that asymmetry is the point, not a gap.
#
# **Exactly one of these originates in `chemclaw.core`, and that is the measurement R2 exists to
# produce.** `core -> api` was the metrics registry and `core -> agent` was
# `logging.ContextFilter`'s ambient-identity getters; both of those imports now resolve inside
# `chemclaw.core` and register as no edge at all. What remains from core is the connector registry,
# which is a real capability layer rather than a primitive that was merely filed one package too
# high, so it is not a move that would retire this entry. The sentence used to read "there is
# exactly one left" of the whole dict, and survived two additions to it — `test_core_has_one_lazy_
# exception_and_the_dict_says_which` is the same claim in a form that fails when it stops being
# true.
_ALLOWED_LAZY_EDGES: dict[Edge, str] = {
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


def test_the_connector_job_wrapper_imports_no_connector() -> None:
    """`durable/connector_job.py`'s central claim, machine-checked for the one module that makes it.

    The wrapper's docstring says it "imports nothing from any connector" — that is the whole seam:
    a child is addressed by a workflow type name and a task queue, two plain strings, so core needs
    no knowledge of any bundle. The policy above cannot express it, because it is *package*
    granular and `chemclaw.durable → chemclaw.connectors` is legitimately allowed for
    `template_activities`. So the claim was machine-unguarded: importing a bundle's workflow class
    straight into the wrapper would have passed every test in this file. Found by the 2026-08-05
    review.

    Read from the AST rather than by importing, so a module that is merely *reachable* from the
    wrapper at runtime does not count — the claim is about what this file declares.
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
    """The agent may name a *core-queue* workflow type; it may never name a bundle's (D-2026-08-17).

    `agent/durable_tools.py` importing `DevelopmentReportWorkflow` to launch it looks like the
    conversation layer reaching into the durable one, and it is not: D-002 forbids merging the two
    *durability models*, and that module stores nothing — it is the thin adapter D-002 asks for.
    Measured, the two workflow-class imports add **10 modules and zero third-party packages** to
    this process, because the report's closure is what core already carries for `gather_evidence`.
    What the typed reference buys is real: `mypy --strict` rejects a wrong argument through
    `start_workflow(Workflow.run, ...)` and is silent through the by-name form.

    The rule that *does* protect this process is therefore one layer down — a bundle's workflow is
    reached by name across its queue, so `bofire`/`botorch`/`tblite` load in the bundle's own worker
    and nowhere else. That held and nothing asserted it. The policy above cannot: it is *package*
    granular and `chemclaw.agent -> chemclaw.connectors` is legitimately allowed for the generated
    tool surface, so importing `chemclaw.connectors.bo.workflows` into an agent tool would have
    passed every other test in this file — the same hole that produced
    `test_the_connector_job_wrapper_imports_no_connector` on the other side of the same seam.

    Bundles are derived from the registry rather than listed, so one added tomorrow is covered on
    the day it is created. Checked in a clean interpreter, because the failure is *transitive*:
    the import that drags a bundle in is rarely the one that names it.
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
    """`ARCHITECTURE.md` states this as a property of the kernel, so it is asserted as one.

    `chemclaw.core` is imported by every entrypoint first, which is why a lazy edge out of it is a
    measurement worth keeping at exactly one: each is a place where the shared kernel reaches back
    into a layer above it, deferred to call time so the import graph stays acyclic. The other two
    entries in `_ALLOWED_LAZY_EDGES` do not originate in `core` and say nothing about this.

    The comment above the dict claimed "there is exactly one left" of the whole dict, and stayed
    there through two additions — a count in prose, three lines from the data that refutes it, in
    the file whose subject is enforcing a rule rather than asking for it.
    """
    from_core = sorted(edge for edge in _ALLOWED_LAZY_EDGES if edge[0] == "chemclaw.core")
    assert from_core == [("chemclaw.core", "chemclaw.connectors")], (
        f"the kernel's lazy exceptions are now {from_core}; ARCHITECTURE.md states there is "
        "exactly one, and a second is a decision rather than an entry"
    )
