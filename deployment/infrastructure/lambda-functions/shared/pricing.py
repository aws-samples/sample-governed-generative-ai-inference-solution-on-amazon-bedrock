# ABOUTME: Shared Bedrock pricing utility for quota cost calculations.
# ABOUTME: Maps model families to per-token-type rates ($/MTok) for cost-based enforcement.

"""
Bedrock pricing rates for cost-based quota enforcement.

Provides per-model-family, per-token-type pricing. Used by quota_monitor
to convert raw token counts into estimated USD spend.

IMPORTANT: These are estimates based on published Bedrock on-demand rates.
Actual costs may differ due to committed throughput, pricing changes, or
custom agreements. Use AWS Cost Explorer for billing truth.

Rates can be overridden via BEDROCK_PRICING_RATES_JSON env var.

Legacy / public-extended-access pricing
---------------------------------------
Bedrock's model lifecycle (ACTIVE -> LEGACY -> EOL) adds a "public extended
access" phase inside the Legacy period for models with EOL dates after
2026-02-01. During that phase "you should expect higher pricing, which will
be set by the model provider" — the uplift is per-model, announced via the
AWS Health Legacy notification, and published (when set) in the "Models with
extended access" table on the Bedrock pricing page. It is NOT exposed via
the modelLifecycle API.
  Sources:
  - https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
    (mechanism + per-model legacy/extended-access/EOL dates; retrieved 2026-07-29)
  - https://aws.amazon.com/bedrock/pricing/ ("Models with extended access"
    table; retrieved 2026-07-29)

LEGACY_MODEL_PRICING below joins those dates against the model IDs this
platform can meter. Selection is date-based: once the extended-access date
passes, the model's ``extended_family`` rate is used IF a verified rate
exists (or an admin supplies one via BEDROCK_PRICING_RATES_JSON under that
family key). Axiom: wrong numbers are worse than no numbers — no rate is
ever invented.

UNPRICED LEGACY MODELS (extended access active or scheduled, provider uplift
not published anywhere citable as of 2026-07-29 — metered at standard family
rates with a structured WARNING log instead of a silent under-meter):
  - Claude Sonnet 4    (extended access ACTIVE since 2026-07-14, EOL 2026-10-14)
        override key: "sonnet-4-extended"
  - Claude Opus 4.1    (LEGACY since 2026-07-08; extended access 2026-10-08,
        EOL 2027-01-08) override key: "opus-4-1-extended"
  - Claude 3.7 Sonnet  (extended access ACTIVE since 2026-04-30 in GovCloud,
        EOL 2026-07-30) override key: "sonnet-3-7-extended"
  - Claude 3 Haiku     (extended access ACTIVE since 2026-06-10, EOL 2026-09-10)
        override key: "haiku-3-extended"
  - Claude 3 Sonnet    (extended access ACTIVE since 2026-04-30 in ap-* regions,
        EOL 2026-07-30) override key: "sonnet-3-extended"
  - Claude Opus 4      (absent from the Bedrock lifecycle table — presumed past
        EOL; any observed usage is warned and metered at its $15/$75 standard
        rate) no override key.
When the provider uplift arrives (AWS Health notification / pricing page),
add it under the model's override key via BEDROCK_PRICING_RATES_JSON, or
promote it into DEFAULT_RATES with a source URL + retrieval date.
"""

import json
import os
from datetime import datetime, timezone

# Per-model-family rates in USD per 1M tokens (as of June 2026)
# Source: https://aws.amazon.com/bedrock/pricing/
DEFAULT_RATES = {
    "unpriced_aip": {
        "input": 0.00,
        "output": 0.00,
        "cache_read": 0.00,
        "cache_write": 0.00,
    },
    "fable": {
        "input": 10.00,
        "output": 50.00,
        "cache_read": 1.00,
        "cache_write": 12.50,
    },
    "opus": {
        "input": 5.00,
        "output": 25.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
    },
    "sonnet": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "haiku": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    # Claude Opus 4 / Opus 4.1 standard rates. These models predate the Opus
    # re-tiering to $5/$25 — the generic "opus" family under-meters them 3x.
    # Sources: https://www.anthropic.com/news/claude-4 ("$15/$75 for Opus 4",
    # published 2025-05-22; retrieved 2026-07-29);
    # https://www.finout.io/blog/claude-pricing-in-2026-for-individuals-organizations-and-developers
    # (Opus 4: $15 in / $75 out / $18.75 cache write / $1.50 cache read,
    # published 2026-06-01; retrieved 2026-07-29).
    "opus-legacy": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    # Claude 3.5 Sonnet (v1 + v2) public-extended-access rates — 2x standard.
    # Source: https://aws.amazon.com/bedrock/pricing/ "Models with extended
    # access": "Claude 3.5 Sonnet v2 (Public Extended Access, Effective
    # 1 Dec 2025) ... $6.00 / $30.00 / cache write $7.50 / cache read $0.60"
    # (retrieved 2026-07-29). v1 row lists the same $6.00/$30.00 with cache
    # N/A (prompt caching unavailable for v1, so cache rates never apply).
    "sonnet-3-5-extended": {
        "input": 6.00,
        "output": 30.00,
        "cache_read": 0.60,
        "cache_write": 7.50,
    },
}

# OpenAI models on the bedrock-runtime endpoint (ChatGPT Work / Codex, ADR-0037).
# Standard tier, short context (<= 272K input tokens), USD per 1M tokens. Global
# CRIS (global.* profiles) carries no premium; US Geo CRIS (us.* profiles) and
# Mantle in-Region carry a 10% premium, so they are separate families. Cache
# write is the card's "Input — cache write" column (1.25x input).
# Sources (all retrieved 2026-10-02):
#   GPT-6 Sol    https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.html
#   GPT-6 Luna   https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html
#   GPT-6 Astra  https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-astra.html
#   GPT-6.1 Sol  https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-sol.html
#   GPT-5.6 Sol  https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-sol.html
#   GPT-5.6 Terra https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-terra.html
#   GPT-5.6 Luna https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html
# Long context: when a request's input exceeds 272K tokens the cards bill the
# WHOLE request at the long-context rate; those rows are the "-long" families
# below, selected by openai_long_context_family() in the metering processor.
OPENAI_RATES = {
    "openai-gpt-6-sol-global": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "openai-gpt-6-sol-us": {"input": 2.20, "output": 11.00, "cache_read": 0.22, "cache_write": 2.75},
    "openai-gpt-6-luna-global": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "openai-gpt-6-luna-us": {"input": 0.11, "output": 0.55, "cache_read": 0.011, "cache_write": 0.1375},
    "openai-gpt-6-astra-global": {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "openai-gpt-6-astra-us": {"input": 11.00, "output": 55.00, "cache_read": 1.10, "cache_write": 13.75},
    "openai-gpt-6-1-sol-global": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "openai-gpt-6-1-sol-us": {"input": 2.20, "output": 11.00, "cache_read": 0.11, "cache_write": 2.75},
    "openai-gpt-5-6-sol-global": {"input": 4.00, "output": 20.00, "cache_read": 0.40, "cache_write": 5.00},
    "openai-gpt-5-6-sol-us": {"input": 4.40, "output": 22.00, "cache_read": 0.44, "cache_write": 5.50},
    "openai-gpt-5-6-terra-global": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_write": 2.50},
    "openai-gpt-5-6-terra-us": {"input": 2.20, "output": 13.20, "cache_read": 0.22, "cache_write": 2.75},
    "openai-gpt-5-6-luna-global": {"input": 0.20, "output": 1.20, "cache_read": 0.02, "cache_write": 0.25},
    "openai-gpt-5-6-luna-us": {"input": 0.22, "output": 1.32, "cache_read": 0.022, "cache_write": 0.275},
    # Long context (> 272K input tokens), same cards.
    "openai-gpt-6-sol-global-long": {"input": 4.00, "output": 15.00, "cache_read": 0.40, "cache_write": 5.00},
    "openai-gpt-6-sol-us-long": {"input": 4.40, "output": 16.50, "cache_read": 0.44, "cache_write": 5.50},
    "openai-gpt-6-luna-global-long": {"input": 0.20, "output": 0.75, "cache_read": 0.02, "cache_write": 0.25},
    "openai-gpt-6-luna-us-long": {"input": 0.22, "output": 0.825, "cache_read": 0.022, "cache_write": 0.275},
    "openai-gpt-6-astra-global-long": {"input": 20.00, "output": 75.00, "cache_read": 2.00, "cache_write": 25.00},
    "openai-gpt-6-astra-us-long": {"input": 22.00, "output": 82.50, "cache_read": 2.20, "cache_write": 27.50},
    "openai-gpt-6-1-sol-global-long": {"input": 4.00, "output": 15.00, "cache_read": 0.20, "cache_write": 5.00},
    "openai-gpt-6-1-sol-us-long": {"input": 4.40, "output": 16.50, "cache_read": 0.22, "cache_write": 5.50},
    "openai-gpt-5-6-sol-global-long": {"input": 8.00, "output": 30.00, "cache_read": 0.80, "cache_write": 10.00},
    "openai-gpt-5-6-sol-us-long": {"input": 8.80, "output": 33.00, "cache_read": 0.88, "cache_write": 11.00},
    "openai-gpt-5-6-terra-global-long": {"input": 4.00, "output": 18.00, "cache_read": 0.40, "cache_write": 5.00},
    "openai-gpt-5-6-terra-us-long": {"input": 4.40, "output": 19.80, "cache_read": 0.44, "cache_write": 5.50},
    "openai-gpt-5-6-luna-global-long": {"input": 0.40, "output": 1.80, "cache_read": 0.04, "cache_write": 0.50},
    "openai-gpt-5-6-luna-us-long": {"input": 0.44, "output": 1.98, "cache_read": 0.044, "cache_write": 0.55},
}
DEFAULT_RATES.update(OPENAI_RATES)

# Long-context threshold from the OpenAI model cards above (input tokens).
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000

# (substring of the lowercased model ID, family stem). Order matters: the
# longer "gpt-6.1-sol" must match before "gpt-6-sol" cannot (distinct strings,
# but keep specific-first for future additions).
OPENAI_MODEL_FAMILIES = (
    ("gpt-6.1-sol", "openai-gpt-6-1-sol"),
    ("gpt-6-sol", "openai-gpt-6-sol"),
    ("gpt-6-luna", "openai-gpt-6-luna"),
    ("gpt-6-astra", "openai-gpt-6-astra"),
    ("gpt-5.6-sol", "openai-gpt-5-6-sol"),
    ("gpt-5.6-terra", "openai-gpt-5-6-terra"),
    ("gpt-5.6-luna", "openai-gpt-5-6-luna"),
)

# An OpenAI model ID not in OPENAI_MODEL_FAMILIES (a new release, GPT-5.5,
# GPT-5.4, or an un-carded variant) is metered at the highest Standard-tier
# rate configured here (GPT-6 Astra, US Geo) with a structured WARNING: an
# over-estimate blocks a budget early, an under-estimate would let spend
# through. Override via BEDROCK_PRICING_RATES_JSON under "openai-unpriced".
OPENAI_UNPRICED_FAMILY = "openai-unpriced"
DEFAULT_RATES[OPENAI_UNPRICED_FAMILY] = dict(OPENAI_RATES["openai-gpt-6-astra-us"])
DEFAULT_RATES[OPENAI_UNPRICED_FAMILY + "-long"] = dict(OPENAI_RATES["openai-gpt-6-astra-us-long"])


def openai_long_context_family(family: str, input_side_tokens: int, rates: dict | None = None) -> str:
    """Switch an OpenAI family to its long-context rates above 272K input tokens.

    ``input_side_tokens`` should be every input-side token of ONE request
    (uncached input plus cache read plus cache write): counting cached tokens
    toward the threshold can only select the higher rate earlier. Non-OpenAI
    families are returned unchanged.
    """
    if not family.startswith("openai-") or input_side_tokens <= OPENAI_LONG_CONTEXT_THRESHOLD:
        return family
    long_family = family + "-long"
    effective = DEFAULT_RATES if rates is None else rates
    return long_family if long_family in effective else family


# Default model family when model cannot be resolved
DEFAULT_FAMILY = "sonnet"

# Per-model lifecycle pricing schedule. ``match`` is a substring of the
# lowercased model ID (matched before the generic family fallthrough — the
# date stamps in base model IDs keep e.g. "claude-sonnet-4-2025" from
# matching claude-sonnet-4-5/4-6). Dates are ISO strings from
# https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
# (retrieved 2026-07-29) unless noted. ``extended_family`` is the rate-table
# key used once ``extended_access_date`` passes — selected only if the key
# exists in the effective rates (DEFAULT_RATES or env override); otherwise
# the model is metered at ``standard_family`` and a structured WARNING is
# logged (A5: metering degrades visibly, never silently).
LEGACY_MODEL_PRICING = (
    {
        "match": "claude-3-5-sonnet-20240620",
        "label": "Claude 3.5 Sonnet",
        "standard_family": "sonnet",
        # Effective date from the pricing page row, which predates the
        # lifecycle table's remaining-region date of 2026-04-30.
        "extended_access_date": "2025-12-01",
        "eol_date": "2026-07-30",
        "extended_family": "sonnet-3-5-extended",
    },
    {
        "match": "claude-3-5-sonnet-20241022",
        "label": "Claude 3.5 Sonnet v2",
        "standard_family": "sonnet",
        "extended_access_date": "2025-12-01",
        "eol_date": "2026-07-30",
        "extended_family": "sonnet-3-5-extended",
    },
    {
        "match": "claude-sonnet-4-2025",
        "label": "Claude Sonnet 4",
        "standard_family": "sonnet",
        "extended_access_date": "2026-07-14",
        "eol_date": "2026-10-14",
        "extended_family": "sonnet-4-extended",  # provider uplift not published — see module docstring
    },
    {
        "match": "claude-opus-4-1",
        "label": "Claude Opus 4.1",
        "standard_family": "opus-legacy",
        "extended_access_date": "2026-10-08",
        "eol_date": "2027-01-08",
        "extended_family": "opus-4-1-extended",  # provider uplift not published — see module docstring
    },
    {
        "match": "claude-opus-4-2025",
        "label": "Claude Opus 4",
        "standard_family": "opus-legacy",
        # Absent from the Bedrock lifecycle table (which lists only Legacy /
        # pending-EOL models) — presumed past EOL. Usage should not occur;
        # if it does, warn and meter at the model's standard rates.
        "extended_access_date": None,
        "eol_date": None,
        "extended_family": None,
        "presumed_eol": True,
    },
    {
        "match": "claude-3-7-sonnet",
        "label": "Claude 3.7 Sonnet",
        "standard_family": "sonnet",
        "extended_access_date": "2026-04-30",  # GovCloud regions; commercial regions already absent
        "eol_date": "2026-07-30",
        "extended_family": "sonnet-3-7-extended",  # provider uplift not published — see module docstring
    },
    {
        # Standard family "haiku" ($1/$5) exceeds this model's own $0.25/$1.25
        # standard rate — a conservative over-meter kept deliberately while
        # its extended-access uplift is unpublished.
        "match": "claude-3-haiku",
        "label": "Claude 3 Haiku",
        "standard_family": "haiku",
        "extended_access_date": "2026-06-10",
        "eol_date": "2026-09-10",
        "extended_family": "haiku-3-extended",  # provider uplift not published — see module docstring
    },
    {
        "match": "claude-3-sonnet",
        "label": "Claude 3 Sonnet",
        "standard_family": "sonnet",
        "extended_access_date": "2026-04-30",  # ap-* regions; commercial rows list no extended access
        "eol_date": "2026-07-30",
        "extended_family": "sonnet-3-extended",  # provider uplift not published — see module docstring
    },
)

# One structured warning per (event, model_id) per Lambda container — the
# metering path resolves families per record and must not flood CloudWatch.
_WARNED_KEYS = set()


def reset_pricing_warnings():
    """Clear warn-once state so tests and long-lived diagnostics are isolated."""
    _WARNED_KEYS.clear()


def _warn_once(event, model_id, detail):
    key = (event, model_id)
    if key in _WARNED_KEYS:
        return
    _WARNED_KEYS.add(key)
    payload = {"level": "WARNING", "event": event, "model_id": model_id}
    payload.update(detail)
    print(json.dumps(payload, sort_keys=True))


def _today(now=None):
    """UTC date as an ISO string (lexically comparable), injectable for tests."""
    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.date().isoformat()
    if isinstance(now, str):
        return now
    return now.isoformat()  # datetime.date


def _resolve_legacy_family(entry, model_id, now, rates):
    """Date-aware rate-family selection for a lifecycle-tracked model."""
    standard = entry["standard_family"]
    if entry.get("presumed_eol"):
        _warn_once(
            "legacy_model_presumed_eol",
            model_id,
            {
                "model": entry["label"],
                "metered_family": standard,
                "detail": (
                    "model is absent from the Bedrock lifecycle table (presumed past EOL); "
                    "usage metered at its last published standard rates"
                ),
            },
        )
        return standard

    extended_date = entry.get("extended_access_date")
    if not extended_date or _today(now) < extended_date:
        return standard

    extended_family = entry.get("extended_family")
    if extended_family and extended_family in rates:
        return extended_family

    _warn_once(
        "unpriced_extended_access",
        model_id,
        {
            "model": entry["label"],
            "metered_family": standard,
            "extended_access_date": extended_date,
            "eol_date": entry.get("eol_date"),
            "override_rate_key": extended_family,
            "detail": (
                "model is in Bedrock public extended access but the provider-set premium is not "
                "published in the API or pricing page — metering at STANDARD rates UNDER-estimates "
                "cost. Supply the premium from the AWS Health Legacy notification via "
                "BEDROCK_PRICING_RATES_JSON under the override_rate_key."
            ),
        },
    )
    return standard


def _openai_route(model_lower):
    """Pricing route for an OpenAI model ID: 'global' or 'us'.

    The invocation log ``modelId`` is the inference-profile ID the client sent
    (e.g. ``global.openai.gpt-6-sol``; verified live 2026-10-02), so the CRIS
    prefix tells the route. ``global.`` is the no-premium Global rate. ``us.``
    is US Geo CRIS. Anything else (a bare ``openai.*`` ID, an ARN, an unknown
    Geo prefix such as ``in.`` or ``us-gov.``) is priced at the premium ``us``
    rate, the higher of the two Standard-tier routes configured here, with a
    WARNING. GovCloud Geo rates on the cards are higher still and are not
    configured (override via BEDROCK_PRICING_RATES_JSON).
    """
    tail = model_lower.rsplit("/", 1)[-1]
    if tail.startswith("global."):
        return "global"
    return "us"


def _resolve_openai_family(model_id, model_lower):
    for match, stem in OPENAI_MODEL_FAMILIES:
        if match in model_lower:
            route = _openai_route(model_lower)
            tail = model_lower.rsplit("/", 1)[-1]
            if route == "us" and not tail.startswith("us."):
                _warn_once(
                    "openai_route_unknown",
                    model_id,
                    {
                        "metered_family": f"{stem}-us",
                        "detail": "OpenAI model ID without a global./us. CRIS prefix; metered at the premium US Geo rate",
                    },
                )
            return f"{stem}-{route}"
    _warn_once(
        "unpriced_openai_model",
        model_id,
        {
            "metered_family": OPENAI_UNPRICED_FAMILY,
            "detail": (
                "no published Bedrock rate is configured for this OpenAI model; metering at the highest "
                "configured Standard-tier OpenAI rate (GPT-6 Astra, US Geo). Add the model card rate to "
                "OPENAI_RATES or override 'openai-unpriced' via BEDROCK_PRICING_RATES_JSON."
            ),
        },
    )
    return OPENAI_UNPRICED_FAMILY


def get_rates() -> dict:
    """Get pricing rates, with optional env var override.

    Override format (BEDROCK_PRICING_RATES_JSON):
    {"sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}}

    Unknown family keys are accepted — this is how an admin prices a model in
    public extended access whose uplift arrived via AWS Health (e.g.
    {"sonnet-4-extended": {"input": 6.00, ...}}; see LEGACY_MODEL_PRICING).
    """
    override = os.environ.get("BEDROCK_PRICING_RATES_JSON", "").strip()
    if override:
        try:
            custom = json.loads(override)
            merged = {k: dict(v) for k, v in DEFAULT_RATES.items()}
            for family, rates in custom.items():
                if family in merged:
                    merged[family].update(rates)
                else:
                    merged[family] = rates
            return merged
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    return {k: dict(v) for k, v in DEFAULT_RATES.items()}


def resolve_model_family(model_id: str, now=None, rates: dict | None = None) -> str:
    """Map a CRIS model ID to its pricing family.

    Lifecycle-tracked models (LEGACY_MODEL_PRICING) resolve first and are
    date-aware: past their public-extended-access date they switch to the
    model's extended rate family when a verified/override rate exists, else
    they stay on the standard family and emit a structured warning (never a
    silent under-meter, never an invented number).

    Examples:
        "us.anthropic.claude-sonnet-4-6-v1" → "sonnet"
        "global.anthropic.claude-opus-4-7"  → "opus"
        "eu.anthropic.claude-haiku-4-5-..."  → "haiku"
        "us.anthropic.claude-fable-5"       -> "fable"
        "us.anthropic.claude-opus-4-1-20250805-v1:0" -> "opus-legacy"
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0" -> "sonnet-3-5-extended"
        "arn:...:application-inference-profile/team" -> "unpriced_aip"

    Args:
        model_id: Bedrock model / inference-profile ID.
        now: Optional datetime/date/ISO-string for date-aware selection
            (defaults to UTC now).
        rates: Optional effective rates dict (defaults to get_rates()) —
            consulted to decide whether an extended-access rate exists.
    """
    model_lower = model_id.lower()
    if ":application-inference-profile/" in model_lower:
        return "unpriced_aip"
    if "openai." in model_lower:
        return _resolve_openai_family(model_id, model_lower)
    for entry in LEGACY_MODEL_PRICING:
        if entry["match"] in model_lower:
            if rates is None:
                rates = get_rates()
            return _resolve_legacy_family(entry, model_id, now, rates)
    if "fable" in model_lower:
        return "fable"
    if "opus" in model_lower:
        return "opus"
    if "haiku" in model_lower:
        return "haiku"
    if "sonnet" in model_lower:
        return "sonnet"
    return DEFAULT_FAMILY


def calculate_cost(
    input_tokens: float,
    output_tokens: float,
    cache_read_tokens: float,
    cache_write_tokens: float = 0,
    model_family: str = DEFAULT_FAMILY,
    rates: dict | None = None,
) -> float:
    """Calculate estimated cost in USD from token counts.

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        cache_read_tokens: Number of cache read tokens
        cache_write_tokens: Number of cache write tokens (often 0 if not tracked)
        model_family: "fable", "opus", "sonnet", or "haiku"
        rates: Pricing rates dict (defaults to get_rates())

    Returns:
        Estimated cost in USD
    """
    if rates is None:
        rates = get_rates()

    family_rates = rates.get(model_family, rates.get(DEFAULT_FAMILY, {}))

    cost = (
        (input_tokens / 1_000_000) * family_rates.get("input", 3.0)
        + (output_tokens / 1_000_000) * family_rates.get("output", 15.0)
        + (cache_read_tokens / 1_000_000) * family_rates.get("cache_read", 0.3)
        + (cache_write_tokens / 1_000_000) * family_rates.get("cache_write", 3.75)
    )
    return cost


def calculate_cache_savings(
    input_tokens: float,
    output_tokens: float,
    cache_read_tokens: float,
    cache_write_tokens: float = 0,
    model_family: str = DEFAULT_FAMILY,
    rates: dict | None = None,
) -> dict:
    """Calculate estimated USD saved by prompt caching vs. no caching.

    The comparison baseline is the same workload with caching disabled:
    every cached token (read or write) would instead be billed at the
    family's regular input rate.

        hypothetical_cost = (input + cache_read + cache_write) * input_rate
                            + output * output_rate
        actual_cost       = input * input_rate
                            + cache_read * cache_read_rate
                            + cache_write * cache_write_rate
                            + output * output_rate
        savings_usd       = hypothetical_cost - actual_cost
                          = cache_read * (input_rate - cache_read_rate)
                            - cache_write * (cache_write_rate - input_rate)

    Savings CAN be negative: cache writes are billed at a premium over the
    input rate (e.g. Sonnet $3.75 vs $3.00 per MTok), so a write-heavy
    workload with low cache reuse costs MORE than not caching at all.
    Negative savings is an honest signal of poor cache configuration
    (prompts churning too often to be re-read).

    Args:
        input_tokens: Number of regular (uncached) input tokens
        output_tokens: Number of output tokens
        cache_read_tokens: Number of tokens read from cache
        cache_write_tokens: Number of tokens written to cache
        model_family: "fable", "opus", "sonnet", or "haiku"
        rates: Pricing rates dict (defaults to get_rates())

    Returns:
        Dict with:
            savings_usd: hypothetical_cost - actual_cost (may be negative)
            hypothetical_cost: Estimated cost without caching (USD)
            actual_cost: Estimated cost with caching (USD)
            cache_hit_rate: cache_read / (input + cache_read), 0.0 when
                there are no input-side tokens (div-by-zero guard)
    """
    if rates is None:
        rates = get_rates()

    family_rates = rates.get(model_family, rates.get(DEFAULT_FAMILY, {}))
    input_rate = family_rates.get("input", 3.0)
    output_rate = family_rates.get("output", 15.0)

    hypothetical_cost = ((input_tokens + cache_read_tokens + cache_write_tokens) / 1_000_000) * input_rate + (
        output_tokens / 1_000_000
    ) * output_rate

    actual_cost = calculate_cost(
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        model_family,
        rates,
    )

    denominator = input_tokens + cache_read_tokens
    cache_hit_rate = (cache_read_tokens / denominator) if denominator > 0 else 0.0

    return {
        "savings_usd": hypothetical_cost - actual_cost,
        "hypothetical_cost": hypothetical_cost,
        "actual_cost": actual_cost,
        "cache_hit_rate": cache_hit_rate,
    }
