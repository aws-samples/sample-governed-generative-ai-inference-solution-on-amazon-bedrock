//go:build windows

// ABOUTME: Windows tests for the collector launcher. A copy of this test binary stands in for
// ABOUTME: otelcol.exe (TestMain runs it in fake-collector mode), so no script host is needed.

package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const fakeCollectorMarkerEnv = "GIP_OTEL_TEST_FAKE_COLLECTOR_MARKER"

// TestMain doubles as the fake collector: with fakeCollectorMarkerEnv set, the
// binary records "<AWS_PROFILE>|<AWS_ACCESS_KEY_ID or unset>|<argv>" to the
// marker file and stays alive like a real collector would.
func TestMain(m *testing.M) {
	if marker := os.Getenv(fakeCollectorMarkerEnv); marker != "" {
		key, ok := os.LookupEnv("AWS_ACCESS_KEY_ID")
		if !ok {
			key = "unset"
		}
		line := fmt.Sprintf("%s|%s|%s\n", os.Getenv("AWS_PROFILE"), key, strings.Join(os.Args[1:], " "))
		_ = os.WriteFile(marker, []byte(line), 0o600)
		time.Sleep(time.Minute)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// fakeCollector installs a copy of this test binary as installDir\otelcol.exe
// plus a config whose receiver is a free loopback port, which it returns.
func fakeCollector(t *testing.T, installDir, marker string) string {
	t.Helper()
	if err := os.MkdirAll(installDir, 0o700); err != nil {
		t.Fatal(err)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(installDir, "otelcol.exe"), data, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv(fakeCollectorMarkerEnv, marker)
	addr := freeLoopbackAddress(t)
	config := fmt.Sprintf("receivers:\n  otlp:\n    protocols:\n      http:\n        endpoint: %s\n", addr)
	if err := os.WriteFile(filepath.Join(installDir, collectorConfigName), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	return addr
}

func freeLoopbackAddress(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

func waitForMarker(t *testing.T, marker string) string {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if data, err := os.ReadFile(marker); err == nil && strings.HasSuffix(string(data), "\n") {
			return strings.TrimSpace(string(data))
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("collector stand-in never started (no %s)", marker)
	return ""
}

// killOnCleanup kills pid and waits for it to exit, so t.TempDir can delete
// otelcol.exe and collector.log (Windows keeps files in use undeletable).
func killOnCleanup(t *testing.T, pid int) {
	t.Helper()
	t.Cleanup(func() {
		p, err := os.FindProcess(pid)
		if err != nil {
			return
		}
		_ = p.Kill()
		_, _ = p.Wait()
	})
}

func readPID(t *testing.T, pidFile string) int {
	t.Helper()
	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("read %s: %v", pidFile, err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatalf("pid file %q: %v", data, err)
	}
	return pid
}

func exitedPID(t *testing.T) int {
	t.Helper()
	for attempt := 0; attempt < 5; attempt++ {
		// A fixed system command that exits at once; its pid is then free.
		cmd := exec.Command("cmd.exe", "/d", "/c", "exit", "0")
		start := time.Now()
		if err := cmd.Run(); err != nil {
			t.Fatal(err)
		}
		if elapsed := time.Since(start); elapsed > 20*time.Second {
			t.Fatalf("the exited-pid helper ran for %v; it acted as a long-lived process", elapsed)
		}
		// Windows reuses pids quickly; retry if this one already belongs to a new process.
		if pid := cmd.Process.Pid; !processAlive(pid) {
			return pid
		}
	}
	t.Fatal("every exited pid was reused at once")
	return 0
}

func TestWindowsStartCollectorStartsSidecarWithCollectorProfile(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	sessionDir := filepath.Join(root, ".gip-session")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	t.Setenv("AWS_PROFILE", "pilot")
	t.Setenv("AWS_ACCESS_KEY_ID", "static-key-from-caller")

	started, pid, err := startCollectorIfNeeded(installDir, sessionDir, "pilot")
	if err != nil {
		t.Fatalf("startCollectorIfNeeded: %v", err)
	}
	if !started || pid <= 0 {
		t.Fatalf("started=%v pid=%d, want a started collector", started, pid)
	}
	killOnCleanup(t, pid)

	if got := readPID(t, filepath.Join(installDir, collectorPIDName)); got != pid {
		t.Errorf("pid file = %d, want %d", got, pid)
	}
	want := "pilot-collector|unset|--config " + filepath.Join(installDir, collectorConfigName)
	if got := waitForMarker(t, marker); got != want {
		t.Errorf("collector saw %q, want %q", got, want)
	}
	if _, err := os.Stat(filepath.Join(sessionDir, collectorLogName)); err != nil {
		t.Errorf("collector log not created in the session dir: %v", err)
	}
	if !processAlive(pid) {
		t.Errorf("collector PID %d is not running after the helper's start returned", pid)
	}
}

func TestWindowsStartCollectorDoesNotStartASecondCollector(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	sessionDir := filepath.Join(root, ".gip-session")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)

	_, first, err := startCollectorIfNeeded(installDir, sessionDir, "gip")
	if err != nil || first <= 0 {
		t.Fatalf("first start: pid=%d err=%v", first, err)
	}
	killOnCleanup(t, first)
	waitForMarker(t, marker)

	started, second, err := startCollectorIfNeeded(installDir, sessionDir, "gip")
	if err != nil {
		t.Fatalf("second call: %v", err)
	}
	if started {
		killOnCleanup(t, second)
		t.Fatalf("a second collector (PID %d) was started while PID %d is running", second, first)
	}
}

func TestWindowsStartCollectorReplacesStalePIDFile(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	stale := exitedPID(t)
	pidFile := filepath.Join(installDir, collectorPIDName)
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(stale)+"\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	_ = os.Remove(marker) // only the restarted collector may write it

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if err != nil || !started {
		t.Fatalf("started=%v err=%v, want a restart after the collector exited", started, err)
	}
	killOnCleanup(t, pid)
	if got := readPID(t, pidFile); got != pid || got == stale {
		t.Errorf("pid file = %d, want new pid %d (stale was %d)", got, pid, stale)
	}
	want := "gip-collector|unset|--config " + filepath.Join(installDir, collectorConfigName)
	if got := waitForMarker(t, marker); got != want {
		t.Errorf("restarted collector saw %q, want %q", got, want)
	}
}

// The installed config names localhost:4318, while the collector (and the CoWork
// otel-helper --proxy) listen on 127.0.0.1 only. On Windows "localhost" resolves
// to ::1 first and a refused loopback connect is retried, so the probe must
// still find the IPv4 listener within its timeout. The pid file is older than
// the grace window, so only the receiver probe can block the second start.
func TestWindowsReceiverProbeFindsIPv4ListenerThroughLocalhost(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	config := fmt.Sprintf("receivers:\n  otlp:\n    protocols:\n      http:\n        endpoint: localhost:%s\n", port)
	if err := os.WriteFile(filepath.Join(installDir, collectorConfigName), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	pidFile := filepath.Join(installDir, collectorPIDName)
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-2 * collectorStartGrace)
	if err := os.Chtimes(pidFile, old, old); err != nil {
		t.Fatal(err)
	}

	start := time.Now()
	if !receiverAnswers("localhost:" + port) {
		t.Fatalf("receiverAnswers(localhost:%s) = false with a 127.0.0.1 listener (took %v)", port, time.Since(start))
	}
	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip: localhost:%s already answers", started, err, port)
	}
}

func TestWindowsStartCollectorNoopWithoutSidecarInstall(t *testing.T) {
	cases := map[string]func(t *testing.T, installDir string){
		"nothing installed": func(t *testing.T, installDir string) {},
		"config only": func(t *testing.T, installDir string) {
			_ = os.WriteFile(filepath.Join(installDir, collectorConfigName), []byte("x"), 0o600)
		},
		"binary only": func(t *testing.T, installDir string) {
			_ = os.WriteFile(filepath.Join(installDir, "otelcol.exe"), []byte("x"), 0o700)
		},
		"unix binary name only": func(t *testing.T, installDir string) {
			_ = os.WriteFile(filepath.Join(installDir, "otelcol"), []byte("x"), 0o700)
			_ = os.WriteFile(filepath.Join(installDir, collectorConfigName), []byte("x"), 0o600)
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			installDir := filepath.Join(root, "gip")
			sessionDir := filepath.Join(root, ".gip-session")
			if err := os.MkdirAll(installDir, 0o700); err != nil {
				t.Fatal(err)
			}
			setup(t, installDir)
			started, pid, err := startCollectorIfNeeded(installDir, sessionDir, "gip")
			if started || pid != 0 || err != nil {
				t.Fatalf("started=%v pid=%d err=%v, want a no-op", started, pid, err)
			}
			if _, err := os.Stat(sessionDir); !os.IsNotExist(err) {
				t.Errorf("session dir created for a non-sidecar install (stat err = %v)", err)
			}
		})
	}
}

func TestWindowsStartCollectorSkipsWhileAnotherInvocationStartsIt(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	sessionDir := filepath.Join(root, ".gip-session")
	fakeCollector(t, installDir, filepath.Join(root, "started"))
	if err := os.MkdirAll(sessionDir, 0o700); err != nil {
		t.Fatal(err)
	}
	held, err := os.OpenFile(filepath.Join(sessionDir, collectorLockName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	if ok, err := tryLockExclusive(held); !ok || err != nil {
		t.Fatalf("take the lock: ok=%v err=%v", ok, err)
	}

	started, pid, err := startCollectorIfNeeded(installDir, sessionDir, "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip while the lock is held", started, err)
	}
}

func TestWindowsStartCollectorSkipsWhenReceiverAddressIsTaken(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	addr := fakeCollector(t, installDir, filepath.Join(root, "started"))
	ln, err := net.Listen("tcp4", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip while %s is taken", started, err, addr)
	}
}

// The helper's normal path (what Claude Code's otelHeadersHelper runs through
// otel-helper.cmd) starts the collector and still prints one JSON object.
func TestWindowsRunStartsCollectorAndKeepsStdoutContract(t *testing.T) {
	home := t.TempDir()
	t.Setenv("USERPROFILE", home)
	t.Setenv("HOME", home)
	t.Setenv("GIP_MONITORING_TOKEN", "")
	installDir := filepath.Join(home, "gip")
	marker := filepath.Join(home, "started")
	fakeCollector(t, installDir, marker)

	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout := os.Stdout
	os.Stdout = w
	code := run(false, "wired")
	w.Close()
	os.Stdout = stdout
	out := make([]byte, 4096)
	n, _ := r.Read(out)

	pid := readPID(t, filepath.Join(installDir, collectorPIDName))
	killOnCleanup(t, pid)
	if code != 0 {
		t.Fatalf("run() = %d, want 0", code)
	}
	var headers map[string]string
	if err := json.Unmarshal(out[:n], &headers); err != nil {
		t.Fatalf("stdout is not one JSON object: %q (%v)", out[:n], err)
	}
	if got := waitForMarker(t, marker); !strings.HasPrefix(got, "wired-collector|") {
		t.Errorf("collector saw %q, want AWS_PROFILE=wired-collector", got)
	}
}

func TestWindowsProcessAlive(t *testing.T) {
	if !processAlive(os.Getpid()) {
		t.Error("processAlive(own pid) = false")
	}
	if processAlive(exitedPID(t)) {
		t.Error("processAlive(exited pid) = true")
	}
}

// Windows environment names are case-insensitive: a lower-case aws_profile or
// aws_access_key_id from the caller must not reach the collector.
func TestWindowsCollectorEnvDropsNamesCaseInsensitively(t *testing.T) {
	env := collectorEnv([]string{"Path=C:\\Windows", "aws_profile=other", "Aws_Access_Key_Id=static"}, "gip")
	got := strings.Join(env, ";")
	if strings.Contains(got, "other") || strings.Contains(got, "static") {
		t.Errorf("collectorEnv kept a differently-cased variable: %q", got)
	}
	if !strings.Contains(got, "AWS_PROFILE=gip-collector") || !strings.Contains(got, "Path=C:\\Windows") {
		t.Errorf("collectorEnv = %q", got)
	}
}
