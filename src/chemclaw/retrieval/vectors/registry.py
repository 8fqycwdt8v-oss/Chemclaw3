"""Which vector store this deployment uses — the one place a provider name becomes an object.

The shape `chemclaw.core.embeddings` and `chemclaw.agent.llm_provider` already have: a config token
selects an implementation, and only this module knows the mapping, so pointing the corpus at a
different vector database is a config change rather than an edit at a call site.

**Nothing is imported until it is chosen.** Each provider's adapter is imported inside its own
branch, so a `pgvector` deployment never loads the Qdrant adapter and — more to the point — never
needs the client package it would ask for. This is the same late-binding the data-source seam and
the warehouse driver use, for the same reason.
"""

import logging

from chemclaw.core.config import settings
from chemclaw.retrieval.vectors.base import VectorStore, VectorStoreConfigError

logger = logging.getLogger(__name__)


def default_vector_store() -> VectorStore:
    """Build the configured external vector store.

    Only ever called when `vector_store_provider` names something other than `pgvector` —
    `pgvector` is not a `VectorStore` at all but the absence of one, since its vectors live in the
    same statement as the catalogue's join and there is nothing to delegate. Asking this function
    for it is therefore a wiring bug rather than a configuration one, and it says so.

    Raises:
        VectorStoreConfigError: The provider is `pgvector`, or a name no adapter implements.
    """
    provider = settings.vector_store_provider
    if provider == "qdrant":
        from chemclaw.retrieval.vectors.qdrant import QdrantVectorStore

        logger.info("vector store: qdrant at %s", settings.vector_store_url)
        return QdrantVectorStore()
    if provider == "pgvector":
        raise VectorStoreConfigError(
            "'pgvector' names no external vector store: its embeddings live in the same Postgres "
            "statement that resolves the citation, so there is nothing to delegate. "
            "`default_document_index()` is what chooses between the two"
        )
    # Unreachable while the setting is a `Literal`, and kept because that is a *current* property of
    # the config rather than a guarantee about the next provider somebody adds.
    raise VectorStoreConfigError(f"no vector store adapter for provider {provider!r}")
