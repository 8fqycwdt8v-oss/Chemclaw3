"""The offline lexical proxy, pinned against the exact regression its own comment names.

`core/fulltext.py`'s module comment records that `_WORD` used to be spelled `[a-z0-9]+` twice, in
the two backends this module now unifies, and that the ASCII-only spelling silently dropped every
non-ASCII letter Postgres indexes. It is also, independently, wrong on plain ASCII: `[a-z0-9]+` is
case-sensitive, so `Suzuki` tokenises as `uzuki` rather than `suzuki` — the capital `S` is not in
the class and breaks the run. `test_case_is_folded_after_tokenising_not_by_the_character_class`
below is that exact bug, written as an assertion rather than as a comment: it fails the moment
`_WORD` reverts to `[a-z0-9]+`, which nothing else in this suite reaches (`reference_tokens` and
`reference_terms` appear in no other test file).
"""

from chemclaw.core.fulltext import reference_terms, reference_tokens


def test_case_is_folded_after_tokenising_not_by_the_character_class() -> None:
    r"""`Suzuki` must tokenise whole and lowercased — not as `uzuki` with the `S` dropped.

    This is the mutant the backlog row names: mutate `_WORD` from `[^\W_]+` to the ASCII-only
    `[a-z0-9]+` and this reproduces the bug the module's own comment describes, because a capital
    letter is outside `[a-z0-9]` and breaks the run instead of being folded into it.
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
