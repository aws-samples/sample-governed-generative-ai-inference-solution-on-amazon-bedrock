"""Pytest configuration and shared fixtures."""

import os
from pathlib import Path

import pytest


# Set AWS region for all tests to avoid NoRegionError
@pytest.fixture(autouse=True, scope="session")
def set_aws_region():
    """Set AWS region for all tests."""
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_REGION"] = "us-east-1"


# Tests must never write into the real ~/.gip (profiles there are a user's real configuration). Every test that
# creates a Config must patch Config.CONFIG_DIR, CONFIG_FILE and PROFILES_DIR: PROFILES_DIR is computed once at
# import, so patching CONFIG_DIR alone still writes profiles into the real home.
_REAL_GIP = Path.home() / ".gip"


def _gip_snapshot():
    if not _REAL_GIP.exists():
        return {}
    return {str(p.relative_to(_REAL_GIP)): p.stat().st_mtime_ns for p in _REAL_GIP.rglob("*") if p.is_file()}


@pytest.fixture(autouse=True)
def isolated_gip_config(tmp_path, monkeypatch):
    """Point Config at a per-test directory, so a test that forgets to patch it cannot write the real ~/.gip."""
    from governed_inference_platform.config import Config

    gip = tmp_path / ".gip"
    monkeypatch.setattr(Config, "CONFIG_DIR", gip)
    monkeypatch.setattr(Config, "CONFIG_FILE", gip / "config.json")
    monkeypatch.setattr(Config, "PROFILES_DIR", gip / "profiles")
    yield gip


@pytest.fixture(autouse=True, scope="session")
def never_write_real_gip_home():
    """Fail the run if any test created or changed a file under the real ~/.gip."""
    before = _gip_snapshot()
    yield
    after = _gip_snapshot()
    changed = sorted(k for k in after if before.get(k) != after[k])
    assert not changed, (
        f"tests wrote into the real {_REAL_GIP}: {changed}; patch Config.CONFIG_DIR, CONFIG_FILE and PROFILES_DIR"
    )
