"""The offline lexical proxy agrees with Postgres full-text search.

Two kinds of test: offline cases pin the tokeniser against mutation (e.g. an ASCII-only `_WORD`
that splits `Suzuki` into `uzuki`), and a Postgres-backed differential test drives both backends
over one chemistry corpus and asserts the same hit sets, which is the only shape that catches a
divergence such as signed or decimal numbers.
"""

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

    No `pytest.mark.asyncio`: `pyproject.toml` sets `--strict-markers` and registers none, so
    an async test would be collected and never awaited.
    """
    return asyncio.run(awaitable)


def test_case_is_folded_after_tokenising_not_by_the_character_class() -> None:
    r"""`Suzuki` tokenises whole and lowercased, not as `uzuki`.

    Fails if `_WORD` is mutated from `[^\W_]+` to `[a-z0-9]+`, since a capital letter would break
    the run instead of being folded.
    """
    assert reference_tokens("Suzuki coupling") == {"suzuki", "coupling"}


def test_non_ascii_letters_are_indexed_not_dropped() -> None:
    r"""`café` and `naïve` tokenise whole — the failure the module comment says motivated `\W`.

    An ASCII-only class (`[a-zA-Z0-9]+`) would split these on the accented letter; Postgres's
    `to_tsvector` does not, so the offline proxy must not either.
    """
    assert reference_tokens("café naïve") == {"café", "naïve"}


def test_tokens_are_lowercased() -> None:
    """A reader who types `Suzuki` must match a corpus written `suzuki`."""
    assert reference_tokens("SUZUKI Amide") == {"suzuki", "amide"}


def test_punctuation_and_underscores_split_tokens() -> None:
    r"""`_` is excluded by `[^\W_]`, and non-word punctuation always splits."""
    assert reference_tokens("amide_coupling, tert-butyl!") == {
        "amide",
        "coupling",
        "tert",
        "butyl",
    }


def test_digits_are_tokens_too() -> None:
    """A structure id or a CAS-like number is indexed the same way a word is."""
    assert reference_tokens("compound 4056 route B") == {"compound", "4056", "route", "b"}


def test_empty_and_symbol_only_text_yields_no_tokens() -> None:
    """No word characters at all — not an error, just nothing to index or query."""
    assert reference_tokens("") == set()
    assert reference_tokens("--- ...") == set()


def test_reference_terms_splits_wanted_from_excluded() -> None:
    """A leading `-` on a word marks it wanted-out, exactly as `websearch_to_tsquery` reads it."""
    wanted, excluded = reference_terms("amide coupling -solvent")
    assert wanted == {"amide", "coupling"}
    assert excluded == {"solvent"}


def test_a_hyphenated_name_is_not_read_as_an_exclusion() -> None:
    """`tert-butyl` is one word with a hyphen inside it, not `-butyl` excluding `butyl`.

    `_EXCLUSION` is anchored on the leading `-` of a whitespace-delimited word; a hyphen in the
    middle of a word never triggers it.
    """
    wanted, excluded = reference_terms("tert-butyl amide")
    assert wanted == {"tert", "butyl", "amide"}
    assert excluded == set()


def test_a_token_both_wanted_and_excluded_is_excluded() -> None:
    """`amide -amide` must exclude, matching the durable `('amid') & !'amid'` reading."""
    wanted, excluded = reference_terms("amide -amide")
    assert wanted == set()
    assert excluded == {"amide"}


def test_a_multi_word_exclusion_excludes_every_word_in_it() -> None:
    """`-solvent selection` is one exclusion word plus a positive word, not two exclusions."""
    wanted, excluded = reference_terms("amide -solvent selection")
    assert wanted == {"amide", "selection"}
    assert excluded == {"solvent"}


def test_a_stop_word_or_symbol_only_query_yields_two_empty_sets() -> None:
    """No real terms in the query at all — the caller must return nothing, not the whole corpus."""
    wanted, excluded = reference_terms("--- ...")
    assert wanted == set()
    assert excluded == set()


def test_reference_terms_lowercases_and_dedupes_across_words() -> None:
    """The two halves of `reference_terms` share `reference_tokens`, case-fold included."""
    wanted, excluded = reference_terms("Suzuki suzuki -Solvent")
    assert wanted == {"suzuki"}
    assert excluded == {"solvent"}


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


def test_a_decimal_is_one_token_because_postgres_emits_a_float_whole() -> None:
    """`98.5` is one token, because Postgres emits a float whole.

    Splitting it would make the offline reference match `98`, which the durable backend does not.
    """
    assert reference_tokens("gave 98.5% ee") == {"gave", "98.5", "ee"}
    assert "98" not in reference_tokens("98.5")


def test_a_sign_is_detached_from_the_number_it_precedes() -> None:
    """A sign is detached from the number it precedes, so `-78 C` is indexed by its magnitude.

    Postgres keeps the sign on the lexeme and `websearch_to_tsquery` reads a leading `-` as
    exclusion, so otherwise no query reaches the row.
    """
    assert reference_tokens("charged at -78 C") == {"charged", "at", "78", "c"}
    assert normalize_search_text("held at -78 C") == "held at  78 C"


def test_both_backends_return_the_same_hits_over_a_chemistry_corpus() -> None:
    """Both backends return the same hits over a chemistry corpus.

    `InMemoryNoteIndex` is the reference other lexical tests rely on and `PostgresNoteIndex` is
    production. They may differ on scores and stop words, never on which rows are hits.
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
    """A cryogenic temperature is findable by its magnitude in the durable backend.

    The backends agreeing is not the same as either being right, so the outcome is asserted
    directly.
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
