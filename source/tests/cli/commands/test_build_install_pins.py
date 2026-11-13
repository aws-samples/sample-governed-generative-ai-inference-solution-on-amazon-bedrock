"""Every package named in a pip install of the package builds is pinned to an exact version."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from governed_inference_platform.cli.commands import package as package_mod
from governed_inference_platform.cli.commands.package import PackageCommand
from tests.test_windows_buildspec_yaml import _all_commands, _collect_buildspecs

REPO_ROOT = Path(__file__).resolve().parents[4]
EXACT_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][A-Za-z0-9.+!-]*")


def _lock_versions() -> dict[str, str]:
    text = (REPO_ROOT / "source" / "poetry.lock").read_text(encoding="utf-8")
    return {
        re.sub(r"[-_.]+", "-", name).lower(): version
        for name, version in re.findall(r'^\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', text, re.M)
    }


def _unpinned(requirements: list[str]) -> list[str]:
    return [r for r in requirements if not r.startswith("-") and not EXACT_PIN.fullmatch(r)]


def _pip_install_args(text: str) -> list[list[str]]:
    """Requirement lists of every `pip install` / `pip3 install` / `-m pip install` in a command text."""
    found = []
    for m in re.finditer(r"\bpip3? install\b([^&|\n]*)", text):
        found.append(m.group(1).split())
    return found


def test_locked_requirements_use_the_lock_versions():
    lock = _lock_versions()
    names = ["boto3", "requests", "PyJWT", "cryptography", "keyring", "python-dateutil", "pyinstaller"]
    for pin in package_mod._locked_requirements(names):
        name, version = pin.split("==")
        assert version == lock[re.sub(r"[-_.]+", "-", name).lower()], pin


def test_locked_requirements_refuse_a_package_without_a_pin():
    with pytest.raises(RuntimeError, match="refusing an unpinned install"):
        package_mod._locked_requirements(["a-package-that-is-not-in-the-lock"])


@pytest.fixture
def captured_dockerfiles(monkeypatch):
    """Run the Linux Docker builds with Docker faked; keep the Dockerfile each build writes."""
    dockerfiles = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "buildx"]:
            dockerfiles.append((Path(kwargs["cwd"]) / "Dockerfile").read_text())
            return SimpleNamespace(returncode=1, stdout="", stderr="stop after writing the Dockerfile")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(package_mod.subprocess, "run", MagicMock(side_effect=fake_run))
    return dockerfiles


@pytest.mark.parametrize("builder", ["_build_linux_via_docker", "_build_linux_otel_helper_via_docker"])
def test_linux_docker_builds_pin_every_install(builder, captured_dockerfiles, tmp_path):
    cmd = PackageCommand()
    cmd.option = lambda name: False
    with pytest.raises(RuntimeError, match="Docker build failed"):
        getattr(cmd, builder)(tmp_path, "x64")
    assert len(captured_dockerfiles) == 1
    installs = _pip_install_args(captured_dockerfiles[0])
    assert len(installs) == 2, installs  # pip itself, then the build and runtime packages
    for requirements in installs:
        assert requirements and _unpinned(requirements) == [], f"unpinned install in {builder}: {requirements}"


@pytest.mark.parametrize("logical_id", sorted(_collect_buildspecs()))
def test_codebuild_buildspecs_pin_every_install(logical_id):
    commands = [c for _, _, c in _all_commands(_collect_buildspecs()[logical_id])]
    installs = [args for c in commands for args in _pip_install_args(c)]
    assert installs, f"{logical_id} has no pip install to check"
    for requirements in installs:
        assert requirements and _unpinned(requirements) == [], f"unpinned install in {logical_id}: {requirements}"


def test_macos_cross_arch_venv_pins_every_install(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(package_mod.subprocess, "run", MagicMock(side_effect=fake_run))
    monkeypatch.setattr(package_mod.Path, "home", classmethod(lambda cls: tmp_path))
    package_mod._ensure_cross_arch_venv(
        "x86_64", tmp_path / "python3", package_mod._CREDENTIAL_PROVIDER_RUNTIME_DEPS, MagicMock()
    )
    installs = [argv[argv.index("install") + 1 :] for argv in calls if "install" in argv]
    assert len(installs) == 1, calls
    assert _unpinned(installs[0]) == [], f"unpinned install in the macOS build venv: {installs[0]}"
    assert any(r.startswith("pyinstaller==") for r in installs[0])


# Buildspec pins that deliberately differ from source/poetry.lock: name -> (pinned version, reason).
BUILDSPEC_LOCK_EXCEPTIONS = {
    "certifi": ("2026.10.23", "held at a release without the CA-bundle advisory that affects the lock's version"),
    "nuitka": ("2.7.12", "the Windows build's Nuitka; the lock's dev-group Nuitka is newer"),
    "pyinstaller": ("6.3.0", "the Linux builds' PyInstaller, kept from before the lock was used"),
}
# Chocolatey packages installed without --version: name -> reason.
CHOCO_UNPINNED = {
    "mingw": "the build documents no MinGW version to pin",
}


def _buildspec_pins() -> list[tuple[str, str, str]]:
    pins = []
    for logical_id, spec in sorted(_collect_buildspecs().items()):
        for _, _, command in _all_commands(spec):
            for requirements in _pip_install_args(command):
                for req in requirements:
                    if EXACT_PIN.fullmatch(req):
                        name, version = req.split("==")
                        pins.append((logical_id, name, version))
    return pins


def test_buildspec_pins_equal_the_lock_except_the_listed_ones():
    lock = _lock_versions()
    pins = _buildspec_pins()
    assert pins, "no buildspec pins found"
    mismatches = []
    for logical_id, name, version in pins:
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in BUILDSPEC_LOCK_EXCEPTIONS:
            expected = BUILDSPEC_LOCK_EXCEPTIONS[key][0]
        elif key in lock:
            expected = lock[key]
        else:
            continue  # not in the lock (pip, pywin32): the exact-pin test covers it
        if version != expected:
            mismatches.append(f"{logical_id}: {name}=={version}, expected {expected}")
    assert mismatches == [], "buildspec pins drifted from source/poetry.lock:\n" + "\n".join(mismatches)


def test_every_listed_lock_exception_is_still_in_use():
    used = {re.sub(r"[-_.]+", "-", name).lower() for _, name, _ in _buildspec_pins()}
    assert set(BUILDSPEC_LOCK_EXCEPTIONS) <= used, sorted(set(BUILDSPEC_LOCK_EXCEPTIONS) - used)


CHOCO_COMMAND = re.compile(r"\b(?:choco(?:\.exe)?\s+(?:install|upgrade)|cinst|cup)\s+([^;&|\n]*)")
CHOCO_FLAGS_WITH_VALUE = {
    "--version",
    "-s",
    "--source",
    "--params",
    "--package-parameters",
    "--ia",
    "--install-arguments",
}


def _choco_violations(command: str) -> list[str]:
    """Packages a choco install/upgrade (or cinst/cup) line installs without --version, unless listed."""
    violations = []
    for m in CHOCO_COMMAND.finditer(command):
        tokens, packages, version, i = m.group(1).split(), [], None, 0
        while i < len(tokens):
            flag, eq, value = tokens[i].partition("=")
            if flag.startswith("-"):
                if flag in CHOCO_FLAGS_WITH_VALUE and not eq:
                    i += 1
                    value = tokens[i] if i < len(tokens) else ""
                if flag == "--version":
                    version = value
            else:
                packages.append(tokens[i])
            i += 1
        if len(packages) > 1:
            violations.append(f"more than one package on one line ({', '.join(packages)}): pin each on its own line")
            continue
        violations += [p for p in packages if p not in CHOCO_UNPINNED and not re.fullmatch(r"\d[\w.-]*", version or "")]
    return violations


@pytest.mark.parametrize(
    ("command", "violations"),
    [
        ("choco install python312 --version 3.12.10 -y --no-progress", []),
        ("choco install -y --version=3.12.10 python312", []),
        ("choco install mingw -y --no-progress", []),
        ("choco install -y python312", ["python312"]),
        ("cinst python312", ["python312"]),
        ("choco upgrade python312 -y", ["python312"]),
        (
            "choco install mingw python312 -y",
            ["more than one package on one line (mingw, python312): pin each on its own line"],
        ),
    ],
)
def test_choco_check_reads_every_package_and_spelling(command, violations):
    assert _choco_violations(command) == violations


def test_chocolatey_installs_pin_a_version_unless_listed():
    unpinned = []
    for logical_id, spec in sorted(_collect_buildspecs().items()):
        for _, _, command in _all_commands(spec):
            unpinned += [f"{logical_id}: {v} in: {command}" for v in _choco_violations(command)]
    assert unpinned == [], "choco install without --version:\n" + "\n".join(unpinned)
