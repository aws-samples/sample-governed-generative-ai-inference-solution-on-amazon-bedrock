package main

// ABOUTME: --clear-cache must leave nothing that lets the next run return credentials
// ABOUTME: without a sign-in, and must exit non-zero when a token could not be cleared.

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"gip-go/internal/config"
	"gip-go/internal/storage"
)

// newClearCacheApp seeds a session-storage profile with a valid cached id_token
// and a refresh token under a temporary HOME.
func newClearCacheApp(t *testing.T, profile string) (*credentialApp, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("GIP_MONITORING_TOKEN", "")
	idToken := fakeJWT(t, map[string]interface{}{
		"sub": "user-123", "email": "test@example.com",
		"exp": float64(time.Now().Unix() + 3600),
	})
	writeMonitoringToken(t, home, profile, idToken, time.Now().Unix()+3600)
	if err := storage.SaveRefreshToken(profile, "session", "rt_live"); err != nil {
		t.Fatalf("SaveRefreshToken: %v", err)
	}
	app := &credentialApp{profile: profile, cfg: &config.ProfileConfig{CredentialStorage: "session"}}
	if app.cachedTokenAuthResult() == nil {
		t.Fatal("precondition: run() would take the cached id_token path")
	}
	return app, home
}

// Regression (review, 2026-10-27): --clear-cache removed the refresh token but
// left <profile>-monitoring, so the next run re-federated silently with the
// cached id_token for up to an hour. The package README promises that
// --clear-cache forces re-authentication.
func TestClearCacheForcesSignInOnNextRun(t *testing.T) {
	app, _ := newClearCacheApp(t, "test-clear-cache-forces-sign-in")

	if !app.clearCache(true) {
		t.Fatal("clearCache(true) = false, want every token cleared")
	}

	if app.getCachedCredentials() != nil {
		t.Error("next run would return cached AWS credentials")
	}
	if app.cachedTokenAuthResult() != nil {
		t.Error("next run would re-federate silently with the cached id_token")
	}
	if got := storage.LoadRefreshToken(app.profile, "session"); got != "" {
		t.Errorf("next run would use refresh token %q", got)
	}
}

// The internal callers (quota block, retryable STS failure) keep the id_token,
// so a blocked user is refused again on the next run without a browser window.
func TestClearCacheAfterQuotaBlockKeepsIDToken(t *testing.T) {
	app, _ := newClearCacheApp(t, "test-clear-cache-keeps-id-token")

	if !app.clearCache(false) {
		t.Fatal("clearCache(false) = false")
	}

	if app.cachedTokenAuthResult() == nil {
		t.Error("the cached id_token was removed; a quota-blocked user would get a browser sign-in on every run")
	}
	if got := storage.LoadRefreshToken(app.profile, "session"); got != "" {
		t.Errorf("refresh token %q survived", got)
	}
}

// Regression (review, 2026-10-27): --clear-cache printed "Cleared cached
// credentials" and exited 0 even when a token survived.
func TestClearCacheReportsTokenThatSurvives(t *testing.T) {
	profile := "test-clear-cache-stuck"
	app, home := newClearCacheApp(t, profile)
	// Replace the id_token file with a non-empty directory: os.Remove fails on every OS.
	path := filepath.Join(home, ".gip-session", profile+"-monitoring.json")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(path, "child"), 0o700); err != nil {
		t.Fatal(err)
	}

	if app.clearCache(true) {
		t.Error("clearCache(true) = true although the cached id_token could not be removed")
	}
}
