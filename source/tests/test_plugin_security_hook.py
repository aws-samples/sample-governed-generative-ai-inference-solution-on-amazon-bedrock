import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.proc import run_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_HOOK = REPO_ROOT / "assets" / "claude-code-plugins" / "plugins" / "security" / "hooks" / "security_check.py"


def run_security_hook(input_text: str, project_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Keep the hook's audit log inside the test directory, never the real home.
    env["CLAUDE_SECURITY_AUDIT_LOG"] = str(project_root / ".claude" / "security-audit.log")
    return run_cmd(  # nosec B603
        [sys.executable, str(SECURITY_HOOK)],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def load_security_hook():
    spec = importlib.util.spec_from_file_location("security_check", SECURITY_HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_security_hook_blocks_invalid_json(tmp_path):
    result = run_security_hook("{", tmp_path)

    assert result.returncode == 2
    assert "Invalid JSON input" in result.stderr


def test_security_hook_keeps_audit_logging_best_effort(tmp_path):
    work_file = tmp_path / "safe.py"
    work_file.write_text("print('ok')\n")
    (tmp_path / ".claude").write_text("not a directory")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(work_file),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 0
    assert "Security hook audit warning" in result.stderr


def test_security_hook_still_blocks_secrets(tmp_path):
    work_file = tmp_path / "secret.py"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(work_file),
            "content": 'api_key="abcdefghijklmnopqrstuvwxyz"',
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Potential secrets detected" in result.stderr


def test_security_hook_blocks_windows_style_git_hook_paths(tmp_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / ".git\\hooks\\pre-commit"),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Suspicious file path" in result.stderr


def test_security_hook_blocks_nested_hidden_security_paths(tmp_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "nested" / ".git\\hooks\\pre-commit"),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Suspicious file path" in result.stderr


def test_security_hook_blocks_multiedit_secrets(tmp_path):
    work_file = tmp_path / "secret.py"
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(work_file),
            "edits": [
                {
                    "old_string": "",
                    "new_string": 'api_key="abcdefghijklmnopqrstuvwxyz"',
                }
            ],
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Potential secrets detected" in result.stderr


def test_security_hook_blocks_malformed_multiedit_payload(tmp_path):
    work_file = tmp_path / "safe.py"
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(work_file),
            "edits": [{"old_string": "print('old')"}],
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "MultiEdit edit 0 must include old_string and new_string" in result.stderr


def test_security_hook_permission_errors_fail_closed(monkeypatch):
    security_hook = load_security_hook()

    def fail_stat(_file_path):
        raise PermissionError("denied")

    monkeypatch.setattr(security_hook.os, "stat", fail_stat)

    with pytest.raises(RuntimeError, match="Unable to verify permissions"):
        security_hook.check_file_permissions("safe.py")


COMMAND_HOOK = SECURITY_HOOK.with_name("command_security_check.sh")
needs_bash_and_jq = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("jq"),
    reason="the command hook is a bash script that needs jq",
)


def _hook_env(home: Path, project: Path) -> dict[str, str]:
    """An environment whose home is a test directory, with no audit-log override."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_SECURITY_AUDIT_LOG"}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_PROJECT_DIR": str(project)})
    return env


def test_security_hook_audit_log_defaults_to_home_not_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    work_file = project / "safe.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(work_file), "content": "print('ok')\n"}}
    result = run_cmd(  # nosec B603
        [sys.executable, str(SECURITY_HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=_hook_env(home, project),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    log_file = home / ".claude" / "security-audit.log"
    assert "SECURITY_CHECK Write" in log_file.read_text()
    assert not (project / ".claude").exists(), "the audit log must not be written into the project tree"
    if sys.platform != "win32":
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def _run_command_hook(command: str, home: Path, project: Path, extra_env: dict[str, str] | None = None):
    env = _hook_env(home, project)
    env.update(extra_env or {})
    return run_cmd(  # nosec B603
        ["bash", str(COMMAND_HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        env=env,
        cwd=project,
        check=False,
    )


@needs_bash_and_jq
def test_command_hook_logs_flagged_commands_outside_the_project(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    flagged = 'eval "export API_TOKEN=test-token-123"'

    result = _run_command_hook(flagged, home, project)

    assert result.returncode == 0, result.stderr  # eval is warned about, not blocked
    log_file = home / ".claude" / "security-audit.log"
    assert "EVAL_USAGE" in log_file.read_text()
    assert not (project / ".claude").exists(), "flagged commands must not be written into the project tree"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


@needs_bash_and_jq
def test_command_hook_honours_the_audit_log_override(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    override = tmp_path / "audit" / "commands.log"

    result = _run_command_hook("echo hi && eval echo hi", home, project, {"CLAUDE_SECURITY_AUDIT_LOG": str(override)})

    assert result.returncode == 0, result.stderr
    assert "EVAL_USAGE" in override.read_text()
    assert not (home / ".claude").exists()
    assert not (project / ".claude").exists()


DANGEROUS = "rm -rf / --no-preserve-root"  # only a string: the hook greps it and never runs it
needs_non_root = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="a read-only directory does not stop root"
)


def _read_only_dir(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o500)
    return path


@needs_bash_and_jq
@needs_non_root
def test_command_hook_still_blocks_when_home_is_read_only(tmp_path):
    home, project = _read_only_dir(tmp_path / "home"), tmp_path / "project"
    project.mkdir()
    try:
        result = _run_command_hook(DANGEROUS, home, project)
    finally:
        home.chmod(0o700)

    assert result.returncode == 2, result.stderr
    assert "Security hook audit warning" in result.stderr
    assert not (home / ".claude").exists()


@needs_bash_and_jq
def test_command_hook_still_blocks_when_home_is_unset(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env = {"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)}  # no HOME, no log override
    result = run_cmd(  # nosec B603
        ["bash", str(COMMAND_HOOK)],
        input=json.dumps({"tool_input": {"command": DANGEROUS}}),
        text=True,
        capture_output=True,
        env=env,
        cwd=project,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    assert "Security hook audit warning" in result.stderr
    assert not (project / ".claude").exists()


@needs_bash_and_jq
@needs_non_root
def test_command_hook_still_blocks_when_the_log_override_is_unwritable(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    locked = _read_only_dir(tmp_path / "locked")
    try:
        result = _run_command_hook(
            DANGEROUS, home, project, {"CLAUDE_SECURITY_AUDIT_LOG": str(locked / "sub" / "audit.log")}
        )
    finally:
        locked.chmod(0o700)

    assert result.returncode == 2, result.stderr
    assert "Security hook audit warning" in result.stderr
    assert not (home / ".claude").exists()


@needs_bash_and_jq
def test_command_hook_still_blocks_when_stderr_is_closed(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    result = run_cmd(  # nosec B603
        ["bash", "-c", 'bash "$0" 2>&-', str(COMMAND_HOOK)],
        input=json.dumps({"tool_input": {"command": DANGEROUS}}),
        text=True,
        capture_output=True,
        env=_hook_env(home, project),
        cwd=project,
        check=False,
    )

    assert result.returncode == 2, result.stdout
    assert "DANGEROUS_COMMAND" in (home / ".claude" / "security-audit.log").read_text()
