# ABOUTME: Regression tests for metadata-only Bedrock invocation-logging adoption
# ABOUTME: Verifies every content-delivery flag blocks adoption while metadata-only configs remain adoptable

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "metering_config"
    / "index.py"
)


@pytest.fixture
def mod(monkeypatch):
    return _load_lambda(monkeypatch)


def _load_lambda(monkeypatch):
    module_name = "metering_config_index_test"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.send_response = MagicMock()
    # monkeypatch restores boto3.client afterwards: module.boto3 is the real, process-wide boto3.
    monkeypatch.setattr(module.boto3, "client", MagicMock())
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return module


def _event():
    return {
        "RequestType": "Create",
        "ResourceProperties": {
            "LogGroupName": "/aws/bedrock/gip-metering",
            "RoleArn": "arn:aws:iam::123456789012:role/MeteringRole",
            "AdoptExistingConfig": "true",
        },
    }


@pytest.mark.parametrize(
    "enabled_flag",
    [
        "textDataDeliveryEnabled",
        "imageDataDeliveryEnabled",
        "embeddingDataDeliveryEnabled",
        "videoDataDeliveryEnabled",
        "audioDataDeliveryEnabled",
    ],
)
def test_adoption_rejects_every_content_delivery_flag(mod, enabled_flag):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = {
        "loggingConfig": {
            "cloudWatchConfig": {"logGroupName": "/customer/bedrock"},
            enabled_flag: True,
        }
    }

    mod.lambda_handler(_event(), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "FAILED"
    assert enabled_flag in response.kwargs["reason"]
    bedrock.put_model_invocation_logging_configuration.assert_not_called()


def test_adoption_preserves_metadata_only_existing_config(mod):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = {
        "loggingConfig": {
            "cloudWatchConfig": {"logGroupName": "/customer/bedrock"},
            **dict.fromkeys(mod.DATA_DELIVERY_FLAGS, False),
        }
    }

    mod.lambda_handler(_event(), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "SUCCESS"
    assert response.kwargs["data"] == {"LogGroupName": "/customer/bedrock", "CreatedConfig": "false"}
    bedrock.put_model_invocation_logging_configuration.assert_not_called()


# The lifecycle below is what QUOTA_MONITORING.md documents for AdoptExistingConfig.

METADATA_ONLY_CUSTOMER_CONFIG = {
    "loggingConfig": {
        "cloudWatchConfig": {"logGroupName": "/customer/bedrock"},
        "textDataDeliveryEnabled": False,
        "imageDataDeliveryEnabled": False,
        "embeddingDataDeliveryEnabled": False,
    }
}


def _lifecycle_event(request_type, adopt="true", physical_id=None):
    event = _event()
    event["RequestType"] = request_type
    event["ResourceProperties"]["AdoptExistingConfig"] = adopt
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def test_adopt_without_an_existing_config_creates_a_metadata_only_one(mod):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = {}

    mod.lambda_handler(_lifecycle_event("Create"), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "SUCCESS"
    assert response.kwargs["physical_id"] == "bedrock-invocation-logging-us-east-1-created"
    config = bedrock.put_model_invocation_logging_configuration.call_args.kwargs["loggingConfig"]
    assert config["cloudWatchConfig"]["logGroupName"] == "/aws/bedrock/gip-metering"
    assert not any(config[flag] for flag in mod.DATA_DELIVERY_FLAGS)


def test_update_of_an_adopted_config_keeps_it_and_the_physical_id(mod):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = METADATA_ONLY_CUSTOMER_CONFIG
    adopted = "bedrock-invocation-logging-us-east-1-adopted"

    mod.lambda_handler(_lifecycle_event("Update", physical_id=adopted), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "SUCCESS"
    assert response.kwargs["physical_id"] == adopted  # same ID: no replacement, no Delete
    bedrock.put_model_invocation_logging_configuration.assert_not_called()


def test_update_that_turns_adoption_off_fails_closed(mod):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = METADATA_ONLY_CUSTOMER_CONFIG

    mod.lambda_handler(
        _lifecycle_event("Update", adopt="false", physical_id="bedrock-invocation-logging-us-east-1-adopted"),
        MagicMock(),
    )

    assert mod.send_response.call_args.args[2] == "FAILED"
    bedrock.put_model_invocation_logging_configuration.assert_not_called()


def test_delete_leaves_an_adopted_config_in_place(mod):
    bedrock = mod.boto3.client.return_value

    mod.lambda_handler(
        _lifecycle_event("Delete", physical_id="bedrock-invocation-logging-us-east-1-adopted"), MagicMock()
    )

    assert mod.send_response.call_args.args[2] == "SUCCESS"
    bedrock.delete_model_invocation_logging_configuration.assert_not_called()


def test_delete_removes_a_config_this_stack_created(mod):
    bedrock = mod.boto3.client.return_value

    mod.lambda_handler(
        _lifecycle_event("Delete", physical_id="bedrock-invocation-logging-us-east-1-created"), MagicMock()
    )

    assert mod.send_response.call_args.args[2] == "SUCCESS"
    bedrock.delete_model_invocation_logging_configuration.assert_called_once()


def test_lambda_loader_does_not_leak_the_boto3_client_mock():
    """The loader patches the real, process-wide boto3; a leaked MagicMock broke later CLI tests (test_init.py)."""
    import boto3

    real_client = boto3.client
    with pytest.MonkeyPatch.context() as mp:
        module = _load_lambda(mp)
        assert module.boto3 is boto3
        assert isinstance(boto3.client, MagicMock)
    assert boto3.client is real_client
