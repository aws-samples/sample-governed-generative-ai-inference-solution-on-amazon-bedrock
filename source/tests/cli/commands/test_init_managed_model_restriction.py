"""gip init: managed default model (lock_default_model) and the opt-in restrict_model_selection.

Covers the wizard prompts, the save -> reload round trip (config-sync rule) and the answers file.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.cli.commands.init_answers import build_config_from_answers
from governed_inference_platform.config import Config, Profile

LOCK_PROMPT = (
    "Set the default model for all users in managed settings? "
    "(A default, not a lock; IAM limits which models they can use)"
)
RESTRICT_PROMPT = (
    "Also restrict Claude Code to the model you select below? (writes availableModels; Claude Code v2.1.175 or later)"
)


def _run_prompts(answers, config=None):
    """Answer the managed-scope confirms in order; return (config, prompts asked, output)."""
    config = config if config is not None else {}
    asked = []
    queue = list(answers)

    def confirm(message, *args, **kwargs):
        asked.append((message, kwargs.get("default")))
        prompt = MagicMock()
        prompt.ask.return_value = queue.pop(0)
        return prompt

    printed = []
    with (
        patch("governed_inference_platform.cli.commands.init.questionary.confirm", side_effect=confirm),
        patch("governed_inference_platform.cli.commands.init.Console") as console_cls,
    ):
        console_cls.return_value.print.side_effect = lambda *a, **k: printed.append(" ".join(map(str, a)))
        completed = InitCommand()._prompt_managed_model_settings(config)
    config["_completed"] = completed
    return config, asked, "\n".join(printed)


class TestWizardPrompts:
    @pytest.mark.parametrize("answers", [[None], [True, None]])
    def test_ctrl_c_at_either_prompt_aborts(self, answers):
        config, _, _ = _run_prompts(answers)
        assert config["_completed"] is False

    def test_answered_prompts_complete(self):
        assert _run_prompts([True, True])[0]["_completed"] is True
        assert _run_prompts([False])[0]["_completed"] is True

    def test_default_only(self):
        config, asked, out = _run_prompts([True, False])
        assert [m for m, _ in asked] == [LOCK_PROMPT, RESTRICT_PROMPT]
        assert config["lock_default_model"] is True and config["restrict_model_selection"] is False
        assert "A default only" in out

    def test_default_and_restriction(self):
        config, _, out = _run_prompts([True, True])
        assert config["restrict_model_selection"] is True
        assert "Haiku-tier" in out and "IAM stays the boundary" in out

    def test_no_default_skips_the_restriction_and_says_nothing_is_pinned(self):
        config, asked, out = _run_prompts([False], {"restrict_model_selection": True})
        assert [m for m, _ in asked] == [LOCK_PROMPT]
        assert config["lock_default_model"] is False and config["restrict_model_selection"] is False
        assert "no default model or model pins" in out
        assert "CRIS routing still applied" not in out

    def test_saved_values_seed_the_defaults(self):
        _, asked, _ = _run_prompts([True, True], {"lock_default_model": True, "restrict_model_selection": True})
        assert [d for _, d in asked] == [True, True]

    def test_restriction_prompt_does_not_claim_a_model_is_already_selected(self):
        assert "the model you select below" in RESTRICT_PROMPT
        assert "selected model" not in RESTRICT_PROMPT


def _make_profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "example.okta.com",
        "client_id": "0oa0000000000000000",
        "identity_pool_name": "gip-auth",
        "credential_storage": "keyring",
        "aws_region": "us-east-1",
        "settings_target": "managed",
        "lock_default_model": True,
    }
    data.update(overrides)
    return Profile(**data)


def _rebuild_config(profile: Profile) -> dict:
    cfg = MagicMock(spec=Config)
    cfg.get_profile.return_value = profile
    with (
        patch("governed_inference_platform.cli.commands.init.Config.load", return_value=cfg),
        patch.object(InitCommand, "_stack_exists", return_value=False),
    ):
        return InitCommand()._check_existing_deployment("test")


class TestRoundTrip:
    def test_reload_restores_the_restriction(self):
        for value in (True, False):
            rebuilt = _rebuild_config(_make_profile(restrict_model_selection=value))
            assert rebuilt["restrict_model_selection"] is value
            assert rebuilt["lock_default_model"] is True

    def test_save_writes_the_restriction_to_the_profile(self, tmp_path):
        config_dir = tmp_path / ".gip"
        profiles_dir = config_dir / "profiles"
        profiles_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"schema_version": "2.0", "active_profile": None}))
        answers = {
            "okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"},
            "settings_target": "managed",
            "lock_default_model": True,
            "restrict_model_selection": True,
        }
        config, errors = build_config_from_answers(answers)
        assert errors == []
        with (
            patch.object(Config, "CONFIG_DIR", config_dir),
            patch.object(Config, "CONFIG_FILE", config_file),
            patch.object(Config, "PROFILES_DIR", profiles_dir),
            patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
        ):
            InitCommand()._save_configuration(config, "ci")
        saved = json.loads((profiles_dir / "ci.json").read_text())
        assert saved["restrict_model_selection"] is True and saved["lock_default_model"] is True


class TestAnswersFile:
    def test_defaults_to_false(self):
        config, errors = build_config_from_answers(
            {"okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"}}
        )
        assert errors == [] and config["restrict_model_selection"] is False

    def test_rejects_a_non_boolean(self):
        _, errors = build_config_from_answers(
            {
                "okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"},
                "restrict_model_selection": "yes",
            }
        )
        assert any(e.startswith("restrict_model_selection:") for e in errors), errors


def test_profile_has_the_field_with_a_false_default():
    assert Profile.__dataclass_fields__["restrict_model_selection"].default is False


@pytest.mark.parametrize(
    ("target", "lock", "warned"),
    [("user", True, True), ("managed", False, True), ("managed", True, False)],
)
def test_summary_warns_when_the_restriction_has_no_effect(target, lock, warned):
    from rich.console import Console

    from governed_inference_platform.cli.commands.init_answers import _print_summary

    console = Console(record=True, width=200)
    config = {
        "auth_type": "none",
        "aws": {"region": "us-east-1", "identity_pool_name": "p"},
        "monitoring": {"enabled": False},
        "quota": {"enabled": False},
        "distribution": {},
        "settings_target": target,
        "lock_default_model": lock,
        "restrict_model_selection": True,
    }
    _print_summary(console, config, "p")
    assert ("restrict_model_selection has no effect" in console.export_text()) is warned
