"""Independent hidden verification for abbreviation-aware short help."""

from src.click.utils import _make_default_short_help as short_help


def test_abbreviation_mid_sentence_keeps_later_words() -> None:
    assert short_help("Weigh apples vs. pears please", 25) == "Weigh apples vs. pears..."


def test_abbreviation_does_not_hide_a_later_sentence() -> None:
    assert short_help("Use e.g. this value. Then stop.", 30) == "Use e.g. this value."
