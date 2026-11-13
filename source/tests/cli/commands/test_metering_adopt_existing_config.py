# ABOUTME: Tests for metering_adopt_existing_config: profile field, answers file, init wizard and deploy params
# ABOUTME: `gip deploy metering` passes AdoptExistingConfig=true only when the profile opts in

"""`gip deploy metering` can adopt an existing invocation-logging configuration.

quota-metering.yaml fails closed when the account already has a Bedrock model
invocation logging configuration, unless AdoptExistingConfig=true. Before this
field, gip never passed the parameter, so adoption needed a hand-run
CloudFormation deploy. These tests pin the chain: answers file / wizard ->
Profile.metering_adopt_existing_config -> AdoptExistingConfig=true on every
regional metering stack, and nothing new for profiles that don't opt in.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import questionary

from governed_inference_platform.cli.commands.deploy import DeployCommand
from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.cli.commands.init_answers import (
    build_answers_from_profile,
    build_config_from_answers,
)
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.config import Config, Profile

MINIMAL_ANSWERS = {"okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"}}
BASE_PROFILE = {
    "name": "metered",
    "provider_domain": "company.okta.com",
    "client_id": "0oa0000000000000000",
    "credential_storage": "session",
    "aws_region": "us-east-1",
    "identity_pool_name": "gip-test",
    "federation_type": "direct",
    "quota_monitoring_enabled": True,
    "metering_enabled": True,
    "allowed_bedrock_regions": ["us-east-1", "us-west-2"],
}


@pytest.fixture
def config_paths(tmp_path):
    config_dir = tmp_path / ".gip"
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True)
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"schema_version": "2.0", "active_profile": None}))
    with (
        patch.object(Config, "CONFIG_DIR", config_dir),
        patch.object(Config, "CONFIG_FILE", config_file),
        patch.object(Config, "PROFILES_DIR", profiles_dir),
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        yield profiles_dir


class TestProfileField:
    def test_default_is_off(self):
        assert Profile.from_dict(dict(BASE_PROFILE)).metering_adopt_existing_config is False

    def test_round_trips(self):
        profile = Profile.from_dict({**BASE_PROFILE, "metering_adopt_existing_config": True})
        assert Profile.from_dict(profile.to_dict()).metering_adopt_existing_config is True


def _deploy_metering(profile, console=None):
    """Run the metering deploy with AWS mocked; return the parameter dict of each regional deploy."""
    manager = MagicMock(spec=CloudFormationManager)
    manager.region = profile.aws_region
    # The auth stack is read with DescribeStacks (read_stack), so a failed read is told apart from a missing output.
    manager.cf_client = MagicMock()
    manager.cf_client.describe_stacks.return_value = {
        "Stacks": [
            {
                "Outputs": [
                    {
                        "OutputKey": "DirectSTSRoleArn",
                        "OutputValue": "arn:aws:iam::123456789012:role/BedrockOktaFederatedRole",
                    }
                ]
            }
        ]
    }
    result = MagicMock()
    result.success = True
    result.outputs = {}
    manager.deploy_stack.return_value = result
    packaged = MagicMock(returncode=0, stderr="")
    with (
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager", return_value=manager),
        patch(
            "governed_inference_platform.cli.commands.deploy.get_stack_outputs",
            return_value={"QuotaTableName": "UserQuotaMetrics", "QuotaAlertTopicArn": ""},
        ),
        patch.object(DeployCommand, "_ensure_regional_artifacts_bucket", return_value="artifacts-bucket"),
        patch("governed_inference_platform.cli.commands.deploy.run_checked", return_value=packaged),
    ):
        rc = DeployCommand()._deploy_stack("metering", profile, console or MagicMock(), manager)
    assert rc == 0
    return [
        {p["ParameterKey"]: p["ParameterValue"] for p in call.kwargs["parameters"]}
        for call in manager.deploy_stack.call_args_list
    ]


class TestDeployMeteringParams:
    def test_adopt_passes_parameter_to_every_region(self):
        profile = Profile.from_dict({**BASE_PROFILE, "metering_adopt_existing_config": True})
        deployed = _deploy_metering(profile)
        assert len(deployed) == 2
        assert all(params.get("AdoptExistingConfig") == "true" for params in deployed)

    def test_default_profile_parameters_are_unchanged(self):
        """Profiles that don't opt in send no AdoptExistingConfig (only the parameters every profile sends)."""
        deployed = _deploy_metering(Profile.from_dict(dict(BASE_PROFILE)))
        before = {
            "QuotaTableName": "UserQuotaMetrics",
            "QuotaTableRegion": "us-east-1",
            "MeteringMode": "shadow",
            "AttributedRoles": "BedrockOktaFederatedRole",
        }
        assert deployed == [before, before]

    def test_hand_edited_string_value_warns_and_is_treated_as_off(self):
        console = MagicMock()
        profile = Profile.from_dict({**BASE_PROFILE, "metering_adopt_existing_config": "true"})
        deployed = _deploy_metering(profile, console)
        assert all("AdoptExistingConfig" not in params for params in deployed)
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "metering_adopt_existing_config is 'true', not true or false" in printed


class TestAnswersFile:
    def test_default_is_false(self):
        config, errors = build_config_from_answers(MINIMAL_ANSWERS)
        assert errors == []
        assert config["metering"]["adopt_existing_config"] is False

    def test_true_is_accepted(self):
        answers = {**MINIMAL_ANSWERS, "metering": {"enabled": True, "adopt_existing_config": True}}
        config, errors = build_config_from_answers(answers)
        assert errors == []
        assert config["metering"]["adopt_existing_config"] is True

    def test_non_boolean_is_rejected(self):
        answers = {**MINIMAL_ANSWERS, "metering": {"enabled": True, "adopt_existing_config": "yes"}}
        _, errors = build_config_from_answers(answers)
        assert any(e.startswith("metering.adopt_existing_config:") for e in errors), errors

    def test_from_file_saves_and_export_round_trips(self, config_paths):
        answers = {**MINIMAL_ANSWERS, "metering": {"enabled": True, "adopt_existing_config": True}}
        config, errors = build_config_from_answers(answers)
        assert errors == []
        InitCommand()._save_configuration(config, "metered")

        saved = json.loads((config_paths / "metered.json").read_text(encoding="utf-8"))
        assert saved["metering_adopt_existing_config"] is True
        exported = build_answers_from_profile(InitCommand(), "metered")
        assert exported["metering"]["adopt_existing_config"] is True

    def test_export_carries_every_metering_key_from_the_reload(self, config_paths):
        Config().save_profile(
            Profile.from_dict({**BASE_PROFILE, "metering_mode": "max", "metering_adopt_existing_config": True})
        )
        exported = build_answers_from_profile(InitCommand(), "metered")
        assert exported["metering"] == {"enabled": True, "mode": "max", "adopt_existing_config": True}
        config, errors = build_config_from_answers({**MINIMAL_ANSWERS, "metering": exported["metering"]})
        assert errors == []
        assert config["metering"] == {"enabled": True, "mode": "max", "adopt_existing_config": True}

    def test_export_of_a_token_quota_profile_reimports(self, config_paths):
        """The wizard-only quota.monthly_limit_millions key must not reach the exported answers."""
        Config().save_profile(Profile.from_dict({**BASE_PROFILE, "monthly_token_limit": 300_000_000}))
        exported = build_answers_from_profile(InitCommand(), "metered")
        assert "monthly_limit_millions" not in exported.get("quota", {})
        assert exported["quota"]["monthly_limit"] == 300_000_000
        _, errors = build_config_from_answers({**MINIMAL_ANSWERS, "quota": exported["quota"]})
        assert errors == []


class TestInteractiveInit:
    def test_rerun_keeps_metering_settings(self, config_paths):
        """Re-running the wizard used to save metering as off: no question restores it."""
        Config().save_profile(
            Profile.from_dict({**BASE_PROFILE, "metering_mode": "max", "metering_adopt_existing_config": True})
        )
        cmd = InitCommand()
        existing = cmd._check_existing_deployment("metered")
        assert existing["metering"] == {"enabled": True, "mode": "max", "adopt_existing_config": True}

        existing.setdefault("quota", {})["enabled"] = True
        cmd._save_configuration(existing, "metered")
        saved = Config().load_profile("metered")
        assert (saved.metering_enabled, saved.metering_mode, saved.metering_adopt_existing_config) == (
            True,
            "max",
            True,
        )


class _StopAtMonitoringComplete(Exception):
    pass


def _run_monitoring_step(saved_config, adopt_answer, quota_answer=True):
    """Drive the wizard's monitoring step (sidecar, cost quota) and return (config, adopt_prompts)."""
    progress = MagicMock()
    progress.get_last_step.return_value = "aws_complete"
    progress.get_saved_data.return_value = saved_config
    captured = {}

    def save_step(step, data=None, *a, **k):
        if step == "monitoring_complete":
            captured["config"] = json.loads(json.dumps(data))
            raise _StopAtMonitoringComplete()

    progress.save_step.side_effect = save_step
    adopt_prompts = []

    def fake_confirm(message, *a, **k):
        m = MagicMock()
        text = str(message)
        if "invocation-logging configuration" in text:
            adopt_prompts.append(k.get("default"))
            m.ask.return_value = adopt_answer
        else:
            m.ask.return_value = text == "Enable monitoring?" or (text == "Enable quota monitoring?" and quota_answer)
        return m

    def fake_select(message, *a, **k):
        m = MagicMock()
        text = str(message)
        m.ask.return_value = {
            "Monitoring mode:": "sidecar",
            "How do you want to limit usage?": "cost",
        }.get(text, k.get("default"))
        return m

    def fake_text(message, *a, **k):
        m = MagicMock()
        m.ask.return_value = k.get("default", "")
        return m

    with (
        patch.object(questionary, "confirm", fake_confirm),
        patch.object(questionary, "select", fake_select),
        patch.object(questionary, "text", fake_text),
    ):
        try:
            InitCommand()._gather_configuration(progress)
        except _StopAtMonitoringComplete:
            pass
    assert "config" in captured, "wizard never reached the monitoring_complete checkpoint"
    return captured["config"], adopt_prompts


class TestWizardQuestion:
    BASE = {"auth_type": "oidc", "sso_enabled": True, "aws": {"region": "us-east-1"}}

    def test_asked_when_metering_is_enabled(self):
        config, prompts = _run_monitoring_step({**self.BASE, "metering": {"enabled": True}}, adopt_answer=True)
        assert prompts == [False], "the question must default to No"
        assert config["metering"]["adopt_existing_config"] is True

    def test_default_follows_existing_value(self):
        saved = {**self.BASE, "metering": {"enabled": True, "adopt_existing_config": True}}
        _, prompts = _run_monitoring_step(saved, adopt_answer=True)
        assert prompts == [True]

    def test_not_asked_when_metering_is_off(self):
        config, prompts = _run_monitoring_step({**self.BASE, "metering": {"enabled": False}}, adopt_answer=True)
        assert prompts == []
        assert "adopt_existing_config" not in config.get("metering", {})

    def test_turning_quota_off_turns_restored_metering_off(self):
        # A re-run restores metering from the profile; metering without quota is invalid
        # (validate_config rejects it), so the wizard must not save that combination.
        saved = {**self.BASE, "quota": {"enabled": True}, "metering": {"enabled": True, "adopt_existing_config": True}}
        config, prompts = _run_monitoring_step(saved, adopt_answer=True, quota_answer=False)
        assert prompts == []
        assert config["quota"]["enabled"] is False
        assert config["metering"]["enabled"] is False
