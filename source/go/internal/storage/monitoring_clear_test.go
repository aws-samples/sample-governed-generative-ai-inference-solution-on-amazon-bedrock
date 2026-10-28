package storage

// ABOUTME: ClearMonitoringToken removes the cached id_token (session file and keyring item)
// ABOUTME: so --clear-cache forces a new sign-in; a refused keyring delete is overwritten.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/99designs/keyring"
)

func seedMonitoringToken(m *mockKeyring, profile string) {
	exp := time.Now().Unix() + 3600
	m.store[profile+"-monitoring"] = []byte(fmt.Sprintf(`{"token":"eyJ.live.token","expires":%d,"email":"u@example.com","profile":%q}`, exp, profile))
}

func TestClearMonitoringTokenFromKeyring_Deletes(t *testing.T) {
	m := newMockKeyring()
	seedMonitoringToken(m, "p")

	if err := clearMonitoringTokenFromKeyringImpl(m, "p"); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if _, ok := m.store["p-monitoring"]; ok {
		t.Error("p-monitoring still in the keyring after a successful delete")
	}
	if len(m.setCalls) != 0 {
		t.Errorf("a successful delete must not write anything, got %d Set calls", len(m.setCalls))
	}
}

// Regression (review, 2026-10-27): the same refused-delete case as the refresh
// token (a Keychain item created by another build of credential-process).
func TestClearMonitoringTokenFromKeyring_OverwritesWhenDeleteIsRefused(t *testing.T) {
	m := newMockKeyring()
	seedMonitoringToken(m, "p")
	kr := &refusingKeyring{mockKeyring: m, removeErr: errInvalidOwnerEdit}

	err := clearMonitoringTokenFromKeyringImpl(kr, "p")
	if err == nil {
		t.Fatal("a refused delete must be reported")
	}
	if errors.Is(err, ErrMonitoringTokenNotCleared) {
		t.Fatalf("the token was overwritten, so this is not ErrMonitoringTokenNotCleared: %v", err)
	}
	data, readErr := readMonitoringTokenFromKeyringImpl(kr, "p")
	if readErr != nil {
		t.Fatalf("read after overwrite: %v", readErr)
	}
	if data.Token != "" || data.Expires > time.Now().Unix() {
		t.Errorf("overwritten item still usable: token=%q expires=%d", data.Token, data.Expires)
	}
}

func TestClearMonitoringTokenFromKeyring_ReportsTokenThatSurvives(t *testing.T) {
	m := newMockKeyring()
	seedMonitoringToken(m, "p")
	m.failSet = true
	kr := &refusingKeyring{mockKeyring: m, removeErr: errInvalidOwnerEdit}

	if err := clearMonitoringTokenFromKeyringImpl(kr, "p"); !errors.Is(err, ErrMonitoringTokenNotCleared) {
		t.Fatalf("err = %v, want ErrMonitoringTokenNotCleared when neither delete nor overwrite worked", err)
	}
}

func TestClearMonitoringTokenFromKeyring_AbsentItemIsNotCreated(t *testing.T) {
	for name, removeErr := range map[string]error{
		"keyring.ErrKeyNotFound": keyring.ErrKeyNotFound,
		"fs.ErrNotExist":         fmt.Errorf("remove /x: %w", os.ErrNotExist),
	} {
		t.Run(name, func(t *testing.T) {
			m := newMockKeyring()
			if err := clearMonitoringTokenFromKeyringImpl(&notFoundKeyring{m, removeErr}, "p"); err != nil {
				t.Fatalf("clear: %v", err)
			}
			if len(m.setCalls) != 0 {
				t.Errorf("clearing an absent token created an item (%d Set calls)", len(m.setCalls))
			}
		})
	}
}

// The session-storage copy (~/.gip-session/<profile>-monitoring.json) is removed,
// and GetMonitoringToken then finds no token.
func TestClearMonitoringTokenRemovesSessionFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("GIP_MONITORING_TOKEN", "")
	profile := "test-clear-monitoring-session"
	claims := map[string]interface{}{"exp": float64(time.Now().Unix() + 3600), "email": "u@example.com"}
	if err := SaveMonitoringToken(profile, "session", "eyJ.live.token", claims); err != nil {
		t.Fatalf("save: %v", err)
	}
	if token, _ := GetMonitoringToken(profile, "session"); token == "" {
		t.Fatal("precondition: the saved token should be returned")
	}

	if err := ClearMonitoringToken(profile); err != nil {
		t.Fatalf("ClearMonitoringToken: %v", err)
	}

	if _, err := os.Stat(filepath.Join(home, ".gip-session", profile+"-monitoring.json")); !os.IsNotExist(err) {
		t.Errorf("session file still present (stat err = %v)", err)
	}
	if token, _ := GetMonitoringToken(profile, "session"); token != "" {
		t.Errorf("GetMonitoringToken after clear = %q, want none", token)
	}
}

// A session file that cannot be removed must be reported, not ignored.
func TestClearMonitoringTokenReportsSessionFileThatSurvives(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	profile := "test-clear-monitoring-stuck"
	// A non-empty directory in the file's place makes os.Remove fail on every OS.
	stuck := filepath.Join(home, ".gip-session", profile+"-monitoring.json")
	if err := os.MkdirAll(filepath.Join(stuck, "child"), 0o700); err != nil {
		t.Fatal(err)
	}

	if err := ClearMonitoringToken(profile); !errors.Is(err, ErrMonitoringTokenNotCleared) {
		t.Fatalf("err = %v, want ErrMonitoringTokenNotCleared", err)
	}
}
