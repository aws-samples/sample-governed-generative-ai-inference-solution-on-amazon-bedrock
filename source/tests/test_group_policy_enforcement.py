# ABOUTME: Handler-level tests that a user is denied when any one of their group policies would deny on its own
# ABOUTME: Covers the two-group cases, a monotonicity property, and a differential check against the lowest-limit rule

"""Group policies: every enabled group policy is checked, and any one of them can deny.

Rule: among a user's enabled group policies, quota_check denies when any one of them,
evaluated alone against the user's usage, would deny. The response then names the
first denying policy in lowest-limit order; when nothing denies, it names the group
policy with the lowest limit. A user-specific policy still replaces all group policies,
and the default policy applies only when no group policy does.

Earlier rule: the lowest-limit group policy alone was evaluated, so a blocking limit in
any other group did not apply. The differential test below reproduces that rule by
evaluating only the policy it chose, and checks that the handler never allows what it
denied, and otherwise returns the same response.
"""

import importlib.util
import json
import os
import random
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions" / "quota_check"

ENV = {
    "QUOTA_TABLE": "TestQuotaTable",
    "POLICIES_TABLE": "TestPoliciesTable",
    "MISSING_EMAIL_ENFORCEMENT": "block",
    "ERROR_HANDLING_MODE": "fail_closed",
    "ENABLE_FINEGRAINED_QUOTAS": "true",
    "MONTHLY_TOKEN_LIMIT": "0",
    "MONTHLY_COST_LIMIT_USD": "0",
    "DAILY_COST_LIMIT_USD": "0",
}
EMAIL = "user@example.com"


@pytest.fixture(scope="module")
def mod():
    saved = {key: os.environ.get(key) for key in ENV}
    os.environ.update(ENV)
    try:
        spec = importlib.util.spec_from_file_location("quota_check_group_enforcement", LAMBDA / "index.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["quota_check_group_enforcement"] = module
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


def policy(
    name,
    *,
    kind="group",
    tokens=0,
    daily_tokens=None,
    cost=0,
    daily_cost=0,
    monthly="alert",
    daily="alert",
    mode="token",
    enabled=True,
):
    """A policies-table item as `gip quota set-group` (or `set-user`) writes it."""
    item = {
        "pk": f"POLICY#{kind}#{name}",
        "sk": "CURRENT",
        "policy_type": kind,
        "identifier": name,
        "quota_mode": mode,
        "monthly_token_limit": Decimal(tokens),
        "enforcement_mode": monthly,
        "daily_enforcement_mode": daily,
        "enabled": enabled,
    }
    if daily_tokens:
        item["daily_token_limit"] = Decimal(daily_tokens)
    if cost:
        item["monthly_cost_limit"] = Decimal(str(cost))
    if daily_cost:
        item["daily_cost_limit"] = Decimal(str(daily_cost))
    return item


def check(mod, items, groups, *, month=0, day=0, month_cost=0, day_cost=0, unblock=None):
    """Run lambda_handler for EMAIL with the given policies, groups claim and usage."""
    by_pk = {i["pk"]: i for i in items}
    mod.policies_table = MagicMock()
    mod.policies_table.get_item.side_effect = lambda Key: {"Item": by_pk[Key["pk"]]} if Key["pk"] in by_pk else {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = {
        "total_tokens": Decimal(month),
        "daily_tokens": Decimal(day),
        "daily_date": today,
        "estimated_cost": Decimal(str(month_cost)),
        "daily_cost_usd": Decimal(str(day_cost)),
    }

    def quota_get(Key):
        assert Key["pk"] == f"USER#{EMAIL}"
        if Key["sk"] == "UNBLOCK#CURRENT":
            return {"Item": unblock} if unblock else {}
        if Key["sk"].startswith("MONTH#"):
            return {"Item": dict(usage)}
        return {}

    mod.quota_table = MagicMock()
    mod.quota_table.get_item.side_effect = quota_get
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"email": EMAIL, "groups": list(groups)}}}}}
    return json.loads(mod.lambda_handler(event, None)["body"])


def _both_orders(a, b):
    return pytest.mark.parametrize("order", [[a, b], [b, a]], ids=["ab", "ba"])


# The two-group cases --------------------------------------------------------------------------------------------

DAILY_ALERT_GROUP = policy("A", tokens=100_000, daily_tokens=10_000, monthly="alert", daily="block")
MONTHLY_BLOCK_1M = policy("B", tokens=1_000_000, monthly="block", daily="alert")


@_both_orders(DAILY_ALERT_GROUP, MONTHLY_BLOCK_1M)
def test_daily_block_of_one_group_applies_next_to_a_monthly_block_group(mod, order):
    body = check(mod, order, [p["identifier"] for p in order], month=50_000, day=50_000)
    assert body["allowed"] is False
    assert body["reason"] == "daily_exceeded"
    assert body["policy"] == {"type": "group", "identifier": "A"}


MONTHLY_BLOCK_100K = policy("A", tokens=100_000, monthly="block")
MONTHLY_AND_DAILY_BLOCK_1M = policy("B", tokens=1_000_000, daily_tokens=200_000, monthly="block", daily="block")


@_both_orders(MONTHLY_BLOCK_100K, MONTHLY_AND_DAILY_BLOCK_1M)
def test_lower_monthly_block_applies_next_to_a_group_with_a_daily_block(mod, order):
    body = check(mod, order, [p["identifier"] for p in order], month=500_000, day=0)
    assert body["allowed"] is False
    assert body["reason"] == "monthly_exceeded"
    assert body["policy"] == {"type": "group", "identifier": "A"}


BLOCK_500M = policy("block-500m", tokens=500_000_000, monthly="block")
ALERT_100M = policy("alert-100m", tokens=100_000_000, monthly="alert")


@_both_orders(BLOCK_500M, ALERT_100M)
def test_block_group_blocks_at_its_limit_next_to_a_lower_alert_only_group(mod, order):
    groups = [p["identifier"] for p in order]
    over = check(mod, order, groups, month=600_000_000)
    assert over["allowed"] is False
    assert over["reason"] == "monthly_exceeded"
    assert over["policy"] == {"type": "group", "identifier": "block-500m"}
    assert over["usage"]["monthly_limit"] == 500_000_000

    under = check(mod, order, groups, month=450_000_000)
    assert under["allowed"] is True
    # Nothing denies: the response names the lowest-limit group policy, as before.
    assert under["policy"] == {"type": "group", "identifier": "alert-100m"}


def test_cost_block_of_one_group_applies_next_to_a_token_group(mod):
    cost_group = policy("cost-5", mode="cost", cost=5, monthly="block")
    token_group = policy("tokens-1m", tokens=1_000_000, monthly="block")
    body = check(mod, [token_group, cost_group], ["tokens-1m", "cost-5"], month=10_000, month_cost=7)
    assert body["allowed"] is False
    assert body["reason"] == "monthly_cost_exceeded"
    assert body["policy"] == {"type": "group", "identifier": "cost-5"}


def test_user_policy_still_replaces_group_policies(mod):
    user = policy(EMAIL, kind="user", tokens=1_000_000_000, monthly="alert")
    body = check(mod, [user, MONTHLY_BLOCK_100K], ["A"], month=500_000)
    assert body["allowed"] is True
    assert body["policy"] == {"type": "user", "identifier": EMAIL}


def test_disabled_group_policy_is_not_checked(mod):
    disabled = policy("off", tokens=10, monthly="block", enabled=False)
    body = check(mod, [disabled, ALERT_100M], ["off", "alert-100m"], month=500)
    assert body["allowed"] is True
    assert body["policy"] == {"type": "group", "identifier": "alert-100m"}


def test_unblock_override_still_applies_to_group_policies(mod):
    unblock = {"pk": f"USER#{EMAIL}", "sk": "UNBLOCK#CURRENT", "expires_at": "2999-01-01T00:00:00Z"}
    body = check(mod, [MONTHLY_BLOCK_100K, ALERT_100M], ["A", "alert-100m"], month=500_000, unblock=unblock)
    assert body["allowed"] is True
    assert body["reason"] == "unblocked"


# Random policy sets ---------------------------------------------------------------------------------------------


def _random_policy(rng, name):
    return policy(
        name,
        mode=rng.choice(["token", "cost"]),
        tokens=rng.choice([0, 100_000, 1_000_000]),
        daily_tokens=rng.choice([None, 10_000, 200_000]),
        cost=rng.choice([0, 5, 50]),
        daily_cost=rng.choice([0, 1, 10]),
        monthly=rng.choice(["alert", "block"]),
        daily=rng.choice(["alert", "block"]),
        enabled=rng.random() > 0.1,
    )


def _random_usage(rng):
    return {
        "month": rng.choice([0, 50_000, 150_000, 500_000, 2_000_000]),
        "day": rng.choice([0, 5_000, 50_000, 300_000]),
        "month_cost": rng.choice([0, 3, 10, 100]),
        "day_cost": rng.choice([0, 0.5, 2, 20]),
    }


def _cases(seed, count):
    rng = random.Random(seed)  # nosec B311 - seeded test data, not a security use
    for _ in range(count):
        size = rng.randint(1, 4)
        yield [_random_policy(rng, f"g{n}") for n in range(size + 1)], _random_usage(rng)


@pytest.mark.parametrize("seed", range(6))
def test_adding_a_group_never_turns_a_deny_into_an_allow(mod, seed):
    """Property: for a user who already has an enabled group policy, one more group can only add denials."""
    checked = 0
    for policies, usage in _cases(seed, 120):
        base, extra = policies[:-1], policies[-1]
        if not any(p["enabled"] for p in base):
            continue  # with no group policy the default applies; groups replace it by design
        before = check(mod, base, [p["identifier"] for p in base], **usage)
        after = check(mod, policies, [p["identifier"] for p in policies], **usage)
        if before["allowed"] is False:
            assert after["allowed"] is False, (base, extra, usage, before, after)
        checked += 1
    assert checked > 80


def _lowest_limit_choice(mod, policies):
    """The single group policy the lowest-limit rule applied: the first minimum, in the order the
    handler reads the groups claim (extract_groups_from_claims de-duplicates through a set)."""
    by_id = {p["identifier"]: p for p in policies}
    claim_order = mod.extract_groups_from_claims({"groups": [p["identifier"] for p in policies]})
    enabled = [mod.get_policy("group", g) for g in claim_order if by_id[g]["enabled"]]
    return min(enabled, key=mod._policy_limit_sort_key)["identifier"] if enabled else None


@pytest.mark.parametrize("seed", range(6))
def test_never_allows_what_the_lowest_limit_rule_denied(mod, seed):
    """Differential: same response as the lowest-limit rule, except that it may deny where that rule allowed."""
    stricter = 0
    for policies, usage in _cases(100 + seed, 120):
        groups = [p["identifier"] for p in policies]
        body = check(mod, policies, groups, **usage)
        chosen = _lowest_limit_choice(mod, policies)
        if chosen is None:
            continue
        only = [p for p in policies if p["identifier"] == chosen]
        old = check(mod, only, [chosen], **usage)
        if old["allowed"] is False:
            assert body == old, (policies, usage, old, body)
        elif body["allowed"] is True:
            assert body == old, (policies, usage, old, body)
        else:
            stricter += 1
    assert stricter > 0  # the rule change is exercised, not only the identical cases
