"""The federated role can export the sidecar's OTLP metrics and use Claude Desktop's short-term
Amazon Bedrock API key, without widening classic metric publishing.

Found by an end-to-end test (2026-10-27): the namespace-scoped PutMetricData statement denied
OTLP ingest (HTTP 403 on .../dataset/default, because OTLP requests carry no
cloudwatch:namespace), and a correctly signed bearer token failed with "not authorized to
perform: bedrock:CallWithBearerToken on resource: *" (the action has no resource type).
"""

import json
from pathlib import Path

import pytest
import yaml

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"

TEMPLATES = [
    "bedrock-auth-okta.yaml",
    "bedrock-auth-azure.yaml",
    "bedrock-auth-auth0.yaml",
    "bedrock-auth-google.yaml",
    "bedrock-auth-generic.yaml",
    "bedrock-auth-cognito-pool.yaml",
    "bedrock-auth-idc.yaml",
    "cognito-identity-pool.yaml",
]

USAGE_NAMESPACES = ["GIP/Bedrock/Usage", "AWS/Bedrock"]
OTLP_DATASET = "arn:${AWS::Partition}:cloudwatch:*:${AWS::AccountId}:dataset/default"


class CFLoader(yaml.SafeLoader):
    pass


def _construct(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


for _tag in [
    "!Ref",
    "!Sub",
    "!GetAtt",
    "!If",
    "!Equals",
    "!Not",
    "!Select",
    "!Join",
    "!Split",
    "!FindInMap",
    "!Condition",
    "!Or",
    "!And",
]:
    CFLoader.add_constructor(_tag, _construct)


def _statements(name):
    with (INFRA_DIR / name).open(encoding="utf-8") as f:
        doc = yaml.load(f, Loader=CFLoader)  # nosec B506
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "Sid" in node and "Effect" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def _actions(stmt):
    action = stmt.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


@pytest.mark.parametrize("name", TEMPLATES)
def test_otlp_metrics_allowed_only_on_the_dataset_and_only_without_a_namespace(name):
    otlp = [
        s for s in _statements(name) if "cloudwatch:PutMetricData" in _actions(s) and s.get("Resource") == OTLP_DATASET
    ]
    assert len(otlp) == 1, f"{name}: expected one OTLP dataset statement, found {len(otlp)}"
    assert otlp[0]["Sid"] == "AllowCloudWatchOtlpMetrics", name  # same Sid in every template
    assert otlp[0]["Effect"] == "Allow"
    assert otlp[0]["Condition"]["Null"] == {"cloudwatch:namespace": "true"}, name


@pytest.mark.parametrize("name", TEMPLATES)
def test_classic_metric_publishing_stays_scoped_to_the_usage_namespaces(name):
    for stmt in _statements(name):
        if "cloudwatch:PutMetricData" not in _actions(stmt) or stmt.get("Resource") == OTLP_DATASET:
            continue
        cond = stmt.get("Condition", {})
        assert "StringEqualsIfExists" not in cond, f"{name}: {stmt['Sid']} must not use IfExists"
        assert cond["StringEquals"]["cloudwatch:namespace"] == USAGE_NAMESPACES, f"{name}: {stmt['Sid']}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_short_term_bearer_token_allowed_in_allowed_regions_only(name):
    bearer = [
        s for s in _statements(name) if _actions(s) == ["bedrock:CallWithBearerToken"] and s.get("Resource") == "*"
    ]
    assert len(bearer) == 1, f"{name}: expected one CallWithBearerToken statement on *, found {len(bearer)}"
    cond = bearer[0]["Condition"]["StringEquals"]
    assert cond["aws:RequestedRegion"] == "AllowedBedrockRegions", name
    assert cond["bedrock:BearerTokenType"] == "SHORT_TERM", name


def test_idc_setup_guide_lists_the_statements_the_permission_set_needs():
    """IDC credential-process returns the permission set's credentials and never assumes
    BedrockIDCRole, so the guide must give the permission set the same two statements."""
    guide = (INFRA_DIR.parent.parent / "assets" / "docs" / "providers" / "iam-identity-center-setup.md").read_text(
        encoding="utf-8"
    )
    section = guide[guide.index("#### Sidecar telemetry and Claude Desktop") :]
    block = section[section.index("```json") + len("```json") : section.index("```", section.index("```json") + 7)]
    documented = {s["Sid"]: s for s in json.loads(f"[{block}]")}

    okta = {s["Sid"]: s for s in _statements("bedrock-auth-okta.yaml")}
    for sid in ("AllowCloudWatchOtlpMetrics", "AllowBedrockShortTermBearerToken"):
        expected = json.loads(
            json.dumps(okta[sid])
            .replace("${AWS::Partition}", "aws")
            .replace("${AWS::AccountId}", "<ACCOUNT_ID>")
            .replace('"AllowedBedrockRegions"', '["<REGION_1>", "<REGION_2>"]')
        )
        assert documented[sid] == expected, sid


def _template(name):
    with (INFRA_DIR / name).open(encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


@pytest.mark.parametrize("name", TEMPLATES)
def test_otlp_grant_only_in_sidecar_mode(name):
    """Central mode exports through the ALB collector (JWT-validated attribution), so a direct
    dataset write there would let users post telemetry around it (review, 2026-10-28)."""
    doc = _template(name)
    param = doc["Parameters"]["MonitoringMode"]
    assert param["Default"] == "central", name
    assert sorted(param["AllowedValues"]) == ["central", "sidecar"], name
    assert doc["Conditions"]["SidecarOtlpIngest"] == ["MonitoringEnabled", ["MonitoringMode", "sidecar"]], name

    gated = []

    def walk(node):
        if isinstance(node, list):
            if (
                len(node) == 3
                and node[0] == "SidecarOtlpIngest"
                and isinstance(node[1], dict)
                and node[1].get("Sid") == "AllowCloudWatchOtlpMetrics"
            ):
                gated.append(node[1])
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(doc["Resources"])
    otlp = [s for s in _statements(name) if s.get("Sid") == "AllowCloudWatchOtlpMetrics"]
    assert len(otlp) == 1 and gated == otlp, f"{name}: the OTLP statement must sit under !If SidecarOtlpIngest"


def test_deploy_passes_monitoring_mode_to_every_auth_template():
    deploy = (
        Path(__file__).parent.parent / "governed_inference_platform" / "cli" / "commands" / "deploy.py"
    ).read_text(encoding="utf-8")
    assert deploy.count('f"EnableMonitoring={str(profile.monitoring_enabled).lower()}",') == 4
    assert deploy.count('f"MonitoringMode={_monitoring_mode_param(profile)}",') == 4


@pytest.mark.parametrize(("mode", "expected"), [("sidecar", "sidecar"), ("central", "central"), (None, "central")])
def test_monitoring_mode_param(mode, expected):
    from types import SimpleNamespace

    from governed_inference_platform.cli.commands.deploy import _monitoring_mode_param

    profile = SimpleNamespace() if mode is None else SimpleNamespace(monitoring_mode=mode)
    assert _monitoring_mode_param(profile) == expected
