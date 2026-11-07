# ABOUTME: Tests that `gip deploy auth` sets the Direct STS role's MaxSessionDuration from the profile
# ABOUTME: Covers the value mapping, the pre-deploy range check, the lowering warning and both param builders

"""The OIDC auth templates' MaxSessionDuration parameter follows the profile.

`max_session_duration` used to change only what credential-process requests; the
role kept a fixed 43200, so a modified client could still get 12 hours. `gip
deploy auth` now passes the profile's value to the Direct STS role:

- a profile value in 3600-43200 is passed as-is;
- a profile without a value (None or 0) gets 43200, the role's previous value;
- anything else fails before CloudFormation is called, with a message naming the fix;
- cognito federation creates no Direct STS role, so the parameter is not passed;
- lowering an existing stack's value prints a warning about installed clients.
"""

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError

from governed_inference_platform.cli.commands import deploy as deploy_mod
from governed_inference_platform.cli.commands.deploy import DeployCommand
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.config import Profile

INFRA_DIR = Path(__file__).resolve().parents[4] / "deployment" / "infrastructure"

PROVIDER_PROFILES = {
    "okta": {"provider_domain": "company.okta.com", "client_id": "0oa0000000000000000"},
    "auth0": {"provider_domain": "company.auth0.com", "client_id": "auth0-client-id-0000"},
    "azure": {
        "provider_domain": "login.microsoftonline.com/12345678-1234-1234-1234-123456789012/v2.0",
        "client_id": "12345678-1234-1234-1234-123456789012",
    },
    "google": {"provider_domain": "accounts.google.com", "client_id": "1234-abcd.apps.googleusercontent.com"},
    "cognito": {
        "provider_domain": "my-pool.auth.us-east-1.amazoncognito.com",
        "client_id": "cognitoclientid000000",
        "cognito_user_pool_id": "us-east-1_AbCdEfGhI",
    },
    "generic": {
        "provider_domain": "auth.example.com",
        "client_id": "bedrock-cli-prod",
        "oidc_issuer_url": "https://auth.example.com",
    },
}
TEMPLATES = {
    "okta": "bedrock-auth-okta.yaml",
    "auth0": "bedrock-auth-auth0.yaml",
    "azure": "bedrock-auth-azure.yaml",
    "google": "bedrock-auth-google.yaml",
    "cognito": "bedrock-auth-cognito-pool.yaml",
    "generic": "bedrock-auth-generic.yaml",
}
ALL_PROVIDERS = sorted(PROVIDER_PROFILES)


def _make_profile(provider_type="okta", **overrides):
    field_names = {f.name for f in dataclasses.fields(Profile)}
    values = {
        "name": f"{provider_type}-test",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "sso_enabled": True,
        "provider_type": provider_type,
        "monitoring_enabled": False,
        "federation_type": "direct",
        "max_session_duration": 43200,
    }
    values.update(PROVIDER_PROFILES[provider_type])
    values.update(overrides)
    return Profile(**{k: v for k, v in values.items() if k in field_names})


def _deploy_auth(profile, stack_parameters=None):
    """Run the auth deploy against a mocked CloudFormation manager; return (rc, params, printed)."""
    manager = MagicMock(spec=CloudFormationManager)
    manager.region = profile.aws_region
    result = MagicMock()
    result.success = True
    result.outputs = {}
    manager.deploy_stack.return_value = result
    manager.get_stack_resource_physical_ids.return_value = []  # new stack: no thumbprint lookup
    manager.get_stack_parameters.return_value = dict(stack_parameters or {})

    console = MagicMock()
    with patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager", return_value=manager):
        rc = DeployCommand()._deploy_stack("auth", profile, console, manager)

    call = manager.deploy_stack.call_args
    params = None
    if call is not None:
        params = {p["ParameterKey"]: p["ParameterValue"] for p in call.kwargs["parameters"]}
    printed = " ".join(str(c) for c in console.print.call_args_list)
    return rc, params, printed


class TestRoleMaxSessionDurationValue:
    @pytest.mark.parametrize("value", [3600, 14400, 28800, 43200])
    def test_valid_profile_value_is_used(self, value):
        assert deploy_mod.role_max_session_duration(_make_profile(max_session_duration=value)) == value

    @pytest.mark.parametrize("value", [None, 0])
    def test_profile_without_a_value_keeps_43200(self, value):
        assert deploy_mod.role_max_session_duration(_make_profile(max_session_duration=value)) == 43200

    def test_dataclass_default_is_passed_through(self):
        """A profile file without the key loads the dataclass default (28800), which is also
        what `gip package` writes into config.json, so the role matches what clients request."""
        legacy = {
            "name": "legacy",
            "provider_domain": "company.okta.com",
            "client_id": "0oa0000000000000000",
            "credential_storage": "session",
            "aws_region": "us-east-1",
            "identity_pool_name": "gip-test",
            "federation_type": "direct",
        }
        profile = Profile.from_dict(legacy)
        assert profile.max_session_duration == 28800
        assert deploy_mod.role_max_session_duration(profile) == 28800

    @pytest.mark.parametrize("value", [99, 3599, 43201, 86400, -1, "3600", 3600.0, True])
    def test_invalid_value_raises_with_the_fix(self, value):
        with pytest.raises(ValueError) as excinfo:
            deploy_mod.role_max_session_duration(_make_profile(max_session_duration=value))
        message = str(excinfo.value)
        assert repr(value) in message
        assert "3600" in message and "43200" in message
        assert "gip init" in message
        assert "~/.gip/profiles/okta-test.json" in message
        assert "gip deploy auth" in message


class TestDeployAuthPassesTheProfileValue:
    @pytest.mark.parametrize("provider_type", ALL_PROVIDERS)
    def test_direct_federation_passes_profile_value(self, provider_type):
        rc, params, printed = _deploy_auth(_make_profile(provider_type, max_session_duration=3600))
        assert rc == 0, printed
        assert params["MaxSessionDuration"] == "3600"

    @pytest.mark.parametrize("provider_type", ALL_PROVIDERS)
    def test_every_passed_parameter_exists_in_the_template(self, provider_type):
        """CloudFormation rejects unknown parameters, so the builder must only pass declared ones."""
        _, params, _ = _deploy_auth(_make_profile(provider_type, max_session_duration=7200))
        loader = type("CFLoader", (yaml.SafeLoader,), {})
        loader.add_multi_constructor("!", lambda loader_, suffix, node: None)
        template = yaml.load((INFRA_DIR / TEMPLATES[provider_type]).read_text(encoding="utf-8"), Loader=loader)  # nosec B506
        assert set(params) <= set(template["Parameters"]), sorted(set(params) - set(template["Parameters"]))

    def test_profile_without_value_deploys_43200(self):
        rc, params, printed = _deploy_auth(_make_profile(max_session_duration=None))
        assert rc == 0, printed
        assert params["MaxSessionDuration"] == "43200"

    def test_cognito_federation_does_not_pass_the_parameter(self):
        rc, params, printed = _deploy_auth(_make_profile(federation_type="cognito", max_session_duration=99))
        assert rc == 0, printed
        assert "MaxSessionDuration" not in params

    @pytest.mark.parametrize("value", [99, 43201])
    def test_out_of_range_value_fails_before_cloudformation(self, value):
        rc, params, printed = _deploy_auth(_make_profile(max_session_duration=value))
        assert rc == 1
        assert params is None, "deploy_stack must not be called"
        assert "3600-43200" in printed and "gip deploy auth" in printed

    def test_idc_template_gets_no_max_session_duration(self):
        profile = _make_profile(auth_type="idc", sso_enabled=False, max_session_duration=3600)
        rc, params, printed = _deploy_auth(profile)
        assert rc == 0, printed
        assert "MaxSessionDuration" not in params


class TestLoweringWarning:
    def test_lowering_an_existing_stack_warns(self):
        rc, params, printed = _deploy_auth(
            _make_profile(max_session_duration=3600),
            stack_parameters={"FederationType": "direct", "MaxSessionDuration": "43200"},
        )
        assert rc == 0
        assert params["MaxSessionDuration"] == "3600"
        assert "from 43200s to 3600s" in printed
        assert "gip package" in printed

    def test_stack_created_before_the_parameter_counts_as_43200(self):
        _, _, printed = _deploy_auth(
            _make_profile(max_session_duration=28800), stack_parameters={"FederationType": "direct"}
        )
        assert "from 43200s to 28800s" in printed

    @pytest.mark.parametrize(
        "stack_parameters",
        [
            {},  # new stack
            {"FederationType": "direct", "MaxSessionDuration": "3600"},  # unchanged
            {"FederationType": "direct", "MaxSessionDuration": "1800"},  # raised
            {"FederationType": "cognito"},  # no Direct STS role existed
        ],
    )
    def test_no_warning_otherwise(self, stack_parameters):
        _, _, printed = _deploy_auth(_make_profile(max_session_duration=3600), stack_parameters=stack_parameters)
        assert "Lowering" not in printed

    @pytest.mark.parametrize(
        "error",
        [
            ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeStacks"),
            EndpointConnectionError(endpoint_url="https://cloudformation.us-east-1.amazonaws.com"),
        ],
    )
    def test_unreadable_stack_parameters_give_no_warning(self, error):
        manager = MagicMock(spec=CloudFormationManager)
        manager.get_stack_parameters.side_effect = error
        assert deploy_mod._role_session_shrink_warning(manager, "gip-auth", 3600) is None


class TestShowCommands:
    def _commands(self, profile):
        console = MagicMock()
        rc = DeployCommand()._show_deployment_commands("auth", profile, console)
        return rc, " ".join(str(c) for c in console.print.call_args_list)

    def test_show_commands_includes_profile_value(self):
        rc, printed = self._commands(_make_profile(max_session_duration=14400))
        assert rc == 0
        assert "MaxSessionDuration=14400" in printed

    def test_show_commands_omits_it_for_cognito_federation(self):
        rc, printed = self._commands(_make_profile(federation_type="cognito"))
        assert rc == 0
        assert "MaxSessionDuration" not in printed

    def test_show_commands_reports_an_invalid_value(self):
        rc, printed = self._commands(_make_profile(max_session_duration=99))
        assert rc == 1
        assert "3600-43200" in printed
        assert "aws cloudformation deploy" not in printed

    def test_show_all_commands_returns_failure_for_an_invalid_value(self):
        console = MagicMock()
        stacks = [("auth", "Authentication Stack"), ("auth", "Authentication Stack")]
        assert (
            DeployCommand()._show_all_deployment_commands(stacks, _make_profile(max_session_duration=99), console) == 1
        )
        assert DeployCommand()._show_all_deployment_commands(stacks, _make_profile(), console) == 0
