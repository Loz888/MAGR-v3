"""Independent regression test for the upstream short-help fix."""

from click.utils import _make_default_short_help as short_help


def test_period_after_abbreviation_does_not_end_sentence() -> None:
    assert short_help("Weigh apples vs. pears.", 30) == "Weigh apples vs. pears."


def test_lowercase_word_after_period_is_not_a_boundary() -> None:
    assert short_help("123 567 9. aaaa bbb", 10) == "123 567..."


def test_hyphenated_word_is_shortened() -> None:
    assert short_help("well-behaved.", 8) == "..."


def test_trailing_period_is_preserved() -> None:
    assert short_help("123 567 9.", 10) == "123 567 9."
