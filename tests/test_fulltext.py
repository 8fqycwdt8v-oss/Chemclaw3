"""The one lexical rule, tested directly — because nothing tested it directly.

`chemclaw.core.fulltext` exists for exactly one purpose: to stop the offline reference backends
answering a different question from the Postgres statements they stand in for. Every version of
that rule written twice has drifted, twice invisibly, and the module docstring says so.

**No test file imported it.** Measured: mutating `_WORD` from its Unicode word class to the
ASCII `[a-z0-9]+` — the exact bug its own comment names, the alphabet the two backends used to
spell separately — left every lexical test in this suite passing, and `coverage --cov-branch`
reported **100% line and 100% branch** over the module while it was mutated. Every
`search_lexical` fixture in this suite queries lowercase ASCII, so no assertion anywhere could see
the difference — a coverage number measuring execution rather than checking, on the module whose
whole job is a check.

What the mutant does, measured:

    tokens('HPLC purity')      ['hplc', 'purity']   -> ['purity']
    terms('HPLC')              ({'hplc'}, set())    -> (set(), set())
    tokens('Übergangsmetall')  ['übergangsmetall']  -> ['bergangsmetall']

An all-caps acronym is most of what a process chemist types — HPLC, GC, NMR, THF, DoE — and under
the mutant `HPLC` is not a query at all: `reference_terms` returns two empty sets, which the
reference reads as "no query" and answers with nothing, while Postgres indexes `hplc` and answers
normally. This is a *reference-fidelity* regression rather than a production one, both callers
being in-memory references — and a reference that answers a different question from the backend is
the one failure this module was written to prevent.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.fulltext import reference_terms, reference_tokens
from chemclaw.retrieval.vector_index import (
    InMemoryNoteIndex,
    NoteRecord,
    PostgresNoteIndex,
    note_embedding_key,
)
from tests.pg import migrated_db_or_skip


def test_an_all_caps_acronym_is_a_term() -> None:
    """`HPLC`, `NMR`, `THF` — what a process chemist actually types, and what the mutant loses."""
    assert reference_tokens("HPLC purity") == {"hplc", "purity"}
    assert reference_terms("HPLC") == ({"hplc"}, set())
    assert reference_terms("-HPLC") == (set(), {"hplc"}), "and it is still excludable"


def test_a_non_ascii_letter_is_part_of_its_word() -> None:
    """Postgres indexes it, so the reference must tokenize it — not bite the first letter off.

    `[a-z0-9]+` does not fail loudly on `Übergangsmetall`: it returns `bergangsmetall`, a token that
    matches nothing and looks like a word. The `re.UNICODE` alphabet is what keeps the reference's
    idea of a word the same as `to_tsvector`'s.
    """
    assert reference_tokens("Übergangsmetall") == {"übergangsmetall"}
    assert reference_tokens("Grignard Reagenz für Kupplung") == {
        "grignard",
        "reagenz",
        "für",
        "kupplung",
    }


def test_a_hyphenated_name_is_two_tokens_and_not_an_exclusion() -> None:
    """`tert-butyl` is a substituent, not "butyl excluded" — the `-` rule is leading-only.

    The companion property to the two above: `_EXCLUSION` is anchored on a whitespace-delimited
    word, so the alphabet change and the exclusion rule cannot be confused for one another.
    """
    assert reference_terms("tert-butyl") == ({"tert", "butyl"}, set())


def test_a_digit_suffixed_token_survives_whole() -> None:
    """`2H`, `13C`, `Pd2(dba)3` — the mutant splits these where Postgres does not."""
    assert reference_tokens("2H NMR") == {"2h", "nmr"}


def test_the_reference_and_postgres_agree_on_an_all_caps_query(tmp_path: Path) -> None:
    """The parity this module exists for, on the query shape the mutant breaks.

    The two other tests in this file pin the tokenizer. This one pins the thing the tokenizer is
    *for*: `InMemoryNoteIndex.search_lexical` is the reference every unit test in this repository
    stands on, and it must return the same note as the statement it stands in for. Under the mutant
    the in-memory backend returned `[]` for `HPLC` while Postgres returned the note — a reference
    silently answering a different question, which is the failure the module docstring records
    happening twice already.

    Skips without a database, and says so, because half of this assertion is the database.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        from chemclaw.core import db
        from chemclaw.core.embeddings import embed_texts

        text = "HPLC purity of the isolated solid was 99.2%."
        record = NoteRecord(
            note_id="reaction-hplc",
            text=text,
            embedding=embed_texts([text])[0],
            fingerprint="fp-hplc",
        )
        memory = InMemoryNoteIndex()
        await memory.upsert([record], note_embedding_key())

        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM note_index")
            await conn.commit()
        durable = PostgresNoteIndex()
        await durable.upsert([record], note_embedding_key())

        in_memory = [hit.note_id for hit in await memory.search_lexical("HPLC", 5)]
        in_postgres = [hit.note_id for hit in await durable.search_lexical("HPLC", 5)]

        assert in_postgres == ["reaction-hplc"], "sanity: Postgres indexes an acronym"
        assert in_memory == in_postgres, (
            f"the offline reference answers a different question from the backend it stands in "
            f"for: {in_memory} against {in_postgres}"
        )

    asyncio.run(_run())


@pytest.mark.parametrize("query", ["HPLC", "Übergangsmetall", "2H"])
def test_every_query_this_file_pins_is_a_query_at_all(query: str) -> None:
    """A term that tokenizes to nothing reads as "no query" and returns the whole corpus or none.

    The cheapest expression of the defect: `reference_terms` returning two empty sets is how both
    reference backends decide there is nothing to search for, so a tokenizer that drops a word
    turns a real question into a non-question rather than into a wrong answer.
    """
    wanted, _ = reference_terms(query)
    assert wanted, f"{query!r} tokenized to nothing, so the reference would not search for it"
