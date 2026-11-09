# ABOUTME: Regression tests that `gip deploy auth` always passes AllowOpenAIModels from the profile
# ABOUTME: Explicit pass-through (never a stale CloudFormation default) on every auth-stack code path

"""AllowOpenAIModels must be passed on every path that builds auth-stack parameters.

Mirrors the RestrictToAnthropicModels contract: deployments must not rely on
the template default, so a profile change always reaches the stack.
"""

import re
from pathlib import Path

DEPLOY_PY = Path(__file__).resolve().parents[3] / "governed_inference_platform" / "cli" / "commands" / "deploy.py"


def test_every_restrict_param_is_paired_with_allow_openai_param():
    src = DEPLOY_PY.read_text(encoding="utf-8")
    restrict = len(re.findall(r"RestrictToAnthropicModels=\{", src))
    allow = len(
        re.findall(r"AllowOpenAIModels=\{str\(getattr\(profile, 'allow_openai_models', False\)\)\.lower\(\)\}", src)
    )
    assert restrict == 4  # IDC deploy, OIDC deploy, IDC --show-commands, OIDC --show-commands
    assert allow == restrict
