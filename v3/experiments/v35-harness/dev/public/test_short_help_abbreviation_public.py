"""Public reproduction test for the short-help abbreviation regression."""

from src.click.utils import _make_default_short_help as short_help


def test_period_after_abbreviation_does_not_end_sentence() -> None:
    assert short_help("Weigh apples vs. pears.", 30) == "Weigh apples vs. pears."
