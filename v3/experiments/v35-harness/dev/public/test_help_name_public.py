"""Public reproduction test for the help-option name collision."""

from src.click import argument, command, echo
from src.click.testing import CliRunner


def test_argument_named_help_keeps_its_value() -> None:
    @command()
    @argument("help")
    def cli(help: str) -> None:
        echo(help)

    result = CliRunner().invoke(cli, ["value"])
    assert result.exit_code == 0, result.output
    assert result.output == "value\n"
