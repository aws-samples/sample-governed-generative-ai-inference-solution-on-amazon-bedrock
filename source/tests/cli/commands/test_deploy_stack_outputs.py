# ABOUTME: `gip deploy`'s output summary must not query stacks the mode never deploys
# ABOUTME: (sidecar monitoring has no networking or central collector stack).

"""Regression from an end-to-end test (2026-10-26): in sidecar mode `gip deploy` printed
"Error getting stack outputs ... Stack with id gip-auth-networking does not exist" and the same for
the monitoring stack, because the summary queried stacks that sidecar mode skips."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.commands import deploy as deploy_module
from governed_inference_platform.cli.commands.deploy import DeployCommand


def _profile(monitoring_mode: str, enable_distribution: bool = False):
    return SimpleNamespace(
        stack_names={},
        identity_pool_name="gip-auth",
        aws_region="us-east-1",
        monitoring_enabled=True,
        monitoring_mode=monitoring_mode,
        enable_distribution=enable_distribution,
        quota_monitoring_enabled=False,
    )


def _queried_stacks(profile) -> list[str]:
    queried = []

    def fake_outputs(stack_name, region):
        queried.append(stack_name)
        return {}

    with patch.object(deploy_module, "get_stack_outputs", side_effect=fake_outputs):
        DeployCommand()._show_stack_outputs(profile, MagicMock(), MagicMock())
    return queried


@pytest.mark.parametrize(
    ("mode", "distribution", "networking", "collector"),
    [
        ("sidecar", False, False, False),
        ("sidecar", True, True, False),  # distribution can deploy networking in sidecar mode
        ("central", False, True, True),
    ],
)
def test_summary_queries_only_deployed_stacks(mode, distribution, networking, collector):
    queried = _queried_stacks(_profile(mode, distribution))

    assert ("gip-auth-networking" in queried) is networking
    assert ("gip-auth-otel-collector" in queried) is collector
    assert "gip-auth-dashboard" in queried
