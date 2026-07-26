"""The one place an embedding client is built — the embedding provider seam (plan F10-A1).

Mirrors `agents.llm_provider`: `embed_texts` selects how text is turned into a vector from config
(`settings.embedding_provider`), so pointing Chemclaw at the internal endpoint's `/embeddings` route
versus the offline dev embedder is a single config change, never a code edit at a call site. Only
this module knows how an embedding is produced; retrieval (`report.vector_index`) consumes the
vectors provider-agnostically. It lives in the shared kernel (not `agents/`) because retrieval
infrastructure depends on it — the dependency must point report → chemclaw, never report → agents.

Two providers:
- `hash` (default): a deterministic, dependency-free **feature-hash** of the text's tokens into a
  fixed-width unit vector. It is offline and reproducible (so tests and a no-credential dev run
  work), and gives *token-overlap* cosine similarity — useful as a stand-in, but NOT neural-semantic
  retrieval; production must use a real model. It is explicitly the dev/CI path.
- `openai_compatible`: the internal OpenAI-compatible endpoint's embeddings API, reached with the
  same base_url/generic credential/private-CA transport as the chat client (`agents.llm_provider`).
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Any

from chemclaw.config import settings

# Tokenizer for the hash embedder: lowercase alphanumeric runs. Deliberately trivial — the hash
# embedder is a deterministic dev stand-in, not a linguistic model.
_TOKEN = re.compile(r"[a-z0-9]+")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed each text into an `embedding_dim`-length vector (provider selected by config).

    Args:
        texts: The strings to embed (note bodies at index time, a query at search time).

    Returns:
        One vector per input, in order. Vectors are directly comparable by cosine similarity.

    A half-configured `openai_compatible` selection (missing `llm_base_url`/`embedding_model`)
    is rejected at startup by the config validator, so this path can rely on both being set.
    """
    if not texts:
        return []
    if settings.embedding_provider == "openai_compatible":
        return _openai_compatible_embeddings(texts)
    return [_hash_embedding(text) for text in texts]


def _hash_embedding(text: str) -> list[float]:
    """A deterministic feature-hash embedding of `text` (offline dev path).

    Each token is hashed to a bucket in `[0, embedding_dim)` and a signed count accumulated, then
    the vector is L2-normalized so cosine similarity reduces to normalized token overlap. Empty or
    token-less text yields a zero vector (cosine 0 against everything — no spurious matches).
    """
    dim = settings.embedding_dim
    vector = [0.0] * dim
    for token in _TOKEN.findall(text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        # A sign bit from a second digest byte keeps unrelated tokens from only ever adding, so two
        # texts sharing no tokens are near-orthogonal rather than weakly positively correlated.
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


def _openai_compatible_embeddings(texts: list[str]) -> list[list[float]]:
    """Embed via the internal OpenAI-compatible endpoint (reuses the chat transport config)."""
    client = _openai_client(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_timeout_seconds,
        settings.llm_max_retries,
        settings.llm_tls_ca_bundle,
    )
    response = client.embeddings.create(model=settings.embedding_model, input=texts)
    return [item.embedding for item in response.data]


@lru_cache(maxsize=1)
def _openai_client(
    base_url: str, api_key: str, timeout: float, max_retries: int, ca_bundle: str
) -> Any:
    """One embedding client per transport config, not one per `embed_texts` call.

    Rebuilding the client (and its private-CA httpx transport) on every call would redo TLS setup
    and drop connection keep-alive on the retrieval hot path. Keyed on the transport settings so a
    config change (tests swap `Settings`) yields a fresh client, while a long-lived process reuses
    one. The httpx client pins the internal CA when one is configured, else the system store.
    """
    from openai import OpenAI

    http_client: Any | None = None
    if ca_bundle:
        import httpx

        http_client = httpx.Client(verify=ca_bundle)
    return OpenAI(
        base_url=base_url,
        api_key=api_key or "not-required",
        timeout=timeout,
        max_retries=max_retries,
        http_client=http_client,
    )
