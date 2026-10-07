"""Turning a binding into statements. Every value is bound; only checked identifiers are written.

A binding contributes identifiers (each matched against `binding._IDENTIFIER`), the engine
contributes structure, and everything else (cursor, keys, query vector, limit) is a parameter, so no
ELN column value ever becomes SQL. The one exception is `where:`, inserted literally: it is authored
and reviewed in the same trusted manifest that names the driver to import. Built as text rather than
with a query builder so tests can assert the exact statement sent.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from chemclaw.ingest.eln.warehouse.binding import (
    BindingError,
    CorpusBinding,
    EntryBinding,
    RelatedBinding,
    VectorBinding,
)
from chemclaw.ingest.eln.warehouse.driver import VectorDialect

# The alias the similarity expression gets, so ordering and reading agree on one name and a site
# column called `score` cannot collide with it.
SCORE_COLUMN = "CHEMCLAW_SCORE"


def watermark_expression(entry: EntryBinding) -> str:
    """The column the sync's cursor filters and orders on.

    `COALESCE(modified, created)` when amendments are recorded, so an amended entry counts as new. A
    declared `retracted_at:` joins it, so a withdrawal stamped without touching the amendment column
    is still fetched. Written `GREATEST(W, COALESCE(retracted, W))` because warehouses disagree
    about `GREATEST` over NULL, and a propagating one would null every unretracted watermark.
    """
    if entry.modified_at:
        window = f"COALESCE({entry.modified_at}, {entry.created_at})"
    else:
        window = entry.created_at
    if entry.retracted_at:
        return f"GREATEST({window}, COALESCE({entry.retracted_at}, {window}))"
    return window


def entry_statement(
    entry: EntryBinding, placeholder: str, since: datetime, limit: int, after_key: str = ""
) -> tuple[str, list[Any]]:
    """Every reaction at or after the cursor, oldest first, bounded, as a composite keyset.

    Ascending and limited so the durable sync drains in chunks and each fetch returns the earliest
    outstanding rows. The entry key breaks ties: with the watermark alone, more rows sharing one
    value than `limit` (a DATE column, a bulk reload) would return the same page forever.
    `after_key` continues inside one watermark block, strictly after `(since, after_key)`; empty,
    the page is inclusive at the cursor. The two forms bind `[since, limit]` and `[since, since,
    after_key, limit]`. The cursor predicate is parenthesised so the site's `where:` applies to both
    halves of its `OR`. `SELECT *` because `attributes.include: ['*']` means every column the row
    has.
    """
    watermark = watermark_expression(entry)
    if after_key:
        predicate = (
            f"({watermark} > {placeholder} OR "
            f"({watermark} = {placeholder} AND {entry.key} > {placeholder}))"
        )
        params: list[Any] = [since, since, after_key]
    else:
        predicate = f"({watermark} >= {placeholder})"
        params = [since]
    if entry.where:
        predicate += f" AND ({entry.where})"
    sql = (
        f"SELECT * FROM {entry.relation} "  # identifier checked by `binding._check_identifier`
        f"WHERE {predicate} "
        f"ORDER BY {watermark} ASC, {entry.key} ASC "
        f"LIMIT {placeholder}"
    )
    return sql, [*params, limit]


def corpus_statement(
    corpus: CorpusBinding, placeholder: str, after: str, limit: int
) -> tuple[str, list[Any]]:
    """One bounded page of a bulk reaction corpus, resuming strictly after `after`.

    Keyset rather than `OFFSET` (which rescans skipped rows on every page) or a datetime
    (meaningless for a release loaded at once). An empty `after` starts from the beginning;
    re-draining is safe because every write is an id-keyed upsert.
    """
    cursor = corpus.cursor_column
    predicate = f"{cursor} > {placeholder}" if after else "1 = 1"
    if corpus.where:
        predicate += f" AND ({corpus.where})"
    sql = (
        f"SELECT * FROM {corpus.relation} "  # identifier checked by `binding._check_identifier`
        f"WHERE {predicate} "
        f"ORDER BY {cursor} ASC "
        f"LIMIT {placeholder}"
    )
    return sql, ([after, limit] if after else [limit])


def related_statement(
    block: RelatedBinding, placeholder: str, keys: Sequence[str]
) -> tuple[str, list[Any]]:
    """One child table's rows for a whole batch of entries: one query per block, not per row.

    The `IN (...)` list is a fixed number of placeholders, so values stay bound.
    """
    if not keys:
        raise BindingError("related_statement needs at least one entry key")
    markers = ", ".join(placeholder for _ in keys)
    sql = (
        f"SELECT * FROM {block.relation} "  # identifier checked by `binding._check_identifier`
        f"WHERE {block.foreign_key} IN ({markers})"
    )
    if block.order_by:
        sql += f" ORDER BY {block.foreign_key}, {block.order_by} ASC"
    return sql, list(keys)


def vector_statement(
    vector: VectorBinding,
    placeholder: str,
    dialect: VectorDialect,
    query: str | Sequence[float],
    filters: dict[str, Any],
    top_k: int,
    embedding_dim: int,
) -> tuple[str, list[Any]]:
    """The similarity search, ranked and truncated inside the warehouse.

    `LIMIT` is bound so only `top_k` rows return. `query` is the embedded vector under `embedding:
    local` and the raw text under `server`. The similarity function and the query-vector binding are
    dialect facts supplied by the driver's `VectorDialect`.
    """
    function, direction = dialect.similarity(vector.metric)
    params: list[Any] = []
    if vector.embedding == "server":
        # A model-taking embedder (Cortex) binds the model name ahead of the text; a plain UDF
        # takes only the text. Both are bound, so neither reaches the statement as literal SQL.
        if vector.server_embed_model:
            embedded = f"{vector.server_embed_function}({placeholder}, {placeholder})"
            params.extend([vector.server_embed_model, query])
        else:
            embedded = f"{vector.server_embed_function}({placeholder})"
            params.append(query)
    else:
        # Under `embedding: local` a string is a wiring bug and would be sent as a vector of
        # characters.
        if isinstance(query, str):
            raise BindingError(
                "embedding: local expects an embedded query vector, not the query text"
            )
        # How a vector is bound differs most between dialects, so the driver returns the expression
        # and its single parameter.
        embedded, bound = dialect.query_vector(placeholder, query, embedding_dim)
        params.append(bound)

    columns = ", ".join([vector.key, *vector.content_columns])
    predicates, filter_params = vector_predicates(vector, placeholder, filters)
    params.extend(filter_params)
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    sql = (
        f"SELECT {columns}, "  # identifier checked by `binding._check_identifier`
        f"{function}({vector.vector_column}, {embedded}) AS {SCORE_COLUMN} "
        f"FROM {vector.relation}{where} "
        f"ORDER BY {SCORE_COLUMN} {direction} "
        f"LIMIT {placeholder}"
    )
    params.append(top_k)
    return sql, params


def scope_statement(
    vector: VectorBinding, placeholder: str, filters: dict[str, Any], limit: int
) -> tuple[str, list[Any]]:
    """The keys eligible under `filters`, for an index-ranked source.

    Eligibility must reach the index before its top-k, and only the warehouse can evaluate the
    site's columns. `LIMIT` is one over the caller's cap, so a scope too broad to send is detected
    rather than silently truncated.
    """
    predicates, params = vector_predicates(vector, placeholder, filters)
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    sql = (
        f"SELECT {vector.key} "  # identifier checked by `binding._check_identifier`
        f"FROM {vector.relation}{where} "
        f"LIMIT {placeholder}"
    )
    return sql, [*params, limit + 1]


def resolve_statement(
    vector: VectorBinding, placeholder: str, keys: Sequence[str]
) -> tuple[str, list[Any]]:
    """The content columns for the keys an index returned: the catalogue half of a split search.

    One query for the batch. No `ORDER BY`: the ranking is the store's and the caller re-imposes it.
    The binding's `where:` is enforced here, the only place it can be: it is broad, so enumerating
    it as a scope would exceed the cap. The cost is that an excluded row can occupy a top-k slot, so
    a search may return fewer than `top_k` hits; acceptable for a broad predicate, not for the
    query's narrow filters.
    """
    if not keys:
        raise BindingError("resolve_statement needs at least one key")
    markers = ", ".join(placeholder for _ in keys)
    columns = ", ".join([vector.key, *vector.content_columns])
    predicate = f"{vector.key} IN ({markers})"
    if vector.where:
        predicate += f" AND ({vector.where})"
    sql = (
        f"SELECT {columns} "  # identifier checked by `binding._check_identifier`
        f"FROM {vector.relation} "
        f"WHERE {predicate}"
    )
    return sql, list(keys)


def vector_predicates(
    vector: VectorBinding, placeholder: str, filters: dict[str, Any]
) -> tuple[list[str], list[Any]]:
    """Translate the honoured evidence filters onto the site's own columns.

    Only filters the binding mapped are applied; an unmapped one is ignored rather than guessed.
    Used by the scope query, not the resolve query, which enforces `where:` instead.
    """
    predicates: list[str] = []
    params: list[Any] = []
    if vector.where:
        predicates.append(f"({vector.where})")
    if (tag := filters.get("tag")) and "tag" in vector.filter_columns:
        predicates.append(f"{vector.filter_columns['tag']} = {placeholder}")
        params.append(tag)
    if (since := filters.get("since")) and "since" in vector.filter_columns:
        predicates.append(f"{vector.filter_columns['since']} >= {placeholder}")
        params.append(since)
    if (until := filters.get("until")) and "until" in vector.filter_columns:
        predicates.append(f"{vector.filter_columns['until']} <= {placeholder}")
        params.append(until)
    return predicates, params


def normalise_score(metric: str, raw: float) -> float:
    """Map a metric's raw result onto the 0..1 an `EvidenceChunk` carries.

    A distance is folded through `1/(1+d)`; cosine and inner product are clamped. The returned order
    is authoritative, not this number: sources are fused by rank, and clamping may tie hits.
    """
    if metric == "l2":
        return 1.0 / (1.0 + max(raw, 0.0))
    return max(0.0, min(1.0, raw))
