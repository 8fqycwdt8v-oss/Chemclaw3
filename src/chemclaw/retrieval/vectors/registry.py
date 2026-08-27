"""Which vector store this deployment uses — the one place a provider name becomes an object.

**A vector database is a database this system does not own**, so it attaches the way the warehouse
ELN and the result store do: `vector_store_provider` is either one of the names shipped below or a
`module:callable` building anything else, late-bound through `chemclaw.core.connect.resolve_driver`
when the store is first needed (`D-2026-08-26-the-driver-s-signature-is-the-schema`).

That replaced a closed `Literal` and an `if`-chain, which made a fourth vector database — Milvus,
Weaviate, LanceDB, a pgvector server this system does not run — an edit to two files in `core`
before a single line of adapter existed. The seam this repository built for warehouses refuses to
be that, and there was no reason for the dense half to be different in kind.

**Nothing is imported until it is chosen.** Resolution happens at first use, so a `pgvector`
deployment never loads the Qdrant adapter and — more to the point — never needs the client package
it would ask for. That is the same late-binding the data-source seam and the warehouse driver use,
for the same reason.

**What a custom adapter owes.** It implements `chemclaw.retrieval.vectors.base.VectorStore`, takes
no constructor arguments, and reads its own configuration from `settings` (`vector_store_url`,
`vector_store_api_key`, the collection names and the timeout) exactly as the shipped two do — the
address is a deployment fact, not a per-call one, and a store that needed arguments would need a
manifest that nothing here has. A driver that is missing a method fails on its first search with an
`AttributeError` naming it; there is deliberately no `isinstance` gate, because a
`runtime_checkable` Protocol would pass anything with attributes of the right names anyway.
"""

import logging

from chemclaw.core.config import settings
from chemclaw.core.connect import resolve_driver
from chemclaw.retrieval.vectors.base import VectorStore, VectorStoreConfigError

logger = logging.getLogger(__name__)

# The adapters shipped here, by the short name a deployment writes. `core.config.store` accepts
# these names without knowing what they resolve to (`core` imports no sibling);
# `tests/test_vector_store.py` holds the two declarations in step.
SHIPPED: dict[str, str] = {
    "qdrant": "chemclaw.retrieval.vectors.qdrant:QdrantVectorStore",
    "databricks": "chemclaw.retrieval.vectors.databricks:DatabricksVectorStore",
}


# The one live store, and the configuration that built it. A module slot rather than an
# `lru_cache`, for `core.embeddings._openai_client`'s reason read the other way: an evicted entry
# holding a client with a connection pool is a leak nothing can see, so there is exactly one entry
# and replacing it is a deliberate act. `None` means "not built yet".
_STORE: tuple[tuple[str, ...], VectorStore] | None = None


def _configuration() -> tuple[str, ...]:
    """Every setting that decides *which* store this is, as the cache key.

    Named explicitly rather than hashed off the whole `Settings` object: these five are what the
    two shipped adapters read in their constructors and their client factories, so a change to any
    of them must yield a different store and a change to anything else must not. The same shape as
    `core.embeddings._openai_client`'s key, and for the same reason — a test that swaps `Settings`
    gets a fresh store instead of the previous test's.
    """
    return (
        settings.vector_store_provider,
        settings.vector_store_url,
        settings.vector_store_api_key.get_secret_value(),
        settings.vector_store_endpoint_name,
        str(settings.vector_store_timeout_seconds),
    )


def default_vector_store() -> VectorStore:
    """The configured external vector store, built once per process per configuration.

    Only ever called when `vector_store_provider` names something other than `pgvector` —
    `pgvector` is not a `VectorStore` at all but the absence of one, since its vectors live in the
    same statement as the catalogue's join and there is nothing to delegate. Asking this function
    for it is therefore a wiring bug rather than a configuration one, and it says so.

    **Built once, because nothing can dispose one.** No adapter here has a `close`/`aclose` and no
    caller has anywhere to put one: a store is reached from a retrieve half, and the data-source
    seam builds a half per `gather_evidence` call. So this used to construct a fresh
    `AsyncQdrantClient` — with its own httpx pool — or a fresh Databricks client on every tool
    call, each dropped unreferenced with its sockets held until a garbage collection nobody
    schedules. Holding one per configuration is what makes the absent teardown honest, and it is
    also what the two adapters' own docstrings already claim ("One per process; the client pools
    internally").

    Raises:
        VectorStoreConfigError: The provider is `pgvector`, or a reference that does not resolve to
            something callable.
    """
    global _STORE
    provider = settings.vector_store_provider
    if provider == "pgvector":
        raise VectorStoreConfigError(
            "'pgvector' names no external vector store: its embeddings live in the same Postgres "
            "statement that resolves the citation, so there is nothing to delegate. "
            "`default_document_index()` is what chooses between the two"
        )
    configuration = _configuration()
    if _STORE is not None and _STORE[0] == configuration:
        return _STORE[1]
    reference = SHIPPED.get(provider, provider)
    driver = resolve_driver(reference, error=VectorStoreConfigError, what="vector store provider")
    logger.info(
        "vector store: %s at %s (endpoint %s)",
        provider,
        settings.vector_store_url,
        settings.vector_store_endpoint_name or "-",
    )
    store: VectorStore = driver()
    _STORE = (configuration, store)
    return store


def forget_vector_store() -> None:
    """Drop the remembered store, so the next call builds a fresh one.

    For tests, which construct counting or in-memory stores and would otherwise be handed the one
    a previous test built under an identical configuration. Not `close_…`, because there is nothing
    to close — see `default_vector_store` for why that is the point rather than an oversight.
    """
    global _STORE
    _STORE = None
