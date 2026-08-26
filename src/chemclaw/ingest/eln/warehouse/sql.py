"""Turning a binding into statements. Every value is bound; only checked identifiers are written.

The rule this module exists to hold: **a binding contributes identifiers, the engine contributes
structure, and everything else is a parameter.** Relation and column names reach the statement text,
and each one was matched against `binding._IDENTIFIER` before it got here. The cursor timestamp, the
entry keys of a batch, the query vector and the row limit are bound. There is no path by which a
column *value* — anything a chemist typed into the ELN — becomes SQL.

The one deliberate exception is `where:`, inserted literally. It is authored in the same file as the
`module:callable` the same seam imports, by the same person, and reviewed the same way; a predicate
language that could express "the site's notion of finished" without being a predicate would be a
worse trade than trusting the file we already trust.

Statements are built as text rather than through a query builder because there are four of them and
they are shaped by the binding, not by the caller. A builder would add a dependency and an
indirection to save nothing, and it would make the thing a test wants to assert — the exact string
that would be sent — harder to see rather than easier.
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

    `COALESCE(modified, created)` when the source records amendments, because the ELN sync's
    contract is that an amended entry counts as new (`chemclaw.ingest.eln.adapter.entry_window`
    says so, and both file-drop adapters honour it). Filtering on creation alone would ingest a run
    once and never see the correction a chemist made to it the following week.
    """
    if entry.modified_at:
        return f"COALESCE({entry.modified_at}, {entry.created_at})"
    return entry.created_at


def entry_statement(
    entry: EntryBinding, placeholder: str, since: datetime, limit: int
) -> tuple[str, list[Any]]:
    """Every reaction at or after the cursor, oldest first, bounded.

    **Oldest first and bounded together.** The durable sync drains a source in chunks, persisting
    its cursor after each one; that only makes progress if each fetch returns the *earliest*
    outstanding rows. Ordering ascending and taking the first `limit` is exactly that, and it is
    what keeps a first sync of a warehouse with a decade of history from being one query that tries
    to materialise the decade.

    `SELECT *` because the binding's `attributes.include: ['*']` means "every column the row has",
    and a projection would have to know them — which is the thing nobody knows today.
    """
    watermark = watermark_expression(entry)
    predicate = f"{watermark} >= {placeholder}"
    if entry.where:
        predicate += f" AND ({entry.where})"
    sql = (
        f"SELECT * FROM {entry.relation} "  # identifier checked by `binding._check_identifier`
        f"WHERE {predicate} "
        f"ORDER BY {watermark} ASC "
        f"LIMIT {placeholder}"
    )
    return sql, [since, limit]


def corpus_statement(
    corpus: CorpusBinding, placeholder: str, after: str, limit: int
) -> tuple[str, list[Any]]:
    """One bounded page of a bulk reaction corpus, resuming strictly after `after`.

    **Keyset, not offset, and not a datetime.** `OFFSET n` on a multi-million-row table makes the
    warehouse walk and discard n rows on every page, so a drain gets quadratically slower exactly
    as it gets further in; and a datetime cursor is meaningless for a versioned release that was
    loaded all at once. Resuming after the last key seen is O(index seek) per page and is what
    makes a stopped drain resumable at no cost.

    An empty `after` starts at the beginning, which is the first pass and also a full re-drain.
    Re-drainng is safe: every write the corpus drain makes is an id-keyed upsert.

    `SELECT *` for the same reason `entry_statement` uses it — the binding names the columns it
    reads by path, and a projection would have to know a schema nobody can see yet.
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
    """One child table's rows for a whole batch of entries — one query per block, not per row.

    Per row would be the obvious loop and would issue a query per reaction per table; a batch of a
    hundred reactions across four child tables would be four hundred round trips to a warehouse
    that charges for them. The `IN (...)` list is a fixed number of placeholders, so the values are
    still bound.
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

    Ranking server-side is the whole reason this half exists: the embedding column is already there,
    over a corpus larger than what gets ingested, and the alternative is pulling rows out to score
    them here. `LIMIT` is bound so the warehouse returns `top_k` rows rather than a corpus.

    `query` is the embedded vector under `embedding: local`, and the raw query text under `server`,
    where the warehouse's own function embeds it — the two paths differ only in what stands in the
    similarity call's second argument.

    **The similarity function and the query-vector binding come from the driver**, not from a table
    here: both are dialect facts, and this module contributes structure. See
    `chemclaw.ingest.eln.warehouse.driver.VectorDialect` for why that moved.
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
        # `embedding: local` means the caller embedded the query, so a string here is a wiring bug
        # rather than a binding one — and it would otherwise reach the warehouse as a vector of
        # characters. The guard is what narrows the union as well as what reports it.
        if isinstance(query, str):
            raise BindingError(
                "embedding: local expects an embedded query vector, not the query text"
            )
        # The one value `sql.py` cannot render itself: a 1536-float vector is a *value*, and how it
        # is bound is the sharpest dialect difference of all. The driver returns the expression and
        # the single parameter that fills it.
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

    An index ranks the whole corpus; the caller's filters have to reach it *before* the top-k or a
    narrow tag over a wide relation returns nothing at all — the k nearest vectors would all belong
    to something else. `VectorStore.search` takes eligibility as a set of ids, so it is computed
    here, in the one system that can evaluate a predicate over the site's own columns.

    `LIMIT` is bound and one over the caller's cap, so a scope too broad to send is *detected*
    rather than truncated: a silently truncated eligibility set is a wrong answer that looks like a
    thin corpus. The residual this bounds is the one `retrieval/vectors/README.md` states — a scope
    is a set, and a broad filter over a very large corpus builds a big one.
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
    """The content columns for the keys an index returned — the catalogue half of a split search.

    The counterpart of `ExternalVectorDocumentIndex._resolve`, with the warehouse standing in for
    Postgres: the store answers "which, and how similar", and the system that owns the text answers
    "what does it say". One query for the whole batch, as `related_statement` does and for the same
    reason — a warehouse charges per round trip.

    No `ORDER BY`: the ranking is the store's and the caller re-imposes it. Ordering by the key here
    would look tidy and would silently discard the ranking.

    **`where:` is enforced here, and this is the only place it can be.** It is a *corpus*
    restriction — "these rows are eligible at all" — so it is typically broad, and on an
    index-ranked source the corpus is the size that made an index necessary. Enumerating it as a
    key set to pre-filter with is therefore exactly what `vector_store_max_scope_keys` refuses; a
    first attempt did that and turned a binding's `where:` into "this source answers nothing".
    Applying it to the resolve costs one predicate on a query already keyed to `top_k` rows.

    The residual, stated because it is a real cost: a row the `where:` excludes can still occupy a
    slot in the store's top-k, so a search may return fewer than `top_k` hits. That is the
    post-filter trade this seam otherwise refuses — and it is right *here* and wrong for the query's
    own `tag`/`since`/`until`, because those are narrow. Post-filtering a narrow predicate loses
    everything; post-filtering a broad one loses a slot or two.
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

    Only the keys the binding mapped are applied. An unmapped filter is ignored rather than guessed
    at — inventing a column name would either error on every query or, worse, match a column that
    means something else at this site.

    **The scope query uses this; the resolve query does not.** An index-ranked search pre-filters on
    the *query's* narrow keys and enforces the binding's broad `where:` at the resolve instead —
    `resolve_statement` says why. So `where:` appears in both statements when a scope is built, and
    in the resolve alone when one is not, which is what makes it unconditional either way.
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

    A distance and a similarity are not the same quantity, and the chunk's field is documented as a
    similarity. A distance is folded through `1/(1+d)`, which is monotonic; cosine already lands in
    -1..1 and is clamped.

    **The returned order is authoritative, not this number.** The warehouse has already ranked the
    rows, this system fuses sources by rank position rather than by score, and an inner product is
    genuinely unbounded — so clamping it can tie two hits at 1.0 without changing which one the
    agent sees first. Rank is what carries the ranking; this is what a reader sees beside a chunk.
    """
    if metric == "l2":
        return 1.0 / (1.0 + max(raw, 0.0))
    return max(0.0, min(1.0, raw))
