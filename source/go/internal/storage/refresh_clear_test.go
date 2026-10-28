package storage

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"runtime"
	"slices"
	"testing"

	"github.com/99designs/keyring"
)

// errInvalidOwnerEdit stands in for the macOS Keychain refusing to delete an
// item created by another build of credential-process (errSecInvalidOwnerEdit,
// -25244), which 99designs/keyring returns as "Keychain Error. (-25244)".
var errInvalidOwnerEdit = errors.New("Keychain Error. (-25244)")

// refusingKeyring deletes nothing: Remove of an existing key fails with
// removeErr, the way the Keychain refuses a delete by a different build.
type refusingKeyring struct {
	*mockKeyring
	removeErr error
}

func (k *refusingKeyring) Remove(key string) error {
	if _, ok := k.store[key]; !ok {
		return keyring.ErrKeyNotFound
	}
	return k.removeErr
}

func seedRefreshToken(t *testing.T, m *mockKeyring, profile, token string) {
	t.Helper()
	raw := fmt.Sprintf(`{"refresh_token":%q,"profile":%q,"updated_at":1}`, token, profile)
	m.store[profile+"-refresh"] = []byte(raw)
}

func TestClearRefreshTokenFromKeyring_Deletes(t *testing.T) {
	m := newMockKeyring()
	seedRefreshToken(t, m, "p", "rt_live")

	if err := clearRefreshTokenFromKeyringImpl(m, "p"); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if _, ok := m.store["p-refresh"]; ok {
		t.Error("p-refresh still in the keyring after a successful delete")
	}
	if len(m.setCalls) != 0 {
		t.Errorf("a successful delete must not write anything, got %d Set calls", len(m.setCalls))
	}
}

// Regression (end-to-end test, 2026-10-26): --clear-cache left a working
// <profile>-refresh in the macOS Keychain because ClearRefreshToken discarded
// the kr.Remove error.
func TestClearRefreshTokenFromKeyring_OverwritesWhenDeleteIsRefused(t *testing.T) {
	m := newMockKeyring()
	seedRefreshToken(t, m, "p", "rt_live")
	kr := &refusingKeyring{mockKeyring: m, removeErr: errInvalidOwnerEdit}

	err := clearRefreshTokenFromKeyringImpl(kr, "p")
	if err == nil {
		t.Fatal("a refused delete must be reported (printed in debug mode by --clear-cache)")
	}
	if errors.Is(err, ErrRefreshTokenNotCleared) {
		t.Fatalf("the token was overwritten, so this is not ErrRefreshTokenNotCleared: %v", err)
	}
	if got := loadRefreshTokenFromKeyringImpl(kr, "p"); got != "" {
		t.Errorf("refresh token after clear = %q, want empty", got)
	}
}

func TestClearRefreshTokenFromKeyring_ReportsTokenThatSurvives(t *testing.T) {
	m := newMockKeyring()
	seedRefreshToken(t, m, "p", "rt_live")
	m.failSet = true
	kr := &refusingKeyring{mockKeyring: m, removeErr: errInvalidOwnerEdit}

	err := clearRefreshTokenFromKeyringImpl(kr, "p")
	if !errors.Is(err, ErrRefreshTokenNotCleared) {
		t.Fatalf("err = %v, want ErrRefreshTokenNotCleared when neither delete nor overwrite worked", err)
	}
}

// Nothing stored: clearing must not create an item.
func TestClearRefreshTokenFromKeyring_AbsentItemIsNotCreated(t *testing.T) {
	for name, removeErr := range map[string]error{
		"keyring.ErrKeyNotFound": keyring.ErrKeyNotFound,
		"fs.ErrNotExist":         fmt.Errorf("remove /x: %w", fs.ErrNotExist),
	} {
		t.Run(name, func(t *testing.T) {
			m := newMockKeyring()
			if err := clearRefreshTokenFromKeyringImpl(&notFoundKeyring{m, removeErr}, "p"); err != nil {
				t.Fatalf("clear: %v", err)
			}
			if len(m.setCalls) != 0 {
				t.Errorf("clearing an absent token created an item (%d Set calls)", len(m.setCalls))
			}
		})
	}
}

// notFoundKeyring returns err from every Remove, as backends do for a missing key.
type notFoundKeyring struct {
	*mockKeyring
	err error
}

func (k *notFoundKeyring) Remove(string) error { return k.err }

// TestClearRefreshTokenRemovesKeychainItem_Live exercises the real macOS
// Keychain with a throwaway profile that this test binary creates and deletes,
// so no access dialog can appear. Opt-in: GIP_LIVE_KEYCHAIN_TEST=1 on macOS,
// with HOME left at the real home directory (with any other HOME the keyring
// library would create a new "login" keychain there and macOS would ask for a
// password).
func TestClearRefreshTokenRemovesKeychainItem_Live(t *testing.T) {
	if runtime.GOOS != "darwin" || os.Getenv("GIP_LIVE_KEYCHAIN_TEST") != "1" {
		t.Skip("set GIP_LIVE_KEYCHAIN_TEST=1 on macOS to run against the real Keychain")
	}
	u, err := user.Current()
	if err != nil || os.Getenv("HOME") != u.HomeDir {
		t.Skipf("HOME %q is not the account's home %q; refusing to open a Keychain elsewhere", os.Getenv("HOME"), u.HomeDir)
	}
	suffix := make([]byte, 6)
	if _, err := rand.Read(suffix); err != nil {
		t.Fatal(err)
	}
	profile := "gip-livetest-" + hex.EncodeToString(suffix)
	kr, err := openKeyring()
	if err != nil {
		t.Fatalf("open keyring: %v", err)
	}
	t.Cleanup(func() { _ = kr.Remove(profile + "-refresh") })

	if err := SaveRefreshToken(profile, "keyring", "rt_livetest"); err != nil {
		t.Fatalf("save: %v", err)
	}
	if got := LoadRefreshToken(profile, "keyring"); got != "rt_livetest" {
		t.Fatalf("load before clear = %q", got)
	}
	if err := ClearRefreshToken(profile); err != nil {
		t.Fatalf("ClearRefreshToken: %v", err)
	}
	keys, err := kr.Keys()
	if err != nil {
		t.Fatalf("keys: %v", err)
	}
	if slices.Contains(keys, profile+"-refresh") {
		t.Errorf("%s-refresh is still in the Keychain after ClearRefreshToken", profile)
	}
	if got := LoadRefreshToken(profile, "keyring"); got != "" {
		t.Errorf("load after clear = %q, want empty", got)
	}
}
