"""Independent hidden verification for sequence and empty environment variables."""

from src.click import Context, Option, command


def test_empty_sequence_falls_back_to_auto_envvar() -> None:
    @command()
    def cli() -> None:
        pass

    context = Context(cli, auto_envvar_prefix="MYAPP")
    option = Option(["--name"], envvar=(), show_envvar=True)
    extended = option.get_help_extra(context)
    assert extended["envvars"] == ("MYAPP_NAME",)


def test_single_string_stays_quoted() -> None:
    option = Option(["--name"], envvar="APP_NAME", show_envvar=True)
    assert option.get_error_hint(None).endswith("(env var: 'APP_NAME')")


def test_envvar_with_space_and_comma_is_quoted_individually() -> None:
    option = Option(
        ["--name"],
        envvar=("APP NAME", "LEGACY,NAME"),
        show_envvar=True,
    )
    hint = option.get_error_hint(None)
    assert "'APP NAME', 'LEGACY,NAME'" in hint


def test_empty_envvar_hides_hint() -> None:
    option = Option(["--name"], envvar="", show_envvar=True)
    assert "env var" not in option.get_error_hint(None)
