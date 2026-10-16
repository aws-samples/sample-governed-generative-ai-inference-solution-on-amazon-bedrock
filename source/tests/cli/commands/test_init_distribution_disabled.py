# ABOUTME: Regression test: the wizard's "Disabled" distribution choice must disable distribution
# ABOUTME: questionary replaces Choice(value=None) with its title, which enabled presigned-S3 distribution

"""Regression tests for the Distribution method prompt.

`questionary.Choice("Disabled", value=None)` returns the title "Disabled", so
`distribution_type is not None` was True for every choice and `gip deploy`
created the presigned-S3 distribution stack (IAM user, access key, buckets).
"""

import pytest

from governed_inference_platform.cli.commands.init import (
    DISTRIBUTION_DISABLED,
    normalize_distribution_type,
)
from governed_inference_platform.config import Profile


def test_questionary_none_value_becomes_title():
    """Document the library behaviour that caused the bug."""
    questionary = pytest.importorskip("questionary")
    assert questionary.Choice("Disabled", value=None).value == "Disabled"


@pytest.mark.parametrize("value", [None, DISTRIBUTION_DISABLED, "Disabled"])
def test_disabled_choice_normalizes_to_none(value):
    assert normalize_distribution_type(value) is None


@pytest.mark.parametrize("value", ["presigned-s3", "landing-page"])
def test_real_choices_pass_through(value):
    assert normalize_distribution_type(value) == value


def test_profile_written_by_buggy_wizard_loads_disabled():
    """A profile saved with distribution_type "Disabled" must not deploy distribution."""
    profile = Profile.from_dict(
        {
            "name": "p",
            "provider_domain": "",
            "client_id": "",
            "aws_region": "us-east-1",
            "identity_pool_name": "gip-auth",
            "enable_distribution": True,
            "distribution_type": "Disabled",
        }
    )
    assert profile.enable_distribution is False
    assert profile.distribution_type is None
