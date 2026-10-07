"""The one place an embedding client is built: the embedding provider seam.

`embed_texts` selects the provider from `settings.embedding_provider`, so switching between the
internal endpoint's `/embeddings` route and the offline dev embedder is a config change. Retrieval
consumes the vectors provider-agnostically. It lives in the shared kernel because retrieval depends
on it.

Providers:
- `hash` (default): a deterministic feature-hash of the text's tokens into a unit vector. Offline
  and reproducible, giving token-overlap similarity only — the dev/CI path, not semantic retrieval.
- `openai_compatible`: the internal OpenAI-compatible embeddings API, reached with the same
  base_url, credential and private-CA transport as the chat client.
"""

import hashlib
import logging
import math
import re
import threading
import time
from functools import lru_cache
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.http import gateway_client_kwargs
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric

log = logging.getLogger(__name__)

# Tokenizer for the hash embedder: lowercase alphanumeric runs. Deliberately trivial — the hash
# embedder is a deterministic dev stand-in, not a linguistic model.
_TOKEN = re.compile(r"[a-z0-9]+")


# Recently embedded texts, keyed by (provider, model, dim, text). A bounded FIFO dict rather than
# `lru_cache`, because the API takes a batch and what repeats is individual texts, not batches.
_CacheKey = tuple[str, str]
_CACHE: dict[_CacheKey, list[float]] = {}
# Guards every read, insert and trim of `_CACHE`. Held only around dict work, never around the
# provider call — see `embed_texts`.
_CACHE_LOCK = threading.Lock()


def embedding_config_key() -> str:
    """Which configuration produces a vector right now: provider, endpoint, model and dimension.

    A vector is only reusable for the configuration that made it; comparing queries against another
    model's vectors corrupts every similarity silently. The in-process cache and the durable
    `document_chunks.embedding_key` / `note_index.embedding_key` columns all key on this, and a
    stored
    vector whose key differs is stale and gets re-embedded.

    Shape: `provider:endpoint:dDIM:model`, every slot filled (`ep-none`, `model-none`), with the
    free-form model last so a colon inside it cannot be read as a separator. The endpoint slot is a
    digest of the URL (trailing slash stripped), because a model name is not globally unique and the
    key is persisted per row, where a verbatim URL could leak userinfo. Only `openai_compatible`
    fills
    it. Changing this format makes every stored row stale and re-embeds every corpus.
    """
    endpoint = (
        _endpoint_slot(settings.llm_base_url)
        if settings.embedding_provider == "openai_compatible"
        else "ep-none"
    )
    model = settings.embedding_model or "model-none"
    return f"{settings.embedding_provider}:{endpoint}:d{settings.embedding_dim}:{model}"


@lru_cache(maxsize=8)
def _endpoint_slot(base_url: str) -> str:
    """This endpoint's slot in the key, logged once per URL so an operator can map a digest back.

    `lru_cache` makes the log line once per URL rather than once per embedded text. Logging the URL
    is
    safe because `SecretRedactingFilter` strips userinfo; persisting it is not, hence the digest.
    """
    digest = f"ep-{stable_hash(base_url.rstrip('/'), chars=12)}"
    log.info(
        "embedding endpoint %s is recorded as %s in every stored embedding_key", base_url, digest
    )
    return digest


def _cache_key(text: str) -> _CacheKey:
    """The identity of one embedding: the text *and the configuration that produced it*."""
    return (embedding_config_key(), text)


def embed_texts(texts: list[str], *, cache: bool = True) -> list[list[float]]:
    """Embed each text into an `embedding_dim`-length vector (provider selected by config).

    Args:
        texts: The strings to embed (note bodies at index time, a query at search time).
        cache: Whether this batch may read and populate the in-memory cache. Bulk indexers pass
            `False` so a reindex does not flush the cached query vectors with texts read once.

    Returns:
        One vector per input, in order, comparable by cosine similarity.

    Repeated texts are served from memory and only misses are sent to the provider. Set
    `embedding_cache_size` to 0 to disable.
    """
    if not texts:
        return []
    size = settings.embedding_cache_size
    if size <= 0 or not cache:
        return _embed_uncached(texts)

    keys = [_cache_key(text) for text in texts]
    # The answer is assembled from values this call holds, never re-read from `_CACHE`: the cache is
    # shared across threads, and another thread's trim may evict a key between insert and read.
    with _CACHE_LOCK:
        holding = {key: _CACHE[key] for key in keys if key in _CACHE}
    missing = [text for text, key in zip(texts, keys, strict=True) if key not in holding]
    if missing:
        # Deduplicated so a repeated text costs one embedding. Outside the lock on purpose: this is
        # a
        # network round trip, and holding the lock would serialise every turn's retrieval.
        unique = list(dict.fromkeys(missing))
        holding.update(
            (_cache_key(text), vector)
            for text, vector in zip(unique, _embed_uncached(unique), strict=True)
        )
    with _CACHE_LOCK:
        _CACHE.update(holding)
        # FIFO, oldest first: LRU would cost a move per hit on the hot path. Evicting a key this
        # call just
        # inserted is harmless, since the caller's vector is in `holding`.
        while len(_CACHE) > size:
            del _CACHE[next(iter(_CACHE))]
    return [holding[key] for key in keys]


def _embed_uncached(texts: list[str]) -> list[list[float]]:
    """Embed `texts` through the configured provider, uncached, and record calls, failures and
    duration.

    Instrumented here so both providers book metrics (the `hash` path lets tests prove them
    offline).
    The unit is one provider call per batch, which is what fails and is retried. A warehouse binding
    with `vector: {embedding: server}` embeds inside its own SQL and never reaches this function.
    """
    started = time.perf_counter()
    try:
        if settings.embedding_provider == "openai_compatible":
            vectors = _openai_compatible_embeddings(texts)
        else:
            vectors = [_hash_embedding(text) for text in texts]
    except Exception as exc:
        # `error`, not `failure`: the series HELP documents the outcomes as "ok / error".
        record_metric(
            lambda m: m.increment("chemclaw_embedding_calls_total", 1, {"outcome": "error"})
        )
        record_metric(
            lambda m: m.observe(
                "chemclaw_embedding_duration_seconds", time.perf_counter() - started
            )
        )
        # WARNING and re-raise: the caller decides what to do; this line names the embedder,
        # exception type,
        # batch size and configuration, which no caller's handler preserves.
        log_event(
            log,
            "embedding.failed",
            "embedding %d text(s) failed with %s: %s",
            len(texts),
            type(exc).__name__,
            exc,
            level=logging.WARNING,
            texts=len(texts),
            provider=settings.embedding_provider,
            config_key=embedding_config_key(),
            error=type(exc).__name__,
        )
        raise
    record_metric(lambda m: m.increment("chemclaw_embedding_calls_total", 1, {"outcome": "ok"}))
    record_metric(
        lambda m: m.observe("chemclaw_embedding_duration_seconds", time.perf_counter() - started)
    )
    return vectors


def clear_embedding_cache() -> None:
    """Drop every cached vector.

    Not a correctness hook (the configuration is in the key); it lets tests that count provider
    calls
    start from an empty cache.
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
        settings.llm_api_key.get_secret_value(),
        settings.llm_timeout_seconds,
        settings.llm_max_retries,
        settings.llm_tls_ca_bundle,
    )
    # Chunked to `embedding_batch_size` per request so a whole-corpus reindex stays within provider
    # batch
    # and token limits.
    vectors: list[list[float]] = []
    step = settings.embedding_batch_size
    for start in range(0, len(texts), step):
        chunk = texts[start : start + step]
        response = client.embeddings.create(model=settings.embedding_model, input=chunk)
        # Paired by `index`, never by position: `data` order is not part of the contract and
        # batching
        # servers reorder. A permutation would give every text its neighbour's vector undetectably.
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(chunk))):
            # Sorting only helps while `index` is a valid permutation; a repeated or missing index
            # is refused,
            # because the resulting corruption is invisible once stored.
            raise ValueError(
                f"the embedding endpoint answered a batch of {len(chunk)} with index values "
                f"{[item.index for item in ordered]}, which are not that batch's own positions; "
                "the response cannot be paired with its inputs and every vector it carries would "
                "be attributed to the wrong text"
            )
        vectors.extend(item.embedding for item in ordered)
    return vectors


@lru_cache(maxsize=1)
def _openai_client(
    base_url: str, api_key: str, timeout: float, max_retries: int, ca_bundle: str
) -> Any:
    """One embedding client per transport config, not one per `embed_texts` call.

    Reuses TLS setup and keep-alive across calls; keyed on the transport settings so a config change
    yields a fresh client.
    """
    import httpx
    from openai import OpenAI

    # CA pinning and ignoring an ambient proxy come from `gateway_client_kwargs`, the same transport
    # the
    # chat client uses. Built unconditionally: passing `None` would let the SDK build a client that
    # follows `HTTPS_PROXY`. `Any` because `openai` types `http_client` against a different httpx
    # major.
    http_client: Any = httpx.Client(**gateway_client_kwargs(ca_bundle))
    return OpenAI(
        base_url=base_url,
        api_key=api_key or "not-required",
        timeout=timeout,
        max_retries=max_retries,
        http_client=http_client,
    )
