# ABOUTME: Regression tests for the optional generic-OIDC scope override (oidc_scopes)
# ABOUTME: OneLogin rejects offline_access on the authorization-code flow, so generic profiles must drop it

"""``oidc_scopes`` for ``provider_type: generic``.

OneLogin documents that "The "offline_access" scope is not supported on this grant
type and will return an error if provided"
(https://developers.onelogin.com/openid-connect/api/authorization-code, retrieved
2026-10-02). The Go credential-process sends ``openid profile email offline_access``
for generic providers, so a OneLogin tenant could not sign in. The override is optional;
absent keeps today's scopes.
"""

from unittest.mock import patch

import credential_provider.__main__ as cp
from governed_inference_platform.config import Profile

GENERIC = {
    "provider_domain": "example.onelogin.com",
    "client_id": "00000000-0000-0000-0000-000000000000",
    "aws_region": "eu-central-1",
    "credential_storage": "session",
    "provider_type": "generic",
    "federation_type": "direct",
    "federated_role_arn": "arn:aws:iam::123456789012:role/BedrockOidcFederatedRole",
    "oidc_authorization_endpoint": "https://example.onelogin.com/oidc/2/auth",
    "oidc_token_endpoint": "https://example.onelogin.com/oidc/2/token",
}


def _auth(config):
    with (
        patch.object(cp.MultiProviderAuth, "_load_config", return_value=config),
        patch.object(cp.MultiProviderAuth, "_init_credential_storage"),
    ):
        return cp.MultiProviderAuth(profile="gip")


def test_profile_field_defaults_to_none_for_old_profiles():
    assert (
        Profile(
            name="p",
            provider_domain="d",
            client_id="c",
            credential_storage="session",
            aws_region="eu-central-1",
            identity_pool_name="i",
        ).oidc_scopes
        is None
    )


def test_python_provider_uses_override_for_generic():
    auth = _auth({**GENERIC, "oidc_scopes": " openid profile email groups "})
    assert auth.provider_config["scopes"] == "openid profile email groups"


def test_python_provider_default_unchanged_without_override():
    assert _auth(dict(GENERIC)).provider_config["scopes"] == "openid profile email"


def test_python_provider_named_providers_ignore_override():
    okta = {**GENERIC, "provider_type": "okta", "provider_domain": "x.okta.com", "oidc_scopes": "openid"}
    okta.pop("oidc_authorization_endpoint")
    assert _auth(okta).provider_config["scopes"] == cp.PROVIDER_CONFIGS["okta"]["scopes"]


def test_package_and_init_mirror_the_field():
    """config-sync: the field must reach config.json and survive init round-trips."""
    from pathlib import Path

    cmds = Path(cp.__file__).parent.parent / "governed_inference_platform" / "cli" / "commands"
    assert '"oidc_scopes",' in (cmds / "package.py").read_text(encoding="utf-8")
    init_src = (cmds / "init.py").read_text(encoding="utf-8")
    assert '"oidc_scopes": config_data.get("oidc_scopes")' in init_src
    assert init_src.count('"oidc_scopes",') == 1
    go_cfg = Path(cp.__file__).parent.parent / "go" / "internal" / "config" / "config.go"
    assert 'json:"oidc_scopes,omitempty"' in go_cfg.read_text(encoding="utf-8")
