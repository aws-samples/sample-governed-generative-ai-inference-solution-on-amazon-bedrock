# ABOUTME: Tests for the OpenAI (bedrock-runtime) pricing families in shared/pricing.py
# ABOUTME: Pins the model-card rates, Global vs US Geo routing, long-context switch and unpriced fallback

"""OpenAI pricing families (ChatGPT Work / Codex on bedrock-runtime, ADR-0037).

Rates are pinned to the Amazon Bedrock model cards retrieved 2026-10-02 (URLs in
shared/pricing.py). A changed number here must come with a changed source.
"""

import sys
from pathlib import Path

import pytest

SHARED_PARENT = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions"
if str(SHARED_PARENT) not in sys.path:
    sys.path.insert(0, str(SHARED_PARENT))

from shared import pricing  # noqa: E402

CARD_RATES = {
    # family: (input, cache_write, cache_read, output) per 1M tokens, short context
    "openai-gpt-6-sol-global": (2.00, 2.50, 0.20, 10.00),
    "openai-gpt-6-sol-us": (2.20, 2.75, 0.22, 11.00),
    "openai-gpt-6-luna-global": (0.10, 0.125, 0.01, 0.50),
    "openai-gpt-6-luna-us": (0.11, 0.1375, 0.011, 0.55),
    "openai-gpt-6-astra-global": (10.00, 12.50, 1.00, 50.00),
    "openai-gpt-6-astra-us": (11.00, 13.75, 1.10, 55.00),
    "openai-gpt-6-1-sol-global": (2.00, 2.50, 0.10, 10.00),
    "openai-gpt-6-1-sol-us": (2.20, 2.75, 0.11, 11.00),
    "openai-gpt-5-6-sol-global": (4.00, 5.00, 0.40, 20.00),
    "openai-gpt-5-6-sol-us": (4.40, 5.50, 0.44, 22.00),
    "openai-gpt-5-6-terra-global": (2.00, 2.50, 0.20, 12.00),
    "openai-gpt-5-6-terra-us": (2.20, 2.75, 0.22, 13.20),
    "openai-gpt-5-6-luna-global": (0.20, 0.25, 0.02, 1.20),
    "openai-gpt-5-6-luna-us": (0.22, 0.275, 0.022, 1.32),
}


@pytest.fixture(autouse=True)
def _reset_warnings():
    pricing.reset_pricing_warnings()
    yield
    pricing.reset_pricing_warnings()


@pytest.mark.parametrize(("family", "card"), sorted(CARD_RATES.items()))
def test_short_context_rates_match_model_cards(family, card):
    rates = pricing.get_rates()[family]
    assert (rates["input"], rates["cache_write"], rates["cache_read"], rates["output"]) == card


@pytest.mark.parametrize("family", sorted(CARD_RATES))
def test_cache_write_is_one_point_two_five_times_input(family):
    rates = pricing.get_rates()[family]
    assert rates["cache_write"] == pytest.approx(rates["input"] * 1.25)


@pytest.mark.parametrize("family", sorted(CARD_RATES))
def test_every_family_has_long_context_rates_at_least_short(family):
    rates = pricing.get_rates()
    long_rates = rates[family + "-long"]
    for key in ("input", "output", "cache_read", "cache_write"):
        assert long_rates[key] >= rates[family][key]


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("global.openai.gpt-6-sol", "openai-gpt-6-sol-global"),
        ("us.openai.gpt-6-sol", "openai-gpt-6-sol-us"),
        (
            "arn:aws:bedrock:eu-central-1:123456789012:inference-profile/global.openai.gpt-6-sol",
            "openai-gpt-6-sol-global",
        ),
        ("global.openai.gpt-6.1-sol", "openai-gpt-6-1-sol-global"),
        ("us.openai.gpt-6.1-sol", "openai-gpt-6-1-sol-us"),
        ("global.openai.gpt-6-luna", "openai-gpt-6-luna-global"),
        ("global.openai.gpt-6-astra", "openai-gpt-6-astra-global"),
        ("us.openai.gpt-6-astra", "openai-gpt-6-astra-us"),
        ("global.openai.gpt-5.6-sol", "openai-gpt-5-6-sol-global"),
        ("us.openai.gpt-5.6-terra", "openai-gpt-5-6-terra-us"),
        ("global.openai.gpt-5.6-luna", "openai-gpt-5-6-luna-global"),
    ],
)
def test_openai_model_ids_resolve_to_route_specific_families(model_id, family):
    assert pricing.resolve_model_family(model_id) == family


def test_unprefixed_openai_id_uses_premium_route_and_warns(capsys):
    assert pricing.resolve_model_family("openai.gpt-6-sol") == "openai-gpt-6-sol-us"
    assert '"event": "openai_route_unknown"' in capsys.readouterr().out


@pytest.mark.parametrize("model_id", ["global.openai.gpt-5.5", "us.openai.gpt-5.4", "global.openai.gpt-7-sol"])
def test_unpriced_openai_models_meter_at_highest_published_rate(model_id, capsys):
    family = pricing.resolve_model_family(model_id)
    assert family == pricing.OPENAI_UNPRICED_FAMILY
    rates = pricing.get_rates()
    highest_input = max(r["input"] for k, r in rates.items() if k.startswith("openai-gpt") and not k.endswith("-long"))
    assert rates[family]["input"] == highest_input
    assert '"event": "unpriced_openai_model"' in capsys.readouterr().out


def test_openai_never_falls_through_to_claude_families():
    for model_id in ("global.openai.gpt-6-sol", "global.openai.gpt-5.5", "openai.gpt-oss-120b-1:0"):
        assert not pricing.resolve_model_family(model_id).startswith(("sonnet", "opus", "haiku", "fable"))


def test_anthropic_resolution_is_unchanged():
    assert pricing.resolve_model_family("us.anthropic.claude-sonnet-4-6-v1") == "sonnet"
    assert pricing.resolve_model_family("global.anthropic.claude-opus-4-7") == "opus"
    assert pricing.resolve_model_family("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "haiku"


@pytest.mark.parametrize(("tokens", "expect_long"), [(272_000, False), (272_001, True)])
def test_long_context_threshold(tokens, expect_long):
    family = pricing.openai_long_context_family("openai-gpt-6-sol-global", tokens)
    assert family == ("openai-gpt-6-sol-global-long" if expect_long else "openai-gpt-6-sol-global")


def test_long_context_switch_ignores_non_openai_families():
    assert pricing.openai_long_context_family("sonnet", 1_000_000) == "sonnet"


def test_env_override_can_price_unpriced_openai(monkeypatch):
    monkeypatch.setenv("BEDROCK_PRICING_RATES_JSON", '{"openai-unpriced": {"input": 1.0}}')
    assert pricing.get_rates()["openai-unpriced"]["input"] == 1.0
