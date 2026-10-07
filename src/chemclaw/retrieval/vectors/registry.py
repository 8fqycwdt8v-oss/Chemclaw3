"""Which vector store this deployment uses — the one place a provider name becomes an object.

`vector_store_provider` is a shipped short name or a `module:callable`, resolved late through
`chemclaw.core.connect.resolve_driver`, so a new vector database needs no core edit and an unused
adapter's client package is never loaded.

A custom adapter implements `VectorStore`, takes no constructor arguments, and reads its own
configuration from `settings`. There is no `isinstance` gate (a runtime Protocol check proves
little); a missing method fails on first search with an `AttributeError` naming it.
"""

import logging

from chemclaw.core.config import settings
from chemclaw.core.connect import resolve_driver
from chemclaw.retrieval.vectors.base import VectorStore, VectorStoreConfigError

logger = logging.getLogger(__name__)

# The shipped adapters by short name. `core.config.store` accepts these names without importing
# them; `tests/test_vector_store.py` keeps the two lists in step.
SHIPPED: dict[str, str] = {
    "qdrant": "chemclaw.retrieval.vectors.qdrant:QdrantVectorStore",
    "databricks": "chemclaw.retrieval.vectors.databricks:DatabricksVectorStore",
}


# The one live store and the configuration that built it. A single slot rather than an
# `lru_cache`, so a client with a connection pool is never silently evicted. `None` = not built.
_STORE: tuple[tuple[str, ...], VectorStore] | None = None


def _configuration() -> tuple[str, ...]:
    """Every setting that decides *which* store this is, as the cache key.

    Exactly what the shipped adapters read, so a change to any yields a new store and a change to
    anything else does not.
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

    Only called when `vector_store_provider` is not `pgvector` (whose vectors live in `note_index`),
    so asking for `pgvector` is a wiring bug. Built once because no adapter can be closed and
    retrieve halves are built per call; one per configuration keeps client pools from leaking.

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

    For tests that would otherwise get the store a previous test built under identical
    configuration.
    """
    global _STORE
    _STORE = None
