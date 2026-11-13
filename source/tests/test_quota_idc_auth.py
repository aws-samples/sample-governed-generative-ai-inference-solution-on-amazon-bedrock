# ABOUTME: Tests for dual-auth quota check (JWT + IAM Identity Center)
# ABOUTME: Verifies identity resolution from both OIDC JWT claims and IAM caller ARN

"""Tests for quota_check Lambda dual identity resolution.

These tests verify the identity extraction logic ONLY — they don't test
the full quota calculation flow (that's tested in test_quota_check_lambda.py).
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add the lambda function to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "deployment", "infrastructure", "lambda-functions", "quota_check"
    ),
)

# Lambda initializes boto3 clients at module level; set region to avoid NoRegionError
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import index


class TestIdentityResolution:
    """Test that the Lambda correctly extracts user identity from JWT or IAM ARN."""

    def _make_event(self, jwt_claims=None, caller_arn=None, iam_user_arn=None):
        """Build a mock API Gateway event.

        caller_arn   → legacy payload format 1.0 shape (requestContext.identity)
        iam_user_arn → HTTP API payload format 2.0 shape
                       (requestContext.authorizer.iam.userArn)
        """
        event = {"requestContext": {}}
        if jwt_claims:
            event["requestContext"]["authorizer"] = {"jwt": {"claims": jwt_claims}}
        if iam_user_arn:
            authorizer = event["requestContext"].setdefault("authorizer", {})
            authorizer["iam"] = {
                "userArn": iam_user_arn,
                "callerId": "AROAEXAMPLEID:session",
                "accountId": "123456789012",
            }
        if caller_arn:
            event["requestContext"]["identity"] = {"caller": caller_arn, "userArn": caller_arn}
        return event

    def _unlimited_policy(self):
        return {
            "monthly_limit": 225000000,
            "daily_limit": 0,
            "enforcement_mode": "alert",
            "enabled": True,
            "warning_threshold_80": 180000000,
            "warning_threshold_90": 202500000,
        }

    def test_idc_user_with_v2_iam_authorizer_context(self):
        """IDC user on an HTTP API v2 route: identity comes from authorizer.iam.userArn.

        Regression test for the quota route being deployed with
        PayloadFormatVersion '2.0' and AuthorizationType AWS_IAM
        (quota-monitoring.yaml). A v2 IAM event carries no
        requestContext.identity, so reading only the legacy location resolved
        no identity and the request failed closed with missing_identity.
        """
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/dave@company.com"
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [self._unlimited_policy()]

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("dave@company.com", [])
            assert body["allowed"] is True
            assert body.get("reason") != "missing_identity"

    def test_v2_iam_username_without_email(self):
        """IDC username without @ on a v2 event still resolves via AWSReservedSSO."""
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/erin.claude"
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [self._unlimited_policy()]

            result = index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("erin.claude", [])
            assert json.loads(result["body"])["allowed"] is True

    def test_v2_iam_context_preferred_over_legacy_identity(self):
        """When both shapes are present, only the v2 authorizer.iam ARN is read."""
        event = self._make_event(
            caller_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/legacy@company.com",
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/v2user@company.com",
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [self._unlimited_policy()]

            index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("v2user@company.com", [])

    def test_jwt_preferred_over_v2_iam_context(self):
        """JWT claims still take priority over an IAM Identity Center ARN that names a user."""
        event = self._make_event(
            jwt_claims={"email": "jwt-user@company.com", "sub": "abc"},
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/iam-user@company.com",
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [self._unlimited_policy()]

            index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("jwt-user@company.com", [])

    def test_oidc_user_with_email_claim(self):
        """OIDC user: identity resolved from JWT email claim."""
        event = self._make_event(jwt_claims={"email": "alice@company.com", "sub": "abc123"})

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [
                {
                    "monthly_limit": 225000000,
                    "daily_limit": 0,
                    "enforcement_mode": "alert",
                    "enabled": True,
                    "warning_threshold_80": 180000000,
                    "warning_threshold_90": 202500000,
                }
            ]

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("alice@company.com", [])
            assert body["allowed"] is True

    def test_idc_user_with_email_in_arn(self):
        """IDC user: identity resolved from assumed-role ARN session name."""
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/bob@company.com"
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [
                {
                    "monthly_limit": 225000000,
                    "daily_limit": 0,
                    "enforcement_mode": "alert",
                    "enabled": True,
                    "warning_threshold_80": 180000000,
                    "warning_threshold_90": 202500000,
                }
            ]

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("bob@company.com", [])
            assert body["allowed"] is True

    def test_idc_user_arn_without_email(self):
        """IDC user with ARN that has no email in session name — resolves as username identity.

        Per #597, usernames without @ are now valid identity (not rejected as missing).
        The Lambda resolves the session name and proceeds to quota check.
        """
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/session123"
        )

        # Successful-but-empty policy lookups = "no policy". (The old get_policy
        # swallowed the real boto3 call this test used to leak; infrastructure
        # errors now propagate, so the lookup must be mocked.)
        policies_table = MagicMock()
        policies_table.get_item.return_value = {}
        with patch.object(index, "policies_table", policies_table):
            result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        # Identity is resolved (session123), but no quota policy exists for this user
        assert body["allowed"] is True
        assert body.get("reason") in ("no_policy", None) or "identity" not in body.get("reason", "")

    def test_no_auth_at_all(self):
        """No JWT claims and no IAM identity — missing identity."""
        event = self._make_event()

        result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        assert body["reason"] == "missing_identity"

    def test_jwt_without_email_falls_through_to_arn(self):
        """JWT present but missing email claim — falls through to IAM ARN."""
        event = self._make_event(
            jwt_claims={"sub": "abc123"},  # no email
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/carol@company.com",
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [
                {
                    "monthly_limit": 225000000,
                    "daily_limit": 0,
                    "enforcement_mode": "alert",
                    "enabled": True,
                    "warning_threshold_80": 180000000,
                    "warning_threshold_90": 202500000,
                }
            ]

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("carol@company.com", [])
            assert body["allowed"] is True

    def test_malformed_arn_no_slash(self):
        """Malformed ARN without slashes — should not crash."""
        event = self._make_event(iam_user_arn="not-a-valid-arn")

        result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        # Not an STS assumed-role session ARN, so it names nobody: missing identity
        assert body["reason"] == "missing_identity"

    def test_jwt_preferred_over_arn_when_both_present(self):
        """When both a JWT email and an Identity Center username are available, JWT takes priority."""
        event = self._make_event(
            jwt_claims={"email": "jwt-user@company.com", "sub": "abc"},
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/arn.user",
        )

        with (
            patch.object(index, "resolve_policies_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = [
                {
                    "monthly_limit": 225000000,
                    "daily_limit": 0,
                    "enforcement_mode": "alert",
                    "enabled": True,
                    "warning_threshold_80": 180000000,
                    "warning_threshold_90": 202500000,
                }
            ]

            result = index.lambda_handler(event, None)
            json.loads(result["body"])

            # JWT email takes priority
            mock_resolve.assert_called_once_with("jwt-user@company.com", [])


# Strings that contain an IAM Identity Center session ARN, or nearly are one, without being
# one in full. An IAM user's path may hold ':' and '/', so a user ARN can embed the text.
NOT_A_SESSION_ARN = {
    "iam-user-path": "arn:aws:iam::123456789012:user/:assumed-role/AWSReservedSSO_x/victim@example.com",
    "trailing-newline": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Dev_0123/victim@example.com\n",
    "extra-segment": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Dev_0123/extra/victim@example.com",
    "text-before": "x arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Dev_0123/victim@example.com",
    "region-set": "arn:aws:sts:us-east-1:123456789012:assumed-role/AWSReservedSSO_Dev_0123/victim@example.com",
    "iam-service": "arn:aws:iam::123456789012:assumed-role/AWSReservedSSO_Dev_0123/victim@example.com",
    "short-account": "arn:aws:sts::12345678901:assumed-role/AWSReservedSSO_Dev_0123/victim@example.com",
    "non-ascii-session": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Dev_0123/vict\u00efm@example.com",
}

PARTITIONS = ["aws", "aws-cn", "aws-us-gov"]


class TestIdentityCenterArnParse:
    """idc_username_from_arn names a user only for a string that is, in full, a session ARN."""

    @pytest.mark.parametrize("partition", PARTITIONS)
    def test_session_in_each_partition_names_the_user(self, partition):
        arn = f"arn:{partition}:sts::123456789012:assumed-role/AWSReservedSSO_Dev_0123456789abcdef/alice"
        assert index.idc_username_from_arn(arn) == "alice"

    @pytest.mark.parametrize("arn", list(NOT_A_SESSION_ARN.values()), ids=list(NOT_A_SESSION_ARN))
    def test_anything_else_names_nobody(self, arn):
        assert index.idc_username_from_arn(arn) is None


class TestSigV4CallerIdentity:
    """The AWS_IAM route names a user only for an IAM Identity Center session.

    API Gateway puts the signed caller at requestContext.authorizer.iam.userArn (HTTP API
    payload 2.0). Only an STS assumed-role session ARN, matched in full, whose role name
    starts with AWSReservedSSO_ names a user, by its session name, which IAM Identity
    Center sets. Any other caller chooses its own session name (AssumeRole
    RoleSessionName) or names itself (IAM user), so it resolves no identity.
    """

    UNBLOCK = {
        "pk": "USER#victim@example.com",
        "sk": "UNBLOCK#CURRENT",
        "expires_at": "2099-01-01T00:00:00Z",
        "unblocked_by": "admin@example.com",
        "unblocked_at": "2026-11-01T00:00:00Z",
        "reason": "medical leave, approved by HR",
        "duration_type": "custom",
    }

    def _check(self, event, block_missing=True):
        policy = {"policy_type": "user", "identifier": "x", "monthly_token_limit": 1000, "enforcement_mode": "block"}
        quota_table = MagicMock()
        quota_table.get_item.side_effect = lambda Key: (
            {"Item": dict(self.UNBLOCK)} if Key["sk"] == "UNBLOCK#CURRENT" else {}
        )
        with (
            patch.object(index, "MISSING_EMAIL_ENFORCEMENT", "block" if block_missing else "warn"),
            patch.object(index, "resolve_policies_for_user", return_value=[policy]) as resolve,
            patch.object(index, "quota_table", quota_table),
            patch.object(index, "get_user_usage_summary", return_value={}),
        ):
            body = json.loads(index.lambda_handler(event, None)["body"])
        return body, resolve

    @staticmethod
    def _iam(arn):
        return {"requestContext": {"authorizer": {"iam": {"userArn": arn, "accountId": "123456789012"}}}}

    def test_identity_center_session_names_the_user(self):
        body, resolve = self._check(
            self._iam("arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123/me@example.com")
        )
        resolve.assert_called_once_with("me@example.com", [])
        assert body["reason"] != "missing_identity"

    @pytest.mark.parametrize("partition", PARTITIONS)
    def test_identity_center_session_in_each_partition(self, partition):
        body, resolve = self._check(
            self._iam(f"arn:{partition}:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123/me@example.com")
        )
        resolve.assert_called_once_with("me@example.com", [])
        assert body["reason"] != "missing_identity"

    @pytest.mark.parametrize("arn", list(NOT_A_SESSION_ARN.values()), ids=list(NOT_A_SESSION_ARN))
    def test_string_that_is_not_a_session_arn_resolves_no_identity(self, arn):
        body, resolve = self._check(self._iam(arn))
        resolve.assert_not_called()
        assert body["reason"] == "missing_identity"
        assert body["allowed"] is False

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:sts::123456789012:assumed-role/PowerUserRole/victim@example.com",
            "arn:aws:iam::123456789012:user/victim@example.com",
            "arn:aws:sts::123456789012:assumed-role/SomeRole/AWSReservedSSO-victim",
            "arn:aws:sts::123456789012:assumed-role/MyAWSReservedSSO_BedrockDev_0123/victim@example.com",
            "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123",
        ],
        ids=["chosen-session-name", "iam-user", "reserved-in-session-name", "look-alike-role", "no-session-name"],
    )
    def test_other_callers_resolve_no_identity(self, arn):
        body, resolve = self._check(self._iam(arn))
        resolve.assert_not_called()
        assert body["reason"] == "missing_identity"
        assert body["allowed"] is False

    def test_legacy_identity_location_is_not_read(self):
        """Payload format 2.0 never fills requestContext.identity, so a value there names nobody."""
        event = {
            "requestContext": {
                "identity": {
                    "caller": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123/victim@example.com",
                    "userArn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123/victim@example.com",
                }
            }
        }
        body, resolve = self._check(event)
        resolve.assert_not_called()
        assert body["reason"] == "missing_identity"

    def test_unblock_response_omits_who_unblocked_and_why(self):
        body, _ = self._check(
            self._iam("arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDev_0123/victim@example.com")
        )
        assert body["reason"] == "unblocked"
        assert body["unblock_status"]["is_unblocked"] is True
        assert body["unblock_status"]["expires_at"] == "2099-01-01T00:00:00Z"
        assert "unblocked_by" not in body["unblock_status"]
        assert "reason" not in body["unblock_status"]
