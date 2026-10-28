//go:build !windows

package proc

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// StartBackground starts absPath with argv as a long-lived background service
// and returns its pid without waiting for it.
//
// The child runs in a new session (setsid), so it is not in the caller's
// process group: it outlives the caller and is not signalled when the terminal
// or the parent application that started the caller goes away. When the caller
// exits, the child is re-parented to init/launchd, which reaps it.
//
// env replaces the child's environment (nil inherits the caller's), dir is its
// working directory ("" keeps the caller's), stdin is os.DevNull, and stdout
// and stderr both go to out. The caller may close out after this returns; the
// child keeps its own descriptor.
func StartBackground(absPath string, argv, env []string, dir string, out *os.File) (int, error) {
	if !filepath.IsAbs(absPath) {
		return 0, fmt.Errorf("%w: %q", ErrNotAbsolute, absPath)
	}
	devNull, err := os.OpenFile(os.DevNull, os.O_RDONLY, 0)
	if err != nil {
		return 0, err
	}
	defer devNull.Close()

	p, err := os.StartProcess(absPath, argv, &os.ProcAttr{
		Dir:   dir,
		Env:   env,
		Files: []*os.File{devNull, out, out},
		Sys:   &syscall.SysProcAttr{Setsid: true},
	})
	if err != nil {
		return 0, err
	}
	pid := p.Pid
	// No waiter: the caller is a short-lived helper and init/launchd reaps the
	// child once the caller exits.
	_ = p.Release()
	return pid, nil
}
