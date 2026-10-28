package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"github.com/99designs/keyring"
)

// refreshTokenData is the on-disk format for a cached refresh token.
type refreshTokenData struct {
	Token     string `json:"refresh_token"`
	Profile   string `json:"profile"`
	UpdatedAt int64  `json:"updated_at"`
}

// SaveRefreshToken persists the OIDC refresh_token for a profile.
// In keyring mode it follows the credential storage contract and stays out of
// the filesystem. Session mode stores it at ~/.gip-session/{profile}-refresh.json
// with user-only permissions (0600).
func SaveRefreshToken(profile, credentialStorage, token string) error {
	if token == "" {
		return nil // IdP didn't issue a refresh token — nothing to store
	}

	data := refreshTokenData{
		Token:     token,
		Profile:   profile,
		UpdatedAt: time.Now().Unix(),
	}

	jsonBytes, err := json.Marshal(data)
	if err != nil {
		return err
	}

	if credentialStorage == "keyring" {
		kr, err := openKeyring()
		if err != nil {
			return err
		}
		return saveRefreshTokenToKeyringImpl(kr, profile, jsonBytes)
	}

	dir := sessionDir()
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	path := filepath.Join(dir, profile+"-refresh.json")

	// Write atomically via temp file + rename
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, jsonBytes, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// LoadRefreshToken retrieves the cached refresh_token for a profile.
// Returns empty string if no token is cached (not an error).
func LoadRefreshToken(profile, credentialStorage string) string {
	if credentialStorage == "keyring" {
		kr, err := openKeyring()
		if err == nil {
			if token := loadRefreshTokenFromKeyringImpl(kr, profile); token != "" {
				return token
			}
			if token := loadRefreshTokenFromSession(profile); token != "" {
				_ = SaveRefreshToken(profile, credentialStorage, token)
				return token
			}
		}
		return loadRefreshTokenFromSession(profile)
	}

	return loadRefreshTokenFromSession(profile)
}

func loadRefreshTokenFromSession(profile string) string {
	path := filepath.Join(sessionDir(), profile+"-refresh.json")

	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}

	var data refreshTokenData
	if err := json.Unmarshal(raw, &data); err != nil {
		return ""
	}

	return data.Token
}

// ErrRefreshTokenNotCleared reports that ClearRefreshToken could neither delete
// nor overwrite the keyring item, so a usable refresh token may remain.
var ErrRefreshTokenNotCleared = errors.New("refresh token may remain in the OS keyring")

// ClearRefreshToken removes the cached refresh_token for a profile from the
// session file and the OS keyring. A nil error means no refresh token remains.
// A non-nil error that does not wrap ErrRefreshTokenNotCleared means the keyring
// item could not be deleted but its token was overwritten with an empty one.
func ClearRefreshToken(profile string) error {
	path := filepath.Join(sessionDir(), profile+"-refresh.json")
	os.Remove(path)
	kr, err := openKeyring()
	if err != nil {
		// No keyring backend: SaveRefreshToken could not have stored one there.
		return nil
	}
	return clearRefreshTokenFromKeyringImpl(kr, profile)
}

// clearRefreshTokenFromKeyringImpl deletes the <profile>-refresh item and, when
// the keyring refuses the delete, overwrites it with an empty token.
//
// The overwrite is for the macOS Keychain. Each build of credential-process is
// ad-hoc signed by the Go linker, so the Keychain identifies it by its code
// hash, and an item's access list trusts only the build that created it. After
// an upgrade or a re-package the new build's delete can be refused with
// errSecInvalidOwnerEdit (-25244, see 99designs/keyring#84) while an update of
// the item's data is still accepted, which is why ClearKeyring overwrites
// credentials instead of deleting them. Before this, --clear-cache ignored the
// failed delete, reported success and left a working refresh token behind.
func clearRefreshTokenFromKeyringImpl(kr keyringRW, profile string) error {
	key := profile + "-refresh"
	removeErr := kr.Remove(key)
	if removeErr == nil || errors.Is(removeErr, keyring.ErrKeyNotFound) || errors.Is(removeErr, fs.ErrNotExist) {
		return nil
	}
	empty, err := json.Marshal(refreshTokenData{Profile: profile, UpdatedAt: time.Now().Unix()})
	if err != nil {
		return fmt.Errorf("%w: delete %s: %v; overwrite: %v", ErrRefreshTokenNotCleared, key, removeErr, err)
	}
	if setErr := saveRefreshTokenToKeyringImpl(kr, profile, empty); setErr != nil {
		return fmt.Errorf("%w: delete %s: %v; overwrite: %v", ErrRefreshTokenNotCleared, key, removeErr, setErr)
	}
	return fmt.Errorf("could not delete keyring item %s (%v); overwrote it with an empty token", key, removeErr)
}

func saveRefreshTokenToKeyringImpl(kr keyringRW, profile string, jsonBytes []byte) error {
	return kr.Set(keyring.Item{Key: profile + "-refresh", Data: jsonBytes})
}

func loadRefreshTokenFromKeyringImpl(kr keyringRW, profile string) string {
	item, err := kr.Get(profile + "-refresh")
	if err != nil {
		if errors.Is(err, keyring.ErrKeyNotFound) {
			return ""
		}
		return ""
	}

	var data refreshTokenData
	if err := json.Unmarshal(item.Data, &data); err != nil {
		return ""
	}
	return data.Token
}

// sessionDir returns the session directory path.
func sessionDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".gip-session")
}
