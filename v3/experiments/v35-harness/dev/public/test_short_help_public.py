"""Public tests for the configurable short-help placeholder feature."""

from src.click.utils import _make_default_short_help as short_help


def test_period_after_abbreviation_does_not_end_sentence() -> None:
    assert short_help("Weigh apples vs. pears.", 30) == "Weigh apples vs. pears."


def test_custom_placeholder_is_used_when_shortening() -> None:
    assert short_help("Weigh apples vs. pears please", 20, placeholder="\u2026") == "Weigh apples vs.\u2026"
