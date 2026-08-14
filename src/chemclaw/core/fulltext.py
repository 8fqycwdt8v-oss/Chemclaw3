"""One lexical boolean rule for both hybrid indexes, in the two forms each of them needs.

**Match any term; rank the rows matching every term first; honour an exclusion.** That is the whole
rule, and it has to be *one* rule across four implementations: the note index's durable and
in-memory backends (`chemclaw.retrieval.vector_index`) and the document index's
(`chemclaw.ingest.documents.index`). Every time it has been written twice the two copies have
disagreed, and the disagreement has been invisible — the in-memory reference is what the unit tests
stand on, so a durable backend answering a different question passes every test and returns nothing
in production. That has now happened twice for the same rule: once on the note index (fixed by
PR #173) and once on the document index, which the same fix left ANDing.

**Why this module sits in `chemclaw.core`.** The two backends live in different packages, and
`chemclaw.core` is the only one both already depend on. The alternative — `ingest` importing
`retrieval` for a SQL string — would make the document index depend on the note index module, which
is a layering edge bought for a constant.

**The offline halves are proxies, and only in the ways stated here.** `reference_tokens` has no
stemmer and no stop-word list, so a token count is not a `ts_rank` and "the and of" is a query to
the reference and no query at all to Postgres. What is *not* a proxy, and what this module exists to
keep identical, is the boolean rule: which rows are hits, and that a complete match outranks a
partial one.
"""

import re

# A `-term` exclusion in the raw query text — the one piece of `websearch_to_tsquery`'s syntax the
# offline reference has to understand, because it changes *which rows are hits* rather than how
# they rank. Anchored on the leading `-` of a whitespace-delimited word, exactly as websearch reads
# it, so an ordinary hyphenated name (`tert-butyl`, `a-b-c`) is not an exclusion.
_EXCLUSION = "-"

# Lowercased alphanumeric runs — the offline proxy of `to_tsvector`, shared by both reference
# backends because they used to spell it twice with two different alphabets (`[a-z0-9]+` dropped
# every non-ASCII letter that Postgres does index).
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# The FROM-item both durable statements join against: the query in its two forms, `all_terms` (what
# the reader actually asked for) and `any_terms` (the widened form that decides which rows match).
#
# **`any_terms` ORs the parsed query's *positive* clauses and ANDs its negated ones back on.** The
# widening exists so a four-word question does not answer "nothing known" when the corpus holds
# three notes that each answer part of it (measured on a 15,000-note corpus: the AND form matched
# **0 rows**), and the completeness ordering below is what keeps a full match on top anyway. But a
# query is not only its stems: `-solvent` is an *exclusion*, and widening the tsvector's lexemes —
# which is what this used to do — turned it into a positive OR term, so `amide coupling -solvent`
# returned the solvent notes. Measured on a four-note corpus, live pgvector 0.8.0 / PostgreSQL 16:
#
#   websearch_to_tsquery('english','amide coupling -solvent') -> 'amid' & 'coupl' & !'solvent'
#   the tsvector widening                                     -> 'amid' | 'coupl' | 'solvent'
#   this form                                                 -> ( 'amid' | 'coupl' ) & !'solvent'
#
# and the note whose whole body is "solvent selection guide" matched the middle one.
#
# **The clauses are Postgres's own rendering, split on the top-level ` & `.** That is what makes it
# safe, and it is a different argument from the `quote_literal` this replaced: nothing is quoted by
# hand any more, because every piece is a verbatim substring of `websearch_to_tsquery(%(q)s)::text`
# — a value Postgres produced from a bound parameter, never spliced into statement text. The
# statement itself is a constant; the chemist's query reaches the server only as a parameter, so
# there is no SQL injection surface at all. What the split *could* corrupt is the tsquery, so that
# was measured rather than argued: over 4,022 query strings (fuzzed over an alphabet of quotes,
# operators, `-`, parentheses and non-ASCII) no lexeme rendered with ` & ` inside its quotes, no
# rebuilt query failed to parse, and over 4,008 more the rebuilt query carried **exactly** the
# parsed query's lexemes with exactly the same negation signs. Widening only turns a top-level `&`
# into a `|`; it can neither invent a lexeme nor change one's sign.
#
# **The one residual, stated rather than hidden.** ` & ` is a *text* split, so when the top-level
# operator is `|` it can cut inside a disjunction: `amide or coupling -solvent` parses as
# `'amid' | ('coupl' & !'solvent')` and comes back as `('amid' | 'coupl') & !'solvent'`, which
# applies the exclusion to the whole disjunction. That is the natural reading of what was typed and
# is *stricter* than the parse, never looser — the property the fuzz above pins is that a negated
# stem can never reappear as a positive one.
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

    No stemmer and no stop-word list, deliberately: shipping either would be a second text-search
    configuration to keep in step with the server's. The reference is allowed to differ on *scores*
    and on stop words; it is not allowed to differ on which rows are hits for a query of real terms.
    """
    return {match.group().lower() for match in _WORD.finditer(text)}


def reference_terms(query: str) -> tuple[set[str], set[str]]:
    """Split a raw query into the tokens a hit must carry and the tokens it must not.

    The offline half of `TSQUERY_TERMS`: `websearch_to_tsquery` reads a leading `-` as an exclusion,
    so a reference that tokenizes the query flat scores `amide coupling -solvent` as if the chemist
    had *asked* for solvent — the same defect in the mirror. A token that is both wanted and
    excluded (`amide -amide`) is excluded, which is what the durable `('amid') & !'amid'` does.

    Returns:
        `(wanted, excluded)`. Both empty means there is no query — a stop-word-only or symbol-only
        string, which must return nothing rather than the whole corpus.
    """
    wanted: set[str] = set()
    excluded: set[str] = set()
    for word in query.split():
        target = excluded if word.startswith(_EXCLUSION) else wanted
        target |= reference_tokens(word)
    return wanted - excluded, excluded
