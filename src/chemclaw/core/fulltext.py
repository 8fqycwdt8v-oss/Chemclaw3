"""One lexical boolean rule for both hybrid indexes, in the two forms each of them needs.

Match any term; rank rows matching every term first; honour an exclusion. The rule must be
identical across the note index's durable and in-memory backends and the document index's,
because the in-memory reference is what unit tests stand on; `tests/test_fulltext.py` drives both
backends over one corpus and asserts equal hit sets. It sits in `chemclaw.core` because that is
the one package both indexes depend on.

The offline reference has no stemmer or stop-word list, so its scores differ from `ts_rank`; what
must not differ is which rows are hits and that a complete match outranks a partial one.
"""

import re

# A `-term` exclusion in the raw query, as `websearch_to_tsquery` reads it: only a leading `-` on a
# whitespace-delimited word, so `tert-butyl` is not an exclusion.
_EXCLUSION = "-"

# Lowercased alphanumeric runs: the offline proxy of `to_tsvector`, mirroring its number handling.
# A decimal is one lexeme (`98.5`), and an interior hyphenated number keeps its sign
# (`108-24-7` -> `108`, `-24`, `-7`; also after a letter, as in `LOT-2024`). The decimal alternative
# comes first so `2.5-3.0` takes the float.
_WORD = re.compile(r"\d+\.\d+|(?<=[^\W_])-\d+|[^\W_]+", re.UNICODE)

# A `-` immediately before a digit at a token boundary. Postgres keeps such a hyphen as a sign on
# the lexeme (`-78`), so query `78` misses it and query `-78` is an exclusion; detaching it makes
# negative quantities searchable. Applied to documents and queries alike so CAS and lot numbers
# still match. Anchored at a token boundary so an identifier's interior hyphens stay intact: split,
# `108-24-7` would become three top-level clauses that `TSQUERY_TERMS` then ORs. Trade-off: a query
# meaning "exclude the number 78" now reads as "find 78".
_SIGN_GLUED_TO_NUMBER = re.compile(r"(?<![^\W_])-(?=\d)")


def normalize_search_text(text: str) -> str:
    """`text` with a sign detached from the number it precedes — applied to documents *and* queries.

    Every backend runs it before deriving anything searchable (Postgres before `to_tsvector` and
    `websearch_to_tsquery`, the reference before tokenising); applied on one side only it would
    cause the divergence it removes.
    """
    return _SIGN_GLUED_TO_NUMBER.sub(" ", text)


# The FROM-item both durable statements join against: `all_terms` (what the reader asked for) and
# `any_terms` (the widened form that decides which rows match).
#
# `any_terms` ORs the parsed query's positive top-level clauses and ANDs its negated ones back on,
# so a multi-word question still finds partial matches while `-solvent` stays an exclusion:
# `'amid' & 'coupl' & !'solvent'` becomes `('amid' | 'coupl') & !'solvent'`. Each clause is a
# verbatim substring of Postgres's own rendering of a bound parameter, so nothing is spliced into
# SQL. The text split on ` & ` can cut inside a top-level disjunction, which applies an exclusion to
# the whole disjunction: stricter than the parse, never looser.
TSQUERY_TERMS = (
    "websearch_to_tsquery('english', %(q)s) AS all_terms, "
    "LATERAL ("
    "SELECT CASE WHEN positives = '' THEN all_terms ELSE "
    "('(' || positives || ')' || "
    "CASE WHEN negatives = '' THEN '' ELSE ' & ' || negatives END)::tsquery END "
    # `clause`, not `c`: this fragment is spliced beside `document_chunks c`, and an alias that
    # shadowed the caller's table would resolve `clause NOT LIKE …` against a row type.
    "FROM (SELECT "
    "array_to_string("
    "ARRAY(SELECT clause FROM unnest(clauses) AS clause WHERE clause NOT LIKE '!%%'), ' | ') "
    "AS positives, "
    "array_to_string("
    "ARRAY(SELECT clause FROM unnest(clauses) AS clause WHERE clause LIKE '!%%'), ' & ') "
    "AS negatives "
    "FROM (SELECT string_to_array(all_terms::text, ' & ')) AS split(clauses)) AS parts"
    ") AS widened(any_terms)"
)


def reference_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens — the offline proxy of Postgres `to_tsvector`.

    No stemmer or stop-word list, so as not to maintain a second text-search configuration; scores
    may differ, hit sets for real terms may not. `normalize_search_text` is applied here so no
    in-memory backend can forget it.
    """
    return {match.group().lower() for match in _WORD.finditer(normalize_search_text(text))}


def reference_terms(query: str) -> tuple[set[str], set[str]]:
    """Split a raw query into the tokens a hit must carry and the tokens it must not.

    The offline half of `TSQUERY_TERMS`: a leading `-` is an exclusion. A token both wanted and
    excluded is excluded, matching the durable form.

    Returns:
        `(wanted, excluded)`. Both empty means there is no query (stop-word or symbol only), which
        must return nothing rather than the whole corpus.
    """
    wanted: set[str] = set()
    excluded: set[str] = set()
    for word in normalize_search_text(query).split():
        target = excluded if word.startswith(_EXCLUSION) else wanted
        target |= reference_tokens(word)
    return wanted - excluded, excluded
