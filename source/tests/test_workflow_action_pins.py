"""Every action a workflow uses is pinned to a full commit SHA, and every package named in a
workflow's `pip install` is pinned to an exact version."""

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*-?\s*uses:\s+(\S+)(.*)$")
PIP_INSTALL = re.compile(r"\bpip3? install\b([^&|;#\n]*)")
EXACT_VERSION = r"[0-9][A-Za-z0-9.+!-]*"
EXACT_PIN = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._\[\],-]*=={EXACT_VERSION}")
ENV_REF = re.compile(r"\$\{(\w+)\}|\$\{\{\s*env\.(\w+)\s*\}\}")


def _remote_uses(path: Path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = USES.match(line)
        if m and not m.group(1).startswith("./"):
            yield n, m.group(1), m.group(2)


def _env_versions(text: str) -> dict[str, str]:
    """`NAME: "1.2.3"` entries anywhere in the workflow (env blocks at any level)."""
    return dict(re.findall(rf"^\s*(\w+):\s*[\"']?({EXACT_VERSION})[\"']?\s*$", text, re.M))


def _unpinned_pip_installs(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    env = _env_versions(text)
    unpinned = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in PIP_INSTALL.finditer(line):
            args = [a.strip("\"'") for a in m.group(1).split()]
            requirements = [a for a in args if not a.startswith("-")]
            if not requirements or any(a in ("-r", "--requirement", "-e", "--editable") for a in args):
                unpinned.append(f"{path.name}:{n}: {line.strip()}")
                continue
            for req in requirements:
                resolved = ENV_REF.sub(lambda r: env.get(r.group(1) or r.group(2), "<unset>"), req)
                if not EXACT_PIN.fullmatch(resolved):
                    unpinned.append(f"{path.name}:{n}: {req}")
    return unpinned


def test_workflows_found():
    assert WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow):
    unpinned = [
        f"{workflow.name}:{n}: {ref}" for n, ref, _ in _remote_uses(workflow) if not re.search(r"@[0-9a-f]{40}$", ref)
    ]
    assert unpinned == [], (
        "actions pinned by tag or branch (pin the commit the tag points to, tag as comment):\n" + "\n".join(unpinned)
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_pip_install_pins_each_package(workflow):
    unpinned = _unpinned_pip_installs(workflow)
    assert unpinned == [], "pip install of a package without an exact version (name==x.y.z):\n" + "\n".join(unpinned)


def test_the_pip_check_reads_versions_from_env_and_flags_the_rest(tmp_path):
    wf = tmp_path / "wf.yml"
    wf.write_text(
        'env:\n  TOOL_VERSION: "1.2.3"\njobs:\n  a:\n    steps:\n'
        '      - run: pip install -q "tool==${TOOL_VERSION}" other==4.5\n'
        "      - run: pip install loose\n"
        '      - run: pip install "ghost==${UNSET_VERSION}"\n'
        "      - run: pip install -r requirements.txt\n"
    )
    assert _unpinned_pip_installs(wf) == [
        "wf.yml:7: loose",
        "wf.yml:8: ghost==${UNSET_VERSION}",
        "wf.yml:9: - run: pip install -r requirements.txt",
    ]
