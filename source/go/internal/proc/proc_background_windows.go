//go:build windows

package proc

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"

	"golang.org/x/sys/windows"
)

// StartBackground starts absPath with argv as a long-lived background service
// and returns its pid without waiting for it.
//
// The child gets its own hidden console (CREATE_NO_WINDOW) in a new process
// group, so no window appears, Ctrl+C in the caller's console does not reach
// it, and the console programs it runs itself (credential-process, through the
// AWS SDK's credential_process) inherit that hidden console instead of each
// opening a window. DETACHED_PROCESS is deliberately not used: a child with no
// console makes every console program it starts allocate a visible one.
//
// It first asks to break away from the caller's job object
// (CREATE_BREAKAWAY_FROM_JOB), so the collector outlives an application that
// kills its job on exit, as it outlives the caller on macOS and Linux. When the
// job does not allow breakaway, CreateProcess fails with access denied, and only
// then is the child started inside the job instead.
//
// env replaces the child's environment (nil inherits the caller's), dir is its
// working directory ("" keeps the caller's), stdin is os.DevNull, and stdout
// and stderr both go to out. The caller may close out after this returns; the
// child keeps its own handle.
func StartBackground(absPath string, argv, env []string, dir string, out *os.File) (int, error) {
	if !filepath.IsAbs(absPath) {
		return 0, fmt.Errorf("%w: %q", ErrNotAbsolute, absPath)
	}
	devNull, err := os.OpenFile(os.DevNull, os.O_RDONLY, 0)
	if err != nil {
		return 0, err
	}
	defer devNull.Close()

	const flags = windows.CREATE_NO_WINDOW | windows.CREATE_NEW_PROCESS_GROUP
	start := func(extra uint32) (*os.Process, error) {
		return os.StartProcess(absPath, argv, &os.ProcAttr{
			Dir:   dir,
			Env:   env,
			Files: []*os.File{devNull, out, out},
			Sys:   &syscall.SysProcAttr{HideWindow: true, CreationFlags: flags | extra},
		})
	}
	p, err := start(windows.CREATE_BREAKAWAY_FROM_JOB)
	if errors.Is(err, windows.ERROR_ACCESS_DENIED) {
		p, err = start(0)
	}
	if err != nil {
		return 0, err
	}
	pid := p.Pid
	// Release closes this process's handle to the child; the child keeps
	// running. Nothing waits for it, as on macOS and Linux.
	_ = p.Release()
	return pid, nil
}
