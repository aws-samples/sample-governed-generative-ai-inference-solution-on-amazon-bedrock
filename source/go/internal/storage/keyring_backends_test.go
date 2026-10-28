package storage

// ABOUTME: openKeyring must not hand back the file backend, which has no directory
// ABOUTME: configured and fails every operation (headless Linux CI regression).

import (
	"testing"

	"github.com/99designs/keyring"
)

func TestOSKeyringBackendsExcludeTheFileBackend(t *testing.T) {
	backends := osKeyringBackends()
	if backends == nil {
		t.Fatal("osKeyringBackends returned nil; keyring.Open would then consider every backend, including the file backend")
	}
	for _, backend := range backends {
		if backend == keyring.FileBackend {
			t.Fatalf("osKeyringBackends includes the file backend: %v", backends)
		}
	}
	for _, backend := range keyring.AvailableBackends() {
		if backend == keyring.FileBackend {
			continue
		}
		found := false
		for _, b := range backends {
			if b == backend {
				found = true
			}
		}
		if !found {
			t.Errorf("osKeyringBackends dropped %s (want every available backend except the file backend)", backend)
		}
	}
}
