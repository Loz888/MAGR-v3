"""Independent regression test for the upstream Sentinel identity fix."""

import copy
import pickle

import click
from click._utils import UNSET


def test_copy_preserves_sentinel_identity() -> None:
    assert copy.copy(UNSET) is UNSET
    assert copy.deepcopy(UNSET) is UNSET


def test_pickle_preserves_sentinel_identity() -> None:
    assert pickle.loads(pickle.dumps(UNSET)) is UNSET


def test_option_copy_keeps_sentinel_default() -> None:
    option = click.Option(["--name"])
    duplicate = copy.copy(option)
    assert option.default is UNSET
    assert duplicate.default is UNSET
