# ABOUTME: Contract between the macOS/Linux package and the sidecar collector launcher:
# ABOUTME: the shipped Go otel-helper must start ~/gip/otelcol from the paths install.sh writes.

"""Regression (end-to-end test, 2026-10-26): the sidecar collector never started.

macOS and Linux packages install the Go ``otel-helper`` binary as ``~/gip/otel-helper``
(Claude Code's ``otelHeadersHelper``). Only ``otel-helper.sh``, ``otel-helper.ps1`` and the
Python helper started ``~/gip/otelcol``, and no macOS/Linux package shipped the shell
wrapper, so nothing listened on localhost:4318 and usage never reached CloudWatch.

The Go helper now starts the collector itself (``source/go/cmd/otel-helper/collector.go``;
behavior is covered by ``collector_test.go``). These tests pin the cross-language contract:
the package builds that helper for macOS/Linux, the installer puts it, ``otelcol`` and the
collector config where the helper looks, and ``gip cleanup``/``gip status`` read the pid file
the helper writes.
"""

import re
from pathlib import Path

from governed_inference_platform.cli.commands import cleanup, package, status

SOURCE = Path(__file__).resolve().parents[1]
GO_HELPER = SOURCE / "go" / "cmd" / "otel-helper"


def _go_const(name: str) -> str:
    text = (GO_HELPER / "collector.go").read_text(encoding="utf-8")
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"{name} not found in collector.go"
    return match.group(1)


def _go_func_body(path: Path, signature: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(signature)
    end = text.index("\n}\n", start)
    return text[start:end]


def test_go_otel_helper_starts_collector_on_macos_and_linux():
    collector = (GO_HELPER / "collector.go").read_text(encoding="utf-8")
    assert collector.startswith("//go:build !windows\n"), "the launcher must be compiled for macOS and Linux"
    assert "func ensureCollector(profile string)" in collector
    # Every otelHeadersHelper invocation (normal and --test) goes through run().
    assert "ensureCollector(profile)" in _go_func_body(GO_HELPER / "main.go", "func run(testMode bool, profile string)")


def test_package_builds_that_helper_for_macos_and_linux():
    src = Path(package.__file__).read_text(encoding="utf-8")
    assert 'binaries_to_build.append("otel-helper")' in src
    assert 'f"./cmd/{binary}/"' in src


def test_installer_writes_the_paths_the_launcher_reads():
    src = Path(package.__file__).read_text(encoding="utf-8")
    binary = _go_const("collectorBinaryName")
    config = _go_const("collectorConfigName")
    assert (binary, config) == ("otelcol", "collector-config.yaml")
    # install.sh: the helper itself, then the collector and its config, all under ~/gip.
    assert 'gip_atomic_copy "$OTEL_BINARY" "gip/otel-helper"' in src
    assert f'gip_atomic_copy "$OTELCOL_BINARY" "gip/{binary}"' in src
    assert f'gip_atomic_copy "{config}" "gip/{config}"' in src
    # settings.json's otelHeadersHelper resolves to that installed helper.
    assert "s|__OTEL_HELPER_PATH__|$ACTUAL_HOME/gip/otel-helper|g" in src


def test_cleanup_and_status_read_the_pid_file_the_launcher_writes():
    pid_name = _go_const("collectorPIDName")
    for module in (cleanup, status):
        assert f'"{pid_name}"' in Path(module.__file__).read_text(encoding="utf-8")
    # gip cleanup only stops a process whose command line names ~/gip/otelcol and its config,
    # so the launcher must pass both as absolute paths.
    start = _go_func_body(GO_HELPER / "collector.go", "func startCollectorIfNeeded(")
    assert 'proc.StartBackground(binary, []string{binary, "--config", config}' in start
