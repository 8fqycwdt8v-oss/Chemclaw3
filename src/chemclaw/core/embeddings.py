"""The one place an embedding client is built — the embedding provider seam (plan F10-A1).

Mirrors `chemclaw.agent.llm_provider`: `embed_texts` selects how text is turned into a vector from
config
(`settings.embedding_provider`), so pointing Chemclaw at the internal endpoint's `/embeddings` route
versus the offline dev embedder is a single config change, never a code edit at a call site. Only
this module knows how an embedding is produced; retrieval (`chemclaw.retrieval.vector_index`)
consumes the
vectors provider-agnostically. It lives in the shared kernel (not `agent/`) because retrieval
infrastructure depends on it — the dependency must point report → chemclaw, never report → agents.

Two providers:
- `hash` (default): a deterministic, dependency-free **feature-hash** of the text's tokens into a
  fixed-width unit vector. It is offline and reproducible (so tests and a no-credential dev run
  work), and gives *token-overlap* cosine similarity — useful as a stand-in, but NOT neural-semantic
  retrieval; production must use a real model. It is explicitly the dev/CI path.
- `openai_compatible`: the internal OpenAI-compatible endpoint's embeddings API, reached with the
  same base_url/generic credential/private-CA transport as the chat client
  (`chemclaw.agent.llm_provider`).
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Any

from chemclaw.core.config import settings

# Tokenizer for the hash embedder: lowercase alphanumeric runs. Deliberately trivial — the hash
# embedder is a deterministic dev stand-in, not a linguistic model.
_TOKEN = re.compile(r"[a-z0-9]+")


# Recently embedded texts, keyed by (provider, model, dim, text) — see `embed_texts`. A plain
# dict with FIFO eviction rather than `functools.lru_cache`: the API is a *batch*, and memoizing
# the batch would only ever hit on an identical list, which is not what repeats. Bounded, because
# an unbounded map of every text ever embedded is a slow memory leak in a long-lived retrieval
# process.
_CacheKey = tuple[str, str, int, str]
_CACHE: dict[_CacheKey, list[float]] = {}


def _cache_key(text: str) -> _CacheKey:
    """The identity of one embedding: the text *and the configuration that produced it*.

    Keying on the provider, model and dimension is the same lesson the calculation cache learned
    the hard way (D-011): a vector is only reusable for the configuration that made it. Pointing a
    deployment at a different embedding model and serving the old model's vectors would corrupt
    every similarity comparison, silently and unrecoverably.
    """
    return (
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dim,
        text,
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed each text into an `embedding_dim`-length vector (provider selected by config).

    Args:
        texts: The strings to embed (note bodies at index time, a query at search time).

    Returns:
        One vector per input, in order. Vectors are directly comparable by cosine similarity.

    A half-configured `openai_compatible` selection (missing `llm_base_url`/`embedding_model`)
    is rejected at startup by the config validator, so this path can rely on both being set.

    **Repeats are served from memory (STO-12).** Every retrieval embeds its query, and the same
    query recurs constantly — a refined report run, a retried turn, a user asking again. Under the
    real provider each of those was a network round trip on the interactive path. Only the misses
    are sent, so a batch that is half cached costs half a request rather than a whole one. Set
    `embedding_cache_size` to 0 to disable.
    """
    if not texts:
        return []
    size = settings.embedding_cache_size
    if size <= 0:
        return _embed_uncached(texts)

    keys = [_cache_key(text) for text in texts]
    missing = [text for text, key in zip(texts, keys, strict=True) if key not in _CACHE]
    if missing:
        # Deduplicated before the call: a batch naming the same text twice should cost one
        # embedding, not two, whichever provider is behind it.
        unique = list(dict.fromkeys(missing))
        for text, vector in zip(unique, _embed_uncached(unique), strict=True):
            _CACHE[_cache_key(text)] = vector
    # Read the batch out *before* trimming. The trim is a bound on the cache, not on the answer,
    # and it deletes oldest-first from the whole dict — including keys this very call just inserted
    # or is still holding. Trimming first therefore raised `KeyError` on the line below whenever the
    # batch was larger than `embedding_cache_size`, or when it named a cached text old enough to be
    # evicted by its own insert. `reindex_notes` embeds one text per note in a single batch, so the
    # note-index rebuild failed outright — with a bare `KeyError` naming nothing — for any corpus
    # above 2048 notes, and hybrid retrieval depends on that index.
    vectors = [_CACHE[key] for key in keys]
    # FIFO, oldest first. Not LRU: keeping a recency order costs a move per *hit*, on the hot
    # path, to better serve a workload — repeated identical queries — that a FIFO of this size
    # already serves. A cheaper policy that is right for the actual access pattern.
    while len(_CACHE) > size:
        del _CACHE[next(iter(_CACHE))]
    return vectors


def _embed_uncached(texts: list[str]) -> list[list[float]]:
    """Embed `texts` through the configured provider, with no cache in the way."""
    if settings.embedding_provider == "openai_compatible":
        return _openai_compatible_embeddings(texts)
    return [_hash_embedding(text) for text in texts]


def clear_embedding_cache() -> None:
    """Drop every cached vector.

    A config change cannot serve a stale vector — the configuration is *in* the key — so this is
    not a correctness hook. It exists because the cache outlives an individual test: a test that
    counts how many times the provider was called needs to start from empty, or it measures the
    previous test's leftovers. Production never calls it; a config change there is a restart.
    """
    _CACHE.clear()


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
