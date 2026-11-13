# ABOUTME: Tests which group policy quota_check, quota_monitor and the CLI report for a user in several groups
# ABOUTME: Pins one lowest-limit order in all three, and that gip quota show lists every group policy checked

"""Group policies: the policy that is reported, and that every one is enforced.

quota_check denies when any one of a user's enabled group policies would deny on its
own (handler tests in test_group_policy_enforcement.py). When none denies, the
response names the group policy with the lowest limit; quota_monitor and the CLI's
QuotaPolicyManager.resolve_quota_for_user use the same order, so the three report
the same policy. `gip quota show` also lists every group policy that is checked.
"""

import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.commands.quota import QuotaShowCommand
from governed_inference_platform.models import EnforcementMode, PolicyType, QuotaMode, QuotaPolicy
from governed_inference_platform.quota_policies import QuotaPolicyManager

LAMBDAS = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions"


def _load(name: str, env: dict) -> object:
    """Load a Lambda module with ``env`` set while it imports, then restore os.environ.

    The modules read their configuration at import, so the values only need to be
    present during exec_module; restoring keeps them out of later test modules.
    """
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        module_name = f"{name}_precedence_{id(env)}"
        spec = importlib.util.spec_from_file_location(module_name, LAMBDAS / name / "index.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


ENV = {
    "QUOTA_TABLE": "TestQuotaTable",
    "POLICIES_TABLE": "TestPoliciesTable",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
    "MISSING_EMAIL_ENFORCEMENT": "block",
    "ERROR_HANDLING_MODE": "fail_closed",
    "ENABLE_FINEGRAINED_QUOTAS": "true",
    "MONTHLY_TOKEN_LIMIT": "0",
    "MONTHLY_COST_LIMIT_USD": "0",
    "DAILY_COST_LIMIT_USD": "0",
}


@pytest.fixture(scope="module")
def quota_check():
    return _load("quota_check", dict(ENV))


@pytest.fixture(scope="module")
def quota_monitor():
    return _load("quota_monitor", dict(ENV))


def _item(group, *, tokens=0, cost=0, daily_cost=0, monthly="alert", daily="alert", mode="token"):
    """A policies-table item as `gip quota set-group` writes it."""
    item = {
        "pk": f"POLICY#group#{group}",
        "sk": "CURRENT",
        "policy_type": "group",
        "identifier": group,
        "quota_mode": mode,
        "monthly_token_limit": tokens,
        "enforcement_mode": monthly,
        "daily_enforcement_mode": daily,
        "enabled": True,
    }
    if cost:
        item["monthly_cost_limit"] = cost
    if daily_cost:
        item["daily_cost_limit"] = daily_cost
    return item


BLOCK_HIGH = _item("block-500m", tokens=500_000_000, monthly="block")
ALERT_LOW = _item("alert-100m", tokens=100_000_000, monthly="alert")


def _check_resolve(mod, items):
    by_pk = {i["pk"]: i for i in items}
    mod.policies_table = MagicMock()
    mod.policies_table.get_item.side_effect = lambda Key: {"Item": by_pk[Key["pk"]]} if Key["pk"] in by_pk else {}
    return mod.resolve_quota_for_user("user@example.com", [i["identifier"] for i in items])


def _monitor_resolve(mod, items):
    cache = {f"group:{i['identifier']}": mod._policy_from_item(i) for i in items}
    return mod.resolve_user_quota("user@example.com", [i["identifier"] for i in items], cache)


def _to_policy(i):
    return QuotaPolicy(
        policy_type=PolicyType.GROUP,
        identifier=i["identifier"],
        monthly_token_limit=i["monthly_token_limit"],
        quota_mode=QuotaMode(i["quota_mode"]),
        monthly_cost_limit=i.get("monthly_cost_limit"),
        daily_cost_limit=i.get("daily_cost_limit"),
        enforcement_mode=EnforcementMode(i["enforcement_mode"]),
        daily_enforcement_mode=EnforcementMode(i["daily_enforcement_mode"]),
    )


def _cli_manager(items):
    by_id = {i["identifier"]: _to_policy(i) for i in items}
    with patch("governed_inference_platform.quota_policies.boto3"):
        manager = QuotaPolicyManager("test-table", region="us-east-1")
    manager.get_policy = lambda policy_type, ident: by_id.get(ident) if policy_type == PolicyType.GROUP else None
    return manager


def _cli_resolve(items):
    return _cli_manager(items).resolve_quota_for_user("user@example.com", [i["identifier"] for i in items])


class TestReportedPolicy:
    @pytest.mark.parametrize("order", [[BLOCK_HIGH, ALERT_LOW], [ALERT_LOW, BLOCK_HIGH]])
    def test_quota_check_reports_the_lowest_limit_policy(self, quota_check, order):
        assert _check_resolve(quota_check, order)["identifier"] == "alert-100m"

    @pytest.mark.parametrize("order", [[BLOCK_HIGH, ALERT_LOW], [ALERT_LOW, BLOCK_HIGH]])
    def test_quota_monitor_reports_the_lowest_limit_policy(self, quota_monitor, order):
        assert _monitor_resolve(quota_monitor, order)["identifier"] == "alert-100m"

    @pytest.mark.parametrize("order", [[BLOCK_HIGH, ALERT_LOW], [ALERT_LOW, BLOCK_HIGH]])
    def test_cli_reports_the_lowest_limit_policy(self, order):
        assert _cli_resolve(order).identifier == "alert-100m"

    def test_lowest_limit_wins_between_block_policies(self, quota_check):
        low = _item("block-100m", tokens=100_000_000, monthly="block")
        assert _check_resolve(quota_check, [BLOCK_HIGH, low])["identifier"] == "block-100m"

    def test_cost_limit_orders_cost_mode_policies(self, quota_check):
        cheap = _item("cost-5", cost=5, mode="cost", tokens=900_000_000)
        dear = _item("cost-50", cost=50, mode="cost", tokens=1)
        assert _check_resolve(quota_check, [dear, cheap])["identifier"] == "cost-5"

    def test_user_over_the_block_limit_is_refused(self, quota_check):
        """End to end: 600M tokens used, groups block at 500M and alert at 100M."""
        mod = quota_check
        by_pk = {i["pk"]: i for i in (BLOCK_HIGH, ALERT_LOW)}
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.side_effect = lambda Key: {"Item": by_pk[Key["pk"]]} if Key["pk"] in by_pk else {}
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [
            {},  # no unblock override
            {"Item": {"total_tokens": 600_000_000, "daily_tokens": 0, "input_tokens": 0, "output_tokens": 0}},
        ]
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {"email": "user@example.com", "groups": ["alert-100m", "block-500m"]}}}
            }
        }
        body = json.loads(mod.lambda_handler(event, None)["body"])
        assert body["allowed"] is False
        assert body["reason"] == "monthly_exceeded"
        assert body["policy"]["identifier"] == "block-500m"


class TestQuotaShowListsEveryGroupPolicy:
    def _show(self, items, capsys):
        manager = _cli_manager(items)
        command = QuotaShowCommand()
        with (
            patch("governed_inference_platform.cli.commands.quota.Config") as config_cls,
            patch("governed_inference_platform.cli.commands.quota._get_quota_manager", return_value=manager),
        ):
            config_cls.load.return_value.get_profile.return_value = MagicMock()
            command.argument = lambda name: "user@example.com"
            command.option = lambda name: {"groups": ",".join(i["identifier"] for i in items)}.get(name)
            rc = command.handle()
        return rc, " ".join(capsys.readouterr().out.split())

    def test_lists_both_policies_and_the_rule(self, capsys):
        rc, out = self._show([BLOCK_HIGH, ALERT_LOW], capsys)
        assert rc == 0
        assert "Applied Policy: group:alert-100m" in out
        assert "access is denied when any one of them would deny" in out
        assert "group:block-500m: monthly 500.0M tokens (monthly block, daily alert)" in out
        assert "group:alert-100m: monthly 100.0M tokens (monthly alert, daily alert)" in out

    def test_lists_a_daily_spend_limit(self, capsys):
        """A cost group whose only blocking limit is a daily spend cap shows that cap."""
        spend = _item("spend", cost=100, daily_cost=5, monthly="alert", daily="block", mode="cost")
        rc, out = self._show([spend, BLOCK_HIGH], capsys)
        assert rc == 0
        assert "group:spend: monthly 0 tokens, monthly $100.00, daily $5.00 (monthly alert, daily block)" in out
        assert "group:block-500m: monthly 500.0M tokens (monthly block, daily alert)" in out

    def test_single_group_policy_prints_no_list(self, capsys):
        rc, out = self._show([BLOCK_HIGH], capsys)
        assert rc == 0
        assert "Group policies checked" not in out


# Every combination of two group policies over enforcement and limits: the three
# resolvers must choose the same one.
_GRID = [
    _item(f"g{n}", tokens=tokens, cost=cost, monthly=monthly, daily=daily, mode=mode)
    for n, (tokens, cost, monthly, daily, mode) in enumerate(
        itertools.product(
            [0, 100_000_000, 500_000_000],
            [0, 50],
            ["alert", "block"],
            ["alert", "block"],
            ["token", "cost"],
        )
    )
]


@pytest.mark.parametrize("pair", list(itertools.combinations(_GRID, 2))[::7])
def test_three_resolvers_agree(quota_check, quota_monitor, pair):
    chosen = {
        _check_resolve(quota_check, list(pair))["identifier"],
        _monitor_resolve(quota_monitor, list(pair))["identifier"],
        _cli_resolve(list(pair)).identifier,
    }
    assert len(chosen) == 1, (pair, chosen)
