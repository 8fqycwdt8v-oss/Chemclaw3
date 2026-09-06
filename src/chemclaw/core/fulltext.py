"""One lexical boolean rule for both hybrid indexes, in the two forms each of them needs.

**Match any term; rank the rows matching every term first; honour an exclusion.** That is the whole
rule, and it has to be *one* rule across four implementations: the note index's durable and
in-memory backends (`chemclaw.retrieval.vector_index`) and the document index's
(`chemclaw.ingest.documents.index`). Every time it has been written twice the two copies have
disagreed, and the disagreement has been invisible — the in-memory reference is what the unit tests
stand on, so a durable backend answering a different question passes every test and returns nothing
in production. That has now happened three times for the same rule: once on the note index (fixed
by PR #173), once on the document index (which the same fix left ANDing), and once on **numbers** —
see `normalize_search_text` and `_WORD` below. `tests/test_fulltext.py` is the differential test
that would have caught all three: it drives both backends over one chemistry corpus and asserts
they return the same hit sets.

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
#
# **The decimal alternative leads because Postgres emits a float as one lexeme.** Measured on live
# PostgreSQL 16, `to_tsvector('english', 'gave 98.5% ee')` is `'98.5' 'ee' 'gave'` — so a reference
# that split it into `98` and `5` reported a hit for the query `98` that the server does not return.
# That is the *over*-matching direction, which is the worse one: the offline backend the unit tests
# stand on answered a question production cannot.
#
# **The signed interior number is the third alternative, and it mirrors Postgres rather than
# flattening it.** `to_tsvector('english', 'CAS 108-24-7')` is `'108' '-24' '-7'` — the parser keeps
# the hyphen on each interior number as a sign — so a reference that produced `{108, 24, 7}` would
# call a note a hit for the query `24` that the durable backend does not return. The lookbehind is
# any word character rather than a digit, because the parser does the same after a letter:
# `LOT-2024-0031` is `'lot' '-2024' '-0031'`. Ordered after the decimal so `2.5-3.0` takes the
# float first.
_WORD = re.compile(r"\d+\.\d+|(?<=[^\W_])-\d+|[^\W_]+", re.UNICODE)

# A `-` immediately before a digit. Postgres's parser reads that hyphen as a **sign** and keeps it
# on the lexeme, and that is the one place its tokenisation loses a chemist's data outright.
# Measured on live PostgreSQL 16:
#
#   to_tsvector('english', 'charged at -78 C')   -> '-78':3 'c':4 'charged':1
#   … @@ websearch_to_tsquery('english', '78')   -> false      (the magnitude does not find it)
#   websearch_to_tsquery('english', '-78')       -> !'78'      (a leading `-` is an *exclusion*)
#
# So a cryogenic temperature — the commonest negative quantity in process chemistry — was reachable
# by no query at all: `78` misses the row, and `-78` asks to exclude it and matches nearly every
# other row instead.
#
# **Both sides are normalised, and that is load-bearing rather than symmetric for its own sake.**
# An identifier survives today only because the query fragments exactly as the document did — a CAS
# number indexes as `'108' '-24' '-7'` and `websearch_to_tsquery` renders the same query as the
# phrase `'108' <-> '-24' <-> '-7'`, so they meet. Normalising the document alone would have moved
# the document and left the query, breaking every CAS and lot-number lookup that currently works;
# measured after normalising both, `108-24-7` still matches, `78` now finds `-78 °C`, and
# `guide -solvent` still excludes. A word exclusion is untouched in either case, because this
# pattern cannot fire before a letter.
#
# What it costs, stated because it is a real behavioural change: a query meaning "exclude the
# number 78" now reads as "find 78". On a corpus of temperatures, concentrations and equivalents
# that is the reading a chemist intends, and it is the only one under which a negative quantity is
# searchable at all.
#
# **Anchored at a token boundary, and the anchor is the whole correctness of it.** Unanchored, this
# also split the *interior* hyphens of an identifier — and that is not a cosmetic difference,
# because of what `TSQUERY_TERMS` does downstream. `websearch_to_tsquery('108-24-7')` renders
# **one** clause, a phrase (`'108' <-> '-24' <-> '-7'`) the widening leaves whole; normalised to
# `108 24 7` it renders **three** top-level clauses, which the widening then ORs. Measured on a
# 301-note corpus holding one CAS number and 300 ordinary notes mentioning `pH 7`, `24 h` and
# `batch 108`: the unanchored form matched **301 rows** where the anchored one matches **1**, and
# seven of the lexical leg's eight slots went to notes that share a digit.
_SIGN_GLUED_TO_NUMBER = re.compile(r"(?<![^\W_])-(?=\d)")


def normalize_search_text(text: str) -> str:
    """`text` with a sign detached from the number it precedes — applied to documents *and* queries.

    The one normalisation every backend runs before deriving anything searchable: Postgres before
    `to_tsvector` and before `websearch_to_tsquery`, the offline reference before tokenising. It
    lives here for the reason the rest of this module does — applied on one side only, it *is* the
    divergence it exists to remove, and the CAS-number measurement above is what that would have
    cost.
    """
    return _SIGN_GLUED_TO_NUMBER.sub(" ", text)


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

    **`normalize_search_text` is applied here rather than by the caller**, so neither in-memory
    backend can forget it while the two durable ones apply it to their bound parameters.
    """
    return {match.group().lower() for match in _WORD.finditer(normalize_search_text(text))}


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
    for word in normalize_search_text(query).split():
        target = excluded if word.startswith(_EXCLUSION) else wanted
        target |= reference_tokens(word)
    return wanted - excluded, excluded
