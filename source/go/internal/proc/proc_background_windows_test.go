//go:build windows

package proc

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestStartBackgroundRejectsRelativePath(t *testing.T) {
	_, err := StartBackground("otelcol.exe", []string{"otelcol.exe"}, nil, "", os.Stdout)
	if !errors.Is(err, ErrNotAbsolute) {
		t.Fatalf("StartBackground(relative) error = %v, want ErrNotAbsolute", err)
	}
}

// The child gets the given environment, and its stdout goes to out, which the
// caller may close as soon as StartBackground returns.
func TestStartBackgroundRedirectsOutputAndUsesEnv(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "collector.log")
	out, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	env := append(os.Environ(), "GIP_PROC_TEST_HELPER=echo")
	pid, err := StartBackground(exe, []string{exe, "hello", "world"}, env, filepath.Dir(logPath), out)
	out.Close()
	if err != nil || pid <= 0 {
		t.Fatalf("StartBackground: pid=%d err=%v", pid, err)
	}
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if data, _ := os.ReadFile(logPath); string(data) == "hello world" {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	data, _ := os.ReadFile(logPath)
	t.Fatalf("log = %q, want %q", data, "hello world")
}
