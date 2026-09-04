"""The one lexical rule, driven against the server it is a proxy for.

`chemclaw.core.fulltext` exists because the boolean rule has to be identical across four
implementations, and its module docstring records that the two halves have silently disagreed three
times. **It had no test file at all** — `reference_tokens`, `reference_terms` and `core.fulltext`
appeared in zero tests while the module measured 100% line and branch coverage, which is the
clearest available statement that a coverage floor cannot see this class of defect. Mutating `_WORD`
to `[a-z0-9]+` — the exact alphabet its own comment names as the old bug — left 349 retrieval tests
green while `Suzuki` tokenised as `uzuki` and `DMF` as `d`.

So this file is deliberately two halves:

- **Offline unit tests** that kill those mutants, over the vocabulary that actually breaks.
  Chemistry is disproportionately uppercase acronyms (DMF, NMP, THF, HATU), non-ASCII letters
  and numbers carrying units, and a naive ASCII-lowercase alphabet destroys precisely those
  while leaving ordinary English prose untouched — so a corpus of English sentences would not
  have noticed.
- **A differential test against live PostgreSQL**, because the offline half cannot see the failure
  that matters. `InMemoryNoteIndex` is what the unit tests stand on and `PostgresNoteIndex` is what
  production runs; the only way to know they answer the same question is to ask them both. That is
  what would have caught all three historical divergences, and it is what caught the numeric one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.fulltext import normalize_search_text, reference_terms, reference_tokens
from chemclaw.retrieval.vector_index import (
    InMemoryNoteIndex,
    NoteRecord,
    PostgresNoteIndex,
)
from tests.pg import migrated_db_or_skip


def _run(awaitable: Any) -> Any:
    """Drive one coroutine from a sync test, matching this suite's convention.

    No `pytest.mark.asyncio`: `pyproject.toml` sets `--strict-markers` and registers none, so an
    async test would be collected and never awaited.
    """
    return asyncio.run(awaitable)


# One corpus, written the way a process chemist writes. Every entry carries at least one thing the
# naive alphabets get wrong: an uppercase acronym, a non-ASCII letter, a decimal, a signed number,
# an underscore, or an identifier whose hyphens Postgres reads as signs.
CHEMISTRY_CORPUS: dict[str, str] = {
    "n-cas": "Acetic anhydride, CAS 108-24-7, charged as the acylating agent.",
    "n-ee": "The Suzuki coupling gave 98.5% ee and 91% isolated yield.",
    "n-cryo": "Grignard reagent, 2.5 M in Et2O, charged at -78 C and warmed slowly.",
    "n-solvent": "Solvent selection guide: DMF, NMP, THF and 2-MeTHF compared for the amidation.",
    "n-amide": "Amide coupling with HATU in DMF at ambient temperature, 4 h.",
    "n-greek": "The β-lactam ring opened under acidic conditions; Δ-9-THC was not detected.",
    "n-unicode": "Prüfung der Reaktion mit einer naïven Methode im Lösungsmittel.",
    "n-tbu": "tert-butyl ester cleavage with TFA; the tert_butyl form is written both ways.",
    "n-temp": "Reaction held at 80 C for 12 h, then cooled to 0 C before quench.",
}

# Queries a chemist would actually type, chosen so each exercises a different tokenisation hazard.
CHEMISTRY_QUERIES: list[str] = [
    "Suzuki",
    "DMF",
    "suzuki coupling",
    "98.5",
    "108-24-7",
    "CAS 108-24-7",
    "2.5 M",
    "78",
    "tert-butyl",
    "tert_butyl",
    "β-lactam",
    "Lösungsmittel",
    "amide coupling -solvent",
    "solvent",
    "80 C",
    "HATU DMF",
]


def test_case_is_folded_rather_than_stripped() -> None:
    """`Suzuki` is the token `suzuki`, not `uzuki`.

    The mutant this kills is `[a-z0-9]+`, which does not match an uppercase letter and therefore
    starts the token *after* it. Named reactions and solvent acronyms are the words a chemist
    searches by and the words most likely to be capitalised, so this alphabet fails hardest exactly
    where it is used most: `DMF` collapses to the single character `d`.
    """
    assert reference_tokens("Suzuki") == {"suzuki"}
    assert reference_tokens("DMF NMP THF HATU") == {"dmf", "nmp", "thf", "hatu"}


def test_a_non_ascii_letter_is_part_of_a_token_rather_than_a_separator() -> None:
    """`Lösungsmittel` is one token, and Postgres indexes it as one.

    An ASCII-only alphabet splits it into `l` and `sungsmittel` and drops a CJK term entirely — and
    a one-or-two-character fragment then counts as a matched term, which inflates a coverage score
    rather than failing visibly.
    """
    assert reference_tokens("Lösungsmittel") == {"lösungsmittel"}
    assert reference_tokens("naïve café Prüfung") == {"naïve", "café", "prüfung"}
    assert reference_tokens("超純水") == {"超純水"}


def test_an_underscore_separates_tokens_because_postgres_separates_on_it() -> None:
    r"""`tert_butyl` is two tokens, because `to_tsvector` splits it into `tert` and `butyl`.

    Measured on live PostgreSQL 16. This is the `_` in `[^\W_]+` earning its place — dropping it
    would make the reference read `tert_butyl` as one token the server never produces.
    """
    assert reference_tokens("tert_butyl") == {"tert", "butyl"}


def test_a_decimal_is_one_token_because_postgres_emits_a_float_whole() -> None:
    """`98.5` does not become `98` and `5`.

    Splitting it made the offline reference report a hit for the query `98` that the durable backend
    does not return — the over-matching direction, where the backend the tests stand on answers a
    question production cannot.
    """
    assert reference_tokens("gave 98.5% ee") == {"gave", "98.5", "ee"}
    assert "98" not in reference_tokens("98.5")


def test_a_sign_is_detached_from_the_number_it_precedes() -> None:
    """`-78 C` is indexed by its magnitude, so a chemist can find a cryogenic temperature.

    Postgres keeps the sign on the lexeme (`'-78'`), and `websearch_to_tsquery` reads a leading `-`
    as an *exclusion* — so before this normalisation no query reached the row at all: `78` missed it
    and `-78` asked to exclude it.
    """
    assert reference_tokens("charged at -78 C") == {"charged", "at", "78", "c"}
    assert normalize_search_text("held at -78 C") == "held at  78 C"


def test_a_word_exclusion_survives_the_normalisation() -> None:
    """`-solvent` is still an exclusion; only a hyphen before a *digit* is a sign.

    The two readings of `-` have to coexist, and this is the line between them. A reference that
    read `-solvent` as a request for solvent is the defect `reference_terms` was written against.
    """
    wanted, excluded = reference_terms("amide coupling -solvent")
    assert wanted == {"amide", "coupling"}
    assert excluded == {"solvent"}


def test_a_term_both_wanted_and_excluded_is_excluded() -> None:
    """Mirrors the durable `('amid') & !'amid'`, which matches nothing."""
    assert reference_terms("amide -amide") == (set(), {"amide"})


def test_a_query_of_no_real_terms_asks_for_nothing() -> None:
    """Both sets empty means "no query" — it must not read as "the whole corpus"."""
    assert reference_terms("!!! ___ +++") == (set(), set())
    assert reference_terms("") == (set(), set())


def test_both_backends_return_the_same_hits_over_a_chemistry_corpus() -> None:
    """The invariant the module exists for, asserted the only way it can be: by asking both.

    `InMemoryNoteIndex` is the reference every other lexical test stands on and `PostgresNoteIndex`
    is what production runs. The module is explicitly allowed to differ on *scores* and on stop
    words; it is not allowed to differ on **which rows are hits**. Every historical occurrence of
    that divergence was invisible for the same reason — no test ever asked the two the same
    question — and the numeric one was found by writing this.
    """
    _run(migrated_db_or_skip())
    records = [
        NoteRecord(note_id=note_id, text=text, embedding=[0.0] * settings.embedding_dim)
        for note_id, text in CHEMISTRY_CORPUS.items()
    ]
    memory = InMemoryNoteIndex()
    durable = PostgresNoteIndex()
    _run(memory.upsert(records, "probe"))
    _run(durable.upsert(records, "probe"))
    scope = set(CHEMISTRY_CORPUS)

    disagreements: list[str] = []
    for query in CHEMISTRY_QUERIES:
        in_memory = {h.note_id for h in _run(memory.search_lexical(query, 50, within=scope))}
        in_postgres = {h.note_id for h in _run(durable.search_lexical(query, 50, within=scope))}
        if in_memory != in_postgres:
            disagreements.append(
                f"{query!r}: in-memory {sorted(in_memory)} vs postgres {sorted(in_postgres)}"
            )
    assert not disagreements, "the two backends answer different questions:\n" + "\n".join(
        disagreements
    )


def test_a_cryogenic_temperature_is_findable_by_its_magnitude() -> None:
    """The end-to-end form of the defect, against the durable backend rather than the reference.

    Worth its own case beside the differential test above, because the two backends *agreeing* is
    not the same property as either of them being right: before this change both could have been
    made to agree by breaking the reference instead, and a chemist still could not have found the
    run held at -78 °C.
    """
    _run(migrated_db_or_skip())
    records = [
        NoteRecord(note_id=note_id, text=text, embedding=[0.0] * settings.embedding_dim)
        for note_id, text in CHEMISTRY_CORPUS.items()
    ]
    durable = PostgresNoteIndex()
    _run(durable.upsert(records, "probe"))
    scope = set(CHEMISTRY_CORPUS)

    hits = {h.note_id for h in _run(durable.search_lexical("78", 50, within=scope))}
    assert "n-cryo" in hits

    # And the identifier lookup that already worked still does: it survives only because the query
    # and the document fragment identically, so normalising one side alone would have broken it.
    cas = {h.note_id for h in _run(durable.search_lexical("108-24-7", 50, within=scope))}
    assert cas == {"n-cas"}
