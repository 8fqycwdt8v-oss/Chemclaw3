"""What a note's text *is* for a substring search, and how a query is split against it.

One definition, in layer 4, because there were three. `agent.graph_tools.find_notes` searched
`id + type + compound_smiles + tags + body`; `retrieval.vector_index`'s own note-text
builder, since consolidated into `search_text` below — which is what
`GraphRetriever`, the dense embedding and the lexical tsvector all read — searched `id + tags +
body`; `durable.digest` built a third by untyped `getattr` and matched the whole query as one
phrase. Each of the three carried a docstring asserting it agreed with the others.

Measured against the committed 38-note corpus, the disagreement was not theoretical: 5 notes
matched the term `reaction` in the agent's haystack and not in the retriever's, and 14 notes were
findable by their own `compound_smiles` in one and not the other. So `find_notes` handed the model
a note that `gather_evidence` — and therefore every report section — could not then cite.

The union wins rather than the intersection. A note's `type` and its structure are things a
chemist searches *for*, and the retriever was the leg that could not see them.

**What is not here.** Whether every term must match (`find_notes`, the digest) or whether a
partial match still ranks (`GraphRetriever`'s widening fallback) is a ranking policy and stays
with the caller that owns it. This module answers only "what text does a note have" and "what
terms does a query ask for" — the two halves that must not differ.
"""

import re
from collections.abc import Sequence

from chemclaw.kg.note import Note

# Words that carry no retrieval signal but do carry the difference between "biaryl" (three hits)
# and "the biaryl" (none) under a whole-phrase match. Deliberately tiny and English-only: this is
# not stemming or a language model, it is the handful of words a chemist puts around the term they
# actually mean. Terms are matched independently, so dropping `in` costs "in situ" nothing — the
# note is still found by `situ` — and a longer list is resisted because each entry is one more
# word a query can no longer *require*.
_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "is", "of", "on", "or", "our", "the", "to", "with"}
)
# Below this a term matches too much to be worth requiring; two characters is already `pd`.
_MIN_TERM_CHARS = 2


def search_text(note: Note) -> str:
    """The text a substring search sees for `note`: its metadata, structured figures, and body.

    Also the text that is embedded and lexically indexed (`chemclaw.retrieval.vector_index`), so
    the dense vector, the tsvector and the substring sweep agree on what "the note's content"
    means — which is what the function it replaced claimed and did not do.

    `conditions` and `source` are in the haystack because they were the fields all three search
    legs could not see: `ProcessConditions` exists so "the figures a chemist compares reach the
    note as frontmatter rather than only as sentences" — and this was the one function through
    which none of those figures could be *found*, so an `outcome: failure` note was not findable
    by the word "failure". Values only, not the field names: `temperature_c` as a token would
    match every conditions-carrying note against a query about temperature.

    Not memoized per note, deliberately: `Note` is frozen and shared out of the corpus cache, and
    the two mechanisms that could attach a computed haystack to the instance (a private attribute,
    a `__dict__` write) both make a cached note compare unequal to an identical uncached one —
    measured against pydantic's own `__eq__`. ~30 ms per full sweep of a 10k-note corpus is the
    accepted price of keeping equality honest.
    """
    parts = [note.id, note.type, note.compound_smiles or "", *note.tags, note.source or ""]
    if note.conditions is not None:
        parts.extend(str(value) for value in note.conditions.model_dump(exclude_none=True).values())
    parts.append(note.body)
    return " ".join(part for part in parts if part)


def query_terms(query: str) -> list[str]:
    """The terms a note must contain to match `query` — lowercased, split on non-word characters.

    Punctuation splits rather than being stripped, because a chemist's query carries structure in
    it (`Pd(OAc)2`, `4-bromoanisole`, `reactants>>products`) and the parts are what a note's text
    holds. Falls back to the whole query when nothing survives filtering — a search for `the` is
    still a search, and returning "no terms, therefore everything" would be worse than literal.

    The fallback honours `_MIN_TERM_CHARS`: it used to hand back exactly the sub-minimum term the
    filter had just rejected (`"C"` → `['c']`), a single letter that sits in essentially every
    haystack — which turned the narrowest possible query into the broadest possible match. A
    one-character query now returns no terms, and the caller's "nothing matched" is the honest
    answer; a chemist searching a one-atom SMILES wants the structure tools, not a substring.

    A blank query asks for nothing and gets nothing: no terms, so `term_coverage` is zero for every
    note. Not the same case as the fallback above — `""` is in every haystack, so treating an empty
    query as a term would return the entire corpus to a caller who typed nothing.
    """
    stripped = query.strip()
    if not stripped:
        return []
    terms = [
        term
        # `\W` with `re.UNICODE`, not `[^0-9a-z]`, so a non-ASCII letter is part of a term rather
        # than a separator. The ASCII form split `Lösungsmittel` into `l` and `sungsmittel` and
        # dropped `超純水` entirely, and `core.fulltext.reference_tokens` — the other half of the
        # same rule, one layer over — has always used the Unicode alphabet. Two halves of one rule
        # disagreeing about what a word is, which is what that module exists to prevent.
        for term in re.split(r"[\W_]+", query.lower(), flags=re.UNICODE)
        if len(term) >= _MIN_TERM_CHARS and term not in _STOPWORDS
    ]
    if terms:
        return terms
    return [stripped.lower()] if len(stripped) >= _MIN_TERM_CHARS else []


def term_coverage(note: Note, terms: Sequence[str]) -> int:
    """How many of `terms` appear in `note`'s searchable text.

    Coverage rather than a boolean because the two callers want different cuts of the same number:
    `find_notes` and the digest require all of them, `GraphRetriever` ranks by how many matched and
    widens to partial hits when nothing matches completely. Computing it once here is what keeps
    "matched" meaning the same thing in all three.
    """
    return sum(1 for count in term_frequencies(note, terms).values() if count)


def term_frequencies(note: Note, terms: Sequence[str]) -> dict[str, int]:
    """How *often* each of `terms` appears in `note`'s searchable text, omitting the absent ones.

    The same one haystack `term_coverage` counts, read once and reported in more detail, because
    "how many terms matched" cannot separate the notes that tie on it — and on a corpus where every
    hit matches every term, that is all of them. `GraphRetriever` needs a within-note signal to rank
    by; this is the cheapest honest one, and it costs the same scan the boolean already paid for.

    Counting is `str.count`, so it is occurrences of the term as a *substring*, matching exactly
    what `term_coverage` means by "appears" — `search_text` is a substring haystack and a token
    count here would be a second, quietly different definition of a match, which is the defect this
    module's docstring is entirely about.
    """
    haystack = search_text(note).lower()
    counts = {term: haystack.count(term) for term in terms}
    return {term: count for term, count in counts.items() if count}
