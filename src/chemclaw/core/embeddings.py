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
import logging
import math
import re
import threading
from functools import lru_cache
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash

log = logging.getLogger(__name__)

# Tokenizer for the hash embedder: lowercase alphanumeric runs. Deliberately trivial — the hash
# embedder is a deterministic dev stand-in, not a linguistic model.
_TOKEN = re.compile(r"[a-z0-9]+")


# Recently embedded texts, keyed by (provider, model, dim, text) — see `embed_texts`. A plain
# dict with FIFO eviction rather than `functools.lru_cache`: the API is a *batch*, and memoizing
# the batch would only ever hit on an identical list, which is not what repeats. Bounded, because
# an unbounded map of every text ever embedded is a slow memory leak in a long-lived retrieval
# process.
_CacheKey = tuple[str, str]
_CACHE: dict[_CacheKey, list[float]] = {}
# Guards every read, insert and trim of `_CACHE`. Held only around dict work, never around the
# provider call — see `embed_texts`.
_CACHE_LOCK = threading.Lock()


def embedding_config_key() -> str:
    """Which configuration produces a vector right now: provider, endpoint, model and dimension.

    The identity half of an embedding — the same lesson the calculation cache learned the hard way
    (D-011): **a vector is only reusable for the configuration that made it.** Pointing a
    deployment at a different embedding model and then comparing its queries against the old
    model's vectors corrupts every similarity, silently, and no error is ever raised.

    Public because the rule has to hold *durably*, not only in memory. The in-process cache below
    keys on it, and so do `document_chunks.embedding_key` (038) and `note_index.embedding_key`
    (039) — a stored vector whose key is not this one is stale and gets re-embedded
    (`chemclaw.ingest.documents.sync.reembed_stale`, `chemclaw.retrieval.vector_index`). One
    definition, because two spellings of "which model made this" is exactly how the memory cache
    and the tables come to disagree about the same vector.

    **The endpoint is part of the identity, and a model name is not enough on its own.** A model
    name is not globally unique — `text-embedding-3-large` is what the vendor calls it and what any
    gateway proxying it calls it too, and those need not be the same weights. Without the endpoint
    in the key, repointing `llm_base_url` at another vendor serving that name left every stored key
    reading as current, which is precisely the silent-corruption case the column exists to prevent.
    Only `openai_compatible` is affected: the `hash` embedder never reaches the endpoint, so naming
    it there would churn every dev vector on a setting that provably cannot change one. The slot
    stays in the key — filled with `ep-none` rather than left empty — so the key has one shape for
    every provider *and* that shape says what it means. A trailing slash is stripped because
    `.../v1` and `.../v1/` address the same endpoint, and a
    corpus-wide re-embed is too expensive to trigger on a spelling.

    **The endpoint is *identified*, not reproduced.** The slot holds a digest of the URL rather than
    the URL, because this key is written into `document_chunks.embedding_key` and
    `note_index.embedding_key` — one copy per row, in tables nothing prunes and the runtime role can
    read. `llm_base_url` is a plain `str` with no validator forbidding userinfo, so
    `https://svc:token@chemclaw-llm/v1` is a configuration this deployment accepts and the verbatim
    form persisted the password; even without one, an internal hostname does not belong in every row
    of a corpus. A digest keeps the only property the key needs — two endpoints differ, one endpoint
    does not — and the readable part (provider, model, dimension) is what an operator reads a key
    for anyway. Twelve hex characters, because the population being distinguished is the handful of
    endpoints one deployment has ever pointed at, not an adversarially chosen set. `_endpoint_slot`
    logs the URL→digest mapping once, which is the only place an operator can read one back.

    **Every slot is filled, and the one free-form slot is last.** The shape is
    `provider:endpoint:dDIM:model` — `hash:ep-none:d1536:model-none` on a default deployment,
    `openai_compatible:ep-b7be08f1d976:d1536:text-embedding-3-large` on a real one. The previous
    order rendered the default as `hash:::1536`: two empty slots, not one, since `embedding_model`
    also defaults to `""`, and a key an operator reads out of two durable columns must not look
    truncated. The dimension moved ahead of the model and is prefixed `d` for the same reason the
    model went last — a model name is free-form and the separator is `:`, so
    `nomic-embed-text:v1.5` (ordinary Ollama/vLLM naming) produced `…:nomic-embed-text:v1.5:1536`,
    a key with five fields and no way to tell which. With the free-form field last, a colon inside
    it can never be mistaken for a separator.

    **Changing this format re-embeds every corpus**, which is exactly why it changed now: the key is
    compared for equality, so a new shape makes every stored row stale and `reembed_stale` /
    `reindex_notes` rebuild it — the self-healing 038/039 were added for. Today that is a dev
    database. After the first real deployment it is a bill, so the shape had to be settled before
    one exists, not improved afterwards.
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
    """This endpoint's slot in the key, logged once per URL so an operator can read one back.

    The digest is deliberately irreversible (see `embedding_config_key`), which leaves an operator
    holding `ep-b7be08f1d976` out of a database column unable to answer "which endpoint is this?"
    without recomputing it by hand. One INFO line at first use is the whole fix: the mapping exists
    somewhere bounded and operator-facing instead of nowhere.

    `lru_cache` is what makes "once" true — the key is computed per text on the embedding path, so
    an unmemoized log would be one line per embedded chunk. It also stops re-hashing the same URL
    on every call. The URL itself is safe to log because `SecretRedactingFilter` strips userinfo
    from every record that reaches a handler; it is *not* safe to persist, which is why the column
    gets the digest.
    """
    digest = f"ep-{stable_hash(base_url.rstrip('/'), chars=12)}"
    log.info(
        "embedding endpoint %s is recorded as %s in every stored embedding_key", base_url, digest
    )
    return digest


def _cache_key(text: str) -> _CacheKey:
    """The identity of one embedding: the text *and the configuration that produced it*."""
    return (embedding_config_key(), text)


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
    # **The answer is assembled from values this call holds, never re-read from `_CACHE`.**
    # `_CACHE` is a plain dict reached from several threads — `asyncio.to_thread` puts every
    # concurrent turn's retrieval on the default executor — and the earlier shape read the batch
    # back out of it after inserting. Two races followed, both reproduced at the shipped
    # `embedding_cache_size` of 2048: another thread's trim could evict a key between the insert and
    # the read (bare `KeyError`, naming nothing, on the interactive retrieval path), and two trims
    # running together could `del` the same oldest key or mutate the dict mid-iteration
    # (`RuntimeError: dictionary changed size during iteration`). Taking a snapshot under the lock
    # and answering from it removes the class rather than narrowing the window: what the cache holds
    # by the time we return can no longer affect what we return.
    with _CACHE_LOCK:
        holding = {key: _CACHE[key] for key in keys if key in _CACHE}
    missing = [text for text, key in zip(texts, keys, strict=True) if key not in holding]
    if missing:
        # Deduplicated before the call: a batch naming the same text twice should cost one
        # embedding, not two, whichever provider is behind it. Outside the lock on purpose — this
        # is a network round trip under the real provider, and serialising every turn's retrieval
        # behind one mutex would trade a rare crash for a permanent stall.
        unique = list(dict.fromkeys(missing))
        holding.update(
            (_cache_key(text), vector)
            for text, vector in zip(unique, _embed_uncached(unique), strict=True)
        )
    with _CACHE_LOCK:
        _CACHE.update(holding)
        # FIFO, oldest first. Not LRU: keeping a recency order costs a move per *hit*, on the hot
        # path, to better serve a workload — repeated identical queries — that a FIFO of this size
        # already serves. A cheaper policy that is right for the actual access pattern.
        #
        # The trim no longer has to be ordered against the read, which is what the previous
        # "read before trimming" rule bought: it may evict a key this very call just inserted, and
        # the caller still gets its vector because that vector is in `holding`.
        while len(_CACHE) > size:
            del _CACHE[next(iter(_CACHE))]
    return [holding[key] for key in keys]


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
        import ssl

        import httpx

        # An `SSLContext` rather than the path, for the reason `agent/llm_provider._tls_http_client`
        # gives: httpx deprecated `verify=<str>`, and the context names what the bundle is.
        http_client = httpx.Client(verify=ssl.create_default_context(cafile=ca_bundle))
    return OpenAI(
        base_url=base_url,
        api_key=api_key or "not-required",
        timeout=timeout,
        max_retries=max_retries,
        http_client=http_client,
    )
