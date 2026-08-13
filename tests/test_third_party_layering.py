"""The second layering policy: which package may import which *third-party stack*.

`tests/test_layering.py` derives the real package→package import graph and checks it against a
declared policy — but it records an import only `if target.startswith("chemclaw")`
(`_ImportVisitor._record`). So it enforces the *first-party* half of the layering rules and none of
the third-party half, which is the half every architecture document actually writes down:

- `CLAUDE.md`: "Durability lives **only** in Temporal, never in the conversation layer's own
  ad-hoc stores."
- `CLAUDE.md`: "merging them would put Temporal imports inside the physics" (`science/` vs bundle).
- `science/README.md`: "None of these import Temporal, MCP, FastAPI or `chemclaw.agent` … and
  `tests/test_layering.py` keeps it that way."

Measured, only the last clause of that last sentence was true: `science/` *is* clean, but
`import temporalio` in `science/`, `import langgraph` in `durable/` and `import fastapi` in
`kg/` all passed every test in this repo. This file is the missing half, in the same shape as the
first-party one: AST-walk every file, bucket each import by scope, check the derived graph against
a small hand-authored policy.

**Three dictionaries for the stack policy, deliberately not one** (a fourth,
`_KNOWN_PRIVATE_IMPORTS`, answers a different question further down). An allow-list that mixes
"this stack *is* that layer's job" with "this is a violation nobody has fixed yet" is how a policy
stops meaning anything, so the two are separate and named for what they are:

- `_ALLOWED_MODULE_STACKS` — the package owns that stack; the row carries the sentence that says so.
- `_ALLOWED_LAZY_STACKS` — the package may touch it only inside a function, deliberately.
- `_KNOWN_LEAKS` — the architecture forbids it, it exists anyway, and the row names the reason it
  is not fixed here. Keyed by **file**, not by package, so a third module joining an existing leak
  fails: blessing `chemclaw.agent → temporal` wholesale would have made the leak's own growth
  invisible, which is the failure mode this file exists to prevent.

**Pinned in both directions.** Every declared row must still be observed in the tree. Without that
the policy is a snapshot: delete the leaking import and the row sits there re-blessing it for the
next author who reaches for it. `test_layering.py` does the same for its `_CYCLE_EDGES`.

**`_STACKS` is a named watch-list, not a dependency scan.** Only roots that carry a layering
meaning are mapped; `pydantic`, `numpy`, `yaml`, `networkx`, `pypdf` create no layer edge and are
absent on purpose. That is a real limit — a stack nobody named cannot be policed — so a new
architecturally-significant dependency belongs in `_STACKS` at the same time it enters the lockfile.

**That limit bounds the stack policy and nothing else.** It used to bound the private-import
ratchet too, because both questions were asked through the same early return, and the effect was
that a rule whose whole subject is "an unbounded dependency moves a private name" saw eight
distributions. Measured: `import pydantic._internal._model_construction` in `kg/graph.py` passed
this file, all eight tests. The two questions are separate now — a layer edge needs a named stack,
reaching into a dependency's internals does not — and the ratchet covers every root that is not
first-party. It flags nothing today: measured over `src/`, all 31 underscore-prefixed imports name
`chemclaw` itself, which is a different question (`tests/test_layering.py`'s) and not this one's.

**The other limits, each measured rather than supposed.** A review enumerated what this policy
provably cannot see, and every item below is written down because a limit nobody states reads as
coverage:

- **A composed hop.** `from chemclaw.core.temporal_client import Client` in `science/` passes: it
  is a first-party import, so this file never sees it, and `test_layering.py` sees a declared
  `science → core` edge. `core → temporal` is separately declared. Neither policy composes hops,
  so a science module can obtain a live Temporal `Client` through two individually-legal steps.
  This is the sharpest hole and it is **not** closed here — closing it means a re-export policy
  ("which first-party symbols carry a stack with them"), which is a design decision, not a walk.
  Tracked in `BACKLOG.md`.
- **A dynamic import**, which defeats both rules for one reason. `importlib.import_module(
  "temporalio")` in `science/` passes the stack policy and `importlib.import_module(
  "pydantic._internal")` passes the private ratchet: an AST walk cannot resolve a string, and a
  rule banning `importlib` outright would be a different rule. This is the residual the ratchet
  keeps after it stopped being limited to named roots.
- **Attribute access, on either rule.** `sys._getframe`, or `pydantic._internal` reached as an
  attribute of an already-imported `pydantic`, is not an `import` statement and is not seen. The
  rule is about what a module *imports*, which is what breaks at process start.
- **An aliased clock of a root** — anything that reaches a stack without naming its distribution
  root in an `import` statement.
- **Package-keyed allowed rows against file-keyed leaks.** `_KNOWN_LEAKS` is keyed by file so a
  leak cannot grow quietly, and `_ALLOWED_MODULE_STACKS` is keyed by package on purpose: an
  allowed edge is a *design decision about a layer* ("layer 1 IS LangGraph"), where a leak is *debt
  about a file*. The cost is real and worth naming: `("chemclaw.connectors", "langgraph")` says
  "connectors/transport.py builds the tool objects — the one adapter point", and that sentence is
  true of one file out of 54 while the row licenses all of them. Narrowing those rows to files
  would mean re-deciding, per file, what is currently one architectural sentence — and would make
  ordinary growth inside a layer that owns a stack fail the build. The row's *reason* is prose
  about intent; the row itself is about the layer.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"

# Distribution root -> the stack it *is*. Several roots can name one stack (a web framework is
# `fastapi` + `starlette` + `sse_starlette` + `uvicorn`); the policy is written about the stack.
_STACKS: dict[str, str] = {
    "temporalio": "temporal",
    # Layer 1 (D-2026-08-10). The `maf` label that stood beside these until M13 is gone with the
    # dependency: `agent-framework-*` is out of `pyproject.toml`, and a root nothing can install is
    # a row this file's own both-directions pinning would fail on anyway.
    #
    # `langchain_openai`/`langchain_anthropic` are deliberately **not** here: they are provider
    # SDK wrappers, so they belong to the `llm` stack beside `openai` and `anthropic`, and giving
    # them the framework's label would let any package holding the framework row build a model
    # client. That is the distinction `agent/llm_provider.py` exists to keep.
    "langchain": "langgraph",
    "langchain_core": "langgraph",
    "langgraph": "langgraph",
    "langchain_mcp_adapters": "langgraph",
    "deepagents": "langgraph",
    "langchain_openai": "llm",
    "langchain_anthropic": "llm",
    "fastapi": "http",
    "starlette": "http",
    "sse_starlette": "http",
    "uvicorn": "http",
    "mcp": "mcp",
    "psycopg": "postgres",
    "psycopg_pool": "postgres",
    "rdkit": "rdkit",
    "bofire": "ml",
    "botorch": "ml",
    "torch": "ml",
    "linear_operator": "ml",
    "httpx": "httpx",
    "openai": "llm",
    "anthropic": "llm",
    "snowflake": "warehouse",
    # Added after a review measured what `_STACKS` was leaving unpoliced. Three roots present in
    # `src/` carry a layering meaning the first version missed; the rest of what it flagged
    # (`pydantic`, `numpy`, `yaml`, `networkx`, `frontmatter`, `openpyxl`, `pypdf`, …) is correctly
    # absent for the reason the module docstring gives.
    #
    # `jwt` is the most consequential of the three. F4's architecture is "one authorization gate,
    # `require_actor` reject-if-absent": a second module that validates a token is the worst
    # layering violation this system can have, and `import jwt` in `science/`, `connectors/` or
    # `kg/` is exactly what that looks like in source.
    "jwt": "token",
    # Key material. One site, `ingest/eln/warehouse/snowflake.py`, deserialising a private key for
    # the warehouse's key-pair auth. (The review that asked for this row placed it in
    # `api/auth.py` alongside `jwt`; measured, it is not there and never was — `api/auth.py`
    # imports `jwt` only. The two are separate concerns and get separate stacks.)
    "cryptography": "crypto",
    # The xTB engine itself: the `science/` half of the `science/` ↔ `connectors/` pair CLAUDE.md
    # says must never merge. `import tblite` inside `connectors/` is the mirror image of the
    # "Temporal imports inside the physics" the same sentence forbids.
    "tblite": "xtb",
}

Edge = tuple[str, str]  # (chemclaw package, stack)
Site = tuple[str, str]  # (path relative to the repo root, stack or target module)

# ---------------------------------------------------------------------------------------------
# The declared policy.
# ---------------------------------------------------------------------------------------------

# One row per (package, stack) the architecture states is that package's job.
_ALLOWED_MODULE_STACKS: dict[Edge, str] = {
    # core: the shared kernel every layer builds on. `core/README.md` names exactly these.
    ("chemclaw.core", "postgres"): "core/db.py is the one connection pool",
    ("chemclaw.core", "httpx"): "core/http.py is the one HTTP client factory",
    ("chemclaw.core", "temporal"): "core/temporal_client.py is the one client-per-process",
    ("chemclaw.core", "http"): "core/asgi.py + core/worker_http.py are the shared ASGI primitives",
    ("chemclaw.core", "rdkit"): "core/chem.py canonicalises SMILES for every layer",
    # `core/turn_signals.py` publishes a turn's out-of-band signals through `get_stream_writer()`.
    #
    # This is a real coupling the contextvar it replaced did not have, and it is declared rather
    # than worked around because the alternative is worse. The recording ends are `connectors/` and
    # `templates/` — a connector job and a template step both announce their launch — so moving the
    # module into `agent/` would make capability code import layer 1, which is the one direction
    # this file exists to prevent. The kernel already owns the other engines' single primitives on
    # everyone's behalf (`core/db.py` the pool, `core/temporal_client.py` the client-per-process);
    # the stream writer is that same kind of thing, and one publish call is the whole of it.
    ("chemclaw.core", "langgraph"): (
        "core/turn_signals.py publishes a turn's signals on the graph's custom stream; the "
        "recording ends are connectors/ and templates/, so this cannot live in agent/"
    ),
    # agent: layer 1. "LangGraph — conversation orchestration" is the definition of the layer.
    ("chemclaw.agent", "langgraph"): "layer 1 IS LangGraph (D-2026-08-10)",
    ("chemclaw.agent", "postgres"): "durable sessions, preferences and plan approvals (F3)",
    ("chemclaw.agent", "httpx"): "the workload-identity and OBO token exchanges are HTTP",
    # api: layer 1's front door (F2).
    ("chemclaw.api", "http"): "api/ IS the FastAPI + SSE front door",
    ("chemclaw.api", "postgres"): "routes/ops.py reads readiness straight off the pool",
    ("chemclaw.api", "token"): (
        "api/auth.py is the one place an inbound bearer token is validated — F4's 'one "
        "authorization gate'. Every other layer receives an already-resolved actor"
    ),
    # durable: layer 2. "Temporal — durable execution" is the definition of the layer.
    ("chemclaw.durable", "temporal"): "layer 2 IS Temporal",
    ("chemclaw.durable", "postgres"): "job records and the retention sweep own their tables",
    ("chemclaw.durable", "langgraph"): (
        "`template_activities` runs a tool or a model turn as a template step, so it builds the "
        "same tool object a chat turn's surface holds and drives the same graph — which is the "
        "point: a template's calls are governed identically to a conversation's (D-168), and that "
        "is only true while both name the same types"
    ),
    # connectors: the capability seam (D-110/D-118). MCP is the protocol, not the capability.
    ("chemclaw.connectors", "temporal"): "a bundle owns its own workflows, activities and worker",
    ("chemclaw.connectors", "mcp"): "MCP is the protocol a connector server speaks",
    ("chemclaw.connectors", "http"): "each bundle's tool server is an ASGI app",
    ("chemclaw.connectors", "httpx"): "the client that calls a bundle carries the turn's identity",
    ("chemclaw.api", "langgraph"): (
        "api/graph_stream.py translates a compiled graph's stream into the turn event contract "
        "(M8, D-2026-08-10) — the front door's half of driving the graph"
    ),
    ("chemclaw.retrieval", "langgraph"): (
        "retrieval/fanout.py sweeps the evidence sources as a `Send` fan-out, one branch per "
        "source (M10, D-2026-08-10) — the graph is an implementation detail of `gather_evidence` "
        "rather than a second orchestration layer"
    ),
    ("chemclaw.connectors", "langgraph"): (
        "the one adapter point (M7, D-2026-08-10): connectors/transport.py holds each connector's "
        "MCP session open for a turn and registry.py turns what it advertises into LangChain tools"
    ),
    ("chemclaw.connectors", "rdkit"): "bundle tools validate and depict structures",
    # science: pure computation. Its README forbids Temporal/MCP/FastAPI and permits the rest.
    ("chemclaw.science", "rdkit"): "the cheminformatics toolkit is the engine",
    ("chemclaw.science", "xtb"): (
        "science/calc/xtb_engine.py IS the GFN2-xTB engine; `connectors/calc` is its durable-job "
        "and MCP wrapper and must never import the physics itself"
    ),
    ("chemclaw.science", "ml"): "science/bo is BoFire on BoTorch on torch",
    ("chemclaw.science", "postgres"): "the calculation cache is a table (D-011)",
    # the leaf packages: each owns its own tables and nothing else.
    ("chemclaw.kg", "postgres"): "the note-proposal store",
    ("chemclaw.ingest", "postgres"): "the document chunk index",
    ("chemclaw.ingest", "rdkit"): "an ELN row's structure is canonicalised on the way in",
    ("chemclaw.ingest", "crypto"): (
        "the Snowflake binding's key-pair auth deserialises a PEM private key; the only place in "
        "the tree that handles key material outside the identity path"
    ),
    ("chemclaw.memory", "postgres"): "the memory layers are tables",
    ("chemclaw.retrieval", "postgres"): "the vector index is pgvector",
    ("chemclaw.evals", "httpx"): "the live probe drives the real front door over HTTP",
    ("chemclaw.evals", "temporal"): "the live probe polls real durable jobs",
    # cli: the outermost layer — every entrypoint, so it may reach anything below it.
    ("chemclaw.cli", "http"): "connectors_dev and mock_llm serve real ASGI apps",
    ("chemclaw.cli", "httpx"): "the live-storm driver talks to the front door",
    ("chemclaw.cli", "temporal"): "live_jobs and live_storm poll real workflows",
}

# Function-scope-only exceptions: a stack this package must not depend on at *import* time. The
# asymmetry with the dict above is the point — each row is a deliberate lazy import.
_ALLOWED_LAZY_STACKS: dict[Edge, str] = {
    # A row for the kernel's one *framework* import was here — `configure_telemetry` calling the
    # conversation framework's OTel bootstrap inside the function. That bootstrap is now written
    # out against the OTel SDK directly, so the kernel names no conversation framework at any
    # scope, and the row went with the import: this file's own rule is that a declared row must
    # still be observed in the tree, or it re-blesses the edge for the next author.
    ("chemclaw.core", "llm"): (
        "core/embeddings builds the OpenAI-compatible client inside `_openai_client`, same reason"
    ),
    ("chemclaw.agent", "llm"): (
        "agent/llm_provider picks the `langchain_openai` or `langchain_anthropic` wrapper at "
        "runtime; they carry the `llm` label rather than the framework's on purpose, so holding "
        "the `langgraph` row does not also license building a model client"
    ),
    ("chemclaw.cli", "llm"): "cli/mock_llm mirrors the provider's own response types on demand",
    ("chemclaw.evals", "llm"): "the judge client is built per run",
    ("chemclaw.ingest", "warehouse"): (
        "the Snowflake driver is imported inside the connect call, so a deployment that binds no "
        "warehouse never needs it installed (D-2026-08-04: the schema is a file, not an import)"
    ),
}

_ALLOWED_AT_ANY_SCOPE = set(_ALLOWED_MODULE_STACKS) | set(_ALLOWED_LAZY_STACKS)

# Edges the architecture forbids that exist today. Keyed by file so the leak cannot grow quietly.
# Each row says why it is still here; none of them is a blessing.
_KNOWN_LEAKS: dict[Site, str] = {
    ("src/chemclaw/agent/durable_tools.py", "temporal"): (
        "CLAUDE.md: durability lives only in Temporal, never in the conversation layer. This "
        "module holds the "
        "workflow id derivation, the `WorkflowIDReusePolicy` and the status mapping for three "
        "durable jobs — durable policy inside layer 1 — and its own docstring's claim that 'no "
        "durable state lives here' is false for exactly that reason. The fix is one `start_job()` "
        "in `durable/`, which D-2026-08-08-an-outage-is-not-a-missing-job showed cannot be a "
        "single shared reuse policy: 'closed with a decision' and 'closed without one' need "
        "different ones, and an earlier attempt to unify them had to be reverted. Tracked in "
        "BACKLOG.md; until the helper exists this edge is debt, not design"
    ),
    ("src/chemclaw/agent/interaction_tools.py", "temporal"): (
        "the same launch idiom, and the copy that has already diverged: `start_approval` omits "
        "the `id_reuse_policy` the other four pass. Same fix, same BACKLOG row"
    ),
    ("src/chemclaw/templates/registry.py", "temporal"): (
        "the fifth copy of the launch idiom. `templates/` is core's own sequencer, so starting a "
        "`TemplateWorkflow` is legitimate work — reaching for `temporalio` to do it is what is not"
    ),
    ("src/chemclaw/agent/challenge.py", "temporal"): (
        "the sixth copy of the launch idiom, and the one that shows the debt is still accruing. "
        "`start_answer_review` opens the hold a challenge panel's upheld objection needs "
        "(D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared) and catches "
        "`WorkflowAlreadyStartedError` to make re-surfacing idempotent — durable policy inside "
        "layer 1, exactly like the four rows above it. It is function-scope rather than "
        "module-scope so the agent layer stays importable without Temporal, which bounds the leak "
        "and does not remove it. Same fix, same BACKLOG row: one `start_job()` in `durable/`"
    ),
    ("src/chemclaw/agent/job_results.py", "temporal"): (
        "the collection half of the same seam, and the row this file's by-file keying was written "
        "to catch: it was added by a parallel lane of the same campaign, after this test was "
        "drafted against three leaks, and a policy keyed by package would have absorbed it in "
        "silence. `WorkflowFailureError` is caught to report a failed job inside the turn "
        "(D-2026-08-08-an-outage-is-not-a-missing-job) — the classification of a durable failure, "
        "in layer 1, for the same reason as the rows above. Same fix, same BACKLOG row"
    ),
}

# Imports of a *private* module of any dependency: `langgraph.prebuilt._internal` is not API, and
# every dependency here is floor-pinned with no upper bound, so a patch release that moves any such
# symbol is an ImportError at process start of both the front door and the worker. The risk is not
# hypothetical — it is what this rule was written from: layer 1's previous framework had already
# moved two symbols out of its package top level, and two chemclaw modules were importing them from
# a private one. Nothing in that argument is about any particular vendor, so the rule is not
# restricted to `_STACKS`'s roots; `pydantic._internal` is the same bet. Keyed by (file, target).
_KNOWN_PRIVATE_IMPORTS: dict[Site, str] = {
    # Empty, and that is the point of keeping it: the two rows that lived here were removed rather
    # than re-blessed, and then the framework they named was removed too. The dict stays because
    # the ratchet above is what deleted those rows — a private import that gains a public home, or
    # goes away, loses its row on the next run — and because an empty allow-list is the only shape
    # that makes "there are none" an assertion rather than an absence.
}


# ---------------------------------------------------------------------------------------------
# The walk. Same scope rules as tests/test_layering.py, with one difference: `if TYPE_CHECKING:`
# is its own bucket rather than being discarded, so an annotation-only stack import is visible
# instead of silently exempt.
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Imp:
    """One third-party import: where it is written, what it names, and at which scope."""

    path: str
    lineno: int
    target: str
    stack: str
    package: str
    scope: str


class _Visitor(ast.NodeVisitor):
    """Collect every import whose distribution root is in `_STACKS`, tagged by scope."""

    def __init__(self, path: Path) -> None:
        self.rel = path.relative_to(_REPO_ROOT).as_posix()
        self.package = ".".join(path.relative_to(_SRC_ROOT.parent).parts[:2])
        self.func_depth = 0
        self.type_checking_depth = 0
        self.imports: list[_Imp] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._descend(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._descend(node)

    def _descend(self, node: ast.AST) -> None:
        self.func_depth += 1
        self.generic_visit(node)
        self.func_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        """Walk `if TYPE_CHECKING:` bodies into their own bucket; `orelse` is ordinary runtime."""
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
        for stmt in node.orelse:
            self.visit(stmt)

    def _record(self, target: str, lineno: int) -> None:
        """Keep an import if it carries a layer edge, or if it reaches into *any* dependency.

        Two questions, deliberately not one gate. The stack policy is about named roots and
        early-returns for anything `_STACKS` gives no layering meaning — that is the watch-list the
        module docstring describes. The private-import ratchet is about a versioning bet nobody
        made on purpose, and that bet is identical whichever distribution is on the other end of
        it: `pydantic._internal`, `networkx.algorithms._x` and `langgraph._internal` all move
        without a major bump. Filtering both questions through `_STACKS` made the ratchet see eight
        roots while its docstring implied it saw the tree; an unstacked private import is kept with
        `stack=""`, which `_edges` skips and no policy row can match.
        """
        parts = target.split(".")
        stack = _STACKS.get(parts[0], "")
        reaches_inside = parts[0] != "chemclaw" and any(p.startswith("_") for p in parts[1:])
        if not stack and not reaches_inside:
            return
        scope = (
            "type_checking"
            if self.type_checking_depth
            else ("function" if self.func_depth else "module")
        )
        self.imports.append(_Imp(self.rel, lineno, target, stack, self.package, scope))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record the module, or — when private names are pulled from it — each of those.

        `from langgraph import _internal` reaches into private API exactly as
        `from langgraph._internal import x` does, and recording only `node.module` made the first
        form invisible to `_private_imports`: measured, it passed the whole file. It is also the
        form a package re-exporting its own internals produces, so it is the likely one. The
        target is spelled `<module>.<name>` so `(file, target)` still identifies the import, and
        the `(package, stack)` edge is unchanged either way — the distribution root is the same.
        """
        if node.level != 0:  # a relative import is first-party by construction
            self.generic_visit(node)
            return
        module = node.module or ""
        private = [alias.name for alias in node.names if alias.name.startswith("_")]
        for name in private:
            self._record(f"{module}.{name}", node.lineno)
        if not private:
            self._record(module, node.lineno)
        self.generic_visit(node)


def _collect() -> list[_Imp]:
    imports: list[_Imp] = []
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        visitor = _Visitor(f)
        visitor.visit(ast.parse(f.read_text(encoding="utf-8"), filename=str(f)))
        imports.extend(visitor.imports)
    return imports


_IMPORTS = _collect()


def _edges(scope: str) -> dict[Edge, list[_Imp]]:
    """The (package, stack) edges observed at one scope, keyed by edge.

    Imports with no stack are dropped rather than keyed as a `(package, "")` edge: they are in
    `_IMPORTS` only because the private-import ratchet asked for them, they carry no layering
    meaning by construction, and an empty-stack edge matches no policy row — so keeping them would
    report one private import twice, once under a rule that has nothing to say about it.
    """
    out: dict[Edge, list[_Imp]] = defaultdict(list)
    for imp in _IMPORTS:
        if imp.scope == scope and imp.stack:
            out[(imp.package, imp.stack)].append(imp)
    return dict(out)


_MODULE_EDGES = _edges("module")
_FUNCTION_EDGES = _edges("function")
_TYPE_CHECKING_EDGES = _edges("type_checking")


def _format(imports: list[_Imp]) -> str:
    """One line per import. An unstacked one names its distribution root, which is what it has."""
    return "\n".join(
        f"  {i.package} -> {i.stack or i.target.split('.')[0]}: {i.path}:{i.lineno} ({i.target})"
        for i in sorted(imports, key=lambda i: (i.package, i.stack, i.path, i.lineno))
    )


def _undeclared(edges: dict[Edge, list[_Imp]], allowed: set[Edge]) -> list[_Imp]:
    """Imports whose (package, stack) edge is not allowed and which are not a known leak."""
    return [
        imp
        for edge, imports in edges.items()
        if edge not in allowed
        for imp in imports
        if (imp.path, imp.stack) not in _KNOWN_LEAKS
    ]


def test_module_scope_third_party_imports_are_declared() -> None:
    """No package imports a stack at module scope that its layer does not own."""
    bad = _undeclared(_MODULE_EDGES, set(_ALLOWED_MODULE_STACKS))
    assert not bad, "undeclared module-scope third-party import(s):\n" + _format(bad)


def test_function_scope_third_party_imports_are_declared() -> None:
    """A lazy import is still an edge: allowed at module scope, or a declared lazy exception."""
    bad = _undeclared(_FUNCTION_EDGES, _ALLOWED_AT_ANY_SCOPE)
    assert not bad, "undeclared function-scope third-party import(s):\n" + _format(bad)


def test_type_checking_third_party_imports_are_declared() -> None:
    """`if TYPE_CHECKING:` is not an escape hatch from the stack policy.

    There are zero such imports today, which is exactly why the rule can be stated now: it costs
    nothing and it closes the hatch before the first author uses it to dodge a row above.
    """
    bad = _undeclared(_TYPE_CHECKING_EDGES, _ALLOWED_AT_ANY_SCOPE)
    assert not bad, "undeclared TYPE_CHECKING third-party import(s):\n" + _format(bad)


def test_no_declared_module_stack_is_stale() -> None:
    """Pinned in both directions: a row that no longer describes the tree must be deleted."""
    stale = sorted(set(_ALLOWED_MODULE_STACKS) - set(_MODULE_EDGES))
    assert not stale, f"declared module-scope stack row(s) no longer observed — delete: {stale}"


def test_no_declared_lazy_stack_is_stale() -> None:
    """A lazy row must be observed at function scope *and* absent from the module-scope policy.

    The second half is what keeps the dict meaningful: once a stack becomes the package's job at
    module scope, a lazy row for it is no longer an exception, just a duplicate.
    """
    unobserved = sorted(set(_ALLOWED_LAZY_STACKS) - set(_FUNCTION_EDGES))
    assert not unobserved, f"lazy row(s) with no function-scope import — delete: {unobserved}"
    redundant = sorted(set(_ALLOWED_LAZY_STACKS) & set(_ALLOWED_MODULE_STACKS))
    assert not redundant, f"lazy row(s) already allowed at module scope — delete: {redundant}"


def test_no_known_leak_is_stale() -> None:
    """A fixed leak loses its row in the same commit, or the row re-blesses the next author."""
    observed = {(imp.path, imp.stack) for imp in _IMPORTS}
    stale = sorted(set(_KNOWN_LEAKS) - observed)
    assert not stale, f"known-leak row(s) no longer in the tree — delete the row too: {stale}"


def _private_imports() -> list[_Imp]:
    """Imports naming an underscore-prefixed submodule of a dependency, whatever the dependency."""
    return [imp for imp in _IMPORTS if any(p.startswith("_") for p in imp.target.split(".")[1:])]


def test_private_third_party_imports_are_declared() -> None:
    """Reaching into a dependency's private module is allowed only where it is written down."""
    bad = [
        imp for imp in _private_imports() if (imp.path, imp.target) not in _KNOWN_PRIVATE_IMPORTS
    ]
    assert not bad, "undeclared private third-party import(s):\n" + _format(bad)


def test_no_declared_private_import_is_stale() -> None:
    """Same ratchet: a private import that gains a public home loses its row."""
    observed = {(imp.path, imp.target) for imp in _private_imports()}
    stale = sorted(set(_KNOWN_PRIVATE_IMPORTS) - observed)
    assert not stale, f"declared private import(s) no longer in the tree — delete: {stale}"
