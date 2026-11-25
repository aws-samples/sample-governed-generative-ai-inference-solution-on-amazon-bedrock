## Description

<!-- Provide a clear and concise description of what this PR does -->

## Why is this change needed

<!-- Why is this change needed? What problem does it solve? -->
<!-- If it fixes an open issue, please link to the issue here -->

Fixes # (issue)

## Type of Change

<!-- Check all that apply -->

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Infrastructure/deployment change
- [ ] Dependency update
- [ ] CI/CD changes

## Changes Made

<!-- List the key changes in this PR -->

-
-
-

## Additional Notes

<!-- Optional: Any additional context, breaking changes, or areas requiring special review attention -->

---

## Testing Checklist

<!--
IMPORTANT: PRs affecting code must include testing evidence.
Tick what you tested below and paste the output under "Test Results"; the
"Validate Testing Evidence" check (.github/workflows/pr-testing-validation.yml) reads both.

The check skips (steps 1 to 3 of that workflow):
- Dependabot PRs, and PRs with the 'skip-testing-validation' or 'dependencies' label
- PRs that change pr-testing-validation.yml or this template
- Documentation-only PRs: tick only "Documentation update" under Type of Change
-->

### What was tested

<!-- Check what you tested and provide evidence below -->

- [ ] All existing tests pass locally (unit/integration, `gip test`)
- [ ] **`gip` commands tested** (`gip init/deploy/build/package/test`)
- [ ] CloudFormation templates deployed successfully
- [ ] **Binary Installation** tested (`install.sh` / `install.bat`)
- [ ] **End-to-End** authentication flow tested

### Special cases

- [ ] This PR only changes `pr-testing-validation.yml` or this template (the check skips itself)
- [ ] This is a documentation-only change (tick only "Documentation update" above; the check skips)
- [ ] This PR needs the `skip-testing-validation` label (explain why below)

## Testing Evidence

<!-- 
Provide evidence that you've tested the changes:
- Paste screenshots or terminal output
- Link to successful GitHub Actions run
- Or, when testing does not apply, explain why and ask a maintainer for the
  `skip-testing-validation` label
-->

### Test Environment

- **AWS Region**: <!-- e.g. us-east-1, or none -->
- **Python Version**: <!-- e.g. 3.12 -->
- **Operating System**: <!-- e.g. macOS 14 -->

### Test Results

```text
[Paste terminal output, screenshots, or logs here]
```
