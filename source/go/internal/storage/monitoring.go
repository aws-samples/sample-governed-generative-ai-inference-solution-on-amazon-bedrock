package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"gip-go/internal/jwt"
	"github.com/99designs/keyring"
)

// GetMonitoringToken retrieves a valid monitoring token from the configured storage.
func GetMonitoringToken(profile, storageType string) (string, error) {
	// Check environment first. Drop an expired env-var token (mirrors the
	// file/keyring expiry check below and the Python otel-helper) so callers
	// fall through to credential-process refresh instead of attaching a stale
	// token that the collector/ALB would reject with a silent 401.
	if token := os.Getenv("GIP_MONITORING_TOKEN"); token != "" {
		if !jwt.IsTokenExpired(token) {
			return token, nil
		}
	}

	var data *MonitoringTokenData
	var err error

	if storageType == "keyring" {
		data, err = ReadMonitoringTokenFromKeyring(profile)
	} else {
		data, err = readMonitoringTokenFromFile(profile)
	}
	if err != nil || data == nil {
		return "", err
	}

	// Check expiration (10 min buffer)
	now := time.Now().Unix()
	if data.Expires-now <= 600 {
		return "", nil
	}

	return data.Token, nil
}

// SaveMonitoringToken saves the monitoring token to configured storage.
func SaveMonitoringToken(profile, storageType, idToken string, claims map[string]interface{}) error {
	exp := int64(0)
	if v, ok := claims["exp"].(float64); ok {
		exp = int64(v)
	}
	email := ""
	if v, ok := claims["email"].(string); ok {
		email = v
	}

	data := &MonitoringTokenData{
		Token:   idToken,
		Expires: exp,
		Email:   email,
		Profile: profile,
	}

	if storageType == "keyring" {
		return SaveMonitoringTokenToKeyring(data, profile)
	}
	return saveMonitoringTokenToFile(data, profile)
}

func readMonitoringTokenFromFile(profile string) (*MonitoringTokenData, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	path := filepath.Join(home, ".gip-session", profile+"-monitoring.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var data MonitoringTokenData
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	return &data, nil
}

func saveMonitoringTokenToFile(data *MonitoringTokenData, profile string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	dir := filepath.Join(home, ".gip-session")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}

	raw, err := json.Marshal(data)
	if err != nil {
		return err
	}

	path := filepath.Join(dir, profile+"-monitoring.json")
	if err := os.WriteFile(path, raw, 0600); err != nil {
		return err
	}
	return nil
}

// ErrMonitoringTokenNotCleared reports that ClearMonitoringToken could neither
// delete nor overwrite the cached id_token, so the next credential-process run
// may still use it to re-federate without signing in.
var ErrMonitoringTokenNotCleared = errors.New("cached monitoring token (id_token) may remain")

// ClearMonitoringToken removes the cached monitoring id_token for a profile
// from the session file and the OS keyring. credential-process re-federates
// silently with that token until it expires (about an hour), so --clear-cache
// must remove it as well as the refresh token to force a new sign-in.
//
// A nil error means no usable cached token remains. An error that does not
// wrap ErrMonitoringTokenNotCleared means the keyring item could not be
// deleted but was overwritten with an empty, expired token (see
// clearRefreshTokenFromKeyringImpl for why a delete can be refused).
func ClearMonitoringToken(profile string) error {
	var fileErr error
	path := filepath.Join(sessionDir(), profile+"-monitoring.json")
	if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		fileErr = fmt.Errorf("%w: remove %s: %v", ErrMonitoringTokenNotCleared, path, err)
	}
	kr, err := openKeyring()
	if err != nil {
		// No keyring backend: SaveMonitoringToken could not have stored one there.
		return fileErr
	}
	return errors.Join(fileErr, clearMonitoringTokenFromKeyringImpl(kr, profile))
}

// clearMonitoringTokenFromKeyringImpl deletes the <profile>-monitoring item
// and, when the keyring refuses the delete, overwrites it with an empty token
// that has no expiry, which GetMonitoringToken treats as no token. It never
// creates an item that is not there.
func clearMonitoringTokenFromKeyringImpl(kr keyringRW, profile string) error {
	key := profile + "-monitoring"
	removeErr := kr.Remove(key)
	if removeErr == nil || errors.Is(removeErr, keyring.ErrKeyNotFound) || errors.Is(removeErr, fs.ErrNotExist) {
		return nil
	}
	empty, err := json.Marshal(MonitoringTokenData{Profile: profile})
	if err != nil {
		return fmt.Errorf("%w: delete %s: %v; overwrite: %v", ErrMonitoringTokenNotCleared, key, removeErr, err)
	}
	if setErr := kr.Set(keyring.Item{Key: key, Data: empty}); setErr != nil {
		return fmt.Errorf("%w: delete %s: %v; overwrite: %v", ErrMonitoringTokenNotCleared, key, removeErr, setErr)
	}
	return fmt.Errorf("could not delete keyring item %s (%v); overwrote it with an empty token", key, removeErr)
}
