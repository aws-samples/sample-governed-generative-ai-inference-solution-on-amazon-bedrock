//go:build !windows

// ABOUTME: macOS/Linux parts of the collector launcher: the start lock (flock), the
// ABOUTME: process liveness probe (signal 0) and the executable-bit check.

package main

import (
	"errors"
	"os"
	"syscall"
)

const collectorBinarySuffix = ""

// tryLockExclusive takes a non-blocking exclusive flock on f. It returns false
// with no error when another process holds the lock.
func tryLockExclusive(f *os.File) (bool, error) {
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// processAlive reports whether pid names a live process of this user. Signal 0
// checks existence only. EPERM means the pid exists but belongs to another
// user, which is not our collector.
func processAlive(pid int) bool {
	return syscall.Kill(pid, 0) == nil
}

func isExecutableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0
}

// envNameKey returns the environment variable name as the OS compares it.
func envNameKey(name string) string { return name }

// unlock is a no-op: closing the file releases the flock.
func unlock(*os.File) {}

// collectorWorkDir is the collector's working directory: the install directory,
// as before.
func collectorWorkDir(installDir, _ string) string { return installDir }
