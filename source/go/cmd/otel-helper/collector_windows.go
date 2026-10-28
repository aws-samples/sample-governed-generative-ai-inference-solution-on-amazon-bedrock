//go:build windows

package main

// ensureCollector is a no-op on Windows. There, otel-helper.ps1 starts
// %USERPROFILE%\gip\otelcol.exe. otel-helper.cmd runs otel-helper.ps1 only
// when otel-helper.exe fails, so starting the collector from this binary on
// Windows as well is a separate change that needs Windows and antivirus
// validation.
func ensureCollector(profile string) {}
