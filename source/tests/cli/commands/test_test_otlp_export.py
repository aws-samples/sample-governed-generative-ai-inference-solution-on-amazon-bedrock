# ABOUTME: `gip test` checks that the federated role can export OTLP metrics to CloudWatch,
# ABOUTME: the call the sidecar collector makes, and reports a 403 as an IAM policy problem.

"""Regression (end-to-end test, 2026-10-26).

The "Local Collector" check passed on the collector binary alone. It could not see that the
federated role's IAM policy rejected OTLP ingest (HTTP 403 on cloudwatch:PutMetricData,
the collector log), so `gip test` passed while no usage reached CloudWatch.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.credentials import Credentials

from governed_inference_platform.cli.commands import test as test_module
from governed_inference_platform.cli.commands.test import TestCommand as GIPTestCommand

# The collector's error from the end-to-end test, account and identity replaced.
DENIED = (
    '{"Message":"User: arn:aws:sts::111122223333:assumed-role/BedrockCognitoFederatedRole/user@example.com '
    "is not authorized to perform: cloudwatch:PutMetricData on resource: "
    "arn:aws:cloudwatch:us-east-1:111122223333:dataset/default because no identity-based policy allows "
    'the cloudwatch:PutMetricData action"}'
)


def _check(status_code: int, text: str = "", credentials=None, send_error: Exception | None = None, auth_type="oidc"):
    session = MagicMock()
    session.get_credentials.return_value = (
        Credentials("AKIDEXAMPLE", "secret-example", "session-token-example") if credentials is None else credentials
    )
    response = MagicMock(status_code=status_code, text=text)
    http = MagicMock()
    if send_error:
        http.send.side_effect = send_error
    else:
        http.send.return_value = response
    with (
        patch.object(test_module.boto3, "Session", return_value=session) as session_cls,
        patch("botocore.httpsession.URLLib3Session", return_value=http),
    ):
        result = GIPTestCommand()._test_otlp_metrics_export("gip-test-1a2b3c4d", "us-east-1", auth_type)
    return result, session_cls, http


def test_denied_otlp_export_fails_and_names_the_iam_policy():
    result, _, _ = _check(403, DENIED)
    assert result["status"] == "✗"
    assert "HTTP 403" in result["details"]
    assert "IAM policy" in result["details"]
    assert "cloudwatch:PutMetricData" in result["details"]


def test_denied_export_remedy_for_oidc_names_monitoring_mode():
    result, _, _ = _check(403, DENIED)
    assert "MonitoringMode=sidecar" in result["details"]
    assert "permission set" not in result["details"]


def test_denied_export_remedy_for_idc_names_the_permission_set():
    """IDC credentials come from the permission set, so redeploying the auth stack can't fix a 403."""
    result, _, _ = _check(403, DENIED, auth_type="idc")
    assert "permission set" in result["details"]
    assert "Redeploy the auth stack" not in result["details"]


def test_accepted_otlp_export_passes():
    result, _, _ = _check(200, "{}")
    assert result["status"] == "✓"


def test_export_is_an_empty_sigv4_signed_otlp_request_with_the_test_profile():
    _, session_cls, http = _check(200, "{}")

    session_cls.assert_called_once_with(profile_name="gip-test-1a2b3c4d")
    request = http.send.call_args.args[0]
    assert request.method == "POST"
    assert request.url == "https://monitoring.us-east-1.amazonaws.com/v1/metrics"
    assert request.body == b'{"resourceMetrics":[]}'
    assert request.headers["Content-Type"] == "application/json"
    authorization = request.headers["Authorization"]
    authorization = authorization.decode() if isinstance(authorization, bytes) else authorization
    # Credential=<key id>/<date>/<region>/<service>/aws4_request
    scope = re.match(r"AWS4-HMAC-SHA256 Credential=([^,]+),", authorization).group(1).split("/")
    assert (scope[0], scope[2:]) == ("AKIDEXAMPLE", ["us-east-1", "monitoring", "aws4_request"])
    assert request.headers["X-Amz-Security-Token"] in ("session-token-example", b"session-token-example")


def test_rejected_credentials_fail():
    result, _, _ = _check(401, "unauthorized")
    assert result["status"] == "✗" and "HTTP 401" in result["details"]


def test_other_status_or_network_error_is_a_warning():
    assert _check(500, "oops")[0]["status"] == "!"
    assert _check(0, send_error=OSError("connection reset"))[0]["status"] == "!"


def test_missing_credentials_fail():
    session = MagicMock()
    session.get_credentials.return_value = None
    with patch.object(test_module.boto3, "Session", return_value=session):
        result = GIPTestCommand()._test_otlp_metrics_export("gip-test-1a2b3c4d", "us-east-1")
    assert result["status"] == "✗"


@pytest.mark.parametrize("snippet", ["self._test_local_collector(package_dir)", "self._test_otlp_metrics_export("])
def test_checks_run_only_in_the_sidecar_branch(snippet):
    source = Path(test_module.__file__).read_text(encoding="utf-8")
    branch_start = source.index('if profile.monitoring_enabled and monitoring_mode == "sidecar":')
    branch_end = source.index("# Display results", branch_start)
    assert source.count(snippet) == 1
    assert branch_start < source.index(snippet) < branch_end
