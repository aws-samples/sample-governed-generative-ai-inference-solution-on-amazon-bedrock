//go:build !windows

// ABOUTME: Starts the local OTEL collector sidecar (~/gip/otelcol) when this install
// ABOUTME: is in sidecar mode and no collector from an earlier invocation is running.

package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"gip-go/internal/proc"
)

// File names shared with install.sh, otel-helper.sh, the Python otel helper,
// `gip cleanup` and `gip status`. The binary, config and pid file live in
// ~/gip; the log and the start lock live in ~/.gip-session, so ~/gip holds
// nothing the ownership manifest and `gip cleanup` don't know about.
const (
	collectorBinaryName = "otelcol"
	collectorConfigName = "collector-config.yaml"
	collectorPIDName    = "collector.pid"
	collectorLockName   = "collector.lock"
	collectorLogName    = "collector.log"

	// defaultReceiverAddress is the OTLP/HTTP receiver's default endpoint, used
	// when collector-config.yaml does not name one. The installed config sets
	// localhost:4318, the port Claude Code exports to and the CoWork
	// `otel-helper --proxy` also binds (127.0.0.1:4318).
	defaultReceiverAddress = "localhost:4318"
	receiverDialTimeout    = 250 * time.Millisecond

	// collectorStartGrace is how long a live pid in collector.pid counts as a
	// collector that is still starting while nothing answers on its receiver
	// address. The collector binds the receiver only after its sigv4auth
	// extension has fetched credentials, which can wait for a browser sign-in
	// in credential-process (up to 5 minutes, plus up to 1 minute for another
	// sign-in to finish). After that, a live pid whose receiver stays silent is
	// a pid reused by another process after a crash or a reboot.
	collectorStartGrace = 10 * time.Minute
)

// ensureCollector starts the sidecar collector for profile if it is installed
// and not already running. Claude Code runs this helper (otelHeadersHelper) at
// start-up and then periodically, so a collector that stopped is restarted on
// the next invocation. Failures are logged in debug mode only: the helper's
// stdout contract (one JSON object of headers) must not depend on the collector.
func ensureCollector(profile string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return
	}
	started, pid, err := startCollectorIfNeeded(filepath.Join(home, "gip"), filepath.Join(home, ".gip-session"), profile)
	switch {
	case err != nil:
		debugPrint("Could not start the collector sidecar: %v", err)
	case started:
		debugPrint("Started the collector sidecar (PID %d)", pid)
	}
}

// startCollectorIfNeeded starts installDir/otelcol with installDir/collector-config.yaml
// when both exist (sidecar mode) and collectorStartBlocked finds no reason not to.
// Central-mode and IDC zero-binary installs have no otelcol, so this is a no-op
// for them.
//
// The collector runs with AWS_PROFILE=<profile>-collector, a profile the installer
// writes with only a credential_process line, so the collector's AWS SDK always
// gets refreshable credentials from credential-process. Static AWS_* credentials
// inherited from the caller are removed for the same reason: they would take
// precedence over the profile and could not be refreshed.
//
// An exclusive lock on sessionDir/collector.lock makes concurrent invocations
// (several Claude Code sessions starting at once) start at most one collector.
func startCollectorIfNeeded(installDir, sessionDir, profile string) (bool, int, error) {
	binary := filepath.Join(installDir, collectorBinaryName)
	config := filepath.Join(installDir, collectorConfigName)
	if !isExecutableFile(binary) || !isRegularFile(config) {
		return false, 0, nil
	}
	pidFile := filepath.Join(installDir, collectorPIDName)
	receiverAddr := collectorReceiverAddress(config)
	if reason := collectorStartBlocked(pidFile, receiverAddr); reason != "" {
		debugPrint("Not starting the collector sidecar: %s", reason)
		return false, 0, nil
	}

	if err := os.MkdirAll(sessionDir, 0o700); err != nil {
		return false, 0, err
	}
	lock, err := os.OpenFile(filepath.Join(sessionDir, collectorLockName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return false, 0, err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return false, 0, nil // another invocation is starting it right now
		}
		return false, 0, err
	}
	// Re-check under the lock: the invocation that held it may have started one.
	if reason := collectorStartBlocked(pidFile, receiverAddr); reason != "" {
		debugPrint("Not starting the collector sidecar: %s", reason)
		return false, 0, nil
	}

	logFile, err := os.OpenFile(filepath.Join(sessionDir, collectorLogName), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return false, 0, err
	}
	defer logFile.Close()

	pid, err := proc.StartBackground(binary, []string{binary, "--config", config},
		collectorEnv(os.Environ(), profile), installDir, logFile)
	if err != nil {
		return false, 0, err
	}
	if err := writePIDFile(pidFile, pid); err != nil {
		return true, pid, fmt.Errorf("collector started (PID %d) but %s was not written: %w", pid, pidFile, err)
	}
	return true, pid, nil
}

// collectorEnv returns environ with AWS_PROFILE set to <profile>-collector and
// these removed:
//   - static AWS credentials and the Bedrock API key, which the collector must not
//     use instead of the refreshable <profile>-collector profile;
//   - debug switches (COGNITO_AUTH_DEBUG, DEBUG_MODE): the collector lives for days
//     and the AWS SDK copies credential-process stderr into collector.log, so a
//     debug flag inherited from one terminal would write identity details into a
//     persistent log;
//   - tokens and secrets (GIP_MONITORING_TOKEN, GIP_CLIENT_SECRET) that the
//     collector and the credential-process it runs never need.
func collectorEnv(environ []string, profile string) []string {
	drop := map[string]bool{
		"AWS_PROFILE":               true,
		"AWS_ACCESS_KEY_ID":         true,
		"AWS_SECRET_ACCESS_KEY":     true,
		"AWS_SESSION_TOKEN":         true,
		"AWS_CREDENTIAL_EXPIRATION": true,
		"AWS_BEARER_TOKEN_BEDROCK":  true,
		"COGNITO_AUTH_DEBUG":        true,
		"DEBUG_MODE":                true,
		"GIP_MONITORING_TOKEN":      true,
		"GIP_CLIENT_SECRET":         true,
	}
	env := make([]string, 0, len(environ)+1)
	for _, kv := range environ {
		name, _, _ := strings.Cut(kv, "=")
		if !drop[name] {
			env = append(env, kv)
		}
	}
	return append(env, "AWS_PROFILE="+profile+"-collector")
}

// collectorStartBlocked returns why no collector should be started now, or ""
// when one should be:
//   - something already accepts connections on the receiver address: a running
//     collector, or the CoWork `otel-helper --proxy`, which binds the same port.
//     A second collector could not bind it and would leave collector.pid
//     pointing at a process that exited.
//   - collector.pid names a live process written less than collectorStartGrace
//     ago: a collector that is still starting.
//
// A live pid in an older pid file whose receiver is silent is not trusted: after
// a crash or a reboot another process can reuse the pid, and trusting it would
// stop the collector from ever being started again.
func collectorStartBlocked(pidFile, receiverAddr string) string {
	if receiverAnswers(receiverAddr) {
		return receiverAddr + " already accepts connections (a running collector, or the CoWork otel-helper --proxy)"
	}
	if pid, ok := startingCollectorPID(pidFile); ok {
		return fmt.Sprintf("PID %d in %s is still starting", pid, pidFile)
	}
	return ""
}

// receiverAnswers reports whether a TCP connection to addr succeeds.
func receiverAnswers(addr string) bool {
	conn, err := net.DialTimeout("tcp", addr, receiverDialTimeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// startingCollectorPID returns the pid in pidFile when that process is alive and
// the file was written less than collectorStartGrace ago. A missing, empty or
// unparsable file means no collector is starting.
func startingCollectorPID(pidFile string) (int, bool) {
	info, err := os.Stat(pidFile)
	if err != nil || time.Since(info.ModTime()) >= collectorStartGrace {
		return 0, false
	}
	data, err := os.ReadFile(pidFile)
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	// Signal 0 checks existence only. EPERM means the pid exists but belongs to
	// another user, which is not our collector.
	return pid, syscall.Kill(pid, 0) == nil
}

// collectorReceiverAddress returns receivers.otlp.protocols.http.endpoint from
// the collector config, or defaultReceiverAddress when the config does not set
// it. The config is the installer's rendering of collector-config.yaml (or
// collector-config-idc.yaml), so a small indentation-based reader is enough; it
// does not handle YAML flow mappings or anchors.
func collectorReceiverAddress(configPath string) string {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return defaultReceiverAddress
	}
	want := []string{"receivers", "otlp", "protocols", "http", "endpoint"}
	var keys []string
	var indents []int
	for _, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimRight(raw, " \t\r")
		trimmed := strings.TrimLeft(line, " ")
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		key, value, ok := strings.Cut(trimmed, ":")
		if !ok {
			continue
		}
		indent := len(line) - len(trimmed)
		for len(indents) > 0 && indents[len(indents)-1] >= indent {
			keys, indents = keys[:len(keys)-1], indents[:len(indents)-1]
		}
		keys, indents = append(keys, strings.TrimSpace(key)), append(indents, indent)
		if !slices.Equal(keys, want) {
			continue
		}
		if i := strings.Index(value, " #"); i >= 0 {
			value = value[:i]
		}
		if value = strings.Trim(strings.TrimSpace(value), `"'`); value != "" {
			return value
		}
	}
	return defaultReceiverAddress
}

// writePIDFile writes "<pid>\n" (the format otel-helper.sh wrote and
// `gip cleanup` reads) via a temporary file and rename.
func writePIDFile(path string, pid int) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+collectorPIDName+".*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.WriteString(strconv.Itoa(pid) + "\n"); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		os.Remove(tmpName)
		return err
	}
	return nil
}

func isRegularFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func isExecutableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0
}
