"""Independent hidden verification for the short-help placeholder feature."""

from src.click import Command
from src.click.utils import _make_default_short_help as short_help


def test_command_reads_placeholder_from_context_settings() -> None:
    command = Command(
        "demo",
        help="Weigh apples vs. pears please",
        context_settings={"short_help_placeholder": "\u2026"},
    )
    assert command.get_short_help_str(20) == "Weigh apples vs.\u2026"


def test_default_placeholder_stays_three_dots() -> None:
    command = Command("demo", help="Weigh apples vs. pears please")
    assert command.get_short_help_str(12) == "Weigh..."


def test_placeholder_returned_when_width_is_too_small() -> None:
    assert short_help("Weigh apples", 2, placeholder="\u2026") == "\u2026"


def test_lowercase_word_after_period_is_not_a_boundary() -> None:
    assert short_help("123 567 9. aaaa bbb", 10) == "123 567..."
