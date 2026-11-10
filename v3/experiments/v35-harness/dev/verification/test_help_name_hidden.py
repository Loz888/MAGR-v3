"""Independent hidden verification for the help-option name collision."""

from src.click import Context, argument, command
from src.click.core import _HELP_OPTION_STORAGE_NAME
from src.click.testing import CliRunner


def test_default_help_option_uses_reserved_storage_name() -> None:
    @command()
    def cli() -> None:
        pass

    option = cli.get_help_option(Context(cli))
    assert option is not None
    assert option.name == _HELP_OPTION_STORAGE_NAME


def test_help_option_still_renders_help() -> None:
    @command()
    @argument("help")
    def cli(help: str) -> None:
        pass

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
