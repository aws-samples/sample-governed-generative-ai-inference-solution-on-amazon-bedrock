//go:build !windows

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
	"syscall"
	"testing"
	"time"
)

// fakeCollector installs a stand-in for ~/gip/otelcol plus a config file in
// installDir. The stand-in records "<AWS_PROFILE>|<AWS_ACCESS_KEY_ID or unset>|<argv>"
// to marker and then stays alive like a real collector would. The config names
// a free loopback port as the receiver address, so the tests never depend on
// whether something on this machine listens on 4318; it is returned.
func fakeCollector(t *testing.T, installDir, marker string) string {
	t.Helper()
	if err := os.MkdirAll(installDir, 0o700); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\n" +
		"printf '%s|%s|%s\\n' \"$AWS_PROFILE\" \"${AWS_ACCESS_KEY_ID-unset}\" \"$*\" > '" + marker + "'\n" +
		"exec sleep 60\n"
	if err := os.WriteFile(filepath.Join(installDir, collectorBinaryName), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	addr := freeLoopbackAddress(t)
	config := fmt.Sprintf("receivers:\n  otlp:\n    protocols:\n      http:\n        endpoint: %s\n        include_metadata: true\n", addr)
	if err := os.WriteFile(filepath.Join(installDir, collectorConfigName), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	return addr
}

// freeLoopbackAddress returns 127.0.0.1:<port> for a port nothing listens on.
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

// waitForMarker returns the line the fake collector wrote once it has started.
func waitForMarker(t *testing.T, marker string) string {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if data, err := os.ReadFile(marker); err == nil && strings.HasSuffix(string(data), "\n") {
			return strings.TrimSpace(string(data))
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("collector stand-in never started (no %s)", marker)
	return ""
}

func killOnCleanup(t *testing.T, pid int) {
	t.Helper()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
}

func readPID(t *testing.T, pidFile string) int {
	t.Helper()
	data, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("read %s: %v", pidFile, err)
	}
	if !strings.HasSuffix(string(data), "\n") {
		t.Errorf("pid file %q should end with a newline (the format gip cleanup reads)", data)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatalf("pid file %q: %v", data, err)
	}
	return pid
}

// reapedPID returns the pid of a process that has exited and been waited for,
// i.e. a stale pid file entry.
func reapedPID(t *testing.T) int {
	t.Helper()
	cmd := exec.Command("/bin/sh", "-c", "exit 0")
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	return cmd.Process.Pid
}

// R1 regression: the Go otel-helper is what macOS/Linux packages install as
// ~/gip/otel-helper, and before this fix it never started the sidecar collector
// (only otel-helper.sh did, and no package shipped it). Nothing listened on
// localhost:4318 and usage never reached CloudWatch.
func TestStartCollectorStartsSidecarWithCollectorProfile(t *testing.T) {
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
	if _, err := os.Stat(filepath.Join(installDir, collectorLockName)); !os.IsNotExist(err) {
		t.Errorf("the start lock must not be created in ~/gip (gip cleanup removes that directory), stat err = %v", err)
	}
}

func TestStartCollectorDoesNotStartASecondCollector(t *testing.T) {
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
	if got := readPID(t, filepath.Join(installDir, collectorPIDName)); got != first {
		t.Errorf("pid file changed to %d, want %d", got, first)
	}
}

func TestStartCollectorReplacesStalePIDFile(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	stale := reapedPID(t)
	pidFile := filepath.Join(installDir, collectorPIDName)
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(stale)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if err != nil || !started {
		t.Fatalf("started=%v err=%v, want a restart after the collector exited", started, err)
	}
	killOnCleanup(t, pid)
	if got := readPID(t, pidFile); got != pid || got == stale {
		t.Errorf("pid file = %d, want new pid %d (stale was %d)", got, pid, stale)
	}
	waitForMarker(t, marker)
}

// Central mode, IDC zero-binary and an incomplete install have no runnable
// otelcol + config pair: nothing may be started and no state written.
func TestStartCollectorNoopWithoutSidecarInstall(t *testing.T) {
	cases := map[string]func(t *testing.T, installDir string){
		"nothing installed": func(t *testing.T, installDir string) {},
		"config only": func(t *testing.T, installDir string) {
			writeFile(t, filepath.Join(installDir, collectorConfigName), 0o600)
		},
		"binary only": func(t *testing.T, installDir string) {
			writeFile(t, filepath.Join(installDir, collectorBinaryName), 0o755)
		},
		"binary not executable": func(t *testing.T, installDir string) {
			writeFile(t, filepath.Join(installDir, collectorBinaryName), 0o644)
			writeFile(t, filepath.Join(installDir, collectorConfigName), 0o600)
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
			if _, err := os.Stat(filepath.Join(installDir, collectorPIDName)); !os.IsNotExist(err) {
				t.Errorf("pid file written for a non-sidecar install (stat err = %v)", err)
			}
			if _, err := os.Stat(sessionDir); !os.IsNotExist(err) {
				t.Errorf("session dir created for a non-sidecar install (stat err = %v)", err)
			}
		})
	}
}

// Another invocation holding the start lock is starting the collector right
// now; this one must not start a second.
func TestStartCollectorSkipsWhileAnotherInvocationStartsIt(t *testing.T) {
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
	if err := syscall.Flock(int(held.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}

	started, pid, err := startCollectorIfNeeded(installDir, sessionDir, "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip while the lock is held", started, err)
	}
	if _, err := os.Stat(filepath.Join(installDir, collectorPIDName)); !os.IsNotExist(err) {
		t.Errorf("pid file written while another invocation holds the lock (stat err = %v)", err)
	}
}

// The helper's normal path (what Claude Code's otelHeadersHelper runs) starts
// the collector and still prints exactly one JSON object on stdout.
func TestRunStartsCollectorAndKeepsStdoutContract(t *testing.T) {
	home := t.TempDir()
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

func TestCollectorEnvReplacesProfileAndDropsStaticCredentials(t *testing.T) {
	env := collectorEnv([]string{
		"PATH=/usr/bin",
		"AWS_PROFILE=other",
		"AWS_REGION=us-east-1",
		"AWS_ACCESS_KEY_ID=static",
		"AWS_SECRET_ACCESS_KEY=static",
		"AWS_SESSION_TOKEN=static",
		"AWS_CREDENTIAL_EXPIRATION=2000-01-01T00:00:00Z",
		"AWS_BEARER_TOKEN_BEDROCK=bedrock-api-key-static",
	}, "pilot")
	got := strings.Join(env, "\n")
	want := "PATH=/usr/bin\nAWS_REGION=us-east-1\nAWS_PROFILE=pilot-collector"
	if got != want {
		t.Errorf("collectorEnv =\n%s\nwant\n%s", got, want)
	}
}

// Regression (review, 2026-10-27): the CoWork `otel-helper --proxy` binds the
// collector's port, 127.0.0.1:4318. With the proxy running first, every helper
// invocation started an otelcol that failed to bind and left collector.pid
// pointing at a process that had exited.
func TestStartCollectorSkipsWhenReceiverAddressIsTaken(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	addr := fakeCollector(t, installDir, marker)
	proxy, err := net.Listen("tcp4", addr) // stands in for otel-helper --proxy
	if err != nil {
		t.Fatal(err)
	}
	defer proxy.Close()

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip while %s is taken", started, err, addr)
	}
	if _, err := os.Stat(filepath.Join(installDir, collectorPIDName)); !os.IsNotExist(err) {
		t.Errorf("pid file written although no collector was started (stat err = %v)", err)
	}
	time.Sleep(100 * time.Millisecond)
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Errorf("a collector was started (stat err = %v)", err)
	}
}

// Regression (review, 2026-10-27): after a crash or a reboot another process
// can reuse the pid in collector.pid. Trusting it suppressed the start for good.
func TestStartCollectorIgnoresReusedPID(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	pidFile := filepath.Join(installDir, collectorPIDName)
	reused := os.Getpid() // alive, and not a collector
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(reused)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-collectorStartGrace - time.Minute)
	if err := os.Chtimes(pidFile, old, old); err != nil {
		t.Fatal(err)
	}

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if err != nil || !started {
		t.Fatalf("started=%v err=%v, want a start: PID %d is not a collector", started, err, reused)
	}
	killOnCleanup(t, pid)
	if got := readPID(t, pidFile); got != pid {
		t.Errorf("pid file = %d, want %d", got, pid)
	}
	waitForMarker(t, marker)
}

// A collector that has not bound its receiver yet (its sigv4auth extension is
// still fetching credentials) must not get a second one started beside it.
func TestStartCollectorWaitsForACollectorThatIsStillStarting(t *testing.T) {
	root := t.TempDir()
	installDir := filepath.Join(root, "gip")
	marker := filepath.Join(root, "started")
	fakeCollector(t, installDir, marker)
	pidFile := filepath.Join(installDir, collectorPIDName)
	if err := writePIDFile(pidFile, os.Getpid()); err != nil {
		t.Fatal(err)
	}

	started, pid, err := startCollectorIfNeeded(installDir, filepath.Join(root, ".gip-session"), "gip")
	if started || err != nil {
		if pid > 0 {
			killOnCleanup(t, pid)
		}
		t.Fatalf("started=%v err=%v, want a skip while PID %d is starting", started, err, os.Getpid())
	}
}

// The shipped config says localhost:4318 and the CoWork proxy listens on tcp4
// 127.0.0.1:4318, so the probe must reach an IPv4 listener through "localhost".
func TestReceiverAnswersThroughLocalhostForAnIPv4Listener(t *testing.T) {
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := fmt.Sprintf("localhost:%d", ln.Addr().(*net.TCPAddr).Port)
	if !receiverAnswers(addr) {
		t.Errorf("receiverAnswers(%q) = false with a listener on 127.0.0.1", addr)
	}
	_ = ln.Close()
	if receiverAnswers(addr) {
		t.Errorf("receiverAnswers(%q) = true after the listener closed", addr)
	}
}

// The address the launcher probes is the one the shipped configs bind.
func TestCollectorReceiverAddressReadsShippedConfigs(t *testing.T) {
	for _, name := range []string{"collector-config.yaml", "collector-config-idc.yaml"} {
		path := filepath.Join("..", "..", "..", "otel_helper", name)
		if got := collectorReceiverAddress(path); got != "localhost:4318" {
			t.Errorf("%s: receiver address = %q, want localhost:4318", name, got)
		}
	}
	dir := t.TempDir()
	cases := map[string]string{
		"missing file": "",
		"no endpoint":  "receivers:\n  otlp:\n    protocols:\n      grpc:\n        endpoint: 127.0.0.1:4317\n",
		"quoted, with a comment": "extensions:\n  health_check:\n    endpoint: 0.0.0.0:13133\nreceivers:\n  otlp:\n" +
			"    protocols:\n      http:\n        endpoint: \"127.0.0.1:14318\" # local only\n",
	}
	want := map[string]string{"missing file": defaultReceiverAddress, "no endpoint": defaultReceiverAddress, "quoted, with a comment": "127.0.0.1:14318"}
	for name, content := range cases {
		path := filepath.Join(dir, strings.ReplaceAll(name, " ", "-")+".yaml")
		if content != "" {
			if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		if got := collectorReceiverAddress(path); got != want[name] {
			t.Errorf("%s: receiver address = %q, want %q", name, got, want[name])
		}
	}
}

// Regression (review, 2026-10-27): the collector runs for days and the AWS SDK
// copies credential-process stderr into collector.log, so debug switches and
// tokens inherited from the terminal that started Claude Code must not reach it.
func TestCollectorEnvDropsDebugSwitchesAndTokens(t *testing.T) {
	env := collectorEnv([]string{
		"HOME=/home/u",
		"HTTPS_PROXY=http://proxy.example:3128",
		"COGNITO_AUTH_DEBUG=1",
		"DEBUG_MODE=1",
		"GIP_MONITORING_TOKEN=eyJ.id.token",
		"GIP_CLIENT_SECRET=azure-secret",
	}, "pilot")
	got := strings.Join(env, "\n")
	want := "HOME=/home/u\nHTTPS_PROXY=http://proxy.example:3128\nAWS_PROFILE=pilot-collector"
	if got != want {
		t.Errorf("collectorEnv =\n%s\nwant\n%s", got, want)
	}
}

func writeFile(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, []byte("x\n"), mode); err != nil {
		t.Fatal(err)
	}
}
