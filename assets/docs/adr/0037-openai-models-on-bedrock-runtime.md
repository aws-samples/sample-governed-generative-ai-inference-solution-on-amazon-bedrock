# ADR-0037: Admit OpenAI models on bedrock-runtime for ChatGPT Work and Codex; Mantle stays denied

Status: Accepted; live-verified in a sandbox · Date: 2026-10-02

## Context

ChatGPT Work and Codex (ChatGPT desktop app, Codex CLI, IDE extension) have two
Amazon Bedrock providers. `amazon-bedrock` calls Mantle
(`bedrock-mantle.{region}.api.aws/openai/v1`). `amazon-bedrock-runtime` calls
`bedrock-runtime.{region}.amazonaws.com/openai/v1` with cross-Region inference
(CRIS) profiles such as `global.openai.gpt-6-sol`, and authenticates through the
AWS SDK credential chain, `credential_process` included
(<https://learn.chatgpt.com/docs/amazon-bedrock>, retrieved 2026-10-02). Until
now the generated Codex config used Mantle, which every governed policy denies
(ADR-0022), and `RestrictToAnthropicModels=true` admits only `anthropic.*`.

Axiom A4 (ADR-0012) says no endpoint reachable by governed credentials may be
invisible to metering. The GPT-6 Sol model card lists invocation logs as
supported on bedrock-runtime
(<https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.html>,
retrieved 2026-10-02). The sandbox run confirmed it: each Codex call wrote a
metadata-only `ModelInvocationLog` record (`operation: Responses`) with the
caller's assumed-role ARN, `inputTokenCount`, `cacheReadInputTokenCount`,
`cacheWriteInputTokenCount` and `outputTokenCount`, in the source Region.

## Decision

1. New auth-stack parameter `AllowOpenAIModels` (default `'false'`) in all eight
   governed templates, profile field `allow_openai_models`. When `'true'`, two
   conditional statements grant `bedrock:InvokeModel` only, on exactly the
   resources a Responses API call (streaming included) authorizes. These came
   from a least-privilege probe role and the denial messages, not from guesses:
   `inference-profile/*.openai.*`, `foundation-model/openai.*`, the account's
   `project/default` (Regional, `aws:RequestedRegion`), and the region-less
   `::foundation-model/openai.*` that `global.*` profiles also evaluate.
2. `DenyBedrockMantleEndpoint` is unchanged and stays the first, unconditional
   statement. ADR-0022 is unchanged: Mantle still has no invocation logs.
3. The Codex harness generator emits `model_provider = "amazon-bedrock-runtime"`,
   `web_search = "disabled"`, `[model_providers.amazon-bedrock-runtime.aws]`
   profile/region, a Region-valid default model (`us.` from US source Regions,
   `global.` elsewhere, with a residency warning; none in GovCloud), and `codex.env` for the
   desktop app.
4. Budgets for ChatGPT Work users need server-side metering in `max` mode.
   Codex does not emit Claude Code OTEL metrics, so with `shadow` the enforced
   figure is client telemetry (zero). Verified live: same over-budget user,
   `shadow` issued credentials and `max` refused them.
5. Pricing families for GPT-6 Sol/Luna/Astra, GPT-6.1 Sol and GPT-5.6
   Sol/Terra/Luna, Global and US Geo, short and long context, from the model
   cards, Standard tier only. Unknown OpenAI IDs meter at the GPT-6 Astra US
   Geo rate (the highest Standard-tier rate configured) with a WARNING.

## Alternatives considered

- **Flip `RestrictToAnthropicModels=false`.** Rejected: admits every model and
  opaque application inference profiles, not just OpenAI on Runtime.
- **Lift the Mantle deny for Codex.** Rejected: Mantle usage is invisible to
  metering (ADR-0022, A4).
- **Make `requirements.toml` the model control.** Rejected. ChatGPT's managed
  configuration can enforce `model_provider` and a `model_catalog_json`
  (<https://learn.chatgpt.com/docs/config-file/config-reference>, requirements
  section, retrieved 2026-10-02), but it runs on the user's machine and is not a
  security boundary. The generator ships an optional admin `requirements.toml`
  (provider pin, `allowed_login_methods = ["api"]`,
  `allowed_web_search_modes = []`, `[features] in_app_updates = false`) as
  defence in depth; IAM remains the model control.

## Consequences

- From eu/au/jp source Regions the GPT-5.x/6.x models ChatGPT Work uses are
  reachable only through Global CRIS; the live `global.openai.gpt-6-sol` call from eu-central-1 ran in
  `us-east-1` (`inferenceRegion`). Invocation logs and quota records stay in the
  deployment's Regions.
- Any model matching `openai.*` is admitted when the flag is on (GPT-5.x and
  GPT-OSS IDs included); narrower pinning would need per-model ARNs.
- IAM Identity Center: credential-process returns the permission set's
  credentials and does not assume `BedrockIDCRole`, so the flag does not change
  what IdC users can call; a narrower permission set needs the same statements
  (`providers/iam-identity-center-setup.md`, not live-tested with IdC). The
  same holds for `DenyBedrockMantleEndpoint`: for IdC users Mantle is denied
  by what the permission set (and any SCP) allows or denies, so the guide adds
  the explicit deny to the permission set.
- ChatGPT-Work-only users with more than 1M tokens/month raise one
  `metering_drift` alert per month (client telemetry is structurally zero).
- The sandbox run also found and fixed a pre-existing metering bug: accrual had
  failed for every record (fixed by sending accruals through a plain DynamoDB
  client; regression test `TestTransactWireFormat` in
  `source/tests/test_metering_processor.py`).

## Evidence

`docs/LIVE_VALIDATION.md` LV-18; `source/tests/test_model_restriction_param.py`
(`TestAllowOpenAIModels`); `source/tests/test_mantle_fail_closed.py`;
`source/tests/test_pricing_openai.py`; `source/tests/test_metering_processor.py`.
