package oidc

import (
	"testing"

	"gip-go/internal/provider"
)

// OneLogin rejects offline_access on the authorization-code flow
// (https://developers.onelogin.com/openid-connect/api/authorization-code,
// retrieved 2026-10-02), so generic profiles must be able to drop it.
func TestAuthScopes_GenericOverride(t *testing.T) {
	cfg := provider.ConfigFor("generic", "")
	got := authScopes("generic", cfg, &GenericEndpoints{Scopes: "  openid profile email groups "})
	if got != "openid profile email groups" {
		t.Fatalf("authScopes = %q, want override", got)
	}
}

func TestAuthScopes_GenericDefaultUnchanged(t *testing.T) {
	cfg := provider.ConfigFor("generic", "")
	for _, g := range []*GenericEndpoints{nil, {}, {Scopes: "   "}} {
		if got := authScopes("generic", cfg, g); got != "openid profile email offline_access" {
			t.Fatalf("authScopes(%+v) = %q, want legacy default", g, got)
		}
	}
}

func TestAuthScopes_NamedProvidersIgnoreOverride(t *testing.T) {
	cfg := provider.ConfigFor("okta", "")
	if got := authScopes("okta", cfg, &GenericEndpoints{Scopes: "openid"}); got != cfg.Scopes {
		t.Fatalf("okta scopes = %q, want %q", got, cfg.Scopes)
	}
}
