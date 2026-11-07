"""gip init guardrails prompts: an aborted safeguard-tier prompt aborts the wizard."""

from unittest.mock import MagicMock, patch

from governed_inference_platform.cli.commands.init import InitCommand


def _answers(confirm, strength, tier):
    def select(message, *args, **kwargs):
        prompt = MagicMock()
        prompt.ask.return_value = strength if "strength" in str(message) else tier
        return prompt

    confirm_prompt = MagicMock()
    confirm_prompt.ask.return_value = confirm
    return (
        patch("governed_inference_platform.cli.commands.init.questionary.confirm", return_value=confirm_prompt),
        patch("governed_inference_platform.cli.commands.init.questionary.select", side_effect=select),
    )


def _run(confirm, strength, tier, config=None):
    config = config if config is not None else {}
    confirm_patch, select_patch = _answers(confirm, strength, tier)
    with confirm_patch, select_patch:
        result = InitCommand()._prompt_guardrails(config)
    return result, config


def test_ctrl_c_at_the_tier_prompt_aborts_instead_of_choosing_classic():
    result, config = _run(True, "HIGH", None, {"guardrails": {"safeguard_tier": "STANDARD"}})
    assert result is False
    assert config["guardrails"]["safeguard_tier"] == "STANDARD"  # not silently rewritten to CLASSIC


def test_tier_answer_is_stored():
    for tier in ("CLASSIC", "STANDARD"):
        result, config = _run(True, "MEDIUM", tier)
        assert result is True
        assert config["guardrails"] == {"enabled": True, "content_filter_strength": "MEDIUM", "safeguard_tier": tier}


def test_disabled_guardrails_skip_the_tier_prompt():
    result, config = _run(False, None, None)
    assert result is True
    assert config["guardrails"] == {"enabled": False}
