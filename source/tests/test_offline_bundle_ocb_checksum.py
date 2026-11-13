"""scripts/prepare-offline-go-bundle.sh verifies the collector builder (OCB) before it makes it executable or runs it.

Each test runs the script in a throwaway copy of the repository layout, with fake `curl` and `go` first on PATH and
HOME pointed at the test directory. The fake builder is a shell script that records every run in a marker file.
"""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from governed_inference_platform.cli.commands import package as package_mod
from tests.support.proc import run_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "prepare-offline-go-bundle.sh"
PACKAGE_PY = REPO_ROOT / "source" / "governed_inference_platform" / "cli" / "commands" / "package.py"
MANIFEST = REPO_ROOT / "source" / "otel_helper" / "ocb-manifest.yaml"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not (shutil.which("sha256sum") or shutil.which("shasum")),
    reason="runs the bash script, which needs sha256sum or shasum",
)


def _host_asset() -> str:
    """The asset name the script picks on this host (same mapping as the script's uname cases)."""
    ocb_os = "darwin" if platform.system() == "Darwin" else "linux"
    ocb_arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"
    return f"ocb_{package_mod.OCB_VERSION}_{ocb_os}_{ocb_arch}"


def _fake_builder(marker: Path) -> bytes:
    return f'#!/bin/sh\necho ran >> "{marker}"\n'.encode()


@pytest.fixture
def sandbox(tmp_path):
    """A repository copy, fake curl/go, a test HOME, and the paths the tests inspect."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    pkg = repo / "source" / "governed_inference_platform" / "cli" / "commands"
    pkg.mkdir(parents=True)
    shutil.copy2(PACKAGE_PY, pkg / "package.py")
    (repo / "source" / "otel_helper").mkdir(parents=True)
    shutil.copy2(MANIFEST, repo / "source" / "otel_helper" / "ocb-manifest.yaml")
    (repo / "source" / "go").mkdir(parents=True)
    (repo / "source" / "go" / "go.mod").write_text("module example.com/x\n")
    (repo / "source" / "go" / "go.sum").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        '#!/bin/sh\necho called >> "$FAKE_CURL_CALLS"\n'
        'while [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then out="$2"; fi; shift; done\n'
        'cat "$FAKE_OCB_CONTENT" > "$out"\n'
    )
    (bin_dir / "go").write_text(
        '#!/bin/sh\nif [ "$1" = "version" ]; then echo "go version go1.23.4 test/arch"; fi\nexit 0\n'
    )
    for tool in ("curl", "go"):
        (bin_dir / tool).chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "ocb-ran"
    content = tmp_path / "ocb-content"
    content.write_bytes(_fake_builder(marker))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "FAKE_OCB_CONTENT": str(content),
        "FAKE_CURL_CALLS": str(tmp_path / "curl-calls"),
    }
    return {
        "repo": repo,
        "package_py": pkg / "package.py",
        "bundle": tmp_path / "bundle",
        "home": home,
        "marker": marker,
        "content": content,
        "curl_calls": tmp_path / "curl-calls",
        "env": env,
    }


def _run(sandbox, mode: str) -> subprocess.CompletedProcess[str]:
    return run_cmd(  # nosec B603
        ["bash", str(sandbox["repo"] / "scripts" / SCRIPT.name), mode, str(sandbox["bundle"])],
        text=True,
        capture_output=True,
        env=sandbox["env"],
        cwd=sandbox["repo"],
        check=False,
    )


def _pin_fake_builder(sandbox) -> None:
    """Rewrite the copied package.py so every asset's pinned digest is the fake builder's."""
    digest = hashlib.sha256(sandbox["content"].read_bytes()).hexdigest()
    text = sandbox["package_py"].read_text(encoding="utf-8")
    for asset, real in package_mod._OCB_SHA256.items():
        text = text.replace(f'"{asset}": "{real}"', f'"{asset}": "{digest}"')
    sandbox["package_py"].write_text(text, encoding="utf-8")


def test_prepare_refuses_a_download_with_the_wrong_digest_and_never_runs_it(sandbox):
    result = _run(sandbox, "prepare")

    assert result.returncode != 0, result.stdout
    assert "SHA-256 mismatch" in result.stderr
    assert not sandbox["marker"].exists(), "the unverified builder was executed"
    ocb_dir = sandbox["bundle"] / "ocb"
    assert not ocb_dir.exists() or list(ocb_dir.iterdir()) == [], "a refused download must be removed"


def test_prepare_refuses_before_downloading_when_no_digest_is_pinned(sandbox):
    text = sandbox["package_py"].read_text(encoding="utf-8")
    sandbox["package_py"].write_text(text.replace(f'"{_host_asset()}": ', '"ocb_unlisted_asset": '), encoding="utf-8")

    result = _run(sandbox, "prepare")

    assert result.returncode != 0, result.stdout
    assert "No pinned SHA-256" in result.stderr
    assert not sandbox["curl_calls"].exists(), "the builder was downloaded without a digest to check it against"
    assert not sandbox["marker"].exists()


def test_prepare_refuses_an_existing_bundle_file_with_the_wrong_digest(sandbox):
    ocb = sandbox["bundle"] / "ocb" / _host_asset()
    ocb.parent.mkdir(parents=True)
    ocb.write_bytes(_fake_builder(sandbox["marker"]))
    ocb.chmod(0o755)

    result = _run(sandbox, "prepare")

    assert result.returncode != 0, result.stdout
    assert "SHA-256 mismatch" in result.stderr
    assert not sandbox["marker"].exists(), "the unverified builder was executed"
    assert not ocb.exists()


def test_prepare_runs_a_builder_whose_digest_matches(sandbox):
    _pin_fake_builder(sandbox)

    result = _run(sandbox, "prepare")

    assert result.returncode == 0, result.stderr
    assert sandbox["marker"].read_text().count("ran") == 2  # module seeding, then the offline rehearsal
    assert (sandbox["bundle"] / "ocb" / _host_asset()).exists()
    assert Path(f"{sandbox['bundle']}.tar.gz").exists()


def test_install_refuses_a_bundle_builder_with_the_wrong_digest(sandbox):
    ocb = sandbox["bundle"] / "ocb" / _host_asset()
    ocb.parent.mkdir(parents=True)
    ocb.write_bytes(b"not the released collector builder")

    result = _run(sandbox, "install")

    assert result.returncode != 0, result.stdout
    assert "SHA-256 mismatch" in result.stderr
    assert not (sandbox["home"] / ".cache" / "ocb").exists() or not any((sandbox["home"] / ".cache" / "ocb").iterdir())
