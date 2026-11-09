# ABOUTME: Regression tests for the allow_openai_models profile field (ADR-0037, ChatGPT Work / Codex)
# ABOUTME: Covers answers-file default/validation/round-trip, wizard save/reload and all three auth modes

"""`allow_openai_models` flows answers.yaml -> profile -> `gip deploy` (AllowOpenAIModels).

Backwards compatibility: absent in old profiles and answers files -> False, so
existing deployments keep the Anthropic-only policy unchanged.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.config import Config, Profile
from tests.support.cli import CliTester

OIDC_YAML = """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
"""


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


@pytest.fixture
def tester():
    return CliTester(InitCommand())


def _load(profiles_dir: Path, name: str) -> dict:
    return json.loads((profiles_dir / f"{name}.json").read_text())


LEGACY_PROFILE = {
    "name": "legacy",
    "provider_domain": "company.okta.com",
    "client_id": "0oa0000000000000000",
    "identity_pool_name": "gip-auth",
    "credential_storage": "session",
    "aws_region": "us-east-1",
}


def test_profile_default_is_false():
    assert Profile(**LEGACY_PROFILE).allow_openai_models is False


def test_old_profile_dict_without_field_loads_as_false():
    assert Profile.from_dict(dict(LEGACY_PROFILE)).allow_openai_models is False


def test_answers_default_is_false(config_paths, tester, tmp_path):
    answers = tmp_path / "a.yaml"
    answers.write_text(OIDC_YAML)
    assert tester.run(f"--from-file {answers} --profile-name ci") == 0
    assert _load(config_paths, "ci")["allow_openai_models"] is False


@pytest.mark.parametrize(
    ("auth_yaml", "name"),
    [
        (OIDC_YAML, "oidc"),
        ("auth_type: idc\nidc_start_url: https://company.awsapps.com/start\nidc_account_id: '123456789012'\n", "idc"),
        ("auth_type: none\n", "none"),
    ],
)
def test_answers_true_is_saved_for_every_auth_mode(config_paths, tester, tmp_path, auth_yaml, name):
    answers = tmp_path / "a.yaml"
    answers.write_text(auth_yaml + "aws:\n  allow_openai_models: true\n")
    assert tester.run(f"--from-file {answers} --profile-name {name}") == 0
    assert _load(config_paths, name)["allow_openai_models"] is True


def test_answers_non_bool_is_rejected(config_paths, tester, tmp_path, capsys):
    answers = tmp_path / "a.yaml"
    answers.write_text(OIDC_YAML + "aws:\n  allow_openai_models: 'yes'\n")
    assert tester.run(f"--from-file {answers} --profile-name bad") == 1
    assert "aws.allow_openai_models" in capsys.readouterr().out


def test_export_import_round_trip_keeps_true(config_paths, tester, tmp_path):
    answers = tmp_path / "a.yaml"
    answers.write_text(OIDC_YAML + "aws:\n  allow_openai_models: true\n")
    assert tester.run(f"--from-file {answers} --profile-name original") == 0
    exported = tmp_path / "exported.yaml"
    assert tester.run(f"--export-answers {exported} --profile-name original") == 0
    assert tester.run(f"--from-file {exported} --profile-name reimported") == 0
    assert _load(config_paths, "reimported")["allow_openai_models"] is True


def test_wizard_rerun_reloads_saved_value(config_paths, tester, tmp_path):
    """config-sync.md round-trip rule: _check_existing_deployment mirrors _save_configuration."""
    answers = tmp_path / "a.yaml"
    answers.write_text(OIDC_YAML + "aws:\n  allow_openai_models: true\n")
    assert tester.run(f"--from-file {answers} --profile-name rerun") == 0
    rebuilt = InitCommand()._check_existing_deployment("rerun")
    assert rebuilt["aws"]["allow_openai_models"] is True


def test_eu_summary_prints_global_routing_residency_warning(config_paths, tester, tmp_path, capsys):
    answers = tmp_path / "a.yaml"
    answers.write_text(
        OIDC_YAML + "aws:\n  region: eu-central-1\n  cross_region_profile: eu\n  allow_openai_models: true\n"
    )
    assert tester.run(f"--from-file {answers} --profile-name eu") == 0
    out = capsys.readouterr().out
    assert "Global" in out and "global.openai" in out
