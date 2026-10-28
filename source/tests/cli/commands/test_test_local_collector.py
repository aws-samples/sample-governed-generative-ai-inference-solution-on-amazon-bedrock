"""`gip test`'s Local Collector check must not start a second collector.

The Go otel-helper now starts the sidecar on first use, so a user's collector is often already
listening on localhost:4318 when `gip test` runs. Starting another one failed with "address already
in use" (or on the sigv4auth credential lookup) and was reported as a broken binary (end-to-end test,
2026-10-27). The check now runs `otelcol components` and compares it with the config.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from governed_inference_platform.cli.commands import test as test_cmd
from governed_inference_platform.cli.commands.test import TestCommand

COMPONENTS = """buildinfo:
    command: otelcol-claude
receivers:
    - name: otlp
processors:
    - name: attributes
    - name: batch
exporters:
    - name: otlphttp
extensions:
    - name: sigv4auth
"""

CONFIG = """extensions:
  sigv4auth:
    service: "monitoring"
receivers:
  otlp:
    protocols:
      http:
        endpoint: localhost:4318
processors:
  batch/metrics:
    timeout: 60s
  attributes:
    actions: []
exporters:
  otlphttp:
    endpoint: "https://monitoring.us-east-1.amazonaws.com"
service:
  extensions: [sigv4auth]
"""


def _package(tmp_path: Path, config: str = CONFIG) -> Path:
    for suffix in ("macos-arm64", "macos-intel", "linux-arm64", "linux-x64", "windows.exe"):
        (tmp_path / f"otelcol-{suffix}").write_bytes(b"")
    (tmp_path / "collector-config.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def _fake_run(calls, returncode=0, stdout=COMPONENTS, stderr=""):
    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


def test_passes_without_starting_a_collector(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(test_cmd, "run_checked", _fake_run(calls))
    result = TestCommand()._test_local_collector(_package(tmp_path))
    assert result["status"] == "✓", result
    assert len(calls) == 1 and calls[0][1:] == ["components"], calls
    assert "--config" not in calls[0]


def test_reports_components_the_binary_lacks(tmp_path, monkeypatch):
    monkeypatch.setattr(
        test_cmd, "run_checked", _fake_run([], stdout=COMPONENTS.replace("    - name: sigv4auth\n", ""))
    )
    result = TestCommand()._test_local_collector(_package(tmp_path))
    assert result["status"] == "✗"
    assert "extension sigv4auth" in result["details"]


def test_reports_a_binary_that_fails_to_run(tmp_path, monkeypatch):
    monkeypatch.setattr(test_cmd, "run_checked", _fake_run([], returncode=1, stdout="", stderr="bad CPU type"))
    result = TestCommand()._test_local_collector(_package(tmp_path))
    assert result["status"] == "✗"
    assert "bad CPU type" in result["details"]


def test_reports_unparseable_output(tmp_path, monkeypatch):
    monkeypatch.setattr(test_cmd, "run_checked", _fake_run([], stdout=": : not yaml : :\n\t-"))
    result = TestCommand()._test_local_collector(_package(tmp_path))
    assert result["status"] == "✗"


@pytest.mark.parametrize("name", ["otelcol-macos-arm64", "collector-config.yaml"])
def test_missing_package_files_fail_before_running(tmp_path, monkeypatch, name):
    calls = []
    monkeypatch.setattr(test_cmd, "run_checked", _fake_run(calls))
    package = _package(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    (package / name).unlink()
    result = TestCommand()._test_local_collector(package)
    assert result["status"] == "✗"
    assert calls == []
