//go:build windows

// ABOUTME: Windows parts of the collector launcher: the start lock (LockFileEx), the
// ABOUTME: process liveness probe (OpenProcess + exit code) and the .exe binary name.

package main

import (
	"errors"
	"os"
	"strings"

	"golang.org/x/sys/windows"
)

// install.bat installs the collector as %USERPROFILE%\gip\otelcol.exe.
const collectorBinarySuffix = ".exe"

// stillActive is the exit code GetExitCodeProcess reports for a running process.
const stillActive = 259

// tryLockExclusive takes a non-blocking exclusive lock on the first byte of f.
// It returns false with no error when another handle holds the lock. The lock is
// released when f is closed.
func tryLockExclusive(f *os.File) (bool, error) {
	ol := new(windows.Overlapped)
	err := windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ol)
	if err != nil {
		if errors.Is(err, windows.ERROR_LOCK_VIOLATION) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// processAlive reports whether pid names a running process this user can query.
// A pid that does not exist, has exited, or can't be opened (another user's
// process) is not our collector. PROCESS_QUERY_LIMITED_INFORMATION is granted
// across integrity levels for the same user, so an elevated process of this
// user that reuses the pid counts as alive; the receiver probe and the
// 10-minute grace window bound the effect of that.
func processAlive(pid int) bool {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(h)
	var code uint32
	if err := windows.GetExitCodeProcess(h, &code); err != nil {
		return false
	}
	return code == stillActive
}

// isExecutableFile reports whether path is a regular file. Windows has no
// executable permission bit; the .exe name is what makes it runnable.
func isExecutableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

// envNameKey returns the environment variable name as Windows compares it:
// case-insensitively, so aws_profile and AWS_PROFILE are the same variable.
func envNameKey(name string) string { return strings.ToUpper(name) }

// unlock releases the byte-range lock explicitly before the file is closed;
// Microsoft recommends UnlockFileEx over relying on the implicit release.
func unlock(f *os.File) {
	_ = windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, new(windows.Overlapped))
}

// collectorWorkDir runs the collector in the session directory, so the
// long-lived process doesn't hold %USERPROFILE%\gip open and block upgrades,
// uninstall or gip cleanup from removing it.
func collectorWorkDir(_, sessionDir string) string { return sessionDir }
