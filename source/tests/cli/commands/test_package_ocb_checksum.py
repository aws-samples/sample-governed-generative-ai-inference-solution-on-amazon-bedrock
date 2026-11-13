"""The collector builder (OCB) that `gip package` downloads is verified by SHA-256 before it runs."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from governed_inference_platform.cli.commands import package as package_mod
from governed_inference_platform.cli.commands.package import PackageCommand

REPO_ROOT = Path(__file__).resolve().parents[4]
LINUX_ASSET = f"ocb_{package_mod.OCB_VERSION}_linux_amd64"


@pytest.fixture
def linux_host(monkeypatch, tmp_path):
    """A Linux x86_64 packaging host with Go 1.23, whose home (and so ~/.cache/ocb) is tmp_path."""
    monkeypatch.setattr(package_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(package_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(package_mod.Path, "home", classmethod(lambda cls: tmp_path))
    go = SimpleNamespace(returncode=0, stdout="go version go1.23.4 linux/amd64", stderr="")
    monkeypatch.setattr(package_mod.subprocess, "run", MagicMock(return_value=go))
    # Stands in for running OCB: records the call and stops the build right after it.
    ran = MagicMock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="stop"))
    monkeypatch.setattr(package_mod, "run_checked", ran)
    (tmp_path / "out").mkdir()
    return {"ocb_dir": tmp_path / ".cache" / "ocb", "ran": ran}


def _fake_download(content: bytes):
    def urlretrieve(url, filename):
        Path(filename).write_bytes(content)
        return filename, None

    return urlretrieve


def _ocb_was_run(ran: MagicMock) -> bool:
    return any("--skip-compilation" in call.args[0] for call in ran.call_args_list)


def test_digest_table_covers_every_host_the_builder_supports():
    for os_arch in ("darwin_amd64", "darwin_arm64", "linux_amd64", "linux_arm64", "windows_amd64.exe"):
        asset = f"ocb_{package_mod.OCB_VERSION}_{os_arch}"
        assert re.fullmatch(r"[0-9a-f]{64}", package_mod._OCB_SHA256.get(asset, "")), f"no SHA-256 pinned for {asset}"


def test_offline_bundle_script_still_parses_one_version():
    """scripts/prepare-offline-go-bundle.sh reads OCB_VERSION from package.py with sed; exactly one line must match."""
    text = (REPO_ROOT / "source/governed_inference_platform/cli/commands/package.py").read_text(encoding="utf-8")
    assert re.findall(r'OCB_VERSION = "([0-9.]*)"', text) == [package_mod.OCB_VERSION]


def test_verify_rejects_a_file_whose_digest_is_wrong(tmp_path):
    binary = tmp_path / LINUX_ASSET
    binary.write_bytes(b"not the released collector builder")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        package_mod._verify_ocb_binary(binary, LINUX_ASSET)


def test_verify_rejects_an_asset_with_no_pinned_digest(tmp_path):
    binary = tmp_path / "ocb_0.0.1_plan9_amd64"
    binary.write_bytes(b"anything")
    with pytest.raises(RuntimeError, match="No pinned SHA-256"):
        package_mod._verify_ocb_binary(binary, binary.name)


def test_download_with_wrong_digest_is_refused_and_never_run(linux_host, monkeypatch, tmp_path):
    monkeypatch.setattr("urllib.request.urlretrieve", _fake_download(b"tampered release asset"))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    assert not _ocb_was_run(linux_host["ran"])
    assert not (linux_host["ocb_dir"] / LINUX_ASSET).exists(), "a refused download must not stay in the cache"
    assert list(linux_host["ocb_dir"].iterdir()) == []


def test_cached_builder_with_wrong_digest_is_refused_and_never_run(linux_host, tmp_path):
    linux_host["ocb_dir"].mkdir(parents=True)
    (linux_host["ocb_dir"] / LINUX_ASSET).write_bytes(b"replaced after download")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    assert not _ocb_was_run(linux_host["ran"])


def test_missing_digest_refuses_before_downloading(linux_host, monkeypatch, tmp_path):
    monkeypatch.setattr(package_mod, "_OCB_SHA256", {})
    download = MagicMock()
    monkeypatch.setattr("urllib.request.urlretrieve", download)
    with pytest.raises(RuntimeError, match="No pinned SHA-256"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    download.assert_not_called()
    assert not _ocb_was_run(linux_host["ran"])


def test_download_with_matching_digest_is_cached_and_run(linux_host, monkeypatch, tmp_path):
    content = b"the released collector builder"
    monkeypatch.setattr(package_mod, "_OCB_SHA256", {LINUX_ASSET: hashlib.sha256(content).hexdigest()})
    monkeypatch.setattr("urllib.request.urlretrieve", _fake_download(content))
    with pytest.raises(RuntimeError, match="OCB source generation failed"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    cached = linux_host["ocb_dir"] / LINUX_ASSET
    assert cached.read_bytes() == content
    assert _ocb_was_run(linux_host["ran"])
    assert linux_host["ran"].call_args_list[0].args[0][0] == str(cached)


def test_cached_builder_with_matching_digest_is_used_without_downloading(linux_host, monkeypatch, tmp_path):
    content = b"the released collector builder"
    monkeypatch.setattr(package_mod, "_OCB_SHA256", {LINUX_ASSET: hashlib.sha256(content).hexdigest()})
    linux_host["ocb_dir"].mkdir(parents=True)
    cached = linux_host["ocb_dir"] / LINUX_ASSET
    cached.write_bytes(content)
    download = MagicMock()
    monkeypatch.setattr("urllib.request.urlretrieve", download)
    with pytest.raises(RuntimeError, match="OCB source generation failed"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    download.assert_not_called()
    assert linux_host["ran"].call_args_list[0].args[0][0] == str(cached)


def test_download_reaches_the_cache_path_only_after_it_verifies(linux_host, monkeypatch, tmp_path):
    content = b"the released collector builder"
    monkeypatch.setattr(package_mod, "_OCB_SHA256", {LINUX_ASSET: hashlib.sha256(content).hexdigest()})
    cached = linux_host["ocb_dir"] / LINUX_ASSET
    targets, cache_present_at_verify = [], []

    def urlretrieve(url, filename):
        targets.append(Path(filename))
        Path(filename).write_bytes(content)
        return filename, None

    real_verify = package_mod._verify_ocb_binary

    def verify(path, asset):
        cache_present_at_verify.append(cached.exists())
        return real_verify(path, asset)

    monkeypatch.setattr("urllib.request.urlretrieve", urlretrieve)
    monkeypatch.setattr(package_mod, "_verify_ocb_binary", verify)
    with pytest.raises(RuntimeError, match="OCB source generation failed"):
        PackageCommand()._build_otelcol(tmp_path / "out", ["linux-x64"])
    assert targets and all(t != cached for t in targets), f"downloaded straight into the cache path: {targets}"
    assert cache_present_at_verify == [False], "the cache path held the builder before it was verified"
    assert cached.read_bytes() == content
