"""The second layering policy: which package may import which third-party stack.

`tests/test_layering.py` polices first-party edges only. This file AST-walks every file, buckets
each import by scope, and checks the derived (package, stack) graph against three tables:
`_ALLOWED_MODULE_STACKS` (the package owns that stack), `_ALLOWED_LAZY_STACKS` (function-scope
only) and `_KNOWN_LEAKS` (forbidden but present, keyed by file so a leak cannot grow). Every row
must still be observed, so a stale row cannot re-bless an edge. `_STACKS` is a watch-list: only
roots with layering meaning are mapped, so a new significant dependency must be added there.
A separate ratchet, `_KNOWN_PRIVATE_IMPORTS`, covers private-module imports of any dependency.

Known limits: a composed first-party hop (science → core → temporal), dynamic imports via
`importlib`, attribute access to private names, and package-keyed allowed rows licensing every
file in a layer.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"

# Distribution root -> the stack it *is*. Several roots can name one stack (a web framework is
# `fastapi` + `starlette` + `sse_starlette` + `uvicorn`); the policy is written about the stack.
_STACKS: dict[str, str] = {
    "temporalio": "temporal",
    # Layer 1. The provider wrappers `langchain_openai`/`langchain_anthropic` belong to the `llm`
    # stack instead, so holding the framework row does not license building a model client.
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
    # The client half of SSE, an `httpx.AsyncClient` extension, so it shares the `httpx` stack.
    "httpx_sse": "httpx",
    "openai": "llm",
    "anthropic": "llm",
    # The warehouse driver's client, tracked so its lazy import is a declared exception.
    # `databricks-vectorsearch` is reached via `importlib.import_module`, which no AST walk
    # resolves.
    "databricks": "warehouse",
    # `jwt`: there is one authorization gate, so a token-validating import anywhere else is the
    # worst layering violation this system can have.
    "jwt": "token",
    # Key material. No package imports it today and there is no allowed `crypto` row, so any import
    # outside the identity path fails here.
    "cryptography": "crypto",
    # The xTB engine lives in `Chemclaw3-mcp`; there is no allowed `xtb` row, so any import here
    # fails.
    "tblite": "xtb",
    # The unit registry behind `core/units.py`, with the two roots its distribution pulls in. Only
    # the kernel may hold a unit registry; mapping `flexparser`/`flexcache` keeps a direct reach
    # into pint's internals visible.
    "pint": "units",
    "flexparser": "units",
    "flexcache": "units",
    # The BPE tokenizer, mapped although its import is lazy, since an unmapped root is invisible.
    # Its own stack rather than `llm`: counting tokens is not building a model client.
    "tiktoken": "tokenizer",
    # Only the document-share reader decides what a file on a mounted share is: whether a path is
    # excluded (`pathspec`) and what encoding it has (`charset_normalizer`).
    "pathspec": "share",
    "charset_normalizer": "share",
    # `numpy` stays unmapped because every layer may do arithmetic. `scipy`'s submodules are
    # separate capabilities, but `_STACKS` is keyed by root, so one label; the per-row reasons say
    # which is used.
    "scipy": "scipy",
}

Edge = tuple[str, str]  # (chemclaw package, stack)
Site = tuple[str, str]  # (path relative to the repo root, stack or target module)

# ---------------------------------------------------------------------------------------------
# The declared policy.
# ---------------------------------------------------------------------------------------------

# One row per (package, stack) the architecture states is that package's job.
_ALLOWED_MODULE_STACKS: dict[Edge, str] = {
    # --- `scipy`: one row per package that uses it.
    # ------------------------------------------------
    ("chemclaw.memory", "scipy"): (
        "similarity clustering: one `csr @ csr.T` and one `connected_components` in place of an "
        "O(n^2) Python pairwise loop over the same fingerprints"
    ),
    ("chemclaw.analytical", "scipy"): "stability regression over the analytical series",
    ("chemclaw.science", "scipy"): (
        "the RRHO arithmetic's eigenproblem, in `science/calc/thermo.py`"
    ),
    ("chemclaw.core", "scipy"): (
        "`core/units.py` reads the calorie, the hartree and the electronvolt out of "
        "`scipy.constants` instead of transcribing them — the kernel is the one layer that may "
        "hold a physical constant, for the same reason it is the one that may hold a unit registry"
    ),
    # core: the shared kernel every layer builds on. `core/README.md` names exactly these.
    ("chemclaw.core", "postgres"): "core/db.py is the one connection pool",
    ("chemclaw.core", "httpx"): "core/http.py is the one HTTP client factory",
    ("chemclaw.core", "temporal"): "core/temporal_client.py is the one client-per-process",
    ("chemclaw.core", "http"): "core/asgi.py + core/worker_http.py are the shared ASGI primitives",
    ("chemclaw.core", "rdkit"): "core/chem.py canonicalises SMILES for every layer",
    ("chemclaw.core", "units"): (
        "core/units.py is the one unit registry, and it is in the kernel because every layer "
        "compares quantities against it — `analytical/`, `science/calc/` and `connectors/calc/` "
        "all reconcile through it. It is the only module that may hold one: the registry is built "
        "restricted (`pint.UnitRegistry(None)` plus a declared definition list), and a second one "
        "built anywhere else would be a second registry with a different idea of what a percent is"
    ),
    # `core/mcp_session.py` is the one outbound MCP client session. It lives in the kernel because
    # `ingest/labels/labeller.py` also needs it and `ingest -> connectors` is not an edge. This is
    # the client; `connectors -> mcp` below is the server half.
    ("chemclaw.core", "mcp"): (
        "core/mcp_session.py is the one outbound MCP client session; its second caller is in "
        "ingest/, which may not import connectors/"
    ),
    # `core/turn_signals.py` publishes a turn's out-of-band signals through `get_stream_writer()`.
    # Its recorders are `connectors/` and `templates/`, so placing it in `agent/` would make
    # capability code import layer 1; the kernel owns shared engine primitives like this one.
    ("chemclaw.core", "langgraph"): (
        "core/turn_signals.py publishes a turn's signals on the graph's custom stream; the "
        "recording ends are connectors/ and templates/, so this cannot live in agent/"
    ),
    # agent: layer 1. "LangGraph — conversation orchestration" is the definition of the layer.
    ("chemclaw.agent", "langgraph"): "layer 1 IS LangGraph (D-2026-08-10)",
    ("chemclaw.agent", "postgres"): "durable sessions, preferences and plan approvals (F3)",
    # api: layer 1's front door (F2).
    ("chemclaw.api", "http"): "api/ IS the FastAPI + SSE front door",
    ("chemclaw.api", "postgres"): "routes/ops.py reads readiness straight off the pool",
    ("chemclaw.api", "mcp"): (
        "`api/mcp_face.py` serves core's read-only tools over MCP — this system reachable "
        "as a tool by another agent. The transport is `connectors/server.py`'s, reused "
        "rather than rebuilt, and building the `FastMCP` it wraps is what needs the "
        "import (D-2026-08-29-a-digest-nobody-receives-is-not-delivered)"
    ),
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
    ("chemclaw.api", "httpx"): (
        "api/auth.py fetches the tenant JWKS itself: `_HttpxJwkClient` overrides PyJWT's "
        "`fetch_data`, whose `urllib.request.urlopen` has no `trust_env` and follows an ambient "
        "`HTTPS_PROXY` (measured) — so the key set every bearer token is validated against could "
        "come from the proxy. This row is the `token` row's transport half, not a second HTTP "
        "client for the front door: `core/http.py` still owns the trust store it passes"
    ),
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
    ("chemclaw.science", "ml"): "science/bo is BoFire on BoTorch on torch",
    ("chemclaw.science", "postgres"): "the calculation cache is a table (D-011)",
    # The leaf packages: each owns its own tables and nothing else.
    # publish: the outbound result seam. A database client and an HTTP client are its shipped
    # drivers, and `postgres` is also its local outbox.
    ("chemclaw.publish", "postgres"): (
        "the outbox is a table, and the shipped SQL driver reaches a Postgres results store"
    ),
    ("chemclaw.publish", "httpx"): "the shipped HTTP driver POSTs records to a results service",
    ("chemclaw.protocols", "postgres"): (
        "a design and its append-only revision history are two tables (migration 073)"
    ),
    ("chemclaw.exhibits", "postgres"): (
        "an artefact and its append-only revision history are two tables (migration 115), and the "
        "unchecked-figure scan reads the session's stored tool results"
    ),
    ("chemclaw.ingest", "share"): (
        "the mounted share reader is the one place that decides what a path on a share is and "
        "what encoding its bytes are in — `crawl.py` and `binding.py` for the exclusion patterns, "
        "`parse.py` for the decode. Both were module-scope imports the walk could not see until "
        "their roots were mapped, which is why this row arrives after the code did"
    ),
    ("chemclaw.ingest", "postgres"): "the document chunk index",
    ("chemclaw.ingest", "rdkit"): "an ELN row's structure is canonicalised on the way in",
    ("chemclaw.memory", "postgres"): "the memory layers are tables",
    ("chemclaw.deliver", "httpx"): (
        "the webhook channel POSTs a message to one URL. The only outbound HTTP in this "
        "seam, and the reason the channel is opt-in and owes the chart an egress rule; "
        "the `share` channel writes to a mounted directory and holds no client"
    ),
    ("chemclaw.operations", "postgres"): (
        "the operational read model is five SELECTs over this system's own tables"
    ),
    ("chemclaw.retrieval", "postgres"): "the vector index is pgvector",
    ("chemclaw.evals", "httpx"): "the live probe drives the real front door over HTTP",
    ("chemclaw.evals", "langgraph"): (
        "the live-probe judge is a model call like any other, so it composes its prompt out of "
        "`langchain_core` messages and reads the reply's `finish_reason` off `response_metadata` — "
        "the truncation signal that separates `ungraded` from a fabricated `unserved`. It held the "
        "`llm` row instead until the collapse to one gateway, because it built an `anthropic` "
        "client itself; the model now comes from `agent/llm_provider`, which is the only module "
        "that may name a provider client class"
    ),
    ("chemclaw.evals", "temporal"): "the live probe polls real durable jobs",
    # cli: the outermost layer — every entrypoint, so it may reach anything below it.
    ("chemclaw.cli", "http"): "connectors_dev and mock_llm serve real ASGI apps",
    ("chemclaw.cli", "httpx"): "the live-storm driver talks to the front door",
    ("chemclaw.cli", "temporal"): "live_jobs and live_storm poll real workflows",
    ("chemclaw.cli", "langgraph"): (
        "trajectory_census reads stored transcripts, whose rows decode to LangChain messages — "
        "the tool-call sequences it counts live on `AIMessage.tool_calls`, and a census that "
        "re-modelled them would count a copy of the record rather than the record"
    ),
}

# Function-scope-only exceptions: a stack this package must not depend on at *import* time. The
# asymmetry with the dict above is the point — each row is a deliberate lazy import.
_ALLOWED_LAZY_STACKS: dict[Edge, str] = {
    # The kernel names no conversation framework at any scope; telemetry bootstraps the OTel SDK
    # directly.
    ("chemclaw.agent", "tokenizer"): (
        "agent/context_budget.py resolves the encoding inside `_encoding()`, so a deployment "
        "with no baked merge table never imports it at all — the fallback to the chars/4 "
        "estimator is the shipped path until `TIKTOKEN_CACHE_DIR` names a cache"
    ),
    ("chemclaw.core", "llm"): (
        "core/embeddings builds the OpenAI-compatible client inside `_openai_client`, same reason"
    ),
    ("chemclaw.agent", "llm"): (
        "agent/llm_provider picks the `langchain_openai` or `langchain_anthropic` wrapper at "
        "runtime; they carry the `llm` label rather than the framework's on purpose, so holding "
        "the `langgraph` row does not also license building a model client"
    ),
    ("chemclaw.agent", "httpx"): (
        "agent/llm_provider builds the CA-pinned client inside `_tls_http_clients`, so only a "
        "private-CA internal endpoint pays for it. This row used to be module-scope and to say "
        "'the workload-identity and OBO token exchanges are HTTP'; both exchanges were deleted "
        "unused, and what is left of agent's HTTP is one lazy client factory"
    ),
    ("chemclaw.cli", "llm"): "cli/mock_llm mirrors the provider's own response types on demand",
    ("chemclaw.ingest", "warehouse"): (
        "both warehouse drivers are imported inside their own connect call, so a deployment that "
        "binds no warehouse never needs either installed (D-2026-08-04: the schema is a file, not "
        "an import)"
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
        "different ones, and an earlier attempt to unify them had to be reverted. The *wait* half "
        "of that helper now exists — `durable/awaiting.open_wait`, which is why "
        "`agent/pending_tools.py` no longer has a row here and why the runner's review escalation "
        "never needed one — and it carries a third policy again rather than this one's, since a "
        "wait that expires *completes* and only `ALLOW_DUPLICATE` leaves a lapsed question "
        "askable. Tracked in BACKLOG.md; until the job half exists this edge is debt, not design"
    ),
    ("src/chemclaw/templates/registry.py", "temporal"): (
        "the last copy of the launch idiom. `templates/` is core's own sequencer, so starting a "
        "`TemplateWorkflow` is legitimate work — reaching for `temporalio` to do it is what is not"
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

# Imports of a private module of any dependency. Dependencies are floor-pinned with no upper bound,
# so a patch release moving such a symbol is an ImportError at process start. Applies to every
# non-first-party root, not just `_STACKS`. Keyed by (file, target).
_KNOWN_PRIVATE_IMPORTS: dict[Site, str] = {
    # Empty on purpose: a private import that gains a public home or goes away loses its row.
    (
        "src/chemclaw/agent/turn_usage.py",
        "langchain_openai.chat_models.base._create_usage_metadata",
    ): (
        "**The alternative to this row is a second parser of a shape upstream owns.** A judge "
        "reply that fails validation raises inside the SDK before `_agenerate` returns, so "
        "`on_llm_end` "
        "never fires and the call books nothing — measured, 1,100 tokens booked of a served 6,600, "
        "on the verifier's own documented degrade path. The raw HTTP body arrives on "
        "`on_llm_error`, and this is the function that turns a provider's usage block into the "
        "shape the rest of this system counts in. Re-implementing it here would put a copy of "
        "upstream's normalisation — including the cache-token detail keys, which a `service_tier` "
        "response prefixes — in a module that would then drift silently. Declared instead, and "
        "*asserted*: `tests/test_upstream_surface.py` drives the real function so a rename or a "
        "changed output turns red there, which is this repository's answer to a coupling upstream "
        "never promised."
    ),
    ("src/chemclaw/evals/live.py", "httpx_sse._decoders"): (
        "**The public surface here is the defect, not the parser.** `httpx_sse` exports the "
        "*driver* — `aconnect_sse` and `EventSource.aiter_sse` — and keeps the line-to-event state "
        "machine (`SSEDecoder`, `SSELineDecoder`) private. That driver drops the final event of "
        "any stream that ends without a trailing blank line, because `SSEDecoder` emits only when "
        "it is handed an empty line and `_aiter_sse_lines` flushes the *line* buffer and never the "
        "*event* one: measured on a two-frame stream whose second `data:` line has no blank line "
        "behind it, one event where the three "
        "hand-written readers it replaced yielded two. `cli/live_storm` is a chaos harness whose "
        "whole subject is turns cut off mid-stream, so that is the frame nearest every fault it is "
        "run to observe. It also raises `SSEError` from inside the iterator on a 200 that is not a "
        "stream, which ends a whole `cli/live_benchmark` run rather than costing it one question. "
        "The alternative to this row is a fourth hand-written reader of the SSE grammar — which is "
        "exactly what adopting this dependency deleted, and what the multi-line `data:`, the "
        "comment, the `id:`/`retry:` and the CRLF cases in `tests/test_live_probes.py` each cost "
        "when a harness remembers the format instead of parsing it. So the grammar stays "
        "upstream's and only the end of the stream is ours. A rename breaks `chemclaw.evals.live` "
        "at import, "
        "which is process start of the probe runner, the storm and the benchmark — loud, and "
        "none of them is a serving path. Asserted rather than believed: "
        '`tests/test_upstream_surface.py` drives both classes, including the `decode("")` flush '
        "this repository leans on."
    ),
}


# ---------------------------------------------------------------------------------------------
# The walk. Same scope rules as tests/test_layering.py, with one difference: `if TYPE_CHECKING:`
# is its own bucket rather than being discarded, so an annotation-only stack import is visible.
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
        """Keep an import if it carries a layer edge, or if it reaches into any dependency's
        internals.

        The stack policy concerns only `_STACKS` roots; the private-import ratchet concerns every
        dependency. An unstacked private import is kept with `stack=""`, which `_edges` skips.
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
        """Record the module, or, when private names are pulled from it, each of those.

        `from langgraph import _internal` reaches private API just as `from langgraph._internal
        import x` does. The target is spelled `<module>.<name>`; the `(package, stack)` edge is
        unchanged.
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

    Imports with no stack are present only for the private-import ratchet and are dropped here.
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


# Flags that would put the dev group back into the image, whatever else the command says. `--no-dev`
# is not a promise a later flag cannot take back: uv resolves these together, so `--no-dev
# --all-groups` installs the group this test exists to keep out.
_DEV_REINSTATING_FLAGS = ("--all-groups", "--dev", "--group dev", "--only-dev")


def _image_install_commands() -> list[str]:
    """Every `uv sync` in the Containerfile, each on one line with its continuations joined."""
    text = (_REPO_ROOT / "deploy" / "Containerfile").read_text(encoding="utf-8")
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [line.strip() for line in joined.splitlines() if "uv sync" in line]


def test_the_xtb_engine_is_not_in_the_runtime_closure() -> None:
    """The xTB engine is not in the runtime closure, in the manifest or in the image.

    `tblite` stays in the dev group for `tests/test_solvents.py`. A dev-group dependency stays out
    of pods only if the install command excludes it, so `deploy/Containerfile`'s `--no-dev` is
    asserted too, and a later `--all-groups` on that command counts as undoing it.
    """
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = pyproject["project"]["dependencies"]
    dev = pyproject["dependency-groups"]["dev"]

    assert not [spec for spec in runtime if spec.startswith("tblite")], (
        "tblite is a runtime dependency again. No module in src/ may import it (see the row "
        "above), so declaring it here ships a compiled engine to every pod for nothing."
    )
    assert [spec for spec in dev if spec.startswith("tblite")], (
        "tblite left the dev group too — tests/test_solvents.py derives ALPB_SOLVENTS from the "
        "installed copy and will fail without it. Keep it here, not in the runtime closure."
    )

    installs = _image_install_commands()
    assert installs, (
        "deploy/Containerfile no longer installs with `uv sync` — this check is reading for a "
        "command that is gone, so it is proving nothing about what the image contains."
    )
    for command in installs:
        assert "--no-dev" in command, (
            f"the image installs the dev group: `{command}`. The dev group is where tblite lives, "
            "so without `--no-dev` every pod gets the compiled engine back and the assertions "
            "above stop meaning anything about what ships."
        )
        reinstated = [flag for flag in _DEV_REINSTATING_FLAGS if flag in command]
        assert not reinstated, (
            f"`{command}` carries {reinstated} beside `--no-dev`, which puts the dev group back "
            "into the image the flag was there to keep it out of."
        )
