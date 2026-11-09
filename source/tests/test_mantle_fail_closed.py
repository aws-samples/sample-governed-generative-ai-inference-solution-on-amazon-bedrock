from pathlib import Path

import pytest
import yaml

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"

GOVERNED_BEDROCK_TEMPLATES = [
    "bedrock-auth-okta.yaml",
    "bedrock-auth-azure.yaml",
    "bedrock-auth-auth0.yaml",
    "bedrock-auth-google.yaml",
    "bedrock-auth-generic.yaml",
    "bedrock-auth-cognito-pool.yaml",
    "bedrock-auth-idc.yaml",
    "cognito-identity-pool.yaml",
]

NON_RUNTIME_GATEWAY_TEMPLATES = ["bedrock-agentcore-gateway.yaml"]


class CFLoader(yaml.SafeLoader):
    pass


for tag in [
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
    CFLoader.add_constructor(
        tag,
        lambda loader, node: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
        ),
    )


def _load_template(name: str) -> dict:
    with (INFRA_DIR / name).open(encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action")
    if isinstance(action, str):
        return [action]
    if isinstance(action, list):
        return [item for item in action if isinstance(item, str)]
    return []


@pytest.mark.parametrize("template_name", GOVERNED_BEDROCK_TEMPLATES)
def test_governed_bedrock_credentials_explicitly_deny_mantle(template_name):
    doc = _load_template(template_name)
    statements = [node for node in _walk(doc.get("Resources", {})) if isinstance(node, dict) and node.get("Sid")]
    deny = [statement for statement in statements if statement.get("Sid") == "DenyBedrockMantleEndpoint"]

    assert deny == [
        {"Sid": "DenyBedrockMantleEndpoint", "Effect": "Deny", "Action": "bedrock-mantle:*", "Resource": "*"}
    ]
    assert not [
        statement
        for statement in statements
        if statement.get("Effect") == "Allow"
        and any(action.startswith("bedrock-mantle:") for action in _actions(statement))
    ]


@pytest.mark.parametrize("template_name", NON_RUNTIME_GATEWAY_TEMPLATES)
def test_non_runtime_agentcore_gateway_does_not_reference_mantle(template_name):
    text = (INFRA_DIR / template_name).read_text(encoding="utf-8")
    assert "bedrock-mantle" not in text


@pytest.mark.parametrize("template_name", GOVERNED_BEDROCK_TEMPLATES)
def test_mantle_deny_is_unconditional_and_first_even_with_openai_admitted(template_name):
    """ADR-0037: admitting OpenAI on bedrock-runtime must not touch the Mantle deny (ADR-0022)."""
    doc = _load_template(template_name)
    policies = [r for r in doc["Resources"].values() if r.get("Type") == "AWS::IAM::ManagedPolicy"]
    assert len(policies) == 1
    statements = policies[0]["Properties"]["PolicyDocument"]["Statement"]
    # First statement, a plain dict (never wrapped in !If), so no parameter can remove it.
    assert statements[0] == {
        "Sid": "DenyBedrockMantleEndpoint",
        "Effect": "Deny",
        "Action": "bedrock-mantle:*",
        "Resource": "*",
    }
    openai_gated = [s for s in statements if isinstance(s, list) and s and s[0] == "OpenAIModelsAllowed"]
    assert openai_gated, "expected the AllowOpenAIModels statements"
    for _cond, statement, _novalue in openai_gated:
        assert all(not a.startswith("bedrock-mantle:") for a in _actions(statement))
