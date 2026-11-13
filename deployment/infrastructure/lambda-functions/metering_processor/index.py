# ABOUTME: Lambda that meters per-user Bedrock usage server-side from model invocation logs
# ABOUTME: Parses CW Logs subscription events, commits DEDUP# markers + server_* accrual atomically per user via TransactWriteItems

"""Server-side usage metering processor (Phase 1: shadow/reconcile).

Receives Bedrock model invocation log entries via a CloudWatch Logs
subscription filter, maps the caller identity to an email (assumed-role
session name, same ARN pattern as quota_check), prices tokens with
shared/pricing.py, and accrues additive ``server_*`` attributes onto the
existing UserQuotaMetrics month item in the quota region (plain cross-region
DynamoDB calls).

Dedup and accrual are atomic: per user, chunked ``TransactWriteItems``
commits the conditional ``DEDUP#<requestId>`` markers together with the
accrual update, so a partial-batch failure leaves no markers behind and
retries/DLQ redrives are genuinely idempotent (review F4 / R5: the previous
per-event marker write before accrual permanently under-counted on retry).

See assets/docs/designs/server-side-metering-design.md.
"""

import base64
import gzip
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

# shared/pricing.py is bundled as a symlinked subdirectory (same pattern as
# quota_monitor). In Lambda /var/task is already on sys.path; for local test
# loaders we also add this dir (symlink) and its parent (real shared/ dir,
# works even where symlinks aren't materialized, e.g. Windows checkouts).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from shared.pricing import (  # noqa: E402
    calculate_cost,
    get_rates,
    openai_long_context_family,
    resolve_model_family,
)

# Configuration from environment
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
# The quota table lives in the quota region; this stack deploys per Bedrock
# source region, so the table region usually differs from AWS_REGION.
QUOTA_TABLE_REGION = os.environ.get("QUOTA_TABLE_REGION") or os.environ.get("AWS_REGION", "us-east-1")
METERING_MODE = os.environ.get("METERING_MODE", "shadow")
DEDUP_TTL_HOURS = int(os.environ.get("DEDUP_TTL_HOURS", "336"))
SOURCE_REGION = os.environ.get("AWS_REGION", "unknown")


def parse_attributed_roles(value):
    """Parse ATTRIBUTED_ROLES: comma-separated role names; a trailing '*' makes an entry a prefix.

    Returns (exact_names, prefixes). Entries with a '*' anywhere but the end are ignored
    (the template's AllowedPattern rejects them before they get here).
    """
    exact, prefixes = set(), []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.endswith("*") and "*" not in entry[:-1] and len(entry) > 1:
            prefixes.append(entry[:-1])
        elif "*" not in entry:
            exact.add(entry)
    return exact, tuple(prefixes)


# Roles whose session names identify a user: the federated role, the IAM Identity
# Center roles (AWSReservedSSO_*, or a narrower AWSReservedSSO_<set>_*), and any role
# the operator names (for example a gateway assume_role role). A session of any
# other role is metered under UNATTRIBUTED#<role>, whatever its session name.
# Unset or empty attributes no role.
ATTRIBUTED_ROLES = parse_attributed_roles(os.environ.get("ATTRIBUTED_ROLES", ""))
if not ATTRIBUTED_ROLES[0] and not ATTRIBUTED_ROLES[1]:
    print("WARNING: ATTRIBUTED_ROLES is empty; all usage is metered under UNATTRIBUTED#<role>")

# DynamoDB TransactWriteItems accepts at most 100 actions, so each chunk
# carries up to 99 DEDUP# marker Puts plus the one accrual Update.
TRANSACT_MAX_ITEMS = 100

dynamodb = boto3.resource("dynamodb", region_name=QUOTA_TABLE_REGION)
quota_table = dynamodb.Table(QUOTA_TABLE)
# A PLAIN client, not dynamodb.meta.client: the resource's client re-serializes
# every parameter with the high-level TypeSerializer, so the low-level
# AttributeValues built below ({"N": "5"}) went on the wire as maps
# ({"M": {"N": {"S": "5"}}}) and DynamoDB rejected every accrual with
# "Incorrect operand type for operator or function; operator: ADD, operand
# type: MAP" (found live 2026-10-02).
ddb_client = boto3.client("dynamodb", region_name=QUOTA_TABLE_REGION)

# Cache token fields are the design's open question #1: metadata-only delivery
# may or may not include them, and AWS documents several spellings depending
# on the surface:
#   - invocation-log input block: cacheReadInputTokenCount /
#     cacheWriteInputTokenCount
#   - Converse TokenUsage: cacheReadInputTokens / cacheWriteInputTokens
#   - Anthropic-native usage block: cache_read_input_tokens /
#     cache_creation_input_tokens
# Capture defensively within recognized service-owned usage paths: matching
# fields are recorded raw and classified read/write by name.
CACHE_TOKEN_KEY_RE = re.compile(r"cache.*token", re.IGNORECASE)
CACHE_USAGE_PATHS = (("input",), ("output",), ("input", "usage"), ("output", "usage"))


class RecordValidationError(ValueError):
    """An invocation-log record that cannot be safely accrued."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# An STS assumed-role session ARN, in full: partition, sts, empty Region, 12-digit account,
# assumed-role/<role>/<session>, role and session in IAM's characters. Groups: role, session.
# quota_check/index.py holds the same pattern, and a test keeps the two identical.
ASSUMED_ROLE_SESSION_ARN = re.compile(
    r"arn:aws[a-z-]*:sts::[0-9]{12}:assumed-role/([\w+=,.@-]+)/([\w+=,.@-]+)", re.ASCII
)


def role_is_attributed(role, attributed_roles=None):
    """True when ``role`` is named exactly, or starts with a prefix entry, in the allow-list."""
    exact, prefixes = ATTRIBUTED_ROLES if attributed_roles is None else attributed_roles
    return role in exact or any(role.startswith(prefix) for prefix in prefixes)


def extract_identity(arn, attributed_roles=None):
    """Map an invocation log ``identity.arn`` to a quota identity.

    Only sessions of an attributed role (ATTRIBUTED_ROLES) are mapped to a user,
    from an ARN that is, in full, an STS assumed-role session ARN
    (ASSUMED_ROLE_SESSION_ARN, the pattern quota_check uses):
    - Direct STS: session name = sanitized email (contains '@') -> email.
    - IDC: AWSReservedSSO_ role with non-email username -> username.
    - Anything else (a role not in the allow-list, a session name that is not an
      email such as a bound ``sub``, Cognito federation's own session name):
      cannot be mapped per-user -> UNATTRIBUTED#<role> bucket (design Open Q3).

    Returns None when the ARN is not, in full, an assumed-role session ARN (an
    IAM user, or a string that merely contains such an ARN).
    """
    match = ASSUMED_ROLE_SESSION_ARN.fullmatch(arn) if arn else None
    if not match:
        return None
    role, session_name = match.groups()
    if not role_is_attributed(role, attributed_roles):
        return f"UNATTRIBUTED#{role}"
    if "@" in session_name:
        return session_name
    if role.startswith("AWSReservedSSO_"):
        return session_name
    return f"UNATTRIBUTED#{role}"


def _validated_token_count(value):
    """Parse a non-negative integer token count without truncation."""
    if isinstance(value, bool) or value is None:
        raise RecordValidationError("invalid_token_count")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise RecordValidationError("invalid_token_count") from e
    if parsed < 0 or (isinstance(value, float) and not value.is_integer()):
        raise RecordValidationError("invalid_token_count")
    return parsed


def find_cache_tokens(record):
    """Defensively extract cache read/write token counts from a log record.

    Inspects only recognized service-owned usage containers. Caller-controlled
    fields elsewhere in the record may have cache-like names and must not
    influence metering validation. Each distinct key name is counted once, and
    within each class (read / write) the MAX across matching keys is used rather
    than the sum because documented spellings are alternate names for the same
    quantity. Returns (cache_read, cache_write, raw).
    """
    raw = {}

    for path in CACHE_USAGE_PATHS:
        container = record
        for segment in path:
            if not isinstance(container, dict):
                break
            container = container.get(segment)
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            if CACHE_TOKEN_KEY_RE.search(key):
                raw.setdefault(key, _validated_token_count(value))

    cache_read = 0
    cache_write = 0
    for key, value in raw.items():
        key_lower = key.lower()
        if "read" in key_lower:
            cache_read = max(cache_read, value)
        elif "write" in key_lower or "creat" in key_lower:
            cache_write = max(cache_write, value)
    return cache_read, cache_write, raw


def parse_record(record):
    """Parse and validate one invocation log record into metering fields."""
    if not isinstance(record, dict):
        raise RecordValidationError("malformed_record")
    identity_value = record.get("identity")
    identity_block = {} if identity_value is None else identity_value
    input_block = record.get("input", {})
    output_block = record.get("output", {})
    if not all(isinstance(block, dict) for block in (identity_block, input_block, output_block)):
        raise RecordValidationError("malformed_record")
    model_id = record.get("modelId", "") or ""
    if not isinstance(model_id, str):
        raise RecordValidationError("malformed_record")
    identity = extract_identity(identity_block.get("arn", "")) or "UNATTRIBUTED#unknown"
    input_tokens = _validated_token_count(input_block.get("inputTokenCount"))
    output_tokens = _validated_token_count(output_block.get("outputTokenCount"))
    cache_read, cache_write, cache_raw = find_cache_tokens(record)
    if input_tokens + output_tokens + cache_read + cache_write == 0:
        raise RecordValidationError("zero_tokens")
    return {
        "request_id": record.get("requestId", ""),
        "identity": identity,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_raw": cache_raw,
    }


def build_dedup_put(request_id):
    """One transaction action: conditional Put of the DEDUP#<requestId> marker.

    The condition makes redelivered requestIds surface as
    ConditionalCheckFailed cancellation reasons instead of double-counting
    (ADD is not idempotent). Markers carry a TTL so the table does not grow
    unbounded; because markers only ever commit together with their accrual
    (see accrue_identity), TTL expiry can at worst re-count an event after
    DEDUP_TTL_HOURS, never lose one.
    """
    return {
        "Put": {
            "TableName": QUOTA_TABLE,
            "Item": {
                "pk": {"S": f"DEDUP#{request_id}"},
                "sk": {"S": "DEDUP"},
                "ttl": {"N": str(int(datetime.now(timezone.utc).timestamp()) + DEDUP_TTL_HOURS * 3600)},
            },
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }


def build_accrual_update(identity, agg, daily_reset):
    """One transaction action: accrue a user's aggregate onto their month item.

    Mirrors quota_monitor.update_quota_metrics: UpdateExpression ADD for the
    monthly counters, with a server_daily_date guard that resets the daily
    counters (SET instead of ADD) on the first write of a new UTC day.
    Values are in low-level AttributeValue format (TransactWriteItems is a
    client-level API).
    """
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")
    ttl = int((now.replace(day=28) + timedelta(days=32)).replace(day=1).timestamp())

    total = agg["input_tokens"] + agg["output_tokens"] + agg["cache_read_tokens"] + agg["cache_write_tokens"]
    update_expr = (
        "ADD server_total_tokens :delta, server_input_tokens :inp, server_output_tokens :out, "
        "server_cache_read_tokens :cread, server_cache_write_tokens :cwrite, "
        "server_estimated_cost :cost, server_regions :region"
    )
    expr_values = {
        ":delta": {"N": str(int(total))},
        ":inp": {"N": str(int(agg["input_tokens"]))},
        ":out": {"N": str(int(agg["output_tokens"]))},
        ":cread": {"N": str(int(agg["cache_read_tokens"]))},
        ":cwrite": {"N": str(int(agg["cache_write_tokens"]))},
        ":cost": {"N": f"{round(agg['cost_usd'], 6):.6f}"},
        ":region": {"SS": [SOURCE_REGION]},
        ":ts": {"S": now.isoformat().replace("+00:00", "Z")},
        ":ttl": {"N": str(ttl)},
        ":email": {"S": identity},
    }
    if daily_reset:
        update_expr += (
            " SET server_daily_tokens = :delta, server_daily_cost_usd = :cost, "
            "server_daily_date = :date, server_last_updated = :ts, #ttl = :ttl, email = :email"
        )
        expr_values[":date"] = {"S": current_date}
    else:
        update_expr += (
            ", server_daily_tokens :delta, server_daily_cost_usd :cost "
            "SET server_last_updated = :ts, #ttl = :ttl, email = :email"
        )

    return {
        "Update": {
            "TableName": QUOTA_TABLE,
            "Key": {"pk": {"S": f"USER#{identity}"}, "sk": {"S": f"MONTH#{current_month}"}},
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": {"#ttl": "ttl"},
            "ExpressionAttributeValues": expr_values,
        }
    }


def _aggregate(events):
    agg = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}
    for event in events:
        agg["input_tokens"] += event["input_tokens"]
        agg["output_tokens"] += event["output_tokens"]
        agg["cache_read_tokens"] += event["cache_read_tokens"]
        agg["cache_write_tokens"] += event["cache_write_tokens"]
        agg["cost_usd"] += event["cost_usd"]
    return agg


def accrue_identity(identity, events):
    """Atomically commit DEDUP# markers and accrual for one user's events.

    Chunked TransactWriteItems (<=99 conditional marker Puts + 1 accrual
    Update per transaction): markers and accrual succeed or fail together, so
    a partial-batch failure leaves nothing marked and the retry / DLQ redrive
    re-accrues instead of reading the batch as duplicates (review F4 / R5).

    On TransactionCanceledException, requestIds whose marker Put reports
    ConditionalCheckFailed are already-accrued duplicates: they are dropped
    from the chunk and the remainder retried (each retry removes at least one
    event, so the loop is bounded). Any other cancellation (capacity,
    transaction conflict) committed nothing and propagates so the whole
    invocation retries into the DLQ — the fail-visible direction.

    Returns (accrued_count, duplicate_count).
    """
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")
    key = {"pk": f"USER#{identity}", "sk": f"MONTH#{current_month}"}
    response = quota_table.get_item(Key=key)
    needs_reset = response.get("Item", {}).get("server_daily_date") != current_date

    accrued = 0
    duplicates = 0
    for start in range(0, len(events), TRANSACT_MAX_ITEMS - 1):
        remaining = events[start : start + TRANSACT_MAX_ITEMS - 1]
        while remaining:
            transact_items = [build_dedup_put(event["request_id"]) for event in remaining]
            transact_items.append(build_accrual_update(identity, _aggregate(remaining), needs_reset))
            try:
                ddb_client.transact_write_items(TransactItems=transact_items)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                reasons = e.response.get("CancellationReasons", [])
                duplicate_indexes = {
                    i
                    for i, reason in enumerate(reasons[: len(remaining)])
                    if (reason or {}).get("Code") == "ConditionalCheckFailed"
                }
                if not duplicate_indexes:
                    # Cancelled for capacity/conflict reasons: nothing was
                    # committed (markers included), so re-raising is safe —
                    # the redelivered batch re-accrues without undercount.
                    raise
                duplicates += len(duplicate_indexes)
                remaining = [event for i, event in enumerate(remaining) if i not in duplicate_indexes]
                continue
            accrued += len(remaining)
            needs_reset = False
            break
    return accrued, duplicates


def _record_invalid(stats, reason, log_event_id, request_id=""):
    """Log one content-free validation failure and retain batch counts."""
    stats["skipped"] += 1
    stats["invalid_records"] += 1
    stats["invalid_reasons"][reason] = stats["invalid_reasons"].get(reason, 0) + 1
    print(
        json.dumps(
            {
                "level": "ERROR",
                "event": "invalid_metering_record",
                "reason": reason,
                "log_event_id": log_event_id,
                "request_id": request_id or None,
                "source_region": SOURCE_REGION,
            },
            sort_keys=True,
        )
    )


def _emit_invalid_records_metric(stats):
    """Emit an undimensioned EMF metric so any invalid record can alarm."""
    if not stats["invalid_records"]:
        return
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "GIP/Metering",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": "InvalidRecords", "Unit": "Count"}],
                        }
                    ],
                },
                "InvalidRecords": stats["invalid_records"],
                "invalid_reasons": stats["invalid_reasons"],
                "source_region": SOURCE_REGION,
            },
            sort_keys=True,
        )
    )


def lambda_handler(event, context):
    """Process one CloudWatch Logs subscription batch of invocation log events."""
    data = (event.get("awslogs") or {}).get("data")
    if not data:
        print("WARNING: event without awslogs payload — skipping")
        return {"statusCode": 200, "body": json.dumps({"events": 0})}

    payload = json.loads(gzip.decompress(base64.b64decode(data)))
    log_events = payload.get("logEvents", [])

    rates = get_rates()
    per_identity = {}
    seen_request_ids = set()
    stats = {
        "events": len(log_events),
        "accrued": 0,
        "duplicates": 0,
        "skipped": 0,
        "invalid_records": 0,
        "invalid_reasons": {},
    }
    cache_keys_seen = set()
    unpriced_aip_models = set()

    for log_event in log_events:
        try:
            record = json.loads(log_event.get("message", ""))
        except (json.JSONDecodeError, TypeError):
            _record_invalid(stats, "malformed_json", log_event.get("id", ""))
            continue
        if not isinstance(record, dict):
            _record_invalid(stats, "malformed_record", log_event.get("id", ""))
            continue
        request_id = record.get("requestId", "")
        if not isinstance(request_id, str) or not request_id.strip():
            _record_invalid(stats, "missing_request_id", log_event.get("id", ""))
            continue
        try:
            parsed = parse_record(record)
        except RecordValidationError as e:
            _record_invalid(stats, e.reason, log_event.get("id", ""), request_id)
            continue
        if parsed["request_id"] in seen_request_ids:
            # Intra-batch repeat: TransactWriteItems rejects two actions on
            # the same item, so repeats are dropped in memory before chunking.
            stats["duplicates"] += 1
            continue
        seen_request_ids.add(parsed["request_id"])

        cache_keys_seen.update(parsed["cache_raw"].keys())
        family = resolve_model_family(parsed["model_id"], rates=rates)
        if family == "unpriced_aip":
            unpriced_aip_models.add(parsed["model_id"])
        # OpenAI cards bill the whole request at long-context rates above 272K
        # input tokens (ADR-0037); no-op for every other family.
        family = openai_long_context_family(
            family,
            parsed["input_tokens"] + parsed["cache_read_tokens"] + parsed["cache_write_tokens"],
            rates,
        )
        parsed["cost_usd"] = calculate_cost(
            parsed["input_tokens"],
            parsed["output_tokens"],
            parsed["cache_read_tokens"],
            parsed["cache_write_tokens"],
            model_family=family,
            rates=rates,
        )
        per_identity.setdefault(parsed["identity"], []).append(parsed)

    for identity, events in per_identity.items():
        accrued, duplicates = accrue_identity(identity, events)
        stats["accrued"] += accrued
        stats["duplicates"] += duplicates

    if cache_keys_seen:
        # Phase 1 empirical record for design open question #1.
        print(f"INFO: cache token fields observed in invocation logs: {sorted(cache_keys_seen)}")
    if unpriced_aip_models:
        print(
            "WARNING: application inference profile modelId values are opaque; "
            f"server_estimated_cost set to 0 for {sorted(unpriced_aip_models)}"
        )
    _emit_invalid_records_metric(stats)
    print(f"INFO: metering batch processed mode={METERING_MODE} users={len(per_identity)} stats={stats}")
    return {"statusCode": 200, "body": json.dumps(stats)}
