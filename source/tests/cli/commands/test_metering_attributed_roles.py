# ABOUTME: Tests for the AttributedRoles allow-list that gip deploy metering passes to quota-metering.yaml
# ABOUTME: Covers role discovery per auth type, operator-named roles, validation, and the template contract

"""`gip deploy metering` tells the processor which roles' sessions name a user.

The processor maps an assumed-role session name to a user only for roles in
ATTRIBUTED_ROLES. `gip deploy` builds the list from the auth stack's federated
role (OIDC), every IAM Identity Center role (AWSReservedSSO_*), and the
profile's metering_attributed_roles, and refuses to deploy
metering when it cannot name any role.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError

from governed_inference_platform.cli.commands.deploy import (
    DeployCommand,
    attributed_role_warnings,
    metering_attributed_roles,
    read_stack,
)
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.config import Profile
from tests.test_otlp_and_bearer_policy import CFLoader

METERING_TEMPLATE = Path(__file__).resolve().parents[4] / "deployment" / "infrastructure" / "quota-metering.yaml"
BASE = {
    "name": "metered",
    "provider_domain": "company.okta.com",
    "client_id": "0oa0000000000000000",
    "credential_storage": "session",
    "aws_region": "us-east-1",
    "identity_pool_name": "gip-test",
    "federation_type": "direct",
    "session_name_binding": "email",
    "quota_monitoring_enabled": True,
    "metering_enabled": True,
    "allowed_bedrock_regions": ["us-east-1", "us-west-2"],
}
DIRECT_ARN = "arn:aws:iam::123456789012:role/BedrockOktaFederatedRole"


def _profile(**overrides) -> Profile:
    return Profile.from_dict({**BASE, **overrides})


class TestHelper:
    def test_idc_attributes_every_identity_center_role(self):
        """IAM Identity Center sets the session name on every role it creates, whatever the permission set."""
        profile = _profile(auth_type="idc", idc_permission_set_name="BedrockDev")
        assert metering_attributed_roles(profile, None) == ("AWSReservedSSO_*", None)

    def test_idc_without_a_permission_set_name(self):
        assert metering_attributed_roles(_profile(auth_type="idc"), None) == ("AWSReservedSSO_*", None)

    def test_identity_center_prefix_prints_no_warning(self):
        assert attributed_role_warnings("AWSReservedSSO_*") == []

    def test_oidc_uses_the_auth_stack_role(self):
        outputs = {"DirectSTSRoleArn": "arn:aws:iam::123456789012:role/team/path/BedrockOktaFederatedRole"}
        assert metering_attributed_roles(_profile(), outputs) == ("BedrockOktaFederatedRole", None)

    def test_cognito_federation_uses_the_federated_role_output(self):
        outputs = {"FederatedRoleArn": "arn:aws:iam::123456789012:role/gip-CognitoAuthRole"}
        profile = _profile(federation_type="cognito")
        assert metering_attributed_roles(profile, outputs) == ("gip-CognitoAuthRole", None)

    def test_profile_role_arn_alone_is_not_enough(self):
        """The saved ARN can be stale; for OIDC only the auth stack names the role."""
        value, error = metering_attributed_roles(_profile(federated_role_arn=DIRECT_ARN), {})
        assert value is None
        assert "no federated role output" in error

    def test_profile_role_arn_must_match_the_auth_stack(self):
        profile = _profile(federated_role_arn="arn:aws:iam::123456789012:role/OldRole")
        value, error = metering_attributed_roles(profile, {"DirectSTSRoleArn": DIRECT_ARN})
        assert value is None
        assert "OldRole" in error and "BedrockOktaFederatedRole" in error

    def test_matching_profile_role_arn_is_accepted(self):
        profile = _profile(federated_role_arn=DIRECT_ARN)
        assert metering_attributed_roles(profile, {"DirectSTSRoleArn": DIRECT_ARN}) == (
            "BedrockOktaFederatedRole",
            None,
        )

    def test_saved_role_arn_is_not_compared_after_a_switch_to_cognito_federation(self):
        """The field is written only from DirectSTSRoleArn, which a Cognito federation stack doesn't have."""
        profile = _profile(federation_type="cognito", federated_role_arn=DIRECT_ARN, metering_mode="shadow")
        outputs = {
            "FederatedRoleArn": "arn:aws:iam::123456789012:role/gip-CognitoAuthRole",
            "FederationType": "cognito",
        }
        assert metering_attributed_roles(profile, outputs) == ("gip-CognitoAuthRole", None)

    def test_gip_deploy_auth_clears_the_mismatch_it_reports(self):
        """The advice works: the run's stack outputs step saves DirectSTSRoleArn into the profile."""
        profile = _profile(federated_role_arn="arn:aws:iam::123456789012:role/OldRole")
        outputs = {"DirectSTSRoleArn": DIRECT_ARN, "FederationType": "direct"}
        _value, error = metering_attributed_roles(profile, outputs)
        assert "Run 'gip deploy auth'" in error
        config = MagicMock()
        with patch("governed_inference_platform.cli.commands.deploy.get_stack_outputs", return_value=outputs):
            DeployCommand()._show_stack_outputs(profile, MagicMock(), config)
        config.save_profile.assert_called_with(profile)
        assert metering_attributed_roles(profile, outputs) == ("BedrockOktaFederatedRole", None)

    def test_unreadable_auth_stack_is_an_error_even_with_listed_roles(self):
        profile = _profile(metering_attributed_roles=["claude-gateway-bedrock-user"])
        value, error = metering_attributed_roles(profile, None, "could not read stack 'gip-test-stack': Throttling")
        assert value is None
        assert "Throttling" in error

    def test_operator_named_roles_are_appended_once(self):
        profile = _profile(metering_attributed_roles=["claude-gateway-bedrock-user", "BedrockOktaFederatedRole"])
        value, error = metering_attributed_roles(profile, {"DirectSTSRoleArn": DIRECT_ARN})
        assert error is None
        assert value == "BedrockOktaFederatedRole,claude-gateway-bedrock-user"

    def test_oidc_listed_roles_without_the_federated_role_are_an_error(self):
        profile = _profile(metering_attributed_roles=["claude-gateway-bedrock-user"])
        value, error = metering_attributed_roles(profile, {})
        assert value is None
        assert "no federated role output" in error

    def test_auth_none_attributes_only_the_listed_roles(self):
        profile = _profile(
            auth_type="none", sso_enabled=False, metering_attributed_roles=["claude-gateway-bedrock-user"]
        )
        assert metering_attributed_roles(profile, None) == ("claude-gateway-bedrock-user", None)

    @pytest.mark.parametrize("bad", ["*", "a*b", "has space", "comma,inside", "x" * 65, ""])
    def test_invalid_entry_is_rejected(self, bad):
        value, error = metering_attributed_roles(
            _profile(metering_attributed_roles=[bad]), {"DirectSTSRoleArn": DIRECT_ARN}
        )
        assert value is None
        assert "Invalid attributed role name" in error

    def test_trailing_newline_in_a_role_name_is_rejected(self):
        """The whole name must match: a role name read from a stack output is not stripped."""
        value, error = metering_attributed_roles(_profile(), {"DirectSTSRoleArn": DIRECT_ARN + "\n"})
        assert value is None
        assert "Invalid attributed role name" in error

    def test_non_list_is_rejected(self):
        value, error = metering_attributed_roles(_profile(metering_attributed_roles="RoleA"), {})
        assert value is None
        assert "must be a list" in error

    def test_no_role_known_is_an_error(self):
        value, error = metering_attributed_roles(_profile(), {})
        assert value is None
        # For OIDC only the auth stack can name the role, so listing roles is not offered as a fix.
        assert "gip deploy auth" in error and "metering_attributed_roles" not in error

    def test_auth_none_with_no_listed_role_is_an_error(self):
        profile = _profile(auth_type="none", sso_enabled=False)
        value, error = metering_attributed_roles(profile, None)
        assert value is None
        assert "auth type 'none'" in error and "metering_attributed_roles" in error

    def test_profile_field_round_trips(self):
        profile = _profile(metering_attributed_roles=["RoleA", "Prefix_*"])
        assert Profile.from_dict(profile.to_dict()).metering_attributed_roles == ["RoleA", "Prefix_*"]
        assert _profile().metering_attributed_roles == []


def _stack(outputs):
    return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]}


def _deploy_metering(profile, outputs, describe_error=None):
    manager = MagicMock(spec=CloudFormationManager)
    manager.region = profile.aws_region
    manager.cf_client = MagicMock()
    if describe_error:
        manager.cf_client.describe_stacks.side_effect = describe_error
    else:
        manager.cf_client.describe_stacks.return_value = _stack(outputs)
    result = MagicMock(success=True, outputs={})
    manager.deploy_stack.return_value = result
    console = MagicMock()
    with (
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager", return_value=manager),
        patch(
            "governed_inference_platform.cli.commands.deploy.get_stack_outputs",
            return_value={"QuotaTableName": "UserQuotaMetrics", "QuotaAlertTopicArn": ""},
        ),
        patch.object(DeployCommand, "_ensure_regional_artifacts_bucket", return_value="artifacts-bucket"),
        patch(
            "governed_inference_platform.cli.commands.deploy.run_checked",
            return_value=MagicMock(returncode=0, stderr=""),
        ),
    ):
        rc = DeployCommand()._deploy_stack("metering", profile, console, manager)
    params = [
        {p["ParameterKey"]: p["ParameterValue"] for p in call.kwargs["parameters"]}
        for call in manager.deploy_stack.call_args_list
    ]
    printed = " ".join(str(c) for c in console.print.call_args_list)
    return rc, params, printed


class TestDeployMetering:
    def test_every_region_gets_the_role_list(self):
        rc, params, printed = _deploy_metering(_profile(), {"DirectSTSRoleArn": DIRECT_ARN})
        assert rc == 0
        assert [p["AttributedRoles"] for p in params] == ["BedrockOktaFederatedRole"] * 2
        assert "attributes usage only to sessions of: BedrockOktaFederatedRole" in printed
        assert "metered as unattributed until it is listed in metering_attributed_roles" in printed
        assert "style='dim'" not in printed

    def test_failed_auth_stack_read_deploys_nothing(self):
        throttled = ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DescribeStacks")
        profile = _profile(metering_attributed_roles=["claude-gateway-bedrock-user"], federated_role_arn=DIRECT_ARN)
        rc, params, printed = _deploy_metering(profile, {}, describe_error=throttled)
        assert rc == 1
        assert params == []
        assert "could not read stack" in printed and "Rate exceeded" in printed

    def test_broad_prefix_entry_prints_a_warning(self):
        profile = _profile(metering_attributed_roles=["Team*"])
        rc, params, printed = _deploy_metering(profile, {"DirectSTSRoleArn": DIRECT_ARN})
        assert rc == 0
        assert {p["AttributedRoles"] for p in params} == {"BedrockOktaFederatedRole,Team*"}
        assert "'Team*' matches every role whose name starts with 'Team'" in printed

    def test_idc_gets_every_identity_center_role(self):
        rc, params, printed = _deploy_metering(_profile(auth_type="idc", idc_permission_set_name="BedrockDev"), {})
        assert rc == 0
        assert {p["AttributedRoles"] for p in params} == {"AWSReservedSSO_*"}
        assert "Warning" not in printed

    def test_no_known_role_deploys_nothing(self):
        rc, params, printed = _deploy_metering(_profile(), {})
        assert rc == 1
        assert params == []
        assert "Not deploying server-side metering" in printed


class TestReadStack:
    def _manager(self, **kwargs):
        manager = MagicMock()
        manager.region = "us-east-1"
        manager.cf_client.describe_stacks.configure_mock(**kwargs)
        return manager

    def test_returns_the_stack(self):
        stack, error = read_stack(self._manager(return_value=_stack({"A": "1"})), "s")
        assert error is None
        assert stack["Outputs"] == [{"OutputKey": "A", "OutputValue": "1"}]

    def test_absent_stack_is_told_apart_from_a_failed_read(self):
        missing = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack with id s does not exist"}}, "x")
        _, absent = read_stack(self._manager(side_effect=missing), "s")
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "x")
        _, failed = read_stack(self._manager(side_effect=denied), "s")
        assert "does not exist" in absent
        assert failed.startswith("could not read stack 's'") and "not authorized" in failed


class TestTemplateContract:
    @staticmethod
    def _template():
        with METERING_TEMPLATE.open(encoding="utf-8") as f:
            return yaml.load(f, Loader=CFLoader)  # nosec B506

    def test_parameter_has_no_default(self):
        """The first update that passes no list fails validation instead of attributing every role or none."""
        param = self._template()["Parameters"]["AttributedRoles"]
        assert param["Type"] == "String"
        assert "Default" not in param

    def test_processor_receives_the_list(self):
        env = self._template()["Resources"]["MeteringProcessorFunction"]["Properties"]["Environment"]["Variables"]
        assert env["ATTRIBUTED_ROLES"] == "AttributedRoles"  # CFLoader renders !Ref X as "X"

    @pytest.mark.parametrize(
        "value",
        [
            "BedrockOktaFederatedRole",
            "AWSReservedSSO_BedrockDeveloperAccess_*",
            "AWSReservedSSO_*",
            "BedrockOktaFederatedRole,claude-gateway-bedrock-user,Team+Role=x.y@z_*",
        ],
    )
    def test_values_gip_builds_match_the_allowed_pattern(self, value):
        pattern = self._template()["Parameters"]["AttributedRoles"]["AllowedPattern"]
        assert re.fullmatch(pattern, value)

    @pytest.mark.parametrize("value", ["", "*", "a*b", "RoleA,", "Role A", "RoleA,*"])
    def test_pattern_rejects_what_the_processor_would_ignore(self, value):
        pattern = self._template()["Parameters"]["AttributedRoles"]["AllowedPattern"]
        assert not re.fullmatch(pattern, value)
