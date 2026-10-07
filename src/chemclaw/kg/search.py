"""What a note's text is for a substring search, and how a query is split against it.

One definition shared by `agent.graph_tools.find_notes`, the retrievers, the embedding and lexical
indexes and the digest, so a note one search finds the others can cite. The haystack is the union of
the fields any of them searched. Whether every term must match or partial matches rank is the
caller's ranking policy; this module answers only what text a note has and what terms a query asks
for.
"""

import re
from collections import Counter
from collections.abc import Sequence

from chemclaw.kg.note import Note

# The words a question is framed in, as opposed to the words it is about: English closed-class words
# (articles, prepositions, conjunctions, pronouns, determiners, interrogatives, modals, auxiliaries,
# quantifiers), which no note can be about. Kept out of queries because a query requiring all of
# them matches nothing and then widens to any term, where substring matching makes short function
# words hit everything (`so` in `solvent`). Open-class framing verbs are not listed: they did not
# help recall. Two-letter element symbols that collide with function words are unsearchable by
# substring anyway; use the structure tools.
_STOPWORDS = frozenset(
    word
    for category in (
        # articles, prepositions, conjunctions and particles
        "a an and as at about again but by down for from if in into of on or out over so "
        "than then the to too up very with",
        # interrogatives
        "how what when where which who whom whose why",
        # pronouns and determiners
        "he her here his it its me my our she that their them there these they this those "
        "us we you your",
        # modals and auxiliaries
        "am are be been being can could did do does doing done had has have having is may "
        "might must shall should was were will would",
        # negation, affirmation and quantifiers
        "all also any both each ever few just many more most much never no not only other some yes",
    )
    for word in category.split()
)
# Below this a term matches too much to be worth requiring; two characters is already `pd`.
_MIN_TERM_CHARS = 2

# The one tokeniser for queries and haystacks, Unicode-aware (`\W`), so non-ASCII words stay whole,
# as in `core.fulltext.reference_tokens`.
_SPLIT = re.compile(r"[\W_]+")


def search_text(note: Note) -> str:
    """The text a substring search sees for `note`: its metadata, structured figures, and body.

    Also the text that is embedded and lexically indexed, so all search legs agree on a note's
    content. `conditions` and `source` values (not field names) are included so recorded figures and
    outcomes are findable. Not memoized on the frozen, shared `Note`, since attaching state would
    break equality with an identical uncached note.
    """
    parts = [note.id, note.type, note.compound_smiles or "", *note.tags, note.source or ""]
    if note.conditions is not None:
        parts.extend(str(value) for value in note.conditions.model_dump(exclude_none=True).values())
    parts.append(note.body)
    return " ".join(part for part in parts if part)


def query_terms(query: str) -> list[str]:
    """The terms a note must contain to match `query`: lowercased, split on non-word characters.

    Punctuation splits rather than being stripped, so `Pd(OAc)2` yields its parts. If filtering
    leaves nothing, the whole query is used, subject to `_MIN_TERM_CHARS`: a one-character query
    yields no terms. A blank query yields no terms, so it matches nothing rather than everything.
    """
    stripped = query.strip()
    if not stripped:
        return []
    terms = [
        term
        # Unicode-aware split, so a non-ASCII letter is part of a term rather than a separator.
        for term in _SPLIT.split(query.lower())
        if len(term) >= _MIN_TERM_CHARS and term not in _STOPWORDS
    ]
    if terms:
        return terms
    return [stripped.lower()] if len(stripped) >= _MIN_TERM_CHARS else []


def term_coverage(note: Note, terms: Sequence[str]) -> int:
    """How many of `terms` appear in `note`'s searchable text.

    A count so callers can require all terms (`find_notes`, the digest) or rank by coverage
    (`GraphRetriever`). Substring membership on purpose (`ester` finds `polyester`); this decides
    which notes are hits, while `term_frequencies` weighs them.
    """
    haystack = search_text(note).lower()
    return sum(1 for term in terms if term in haystack)


def matched_terms(note: Note, terms: Sequence[str]) -> list[str]:
    """Which of `terms` appear in `note`'s searchable text, in query order, without repeats.

    Same haystack and substring rule as `term_coverage`; shown to the model on each chunk so a weak,
    widened match is visible. Deduplicated, unlike `term_coverage`, whose count is compared against
    `len(terms)`.
    """
    haystack = search_text(note).lower()
    return [term for term in dict.fromkeys(terms) if term in haystack]


def term_frequencies(note: Note, terms: Sequence[str]) -> dict[str, int]:
    """How often each of `terms` appears in `note`'s searchable text, omitting the absent ones.

    A within-note ranking signal for notes that tie on coverage. Counted over whole tokens, not
    substrings: short abbreviations like `dr` or `ee` occur inside ordinary words, and multiplying
    those spurious hits would outrank the note that actually reports them. Membership stays with
    `term_coverage`, so a substring-only match is a hit that earns no weight here.
    """
    wanted = set(terms)
    counts = Counter(token for token in _SPLIT.split(search_text(note).lower()) if token in wanted)
    return {term: counts[term] for term in terms if counts[term]}
