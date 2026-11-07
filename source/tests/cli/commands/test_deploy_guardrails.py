import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.deploy import (
    GUARDRAIL_PROFILES,
    VALID_STACKS,
    DeployCommand,
    build_guardrails_params,
    guardrails_cleanup_regions,
    guardrails_name,
    guardrails_preflight,
    guardrails_region_tier,
    guardrails_regions,
    guardrails_standard_tier_warnings,
    guardrails_tier_plan,
)
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.config import Profile


def _profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "provider_type": "okta",
        "allowed_bedrock_regions": ["us-east-1", "us-west-2"],
    }
    data.update(overrides)
    return Profile.from_dict(data)


def _run_deploy(profile, args):
    with (
        patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
    ):
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        MockCFM.return_value.get_stack_status.return_value = None
        tester = CommandTester(DeployCommand())
        return tester.execute(args)


class TestRegistration:
    def test_valid_stacks_includes_guardrails(self):
        assert "guardrails" in VALID_STACKS

    def test_destroyable_guardrails_before_auth(self):
        assert "guardrails" in DESTROYABLE_STACKS
        assert DESTROYABLE_STACKS.index("guardrails") < DESTROYABLE_STACKS.index("auth")

    def test_go_config_mirrors_guardrails_fields(self):
        config_go = Path(__file__).resolve().parents[4] / "source" / "go" / "internal" / "config" / "config.go"
        content = config_go.read_text(encoding="utf-8")

        assert 'json:"guardrails_enabled' in content
        assert 'json:"guardrails_name' in content
        assert 'json:"guardrails_content_filter_strength' in content
        assert 'json:"guardrails_safeguard_tier' in content
        assert 'json:"guardrails_model_include_list' in content
        assert 'json:"guardrails_kms_key_arn' in content


class TestParams:
    def test_guardrails_name_is_bedrock_safe_and_bounded(self):
        profile = _profile(identity_pool_name="team/path with spaces and a very very very very long suffix")
        name = guardrails_name(profile)

        assert len(name) <= 50
        assert name == "team-path-with-spaces-and-a-very-very-very-very-lo"

    def test_build_guardrails_params_defaults_to_all_models(self):
        params = build_guardrails_params(_profile())

        assert "GuardrailName=gip-test-guardrail" in params
        assert "ContentFilterStrength=MEDIUM" in params
        assert not any(p.startswith("ModelIncludeList=") for p in params)

    def test_build_guardrails_params_carries_optional_scope_and_kms(self):
        params = build_guardrails_params(
            _profile(
                guardrails_name="team-guardrail",
                guardrails_content_filter_strength="HIGH",
                guardrails_model_include_list=["anthropic.claude-sonnet-5", "anthropic.claude-haiku-4-5"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert "GuardrailName=team-guardrail" in params
        assert "ContentFilterStrength=HIGH" in params
        assert "ModelIncludeList=anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5" in params
        assert any(p.startswith("KmsKeyArn=arn:aws:kms:") for p in params)

    def test_preflight_rejects_invalid_strength_and_model_list(self):
        ok, msg = guardrails_preflight(_profile(guardrails_content_filter_strength="NONE"))
        assert not ok and "LOW, MEDIUM, HIGH" in msg

        ok, msg = guardrails_preflight(_profile(guardrails_model_include_list=["valid", "bad,model"]))
        assert not ok and "without commas" in msg

    def test_preflight_rejects_multi_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444"
            )
        )

        assert not ok and "exactly one region" in msg

    def test_preflight_rejects_wrong_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                allowed_bedrock_regions=["us-west-2"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert not ok and "us-west-2" in msg

    def test_preflight_accepts_same_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                allowed_bedrock_regions=["us-east-1"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert ok and msg is None

    def test_regions_expand_sentinels_and_filter_partitions(self):
        regions = guardrails_regions(_profile(allowed_bedrock_regions=["all-commercial", "us-gov-west-1"]))

        assert "us-east-1" in regions
        assert "eu-west-1" in regions
        assert "us-gov-west-1" not in regions

    def test_empty_region_list_matches_auth_fallback(self):
        with patch(
            "governed_inference_platform.models.get_all_bedrock_regions",
            return_value=["us-east-1", "us-west-2", "us-gov-west-1"],
        ):
            regions = guardrails_regions(_profile(allowed_bedrock_regions=[]))

        assert regions == ["us-east-1", "us-west-2"]


class TestDispatch:
    def test_cleanup_regions_only_adds_explicit_abandoned_regions(self, monkeypatch):
        profile = _profile(allowed_bedrock_regions=["us-east-1"])
        monkeypatch.setenv("GIP_GUARDRAILS_CLEANUP_REGIONS", "ap-east-1,us-west-2")

        assert guardrails_cleanup_regions(profile) == ["us-east-1", "ap-east-1", "us-west-2"]

    def test_orphan_scan_only_probes_enabled_and_explicit_regions(self):
        profile = _profile(guardrails_enabled=False, allowed_bedrock_regions=["us-east-1", "ap-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }

        with patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as manager_cls:
            manager_cls.return_value.get_stack_status.return_value = None
            DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        assert {call.kwargs["region"] for call in manager_cls.call_args_list} == {"us-west-2"}

    def test_orphan_scan_falls_back_to_primary_region_when_discovery_fails(self):
        profile = _profile(guardrails_enabled=False, allowed_bedrock_regions=["us-east-1", "ap-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.side_effect = RuntimeError("denied")

        with patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as manager_cls:
            DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        manager_cls.assert_not_called()

    def test_deploy_guardrails_requires_opt_in(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=False), "guardrails")
        output = capsys.readouterr().out

        assert rc == 1
        assert "Guardrails enforcement is not enabled" in output

    def test_deploy_guardrails_dry_run_schedules_stack(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=True), "guardrails --dry-run")
        output = capsys.readouterr().out

        assert rc == 0
        assert "Unknown stack" not in output
        assert "guardrails" in output

    def test_show_commands_prints_each_guardrails_region(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=True), "guardrails --show-commands")
        output = capsys.readouterr().out

        assert rc == 0
        assert "guardrails-enforcement.yaml" in output
        assert "--region us-east-1" in output
        assert "--region us-west-2" in output

    def test_explicit_guardrails_failure_returns_nonzero(self, capsys):
        profile = _profile(guardrails_enabled=True)
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
            patch.object(DeployCommand, "_deploy_stack", return_value=1),
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            rc = CommandTester(DeployCommand()).execute("guardrails")

        assert rc == 1
        assert "Failed to deploy guardrails stack" in capsys.readouterr().out

    def test_orphan_check_includes_disabled_guardrails_stack(self):
        profile = _profile(guardrails_enabled=False)
        cf_manager = MagicMock()
        cf_manager.get_stack_status.side_effect = lambda name: (
            "CREATE_COMPLETE" if name.endswith("guardrails") else None
        )
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": profile.aws_region}]
        }

        with patch(
            "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
            return_value=[profile.aws_region],
        ):
            orphaned = DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        assert any(orphan[0] == "guardrails" for orphan in orphaned)

    def test_orphan_check_finds_guardrails_abandoned_by_region_change(self):
        profile = _profile(guardrails_enabled=True, allowed_bedrock_regions=["us-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }
        stale_region_manager = MagicMock()
        stale_region_manager.get_stack_status.return_value = "CREATE_COMPLETE"

        with (
            patch(
                "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
                return_value=["us-east-1", "us-west-2"],
            ),
            patch(
                "governed_inference_platform.cli.commands.deploy.CloudFormationManager",
                return_value=stale_region_manager,
            ),
        ):
            orphaned = DeployCommand()._check_orphaned_stacks(
                [("guardrails", "Bedrock Guardrails account-level enforcement")],
                profile,
                cf_manager,
                None,
            )

        assert any(orphan[0] == "guardrails" for orphan in orphaned)
        assert any(orphan[0] == "guardrails" and orphan[3] == "us-west-2" for orphan in orphaned)

    def test_orphan_check_skips_inaccessible_guardrails_cleanup_region(self):
        profile = _profile(guardrails_enabled=True, allowed_bedrock_regions=["us-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }
        inaccessible_region_manager = MagicMock()
        inaccessible_region_manager.get_stack_status.side_effect = RuntimeError("region denied")

        with (
            patch(
                "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
                return_value=["us-east-1", "us-west-2"],
            ),
            patch(
                "governed_inference_platform.cli.commands.deploy.CloudFormationManager",
                return_value=inaccessible_region_manager,
            ),
        ):
            orphaned = DeployCommand()._check_orphaned_stacks(
                [("guardrails", "Bedrock Guardrails account-level enforcement")],
                profile,
                cf_manager,
                None,
            )

        assert not orphaned


def test_no_iam_guardrail_identifier_condition_in_auth_templates():
    infra = Path(__file__).parent.parent.parent.parent.parent / "deployment" / "infrastructure"
    auth_templates = list(infra.glob("bedrock-auth-*.yaml")) + [infra / "cognito-identity-pool.yaml"]

    assert auth_templates
    for template in auth_templates:
        assert "bedrock:GuardrailIdentifier" not in template.read_text(encoding="utf-8")


class TestSafeguardTier:
    """Opt-in Standard tier: per-Region guardrail profile, Classic unchanged."""

    def test_classic_default_passes_no_tier_parameters(self):
        profile = _profile()
        assert profile.guardrails_safeguard_tier == "CLASSIC"
        for region in ("us-east-1", "us-west-2"):
            params = build_guardrails_params(profile, region)
            assert params == ["GuardrailName=gip-test-guardrail", "ContentFilterStrength=MEDIUM"]

    def test_standard_passes_the_regions_guardrail_profile(self):
        profile = _profile(guardrails_safeguard_tier="STANDARD")
        cases = {
            "us-east-1": "us.guardrail.v1:0",
            "us-west-2": "us.guardrail.v1:0",
            "eu-west-1": "eu.guardrail.v1:0",
            "eu-west-2": "uk.guardrail.v1:0",
            "ap-northeast-1": "apac.guardrail.v1:0",
            "ap-southeast-2": "au.guardrail.v1:0",
            "ca-central-1": "ca.guardrail.v1:0",
        }
        for region, profile_id in cases.items():
            params = build_guardrails_params(profile, region)
            assert "SafeguardTier=STANDARD" in params
            assert f"GuardrailProfileId={profile_id}" in params

    def test_region_without_a_guardrail_profile_falls_back_to_classic(self):
        assert "sa-east-1" not in GUARDRAIL_PROFILES
        profile = _profile(guardrails_safeguard_tier="STANDARD", allowed_bedrock_regions=["us-east-1", "sa-east-1"])
        assert guardrails_preflight(profile) == (True, None)
        plan = {r.region: r for r in guardrails_tier_plan(profile)[0]}
        assert plan["us-east-1"].tier == "STANDARD" and plan["sa-east-1"].tier == "CLASSIC"
        assert plan["sa-east-1"].classic_reason == "no guardrail profile in this Region"
        assert build_guardrails_params(profile, "sa-east-1") == build_guardrails_params(
            _profile(allowed_bedrock_regions=["us-east-1", "sa-east-1"]), "sa-east-1"
        )

    def test_preflight_accepts_standard_where_every_region_has_a_profile(self):
        profile = _profile(guardrails_safeguard_tier="STANDARD", allowed_bedrock_regions=["us-east-1", "us-west-2"])
        assert guardrails_preflight(profile) == (True, None)

    def test_preflight_rejects_unknown_tier(self):
        ok, msg = guardrails_preflight(_profile(guardrails_safeguard_tier="PREMIUM"))
        assert not ok and "CLASSIC or STANDARD" in msg

    def test_warns_when_allowed_regions_omit_profile_destinations(self):
        profile = _profile(guardrails_safeguard_tier="STANDARD", allowed_bedrock_regions=["us-east-1"])
        warnings = guardrails_standard_tier_warnings(profile)
        assert len(warnings) == 1
        assert "us-east-2" in warnings[0] and "us-west-2" in warnings[0]
        full = _profile(
            guardrails_safeguard_tier="STANDARD", allowed_bedrock_regions=["us-east-1", "us-east-2", "us-west-2"]
        )
        assert guardrails_standard_tier_warnings(full) == []
        assert guardrails_standard_tier_warnings(_profile(allowed_bedrock_regions=["us-east-1"])) == []

    def test_nested_answers_shape_loads_the_tier(self):
        profile = _profile(guardrails={"enabled": True, "safeguard_tier": "STANDARD"})
        assert profile.guardrails_safeguard_tier == "STANDARD"
        assert _profile(guardrails={"enabled": True}).guardrails_safeguard_tier == "CLASSIC"

    def test_profile_table_destinations_include_the_source_region_or_its_geography(self):
        """Each guardrail profile routes within one geography (docs table, read 2026-11-07)."""
        prefixes = {"us": ("us-east-", "us-west-"), "us-gov": ("us-gov-",), "eu": ("eu-", "il-")}
        exact = {"uk": lambda src: {src}, "au": lambda src: {src}, "ca": lambda src: {"ca-central-1", "ca-west-1"}}
        for source, (profile_id, destinations) in GUARDRAIL_PROFILES.items():
            assert source in destinations, (source, destinations)
            geo = profile_id.split(".")[0]
            if geo in prefixes:
                assert all(d.startswith(prefixes[geo]) for d in destinations), (source, destinations)
            if geo in exact:
                assert set(destinations) == exact[geo](source), (source, destinations)


def test_preflight_rejects_inference_profile_ids_in_the_include_list():
    """CloudFormation rejected us.anthropic.claude-haiku-4-5-20251001-v1:0 in IncludedModels at early
    validation (live, 2026-11-07); the preflight must stop it first and name the fix."""
    ok, msg = guardrails_preflight(
        _profile(guardrails_model_include_list=["us.anthropic.claude-haiku-4-5-20251001-v1:0"])
    )
    assert not ok
    assert "us.anthropic.claude-haiku-4-5-20251001-v1:0" in msg and "foundation-model IDs" in msg
    for good in ("anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-sonnet-5", "ALL"):
        assert guardrails_preflight(_profile(guardrails_model_include_list=[good])) == (True, None), good


# --- Review fixes: residency, empty Region list, auth-stack grant ---------------------------------


def test_preflight_rejects_standard_outside_a_residency_geography():
    """jp model routing stays in Japan; Tokyo's only guardrail profile (apac) evaluates elsewhere."""
    profile = _profile(
        aws_region="ap-northeast-1",
        cross_region_profile="jp",
        allowed_bedrock_regions=["ap-northeast-1"],
        guardrails_safeguard_tier="STANDARD",
    )
    ok, msg = guardrails_preflight(profile)
    assert not ok
    assert "ap-northeast-1" in msg and "apac.guardrail.v1:0" in msg
    for region in ("ap-south-1", "ap-northeast-2", "ap-southeast-1", "ap-southeast-2"):
        assert region in msg
    assert "ap-northeast-3" not in msg  # in the jp geography
    assert "CLASSIC" in msg
    # The legacy "japan" config key means the same geography.
    profile.cross_region_profile = "japan"
    assert not guardrails_preflight(profile)[0]
    # Classic is unaffected.
    profile.guardrails_safeguard_tier = "CLASSIC"
    assert guardrails_preflight(profile) == (True, None)


def test_preflight_allows_standard_inside_residency_geographies():
    cases = [
        ("eu-west-1", "eu", ["eu-west-1", "eu-central-1"]),
        ("eu-west-2", "eu", ["eu-west-2"]),  # uk profile evaluates in London only
        ("ap-southeast-2", "au", ["ap-southeast-2"]),
        ("us-gov-west-1", "us-gov", ["us-gov-west-1", "us-gov-east-1"]),
        ("ap-northeast-1", "apac", ["ap-northeast-1"]),  # not a residency prefix
    ]
    for region, prefix, allowed in cases:
        profile = _profile(
            aws_region=region,
            cross_region_profile=prefix,
            allowed_bedrock_regions=allowed,
            guardrails_safeguard_tier="STANDARD",
        )
        assert guardrails_preflight(profile) == (True, None), (region, prefix)


def test_preflight_names_the_empty_allowed_regions_list_for_standard():
    ok, msg = guardrails_preflight(_profile(allowed_bedrock_regions=[], guardrails_safeguard_tier="STANDARD"))
    assert not ok
    assert "needs an explicit allowed_bedrock_regions list" in msg
    assert guardrails_preflight(_profile(allowed_bedrock_regions=[]))[0]  # Classic unchanged


POLICY_ARN = "arn:aws:iam::123456789012:policy/gip-test-stack-BedrockAccessPolicy-ABC"


def _boto_clients(document=None, error=None):
    from botocore.exceptions import ClientError

    cfn, iam = MagicMock(), MagicMock()
    if error == "no-stack":
        cfn.describe_stack_resource.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack 'gip-test-stack' does not exist"}},
            "DescribeStackResource",
        )
    cfn.describe_stack_resource.return_value = {"StackResourceDetail": {"PhysicalResourceId": POLICY_ARN}}
    iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v3"}}
    if error == "access-denied":
        iam.get_policy_version.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "GetPolicyVersion"
        )
    iam.get_policy_version.return_value = {"PolicyVersion": {"Document": document}}
    return cfn, iam, lambda service, **_: {"cloudformation": cfn, "iam": iam}[service]


def _policy(*resources):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowOwnAccountGuardrails",
                "Effect": "Allow",
                "Action": "bedrock:ApplyGuardrail",
                "Resource": list(resources),
            }
        ],
    }


OLD_POLICY = _policy("arn:aws:bedrock:*:123456789012:guardrail/*")
NEW_POLICY = _policy("arn:aws:bedrock:*:123456789012:guardrail/*", "arn:aws:bedrock:*:123456789012:guardrail-profile/*")


def _deploy_guardrails_stack(profile, client_factory):
    from rich.console import Console

    cf_manager = MagicMock()
    with (
        patch("boto3.client", side_effect=client_factory),
        patch(
            "governed_inference_platform.cli.commands.deploy.CloudFormationManager",
            side_effect=AssertionError("tests must stay in the profile Region"),
        ),
    ):
        rc = DeployCommand()._deploy_stack("guardrails", profile, Console(), cf_manager)
    return rc, cf_manager


def _standard_profile(**overrides):
    return _profile(
        guardrails_enabled=True,
        guardrails_safeguard_tier="STANDARD",
        allowed_bedrock_regions=["us-east-1"],
        **overrides,
    )


def test_standard_deploy_stops_when_auth_stack_lacks_guardrail_profile_grant(capsys):
    cfn, iam, factory = _boto_clients(OLD_POLICY)
    rc, cf_manager = _deploy_guardrails_stack(_standard_profile(), factory)
    out = capsys.readouterr().out
    assert rc == 1
    assert "gip deploy auth" in out and "guardrail-profile/*" in out
    cf_manager.deploy_stack.assert_not_called()
    cfn.describe_stack_resource.assert_called_once_with(
        StackName="gip-test-stack", LogicalResourceId="BedrockAccessPolicy"
    )
    iam.get_policy_version.assert_called_once_with(PolicyArn=POLICY_ARN, VersionId="v3")


def test_standard_deploy_proceeds_when_auth_stack_has_the_grant(capsys):
    _, _, factory = _boto_clients(NEW_POLICY)
    rc, cf_manager = _deploy_guardrails_stack(_standard_profile(), factory)
    out = capsys.readouterr().out
    assert rc == 0
    cf_manager.deploy_stack.assert_called_once()
    assert "could not be verified" not in out


def test_standard_deploy_uses_the_profile_auth_stack_name_and_url_encoded_documents():
    import urllib.parse

    from governed_inference_platform.cli.commands.deploy import guardrails_auth_grant_status

    cfn, _, factory = _boto_clients(urllib.parse.quote(json.dumps(NEW_POLICY)))
    profile = _standard_profile(stack_names={"auth": "custom-auth"})
    with patch("boto3.client", side_effect=factory):
        assert guardrails_auth_grant_status(profile) == ("ok", None)
    cfn.describe_stack_resource.assert_called_once_with(
        StackName="custom-auth", LogicalResourceId="BedrockAccessPolicy"
    )


def test_standard_deploy_warns_and_continues_when_the_grant_cannot_be_read(capsys):
    for error in ("no-stack", "access-denied"):
        _, _, factory = _boto_clients(NEW_POLICY, error=error)
        rc, cf_manager = _deploy_guardrails_stack(_standard_profile(), factory)
        out = capsys.readouterr().out
        assert rc == 0, error
        assert "could not be verified" in out, error
        cf_manager.deploy_stack.assert_called_once()


def test_classic_deploy_never_reads_the_auth_policy():
    client = MagicMock(side_effect=AssertionError("Classic must not call AWS for the grant check"))
    rc, cf_manager = _deploy_guardrails_stack(
        _profile(guardrails_enabled=True, allowed_bedrock_regions=["us-east-1"]), client
    )
    assert rc == 0
    cf_manager.deploy_stack.assert_called()


# --- Standard tier applied per Region ----------------------------------------------------------


def _tiers(profile):
    plan, stop = guardrails_tier_plan(profile)
    return {r.region: r.tier for r in plan}, stop


def _standard(region, prefix, allowed):
    return _profile(
        aws_region=region,
        cross_region_profile=prefix,
        allowed_bedrock_regions=allowed,
        guardrails_safeguard_tier="STANDARD",
        guardrails_enabled=True,
    )


def _default_destinations(model_key, prefix):
    from governed_inference_platform.models import get_destination_regions_for_model_profile

    return list(get_destination_regions_for_model_profile(model_key, prefix))


def test_us_default_regions_use_standard_in_us_and_classic_in_ca_west_1():
    """The walkthrough default: a current US model's destinations include ca-west-1, which has no profile."""
    allowed = _default_destinations("sonnet-4-6", "us")
    assert "ca-west-1" in allowed
    tiers, stop = _tiers(_standard("us-east-1", "us", allowed))
    assert stop is None
    assert tiers["ca-west-1"] == "CLASSIC"
    assert all(tiers[r] == "STANDARD" for r in ("us-east-1", "us-east-2", "us-west-2") if r in tiers)
    profile = _standard("us-east-1", "us", allowed)
    assert guardrails_preflight(profile) == (True, None)
    assert "SafeguardTier=STANDARD" not in build_guardrails_params(profile, "ca-west-1")
    assert "GuardrailProfileId=us.guardrail.v1:0" in build_guardrails_params(profile, "us-east-1")


def test_au_default_regions_use_standard_in_sydney_and_classic_in_melbourne():
    allowed = _default_destinations("sonnet-4-6", "au")
    assert set(allowed) == {"ap-southeast-2", "ap-southeast-4"}
    profile = _standard("ap-southeast-2", "au", allowed)
    plan = {r.region: r for r in guardrails_tier_plan(profile)[0]}
    assert plan["ap-southeast-2"].tier == "STANDARD" and plan["ap-southeast-2"].guardrail_profile == "au.guardrail.v1:0"
    assert plan["ap-southeast-4"].tier == "CLASSIC"
    assert "outside the au data-residency geography" in plan["ap-southeast-4"].classic_reason
    assert guardrails_preflight(profile) == (True, None)


def test_jp_is_a_hard_stop_because_no_region_can_use_standard():
    allowed = _default_destinations("sonnet-4-6", "jp")
    ok, msg = guardrails_preflight(_standard("ap-northeast-1", "jp", allowed))
    assert not ok
    assert "can't be used in any guardrails Region" in msg
    assert "apac.guardrail.v1:0" in msg and "outside the jp data-residency geography" in msg
    assert "ap-northeast-3: no guardrail profile in this Region" in msg
    assert "CLASSIC" in msg


def test_eu_default_regions_are_all_standard():
    allowed = _default_destinations("sonnet-4-6", "eu")
    tiers, stop = _tiers(_standard("eu-west-1", "eu", allowed))
    assert stop is None and set(tiers.values()) == {"STANDARD"}, tiers


def test_global_uses_standard_where_a_profile_exists():
    allowed = _default_destinations("sonnet-4-6", "global")
    tiers, stop = _tiers(_standard("us-east-1", "global", allowed))
    assert stop is None
    for region, tier in tiers.items():
        assert tier == ("STANDARD" if region in GUARDRAIL_PROFILES else "CLASSIC"), region
    assert "STANDARD" in tiers.values() and "CLASSIC" in tiers.values()


def test_empty_allowed_regions_is_a_hard_stop_for_standard_only():
    ok, msg = guardrails_preflight(_standard("us-east-1", "us", []))
    assert not ok and "needs an explicit allowed_bedrock_regions list" in msg
    assert guardrails_preflight(_profile(allowed_bedrock_regions=[]))[0]


def test_classic_profile_never_gets_standard_parameters():
    profile = _profile(allowed_bedrock_regions=["us-east-1", "ca-west-1"])
    assert {r.tier for r in guardrails_tier_plan(profile)[0]} == {"CLASSIC"}
    for region in ("us-east-1", "ca-west-1"):
        assert not any(
            p.startswith(("SafeguardTier", "GuardrailProfileId")) for p in build_guardrails_params(profile, region)
        )


def test_show_commands_with_a_classic_fallback_region_returns_cleanly(capsys):
    from rich.console import Console

    profile = _standard("us-east-1", "us", ["us-east-1", "us-east-2", "us-west-2", "ca-west-1"])
    rc = DeployCommand()._show_deployment_commands("guardrails", profile, Console(width=200))
    out = capsys.readouterr().out
    assert rc == 0
    assert "ca-west-1" in out and "no guardrail profile in this Region" in out
    assert out.count("SafeguardTier=STANDARD") == 3
    # aws cloudformation deploy keeps an omitted parameter's previous value, so the Classic
    # fallback Region's printed command names the tier explicitly.
    assert out.count("SafeguardTier=CLASSIC") == 1 and out.count("GuardrailProfileId=") == 4


def test_full_deploy_stops_before_any_stack_on_a_guardrails_hard_stop(capsys):
    profile = _standard("ap-northeast-1", "jp", ["ap-northeast-1"])
    with patch.object(DeployCommand, "_deploy_stack") as deploy_stack:
        rc = _run_deploy(profile, "")
    assert rc == 1
    deploy_stack.assert_not_called()


def test_show_commands_returns_1_on_a_hard_stop(capsys):
    from rich.console import Console

    rc = DeployCommand()._show_deployment_commands(
        "guardrails", _standard("ap-northeast-1", "jp", ["ap-northeast-1"]), Console(width=200)
    )
    assert rc == 1
    assert "can't be used in any guardrails Region" in capsys.readouterr().out


def test_deploy_prints_the_per_region_table_and_deploys_classic_fallbacks(capsys):
    from rich.console import Console

    profile = _standard("us-east-1", "us", ["us-east-1", "ca-west-1"])
    cf_manager = MagicMock()
    regional = MagicMock()
    with (
        patch(
            "governed_inference_platform.cli.commands.deploy.guardrails_auth_grant_status",
            return_value=("ok", None),
        ),
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager", return_value=regional),
    ):
        rc = DeployCommand()._deploy_stack("guardrails", profile, Console(width=200), cf_manager)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Guardrails safeguard tier by Region" in out and "Classic: no guardrail profile" in out
    us_params = {p["ParameterKey"]: p["ParameterValue"] for p in cf_manager.deploy_stack.call_args.kwargs["parameters"]}
    ca_params = {p["ParameterKey"]: p["ParameterValue"] for p in regional.deploy_stack.call_args.kwargs["parameters"]}
    assert us_params["SafeguardTier"] == "STANDARD"
    assert "SafeguardTier" not in ca_params and "GuardrailProfileId" not in ca_params


def test_catalog_wide_default_regions_never_put_standard_outside_residency():
    """Every catalog model and profile, with its default destination Regions: no exception, and no
    Standard Region whose guardrail profile leaves a residency geography."""
    from governed_inference_platform.models import (
        CLAUDE_MODELS,
        get_available_profiles_for_model,
        residency_geography_regions,
    )

    checked = 0
    for model_key in CLAUDE_MODELS:
        for prefix in get_available_profiles_for_model(model_key):
            allowed = _default_destinations(model_key, prefix)
            if not allowed:
                continue
            region = allowed[0]
            profile = _standard(region, prefix, allowed)
            plan, stop = guardrails_tier_plan(profile)
            guardrails_preflight(profile)
            for r in plan:
                build_guardrails_params(profile, r.region)
                if r.tier != "STANDARD":
                    assert r.classic_reason, (model_key, prefix, r)
                    continue
                assert r.region in GUARDRAIL_PROFILES, (model_key, prefix, r)
                geography = residency_geography_regions(prefix)
                if geography is not None:
                    assert all(d == r.region or d in geography for d in r.destinations), (model_key, prefix, r)
            if stop is None:
                assert any(r.tier == "STANDARD" for r in plan), (model_key, prefix)
            checked += 1
    assert checked > 20


def test_region_tier_is_a_pure_function():
    with patch("boto3.client", side_effect=AssertionError("no AWS calls")):
        assert guardrails_region_tier(_standard("us-east-1", "us", ["us-east-1"]), "us-east-1").tier == "STANDARD"
