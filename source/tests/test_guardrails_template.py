from pathlib import Path

import yaml

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"


class CFLoader(yaml.SafeLoader):
    pass


def _construct(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for tag in ["!Ref", "!GetAtt", "!Sub", "!If", "!Equals", "!Not", "!Split"]:
    CFLoader.add_constructor(tag, _construct)


def _template() -> dict:
    with (INFRA_DIR / "guardrails-enforcement.yaml").open(encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


def test_creates_guardrail_version_enforcement_and_dashboard():
    resources = _template()["Resources"]

    assert resources["Guardrail"]["Type"] == "AWS::Bedrock::Guardrail"
    assert resources["GuardrailVersion"]["Type"] == "AWS::Bedrock::GuardrailVersion"
    assert resources["EnforcedGuardrailConfiguration"]["Type"] == "AWS::Bedrock::EnforcedGuardrailConfiguration"
    assert resources["GuardrailsDashboard"]["Type"] == "AWS::CloudWatch::Dashboard"


def test_guardrail_blocks_all_harmful_content_categories():
    guardrail = _template()["Resources"]["Guardrail"]["Properties"]
    filters = guardrail["ContentPolicyConfig"]["FiltersConfig"]

    assert {f["Type"] for f in filters} == {
        "HATE",
        "INSULTS",
        "SEXUAL",
        "VIOLENCE",
        "MISCONDUCT",
    }
    for content_filter in filters:
        assert content_filter["InputAction"] == "BLOCK"
        assert content_filter["OutputAction"] == "BLOCK"
        assert content_filter["InputEnabled"] is True
        assert content_filter["OutputEnabled"] is True
        assert content_filter["InputStrength"] == "ContentFilterStrength"
        assert content_filter["OutputStrength"] == "ContentFilterStrength"


def test_enforced_configuration_uses_version_and_selective_system_guarding():
    enforcement = _template()["Resources"]["EnforcedGuardrailConfiguration"]["Properties"]

    assert enforcement["GuardrailIdentifier"] == "GuardrailVersion.GuardrailArn"
    assert enforcement["GuardrailVersion"] == "GuardrailVersion.Version"
    assert enforcement["SelectiveContentGuarding"] == {
        "Messages": "COMPREHENSIVE",
        "System": "SELECTIVE",
    }


def test_model_enforcement_is_optional_empty_means_all_models():
    enforcement = _template()["Resources"]["EnforcedGuardrailConfiguration"]["Properties"]
    model_enforcement = enforcement["ModelEnforcement"]

    assert model_enforcement[0] == "HasModelIncludeList"
    assert model_enforcement[1]["IncludedModels"] == [",", "ModelIncludeList"]
    assert model_enforcement[1]["ExcludedModels"] == []
    assert model_enforcement[2] == "AWS::NoValue"


def test_dashboard_uses_metadata_only_guardrail_metrics():
    body = _template()["Resources"]["GuardrailsDashboard"]["Properties"]["DashboardBody"]
    dashboard_json = body[0]

    assert "AWS/Bedrock/Guardrails" in dashboard_json
    assert "Invocations" in dashboard_json
    assert "InvocationsIntervened" in dashboard_json
    assert "InvocationLatency" in dashboard_json
    assert "TextUnitCount" in dashboard_json
    assert "input" not in dashboard_json.lower()
    assert "output" not in dashboard_json.lower()


def test_dashboard_name_is_regional_and_version_links_policy_revision():
    resources = _template()["Resources"]
    params = _template()["Parameters"]

    dashboard_name = resources["GuardrailsDashboard"]["Properties"]["DashboardName"]
    assert dashboard_name == "${GuardrailName}-${AWS::Region}-guardrails"
    condition, standard, classic = resources["GuardrailVersion"]["Properties"]["Description"]
    assert condition == "UseStandardTier"
    assert params["PolicyRevision"]["Default"] == "v2"
    assert "Increment this value whenever any" in params["PolicyRevision"]["Description"]
    for description in (standard, classic):
        assert "${ContentFilterStrength}" in description
        assert "${PolicyRevision}" in description
        assert "policy-v2" not in description


def test_safeguard_tier_is_opt_in_and_classic_leaves_the_guardrail_unchanged():
    """CLASSIC is the default and adds no tier, cross-Region or version-description change,
    so existing stacks are not updated. The tier is part of the Standard version description
    (F-019), so switching tiers replaces the immutable version."""
    template = _template()
    params = template["Parameters"]
    guardrail = template["Resources"]["Guardrail"]["Properties"]

    assert params["SafeguardTier"]["Default"] == "CLASSIC"
    assert params["SafeguardTier"]["AllowedValues"] == ["CLASSIC", "STANDARD"]
    assert params["GuardrailProfileId"]["Default"] == ""
    # The loader drops the !Equals/!Ref tags, leaving their arguments.
    assert template["Conditions"]["UseStandardTier"] == ["SafeguardTier", "STANDARD"]

    assert guardrail["ContentPolicyConfig"]["ContentFiltersTierConfig"] == [
        "UseStandardTier",
        {"TierName": "STANDARD"},
        "AWS::NoValue",
    ]
    condition, cross_region, no_value = guardrail["CrossRegionConfig"]
    assert (condition, no_value) == ("UseStandardTier", "AWS::NoValue")
    assert cross_region["GuardrailProfileArn"] == (
        "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}:guardrail-profile/${GuardrailProfileId}"
    )

    _, standard, classic = template["Resources"]["GuardrailVersion"]["Properties"]["Description"]
    assert (
        classic == "Immutable version for ${GuardrailName} strength ${ContentFilterStrength} policy ${PolicyRevision}"
    )
    assert "tier STANDARD profile ${GuardrailProfileId}" in standard


def test_standard_tier_requires_a_guardrail_profile():
    rule = _template()["Rules"]["StandardTierNeedsGuardrailProfile"]
    assert rule["RuleCondition"] == ["SafeguardTier", "STANDARD"]
    assert rule["Assertions"][0]["Assert"] == [["GuardrailProfileId", ""]]  # !Not [!Equals [!Ref ..., '']]
    assert "GuardrailProfileId" in rule["Assertions"][0]["AssertDescription"]
