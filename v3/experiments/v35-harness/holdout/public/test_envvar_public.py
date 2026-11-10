"""Public reproduction test for the sequence environment variable hint."""

from src.click import Option


def test_error_hint_lists_each_sequence_envvar() -> None:
    option = Option(
        ["--name"],
        envvar=("APP_NAME", "LEGACY_NAME"),
        show_envvar=True,
    )
    hint = option.get_error_hint(None)
    assert "'APP_NAME', 'LEGACY_NAME'" in hint
    assert "('APP_NAME'" not in hint
