"""Independent regression test for the upstream help-option name collision fix."""

import click
from click.testing import CliRunner


def test_argument_named_help_keeps_its_value() -> None:
    @click.command()
    @click.argument("help")
    def cli(help: str) -> None:
        click.echo(help)

    result = CliRunner().invoke(cli, ["value"])
    assert result.exit_code == 0, result.output
    assert result.output == "value\n"


def test_default_help_option_uses_reserved_storage_name() -> None:
    @click.command()
    def cli() -> None:
        pass

    option = cli.get_help_option(click.Context(cli))
    assert option is not None
    assert option.name == "_click_default_help"
