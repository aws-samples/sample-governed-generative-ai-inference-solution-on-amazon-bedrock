# ABOUTME: Tests that gip deploy refuses metering_mode 'max' when metering cannot attribute usage to a user
# ABOUTME: Covers the predicate per federation mode and binding, and that the refusal runs before any AWS call

"""`gip deploy` refuses server-side metering in `max` mode that cannot attribute a user.

The metering processor keys a user on the assumed-role session name. Only IAM
Identity Center sessions and Direct STS sessions bound to the `email` claim name
the user: binding `none` lets the client choose the name, binding `sub` yields a
name the quota API never reads, and Cognito identity pool sessions carry no user.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.deploy import (
    METERING_IDENTITY_STACKS,
    DeployCommand,
    metering_attribution_check,
)
from governed_inference_platform.config import Profile

# The order the refusal quotes, in the words of COST_ATTRIBUTION.md, migration step 3.
MAX_ORDER = (
    "session_name_binding to 'email' in the profile, run 'gip package' and distribute the new clients, "
    "then run 'gip deploy'"
)
COST_ATTRIBUTION = Path(__file__).resolve().parents[4] / "assets" / "docs" / "COST_ATTRIBUTION.md"


def test_migration_doc_gives_the_quoted_order_for_max():
    """The doc's migration list has a 'max' step with the same three actions in the same order."""
    text = COST_ATTRIBUTION.read_text(encoding="utf-8")
    section = text[text.index("### Binding session names to the token claim") :]
    section = section[section.index("Migration (update-safe):") :]
    step = section[section.index("with `metering_mode: max`") :]
    step = " ".join(step[: step.index("\n4. ")].split())
    positions = [
        step.index("`session_name_binding: email` in the profile"),
        step.index("run `gip package` and distribute the new clients"),
        step.index("then run `gip deploy`"),
    ]
    assert positions == sorted(positions)


def _profile(**overrides) -> Profile:
    data = {
        "name": "metered",
        "provider_domain": "company.okta.com",
        "client_id": "0oa0000000000000000",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "provider_type": "okta",
        "federation_type": "direct",
        "session_name_binding": "email",
        "monitoring_enabled": True,
        "quota_monitoring_enabled": True,
        "metering_enabled": True,
        "metering_mode": "max",
        "allowed_bedrock_regions": ["us-east-1"],
    }
    data.update(overrides)
    return Profile.from_dict(data)


class TestPredicate:
    def test_direct_sts_with_email_binding_is_attributable(self):
        assert metering_attribution_check(_profile()) == ("ok", None)

    def test_iam_identity_center_is_attributable(self):
        profile = _profile(auth_type="idc", federation_type="cognito", session_name_binding="none")
        assert metering_attribution_check(profile) == ("ok", None)

    def test_metering_off_is_never_refused(self):
        profile = _profile(metering_enabled=False, session_name_binding="none")
        assert metering_attribution_check(profile) == ("ok", None)

    @pytest.mark.parametrize("binding", ["none", "sub"])
    def test_direct_sts_without_email_binding_is_refused_in_max(self, binding):
        verdict, msg = metering_attribution_check(_profile(session_name_binding=binding))
        assert verdict == "refuse"
        assert f"session_name_binding is '{binding}'" in msg
        assert "Set session_name_binding to 'email'" in msg
        assert "metering_mode to 'shadow'" in msg

    def test_binding_none_message_quotes_the_max_migration_order(self):
        _, msg = metering_attribution_check(_profile(session_name_binding="none"))
        assert MAX_ORDER in msg
        assert "Binding session names to the token claim" in msg

    def test_binding_sub_message_says_there_is_no_update_safe_order(self):
        _, msg = metering_attribution_check(_profile(session_name_binding="sub"))
        assert "accepts one claim at a time" in msg

    def test_cognito_identity_pool_is_refused_in_max(self):
        verdict, msg = metering_attribution_check(_profile(federation_type="cognito", session_name_binding="none"))
        assert verdict == "refuse"
        assert "Cognito identity pool sessions carry no user" in msg
        assert "federation_type 'direct'" in msg

    def test_shadow_mode_gets_a_notice_not_a_refusal(self):
        verdict, msg = metering_attribution_check(_profile(session_name_binding="sub", metering_mode="shadow"))
        assert verdict == "notice"
        assert "refuses metering_mode 'max'" in msg


DEPLOYED_EMAIL_DIRECT = {"SessionNameBinding": "email", "FederationType": "direct"}


def _auth_stack(parameters, status="UPDATE_COMPLETE"):
    stack = {"Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]}
    if status is not None:
        stack["StackStatus"] = status
    return {"Stacks": [stack]}


def _run(profile, args, deployed=DEPLOYED_EMAIL_DIRECT, describe_error=None, status="UPDATE_COMPLETE"):
    """Run `gip deploy <args>` with AWS mocked; ``deployed`` and ``status`` describe the auth stack."""
    with (
        patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
        patch.object(DeployCommand, "_deploy_stack", return_value=0) as deploy_stack,
    ):
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "metered"
        MockCFM.return_value.get_stack_status.return_value = None
        MockCFM.return_value.region = profile.aws_region
        describe = MockCFM.return_value.cf_client.describe_stacks
        if describe_error:
            describe.side_effect = describe_error
        else:
            describe.return_value = _auth_stack(deployed, status)
        tester = CommandTester(DeployCommand())
        rc = tester.execute(args)
    return rc, tester.io.fetch_output(), MockCFM, deploy_stack


def _flat(text: str) -> str:
    """Collapse whitespace: rich wraps long lines at the console width."""
    return " ".join(text.split())


class TestDeployRefusal:
    @pytest.mark.parametrize("args", ["metering", "quota", "auth", "", "metering --dry-run", "quota --show-commands"])
    def test_refuses_before_any_cloudformation_call(self, args, capsys):
        rc, output, cfm, deploy_stack = _run(_profile(session_name_binding="sub"), args)
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 1
        assert "Refusing to deploy: metering_mode is 'max'" in printed
        cfm.assert_not_called()
        deploy_stack.assert_not_called()

    def test_email_binding_deploys(self, capsys):
        rc, output, cfm, deploy_stack = _run(_profile(), "metering")
        assert rc == 0
        assert "Refusing to deploy" not in output + capsys.readouterr().out
        deploy_stack.assert_called_once()

    def test_unrelated_stack_is_not_refused(self, capsys):
        rc, output, _cfm, deploy_stack = _run(_profile(session_name_binding="none"), "dashboard")
        assert rc == 0
        assert "Refusing to deploy" not in output + capsys.readouterr().out
        deploy_stack.assert_called_once()

    def test_shadow_mode_prints_notice_and_deploys(self, capsys):
        rc, output, _cfm, deploy_stack = _run(_profile(session_name_binding="none", metering_mode="shadow"), "metering")
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 0
        assert "cannot attribute usage to a user" in printed
        deploy_stack.assert_called_once()

    def test_identity_stacks_are_auth_quota_metering(self):
        assert set(METERING_IDENTITY_STACKS) == {"auth", "quota", "metering"}


class TestDeployedAuthStack:
    """In 'max' mode, a run that deploys quota or metering but not auth checks the deployed trust."""

    @pytest.mark.parametrize("args", ["metering", "quota", "metering --dry-run", "quota --show-commands"])
    def test_deployed_binding_none_is_refused(self, args, capsys):
        rc, output, cfm, deploy_stack = _run(
            _profile(), args, deployed={"SessionNameBinding": "none", "FederationType": "direct"}
        )
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 1
        assert "the deployed auth stack 'gip-test-stack' has SessionNameBinding 'none'" in printed
        assert "gip deploy auth" in printed
        assert MAX_ORDER in printed
        cfm.return_value.cf_client.describe_stacks.assert_called_once_with(StackName="gip-test-stack")
        deploy_stack.assert_not_called()

    def test_deployed_cognito_federation_is_refused(self, capsys):
        rc, output, _cfm, deploy_stack = _run(
            _profile(), "quota", deployed={"SessionNameBinding": "email", "FederationType": "cognito"}
        )
        assert rc == 1
        assert "FederationType 'cognito'" in _flat(output + capsys.readouterr().out)
        deploy_stack.assert_not_called()

    def test_stack_without_the_binding_parameter_is_refused(self, capsys):
        rc, output, _cfm, deploy_stack = _run(_profile(), "metering", deployed={"FederationType": "direct"})
        assert rc == 1
        assert "SessionNameBinding 'none'" in _flat(output + capsys.readouterr().out)
        deploy_stack.assert_not_called()

    def test_unreadable_auth_stack_is_refused(self, capsys):
        throttled = ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DescribeStacks")
        rc, output, _cfm, deploy_stack = _run(_profile(), "metering", describe_error=throttled)
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 1
        assert "could not read stack 'gip-test-stack'" in printed and "Rate exceeded" in printed
        deploy_stack.assert_not_called()

    def test_absent_auth_stack_is_refused(self, capsys):
        missing = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack with id gip-test-stack does not exist"}},
            "DescribeStacks",
        )
        rc, output, _cfm, deploy_stack = _run(_profile(), "quota", describe_error=missing)
        assert rc == 1
        assert "stack 'gip-test-stack' does not exist" in _flat(output + capsys.readouterr().out)
        deploy_stack.assert_not_called()

    def test_deployed_email_direct_deploys(self, capsys):
        rc, output, cfm, deploy_stack = _run(_profile(), "quota")
        assert rc == 0
        assert "Refusing to deploy" not in output + capsys.readouterr().out
        cfm.return_value.cf_client.describe_stacks.assert_called_once_with(StackName="gip-test-stack")
        deploy_stack.assert_called_once()

    @pytest.mark.parametrize(
        "status",
        [
            "UPDATE_IN_PROGRESS",
            "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_FAILED",
            "CREATE_FAILED",
            None,
        ],
    )
    def test_status_not_ending_in_complete_is_refused(self, status, capsys):
        rc, output, _cfm, deploy_stack = _run(_profile(), "quota", status=status)
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 1
        assert "can't be checked" in printed
        assert f"stack 'gip-test-stack' is in status '{status or 'unknown'}'" in printed
        deploy_stack.assert_not_called()

    @pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"])
    def test_status_ending_in_complete_is_read(self, status, capsys):
        rc, output, _cfm, deploy_stack = _run(_profile(), "metering", status=status)
        assert rc == 0
        assert "Refusing to deploy" not in output + capsys.readouterr().out
        deploy_stack.assert_called_once()

    def test_run_that_deploys_auth_does_not_read_it(self, capsys):
        rc, _output, cfm, deploy_stack = _run(
            _profile(), "auth", deployed={"SessionNameBinding": "none", "FederationType": "direct"}
        )
        assert rc == 0
        cfm.return_value.cf_client.describe_stacks.assert_not_called()
        deploy_stack.assert_called_once()

    def test_full_deploy_does_not_read_it(self, capsys):
        """A full deploy plans the auth stack first, so the old stack's binding never blocks it."""
        with patch.object(DeployCommand, "_show_stack_outputs"):
            rc, _output, cfm, deploy_stack = _run(
                _profile(), "", deployed={"SessionNameBinding": "none", "FederationType": "direct"}
            )
        deployed = [c.args[0] for c in deploy_stack.call_args_list]
        assert rc == 0
        cfm.return_value.cf_client.describe_stacks.assert_not_called()
        assert deployed[0] == "auth"
        assert {"quota", "metering"} <= set(deployed)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"metering_mode": "shadow"},
            {"auth_type": "idc", "federation_type": "cognito", "session_name_binding": "none"},
        ],
        ids=["shadow", "idc"],
    )
    def test_no_read_outside_oidc_max(self, overrides, capsys):
        rc, _output, cfm, deploy_stack = _run(
            _profile(**overrides), "metering", deployed={"SessionNameBinding": "none", "FederationType": "direct"}
        )
        assert rc == 0
        cfm.return_value.cf_client.describe_stacks.assert_not_called()
        deploy_stack.assert_called_once()


QUOTA_MONITORING = COST_ATTRIBUTION.parent / "QUOTA_MONITORING.md"


class TestCognitoUserPoolNotice:
    """In 'max' mode a Cognito user pool profile gets one notice that users can change their email claim."""

    def _cognito(self, **overrides):
        return _profile(
            provider_type="cognito", provider_domain="gip-test.auth.us-east-1.amazoncognito.com", **overrides
        )

    def test_max_prints_one_notice(self, capsys):
        rc, output, _cfm, deploy_stack = _run(self._cognito(), "metering")
        printed = _flat(output + capsys.readouterr().out)
        assert rc == 0
        assert printed.count("Cognito user pool") == 1
        assert "another address they own" in printed
        assert "every limit (user, group and default policies) starts again" in printed
        assert "name+tag@" in printed
        assert "QUOTA_MONITORING.md, 'Identity Caveats'" in printed
        deploy_stack.assert_called_once()

    @pytest.mark.parametrize(
        ("profile_kwargs", "provider"),
        [({"metering_mode": "shadow"}, "cognito"), ({}, "okta")],
        ids=["shadow", "other-provider"],
    )
    def test_no_notice_otherwise(self, profile_kwargs, provider, capsys):
        profile = self._cognito(**profile_kwargs) if provider == "cognito" else _profile(**profile_kwargs)
        rc, output, _cfm, _deploy = _run(profile, "metering")
        assert rc == 0
        assert "Cognito user pool" not in _flat(output + capsys.readouterr().out)


def test_identity_caveat_matches_the_user_pool_template():
    """The caveat must not say that app clients stop writing email: email is required, so they keep write access.

    It must also not offer policies as a remedy for a move to another address: the new
    address starts a usage row at zero, which every policy then counts from.
    """
    text = QUOTA_MONITORING.read_text(encoding="utf-8")
    caveats = text[text.index("### Identity Caveats") :]
    caveats = " ".join(caveats[: caveats.index("\n### ", 5)].split())
    assert "WriteAttributes" not in caveats
    assert "AttributesRequireVerificationBeforeUpdate" in caveats
    assert "stack update" in caveats
    assert "colleague's address" in caveats
    assert "another address they own" in caveats
    assert "every limit starts again" in caveats
    assert "`name+tag@example.com`" in caveats
    assert "rather than user policies" not in caveats
    assert "(adr/0015-session-name-binding.md#per-provider-matrix)" in caveats
