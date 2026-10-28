# ABOUTME: `gip test` must remove the temporary [profile gip-test-*] section it adds to the AWS
# ABOUTME: config, on success, failure and Ctrl-C, and leave every other byte of the file as it was.

"""Regression (end-to-end test, 2026-10-26).

`gip test` adds ``[profile gip-test-<hex>]`` with ``aws configure set`` so the AWS CLI runs the
packaged credential-process. It never removed the section (the success path only blanked
``credential_process``), so every run left one behind in the admin's ``~/.aws/config`` and
``gip cleanup``, which only removes manifest-owned state, could not remove it.
"""

import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from governed_inference_platform.cli.commands import test as test_module
from governed_inference_platform.cli.commands.test import TestCommand as GIPTestCommand
from governed_inference_platform.cli.commands.test import _restore_aws_config, _without_profile_section

ORIGINAL = (
    b"[default]\r\nregion = eu-west-1\r\n\r\n"
    b"# admin notes stay\r\n[profile admin]\r\nregion = us-east-1\r\n"
    b"[sso-session corp]\r\nsso_region = us-east-1"  # no trailing newline
)


def _fake_aws_configure_set(config_path: Path):
    """Mimic awscli's ConfigFileWriter for `aws configure set profile.<name>.<key> <value>`.

    A new section is appended, with a newline first when the file doesn't end in one. Updating
    an existing section rewrites the whole file in text mode, which turns CRLF into LF on POSIX.
    """

    def run(argv, **kwargs):
        assert argv[:3] == ["aws", "configure", "set"], argv
        _, profile_name, key = argv[3].split(".", 2)
        value = argv[4]
        header = f"[profile {profile_name}]"
        if not config_path.exists():
            config_path.write_bytes(b"")
        text = config_path.read_bytes().decode()
        lines = text.replace("\r\n", "\n").splitlines(keepends=True)
        if any(line.strip() == header for line in lines):
            index = next(i for i, line in enumerate(lines) if line.strip() == header)
            lines.insert(index + 1, f"{key} = {value}\n")
            config_path.write_bytes("".join(lines).encode())
        else:
            addition = ("\n" if text and not text.endswith("\n") else "") + f"{header}\n{key} = {value}\n"
            config_path.write_bytes(text.encode() + addition.encode())
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    return run


@pytest.fixture
def aws_config(tmp_path, monkeypatch):
    path = tmp_path / "aws" / "config"
    path.parent.mkdir()
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setattr(test_module.subprocess, "run", _fake_aws_configure_set(path))
    return path


def _command_whose_run_does(action):
    command = GIPTestCommand()

    def run_tests():
        command._create_temporary_profile("gip-test-1a2b3c4d", '"/pkg/credential-process" --profile pilot', "us-east-1")
        assert b"[profile gip-test-1a2b3c4d]" in test_module._aws_config_path().read_bytes()
        return action()

    command._run_tests = run_tests
    return command


def _raise_interrupt():
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("outcome", "action"),
    [("success", lambda: 0), ("failure", lambda: 1), ("ctrl-c", _raise_interrupt)],
)
def test_temporary_profile_removed_and_config_bytes_restored(aws_config, outcome, action):
    aws_config.write_bytes(ORIGINAL)
    command = _command_whose_run_does(action)

    if outcome == "ctrl-c":
        with pytest.raises(KeyboardInterrupt):
            command.handle()
    else:
        assert command.handle() == action()

    assert aws_config.read_bytes() == ORIGINAL


def test_config_file_created_by_the_test_is_removed(aws_config):
    command = _command_whose_run_does(lambda: 1)

    command.handle()

    assert not aws_config.exists()


def test_concurrent_change_elsewhere_in_the_config_is_kept(aws_config):
    aws_config.write_bytes(b"[profile admin]\nregion = us-east-1\n")

    def edit_meanwhile():
        with aws_config.open("ab") as handle:
            handle.write(b"[profile added-meanwhile]\nregion = eu-central-1\n")
        return 0

    _command_whose_run_does(edit_meanwhile).handle()

    assert aws_config.read_bytes() == (
        b"[profile admin]\nregion = us-east-1\n[profile added-meanwhile]\nregion = eu-central-1\n"
    )


def _symlinked_config(tmp_path: Path) -> tuple[Path, Path]:
    """An AWS config that is a symlink into a dotfiles directory, as some admins keep it."""
    target = tmp_path / "dotfiles" / "aws-config"
    target.parent.mkdir()
    target.write_bytes(ORIGINAL)
    link = tmp_path / "aws" / "config"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as error:  # Windows without symlink rights
        pytest.skip(f"cannot create a symlink here: {error}")
    return link, target


def test_symlinked_config_keeps_the_link_and_restores_its_target(tmp_path, monkeypatch):
    """Regression (review, 2026-10-27): `aws configure set` writes through a symlinked config, and
    the restore replaced the link itself with a regular file, leaving the section in the target."""
    link, target = _symlinked_config(tmp_path)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(link))
    monkeypatch.setattr(test_module.subprocess, "run", _fake_aws_configure_set(link))

    _command_whose_run_does(lambda: 0).handle()

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert link.resolve() == target.resolve()
    assert target.read_bytes() == ORIGINAL


def test_without_profile_section_removes_only_that_section():
    content = (
        b"[profile keep-a]\nregion = a\n"
        b"[profile gip-test-1a2b3c4d]\ncredential_process = x\nregion = y\n"
        b"  [profile keep-b]  \nregion = b\n"
        b"[profile gip-test-1a2b3c4d-other]\nregion = c\n"
    )
    assert _without_profile_section(content, "gip-test-1a2b3c4d") == (
        b"[profile keep-a]\nregion = a\n  [profile keep-b]  \nregion = b\n[profile gip-test-1a2b3c4d-other]\nregion = c\n"
    )


def test_restore_is_a_no_op_when_the_section_is_gone(tmp_path):
    path = tmp_path / "config"
    path.write_bytes(b"[profile admin]\r\nregion = us-east-1")
    _restore_aws_config(path, "gip-test-1a2b3c4d", b"something else")
    assert path.read_bytes() == b"[profile admin]\r\nregion = us-east-1"


def test_both_call_sites_create_the_profile_through_the_tracked_helper():
    source = Path(test_module.__file__).read_text(encoding="utf-8")
    helper = source[source.index("def _create_temporary_profile") : source.index("def _remove_temporary_profiles")]
    assert source.count('"aws", "configure", "set"') == helper.count('"aws", "configure", "set"') == 2
    assert len(re.findall(r"self\._create_temporary_profile\(\s*test_profile,", source)) == 2


@pytest.mark.skipif(shutil.which("aws") is None, reason="needs the AWS CLI")
def test_real_aws_cli_writes_are_undone_byte_for_byte(tmp_path, monkeypatch):
    """Same check against what the installed AWS CLI actually writes (line endings included)."""
    path = tmp_path / "config"
    path.write_bytes(ORIGINAL)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    command = GIPTestCommand()

    def run_tests():
        assert command._create_temporary_profile("gip-test-1a2b3c4d", "/bin/false", "us-east-1")
        assert b"[profile gip-test-1a2b3c4d]" in path.read_bytes()
        return 1

    command._run_tests = run_tests
    env_before = dict(os.environ)
    command.handle()

    assert dict(os.environ) == env_before
    assert path.read_bytes() == ORIGINAL


@pytest.mark.skipif(shutil.which("aws") is None, reason="needs the AWS CLI")
def test_real_aws_cli_writes_through_a_symlinked_config_are_undone(tmp_path, monkeypatch):
    link, target = _symlinked_config(tmp_path)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(link))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    command = GIPTestCommand()

    def run_tests():
        assert command._create_temporary_profile("gip-test-1a2b3c4d", "/bin/false", "us-east-1")
        assert b"[profile gip-test-1a2b3c4d]" in target.read_bytes()
        return 1

    command._run_tests = run_tests
    command.handle()

    assert link.is_symlink()
    assert target.read_bytes() == ORIGINAL
