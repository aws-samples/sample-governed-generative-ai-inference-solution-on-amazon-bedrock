"""Tests for managed-settings model lock opt-out behavior (issue #665).

Ensures that ANTHROPIC_MODEL and ANTHROPIC_DEFAULT_*_MODEL env vars are only
written to managed-settings.json when lock_default_model is True.
"""

import json
import tempfile
from pathlib import Path

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile
from governed_inference_platform.models import resolve_model_for_tier


class TestModelLockManagedSettings:
    """Verify model env vars are gated on lock_default_model."""

    def _make_profile(self, lock_default_model=False, selected_model="us.anthropic.claude-sonnet-4-6:0"):
        return Profile(
            name="Test",
            provider_domain="test.okta.com",
            client_id="client-id",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            selected_model=selected_model,
            model_alias="sonnet",
            cross_region_profile="us",
            lock_default_model=lock_default_model,
            settings_target="managed",
            # Monitoring off keeps _create_claude_settings from shelling out to the AWS CLI
            # (the central-monitoring endpoint lookup); these tests cover the model settings only.
            monitoring_enabled=False,
        )

    def _generate_and_read_settings(self, profile):
        """Call _create_claude_settings and return the generated JSON dict."""
        cmd = PackageCommand()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            cmd._create_claude_settings(output_dir, profile)
            settings_path = output_dir / "gip-settings" / "managed-settings.json"
            assert settings_path.exists(), f"Expected {settings_path} to be created"
            return json.loads(settings_path.read_text(encoding="utf-8"))

    def test_model_env_vars_absent_when_lock_false(self):
        """When lock_default_model=False, managed-settings must NOT contain ANTHROPIC_MODEL."""
        profile = self._make_profile(lock_default_model=False)
        settings = self._generate_and_read_settings(profile)
        env = settings.get("env", {})
        assert "ANTHROPIC_MODEL" not in env, (
            "ANTHROPIC_MODEL should not be in managed-settings when lock_default_model=False"
        )
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
        assert "ANTHROPIC_SMALL_FAST_MODEL" not in env

    def test_model_env_vars_present_when_lock_true(self):
        """When lock_default_model=True, managed-settings MUST contain model env vars."""
        profile = self._make_profile(lock_default_model=True)
        settings = self._generate_and_read_settings(profile)
        env = settings.get("env", {})
        assert "ANTHROPIC_MODEL" in env, "ANTHROPIC_MODEL should be in managed-settings when lock_default_model=True"
        assert env["ANTHROPIC_MODEL"] == "sonnet"
        # At least some tier models should be set
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" in env or "ANTHROPIC_SMALL_FAST_MODEL" in env

    def test_lock_default_model_defaults_to_false(self):
        """Profile with no explicit lock_default_model should default to False."""
        profile = Profile(
            name="Test",
            provider_domain="test.okta.com",
            client_id="client-id",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            selected_model="us.anthropic.claude-sonnet-4-6:0",
            model_alias="sonnet",
            cross_region_profile="us",
            settings_target="managed",
        )
        assert profile.lock_default_model is False, "lock_default_model should default to False"

    def test_backward_compat_old_profiles_default_unlocked(self):
        """Profiles saved before lock_default_model field existed should behave as unlocked."""
        profile = self._make_profile(lock_default_model=False)
        settings = self._generate_and_read_settings(profile)
        env = settings.get("env", {})
        # The critical assertion: old behavior (no lock field = False) means NO model lock
        assert "ANTHROPIC_MODEL" not in env

    def test_no_model_env_when_no_selected_model(self):
        """When no model is selected, no model env vars regardless of lock setting."""
        profile = self._make_profile(lock_default_model=True, selected_model=None)
        settings = self._generate_and_read_settings(profile)
        env = settings.get("env", {})
        assert "ANTHROPIC_MODEL" not in env


SONNET_AIP = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/sonnetprod"
HAIKU_AIP = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/haikuprod"
OPUS_AIP = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/opusprod"

# Tier variables each ANTHROPIC_MODEL alias resolves through in Claude Code.
ALIAS_TIER_VARS = {
    "sonnet": ["ANTHROPIC_DEFAULT_SONNET_MODEL"],
    "opus": ["ANTHROPIC_DEFAULT_OPUS_MODEL"],
    "opusplan": ["ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"],
    "haiku": ["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
    "fable": ["ANTHROPIC_DEFAULT_FABLE_MODEL"],
}


class TestManagedSettingsAvailableModels:
    """restrict_model_selection (with lock_default_model, managed scope) restricts model selection.

    A managed `model`/ANTHROPIC_MODEL is only a default in Claude Code; the
    allowlist is what restricts selection. On Amazon Bedrock the allowlist
    must name the provider-form IDs the tier variables pin (CRIS IDs or AIP
    ARNs), and enforceAvailableModels keeps the Default option inside it.
    """

    def _profile(self, **overrides):
        values = {
            "name": "Test",
            "provider_domain": "test.okta.com",
            "client_id": "client-id",
            "credential_storage": "keyring",
            "aws_region": "us-east-1",
            "identity_pool_name": "test-pool",
            "selected_model": "us.anthropic.claude-sonnet-4-6",
            "model_alias": "sonnet",
            "cross_region_profile": "us",
            "lock_default_model": True,
            "restrict_model_selection": True,
            "settings_target": "managed",
            "monitoring_enabled": False,
        }
        values.update(overrides)
        return Profile(**values)

    def _settings(self, profile):
        cmd = PackageCommand()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            cmd._create_claude_settings(output_dir, profile)
            name = "managed-settings.json" if profile.settings_target == "managed" else "settings.json"
            return json.loads((output_dir / "gip-settings" / name).read_text(encoding="utf-8"))

    def test_lock_on_writes_allowlist_of_pinned_ids(self):
        settings = self._settings(self._profile())
        env = settings["env"]
        assert settings["availableModels"] == [
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"],
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
        ]
        assert settings["enforceAvailableModels"] is True

    def test_lock_off_managed_writes_no_allowlist(self):
        settings = self._settings(self._profile(lock_default_model=False))
        assert "availableModels" not in settings
        assert "enforceAvailableModels" not in settings
        assert "ANTHROPIC_MODEL" not in settings["env"]

    def test_user_scope_never_writes_allowlist(self):
        for lock in (True, False):
            settings = self._settings(self._profile(settings_target="user", lock_default_model=lock))
            assert settings["env"]["ANTHROPIC_MODEL"] == "sonnet"
            assert "availableModels" not in settings
            assert "enforceAvailableModels" not in settings

    def test_no_selected_model_writes_no_allowlist(self):
        settings = self._settings(self._profile(selected_model=None, model_alias=None))
        assert "availableModels" not in settings
        assert "enforceAvailableModels" not in settings

    def test_aip_overrides_are_listed_instead_of_cris_ids(self):
        settings = self._settings(
            self._profile(inference_profile_sonnet_arn=SONNET_AIP, inference_profile_haiku_arn=HAIKU_AIP)
        )
        assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == SONNET_AIP
        assert settings["availableModels"] == [SONNET_AIP, HAIKU_AIP]
        assert settings["enforceAvailableModels"] is True

    def test_opus_aip_with_sonnet_default_lists_only_sonnet_and_haiku(self):
        settings = self._settings(self._profile(inference_profile_opus_arn=OPUS_AIP))
        assert OPUS_AIP not in settings["availableModels"]
        assert settings["availableModels"][0] == settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"]

    def test_opusplan_lists_opus_then_sonnet_then_haiku(self):
        settings = self._settings(self._profile(selected_model="us.anthropic.claude-opus-4-8", model_alias="opusplan"))
        env = settings["env"]
        assert settings["availableModels"] == [
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"],
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"],
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
        ]

    def test_fable_is_pinned_and_listed_when_locked(self):
        settings = self._settings(self._profile(selected_model="us.anthropic.claude-fable-5", model_alias="fable"))
        assert settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "us.anthropic.claude-fable-5"
        assert settings["availableModels"][0] == "us.anthropic.claude-fable-5"

    def test_fable_pin_not_added_without_lock(self):
        settings = self._settings(
            self._profile(
                selected_model="us.anthropic.claude-fable-5",
                model_alias="fable",
                settings_target="user",
                lock_default_model=False,
            )
        )
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in settings["env"]

    def test_unknown_model_without_alias_lists_the_model_id(self):
        settings = self._settings(self._profile(selected_model="us.anthropic.claude-custom-9", model_alias=None))
        assert settings["env"]["ANTHROPIC_MODEL"] == "us.anthropic.claude-custom-9"
        assert settings["availableModels"][0] == "us.anthropic.claude-custom-9"

    def test_every_value_the_default_resolves_to_is_allowed(self):
        """Property over geographies x aliases: the lock never blocks its own default.

        Covers residency fallbacks such as jp, where the opus tier is pinned to
        a Sonnet ID, and us-gov, where every tier resolves to one Sonnet ID.
        """
        for prefix in ("us", "eu", "global", "jp", "au", "apac", "us-gov"):
            for alias in ALIAS_TIER_VARS:
                tier = "opus" if alias == "opusplan" else alias
                selected = resolve_model_for_tier(tier, prefix)
                for aip in ({}, {"inference_profile_sonnet_arn": SONNET_AIP, "inference_profile_haiku_arn": HAIKU_AIP}):
                    settings = self._settings(
                        self._profile(cross_region_profile=prefix, model_alias=alias, selected_model=selected, **aip)
                    )
                    env, allowed = settings["env"], settings["availableModels"]
                    case = (prefix, alias, bool(aip))
                    default_var = ALIAS_TIER_VARS[alias][0]
                    aip_tier = {
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET_AIP,
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU_AIP,
                    }
                    expected_pin = aip_tier.get(default_var, selected) if aip else selected
                    assert env[default_var] == expected_pin, case
                    expected_first = env[default_var]
                    assert allowed[0] == expected_first, case
                    for var in ALIAS_TIER_VARS[alias] + ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]:
                        assert env[var] in allowed, (case, var)
                    assert len(allowed) == len(set(allowed)), case
                    assert not set(allowed) & set(ALIAS_TIER_VARS), (case, "bare alias in list")
                    assert settings["enforceAvailableModels"] is True, case

    def test_lock_pins_the_selected_version_not_the_newest_in_tier(self):
        """Opus 4.6 selected in gip init must lock users to Opus 4.6, not the newest Opus."""
        selected = "us.anthropic.claude-opus-4-6-v1"
        assert resolve_model_for_tier("opus", "us") != selected, "fixture needs an older-than-newest model"
        settings = self._settings(self._profile(selected_model=selected, model_alias="opus"))
        env = settings["env"]
        assert env["ANTHROPIC_MODEL"] == "opus"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == selected
        assert settings["availableModels"] == [selected, env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]]

    def test_opusplan_lock_pins_selected_opus_and_keeps_sonnet_tier(self):
        selected = "us.anthropic.claude-opus-4-6-v1"
        settings = self._settings(self._profile(selected_model=selected, model_alias="opusplan"))
        env = settings["env"]
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == selected
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == resolve_model_for_tier("sonnet", "us")
        assert settings["availableModels"][:2] == [selected, env["ANTHROPIC_DEFAULT_SONNET_MODEL"]]

    def test_haiku_lock_pins_small_fast_to_selected(self):
        selected = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        settings = self._settings(self._profile(selected_model=selected, model_alias="haiku"))
        env = settings["env"]
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == selected
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == selected
        assert settings["availableModels"] == [selected]

    def test_aip_override_wins_over_selected_version_pin(self):
        settings = self._settings(
            self._profile(
                selected_model="us.anthropic.claude-opus-4-6-v1",
                model_alias="opus",
                inference_profile_opus_arn=OPUS_AIP,
            )
        )
        assert settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == OPUS_AIP
        assert settings["availableModels"][0] == OPUS_AIP

    def test_selected_version_pin_not_applied_without_lock(self):
        """User scope keeps the newest-in-tier pin; the unlocked behavior is unchanged."""
        selected = "us.anthropic.claude-opus-4-6-v1"
        for lock in (True, False):
            settings = self._settings(
                self._profile(
                    selected_model=selected, model_alias="opus", settings_target="user", lock_default_model=lock
                )
            )
            assert settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == resolve_model_for_tier("opus", "us")

    def test_package_prints_no_restriction_upgrade_notice(self, capsys):
        """Existing profiles no longer change behavior, so gip package announces nothing."""
        for overrides in ({}, {"restrict_model_selection": False}, {"lock_default_model": False}):
            self._settings(self._profile(**overrides))
            assert "now restricts" not in capsys.readouterr().out, overrides

    # --- lock_default_model alone is the managed default again (a default, not a lock) ---

    def test_lock_without_restrict_writes_default_and_newest_pins_but_no_allowlist(self):
        selected = "us.anthropic.claude-opus-4-6-v1"
        settings = self._settings(
            self._profile(selected_model=selected, model_alias="opus", restrict_model_selection=False)
        )
        env = settings["env"]
        assert env["ANTHROPIC_MODEL"] == "opus"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == resolve_model_for_tier("opus", "us") != selected
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == resolve_model_for_tier("sonnet", "us")
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"]
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == resolve_model_for_tier("haiku", "us")
        assert "availableModels" not in settings
        assert "enforceAvailableModels" not in settings

    def test_lock_without_restrict_matches_the_pre_restriction_managed_default(self):
        """Same model keys as the managed default before the restriction existed, for every alias."""
        for alias in ALIAS_TIER_VARS:
            settings = self._settings(self._profile(model_alias=alias, restrict_model_selection=False))
            env = settings["env"]
            model_keys = {k for k in env if k.startswith("ANTHROPIC_")}
            assert model_keys == {
                "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
            }, alias
            assert env["ANTHROPIC_MODEL"] == alias
            assert "availableModels" not in settings, alias

    def test_restrict_is_a_no_op_without_managed_scope_or_lock(self):
        selected = "us.anthropic.claude-opus-4-6-v1"
        user = self._settings(self._profile(selected_model=selected, model_alias="opus", settings_target="user"))
        assert "availableModels" not in user and "enforceAvailableModels" not in user
        assert user["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == resolve_model_for_tier("opus", "us")
        unlocked = self._settings(self._profile(selected_model=selected, model_alias="opus", lock_default_model=False))
        assert "availableModels" not in unlocked
        assert not any(k.startswith("ANTHROPIC_") for k in unlocked["env"])

    def test_restrict_defaults_to_false_for_old_profiles(self):
        profile = Profile.from_dict(
            {
                "name": "Old",
                "provider_domain": "test.okta.com",
                "client_id": "client-id",
                "aws_region": "us-east-1",
                "identity_pool_name": "test-pool",
                "selected_model": "us.anthropic.claude-sonnet-4-6",
                "lock_default_model": True,
                "settings_target": "managed",
                "monitoring_enabled": False,
            }
        )
        assert profile.restrict_model_selection is False
        assert "availableModels" not in self._settings(profile)


def test_go_config_mirrors_restrict_model_selection():
    config_go = Path(__file__).resolve().parents[4] / "source" / "go" / "internal" / "config" / "config.go"
    assert 'json:"restrict_model_selection,omitempty"' in config_go.read_text(encoding="utf-8")
