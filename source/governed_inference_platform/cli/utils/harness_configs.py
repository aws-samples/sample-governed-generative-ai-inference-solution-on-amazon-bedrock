# ABOUTME: Pure generators for per-harness Bedrock config files (OpenCode, ChatGPT Work/Codex, Pi, Aider).
# ABOUTME: Every harness reuses the same AWS profile whose credential_process handles SSO/quota.

"""Config generators for coding harnesses other than Claude Code.

The installer writes an AWS profile into ``~/.aws/config`` whose
``credential_process`` points at the solution's credential-process binary
(see ``package.py``). Any harness that resolves credentials through the AWS
SDK default chain therefore inherits the same OIDC SSO login, quota
enforcement, session-name cost attribution, and IAM model/region guardrails
with zero harness-specific auth work. These generators only produce the
per-harness *provider* configuration (profile + region + model IDs).

Config formats verified against vendor docs (URLs cited per generator).
"""

import json
from dataclasses import dataclass

from governed_inference_platform.cli.utils.cowork_3p import WEBSEARCH_MCP_SERVER_NAME

# Harness machine names accepted by `gip package --harnesses`.
# "claude-code" is always packaged and never produces a harnesses/ entry.
CLAUDE_CODE_HARNESS = "claude-code"

# Install-time placeholder for the credential-process binary path inside
# generated MCP entries. Resolved by install.sh / install.bat to the per-OS
# absolute path (same pattern as __CREDENTIAL_PROCESS_PATH__ in settings.json).
CREDENTIAL_PROCESS_PLACEHOLDER = "__CREDENTIAL_PROCESS_PATH__"


@dataclass(frozen=True)
class McpProxyConfig:
    """Gateway MCP wiring for harnesses that need the stdio shim (R10 §4.2).

    ``credential_process_path`` is usually ``CREDENTIAL_PROCESS_PLACEHOLDER``
    so the installer can substitute the per-OS absolute binary path; an
    explicit absolute path is used verbatim. The generators stay pure: the
    caller (package.py) resolves the gateway URL and applies the gating
    (web_search_enabled + OIDC-only) before constructing this.
    """

    gateway_url: str
    credential_process_path: str = CREDENTIAL_PROCESS_PLACEHOLDER


@dataclass(frozen=True)
class HarnessConfig:
    """A generated config file plus its setup instructions for one harness."""

    harness: str  # machine name, e.g. "opencode"
    display_name: str
    filename: str  # file name written under harnesses/<harness>/
    content: str
    setup_instructions: str  # markdown, assembled into harnesses/README.md
    source_url: str
    # Additional (filename, content) pairs written next to ``filename``.
    extra_files: tuple[tuple[str, str], ...] = ()


def _default_model(model_ids: dict[str, str]) -> str | None:
    """Pick the default model: sonnet tier if resolved, else any tier."""
    if "sonnet" in model_ids:
        return model_ids["sonnet"]
    for tier in ("opus", "haiku"):
        if tier in model_ids:
            return model_ids[tier]
    return next(iter(model_ids.values()), None)


def generate_opencode_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate opencode.json for OpenCode's amazon-bedrock provider.

    Format source: https://opencode.ai/docs/providers/#amazon-bedrock
    - Provider key is "amazon-bedrock"; options.region / options.profile select
      the AWS region and named profile (resolved via the AWS SDK chain, which
      invokes our credential_process).
    - Models are declared under provider.amazon-bedrock.models keyed by model
      ID; CRIS inference-profile IDs (e.g. us.anthropic.claude-sonnet-...) are
      used directly as the model key. Top-level "model" selects the default as
      "<provider>/<model-id>".

    MCP (web search): OpenCode's ``type:"remote"`` supports only static headers
    (``{env:VAR}`` interpolation, no header command), so a bearer entry dies at
    the ~1h id_token expiry — and its automatic OAuth-on-401 opens a browser
    against a gateway that is not an OAuth AS. When ``mcp_proxy`` is set, emit
    a ``type:"local"`` (stdio) entry through the credential-process
    ``--mcp-proxy`` shim instead, which injects a fresh Authorization header
    per request (format source: https://opencode.ai/docs/mcp-servers/).
    """
    source_url = "https://opencode.ai/docs/providers/#amazon-bedrock"
    models = {
        model_id: {"name": f"Claude ({tier} tier, Bedrock cross-region inference)"}
        for tier, model_id in model_ids.items()
    }
    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "amazon-bedrock": {
                "options": {
                    "region": aws_region,
                    "profile": aws_profile_name,
                },
                "models": models,
            }
        },
    }
    default = _default_model(model_ids)
    if default:
        config["model"] = f"amazon-bedrock/{default}"

    mcp_section = ""
    if mcp_proxy:
        config["mcp"] = {
            WEBSEARCH_MCP_SERVER_NAME: {
                "type": "local",
                "command": [
                    mcp_proxy.credential_process_path,
                    "--profile",
                    aws_profile_name,
                    "--mcp-proxy",
                    mcp_proxy.gateway_url,
                ],
                "enabled": True,
            }
        }
        mcp_section = f"""
**Web search (MCP):** the config includes an `mcp.{WEBSEARCH_MCP_SERVER_NAME}` entry that
runs the credential-process binary in `--mcp-proxy` mode — a local stdio MCP server that
forwards to the AgentCore gateway with a fresh Authorization header per request (no ~1h
token expiry). The installer resolves the `{CREDENTIAL_PROCESS_PLACEHOLDER}` placeholder to the
installed binary path; if you copied this file before running the installer, replace it
manually (e.g. `~/gip/credential-process`). Do NOT convert the entry
to `type:"remote"`: OpenCode's remote headers are static, and on token expiry its
automatic OAuth kicks in, opens a browser against the gateway, and fails.
"""

    instructions = f"""### OpenCode

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile in `~/.aws/config`).
2. Copy `~/gip/harnesses/opencode/opencode.json` into your project root (or merge it into
   `~/.config/opencode/opencode.json` for a global default).
3. Launch `opencode`. The AWS SDK resolves the `{aws_profile_name}` profile, which triggers
   the credential-process binary: browser SSO on first use, silent refresh afterwards.

The `provider.amazon-bedrock.options.profile` and `options.region` keys pin the harness to
the managed profile and region. Model entries use the same Bedrock CRIS inference-profile
IDs that Claude Code receives.
{mcp_section}"""
    return HarnessConfig(
        harness="opencode",
        display_name="OpenCode",
        filename="opencode.json",
        content=json.dumps(config, indent=2) + "\n",
        setup_instructions=instructions,
        source_url=source_url,
    )


# ChatGPT Work / Codex on Bedrock Runtime (ADR-0037). Source:
# https://learn.chatgpt.com/docs/amazon-bedrock (retrieved 2026-10-02):
# `amazon-bedrock-runtime` calls https://bedrock-runtime.{region}.amazonaws.com/openai/v1
# with CRIS inference-profile IDs; `amazon-bedrock` selects Mantle, which the
# governed IAM policy denies (no invocation logs, ADR-0022).
CODEX_RUNTIME_PROVIDER = "amazon-bedrock-runtime"
CODEX_DEFAULT_OPENAI_MODEL = "openai.gpt-6-sol"

# Admin-enforced requirements (requirements.toml). Schema sources, retrieved 2026-10-02:
# https://learn.chatgpt.com/docs/enterprise/managed-configuration ("Admin-enforced
# requirements", "Manage authentication locally", "Pin feature flags") and the
# requirements.toml section of https://learn.chatgpt.com/docs/config-file/config-reference
# (`model_provider`: "Enforce the model provider ID, overriding local and session
# configuration"; `allowed_login_methods`: "`api` permits API authentication, including
# Amazon Bedrock"; `allowed_web_search_modes = []` "effectively allows only disabled";
# `features.in_app_updates`: "Set to false in requirements.toml to disable in-app
# updates"). Codex CLI 0.160.0's app-server schema (ConfigRequirements) carries the
# same four keys: modelProvider, allowedLoginMethods, allowedWebSearchModes,
# featureRequirements.
CODEX_REQUIREMENTS_TOML = f"""# Admin-enforced requirements for ChatGPT Work and Codex on Amazon Bedrock Runtime.
# OPTIONAL and for administrators only: users cannot override these values.
# This file is NOT read from ~/gip or ~/.codex. Deploy it to one of:
#   macOS / Linux: /etc/codex/requirements.toml
#   Windows:       %ProgramData%\\OpenAI\\Codex\\requirements.toml
#   macOS MDM:     preference domain com.openai.codex, key requirements_toml_base64
#                  (base64 of this file, no line wrapping)
# Schema: https://learn.chatgpt.com/docs/enterprise/managed-configuration
#         https://learn.chatgpt.com/docs/config-file/config-reference (requirements.toml)
#
# This is a client-side control. The security boundary stays in IAM: the governed
# role denies bedrock-mantle and admits OpenAI models only with AllowOpenAIModels=true.

# Pin the Bedrock Runtime provider. Selecting `amazon-bedrock` (Mantle) is denied by
# IAM anyway, because Mantle writes no invocation logs and cannot be metered.
model_provider = "{CODEX_RUNTIME_PROVIDER}"

# API authentication only (covers Amazon Bedrock). Blocks ChatGPT-account sign-in.
# Honored only in the local system file or macOS MDM, not in cloud-managed policy.
allowed_login_methods = ["api"]

# Hosted web search is not available on Bedrock Runtime. An empty list allows only
# "disabled".
allowed_web_search_modes = []

[features]
# Disables the ChatGPT desktop app's own updater. Ship app updates with your
# software deployment tool instead, or delete this line to keep in-app updates.
in_app_updates = false
"""


def codex_runtime_model(aws_region: str) -> str | None:
    """Default ChatGPT Work / Codex model ID for a Bedrock source Region.

    The GPT-5.x/6.x OpenAI models on bedrock-runtime are CRIS-only (no in-Region
    IDs; the GPT-OSS models differ). US source Regions get the US Geo profile
    (``us.``), which keeps inference in the US; every other commercial Region
    gets the Global profile (``global.``), which can route worldwide (the GPT-6 Sol model card lists eu-* source Regions as
    Global-only; ca-* also list US Geo, which the generator does not select).
    Returns None for AWS GovCloud, where the default model has no profile (some
    models list a ``us-gov.`` Geo profile instead).
    Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.html
    (retrieved 2026-10-02).
    """
    if aws_region.startswith("us-gov-"):
        return None
    if aws_region.startswith("us-"):
        return f"us.{CODEX_DEFAULT_OPENAI_MODEL}"
    return f"global.{CODEX_DEFAULT_OPENAI_MODEL}"


def generate_codex_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate ~/.codex/config.toml (+ ~/.codex/.env) for ChatGPT Work and Codex.

    Format source: https://learn.chatgpt.com/docs/amazon-bedrock (retrieved 2026-10-02)
    - ``model_provider = "amazon-bedrock-runtime"`` selects the Bedrock Runtime
      endpoint (CRIS profiles, invocation logs). The ChatGPT desktop app, Codex
      CLI, IDE extension and SDK read the same local configuration layers.
    - ``web_search = "disabled"``: hosted web search is not available on
      Runtime (the same page).
    - ``[model_providers.amazon-bedrock-runtime.aws] profile/region`` pins the
      AWS profile whose credential_process is the solution's binary. The
      config reference documents these keys for ``amazon-bedrock``; Codex CLI
      0.160.0 honored them for ``amazon-bedrock-runtime`` in the 2026-10-02
      live test (docs/LIVE_VALIDATION.md LV-18).
    - The desktop app and IDE extension may not inherit shell variables, so
      the same values also go into ``~/.codex/.env`` (extra file ``codex.env``).

    MCP (web search): Codex's native streamable-HTTP auth
    (``bearer_token_env_var`` / ``http_headers`` / ``env_http_headers``) is
    static for the process lifetime, so sessions longer than the ~1h id_token
    TTL lose the server. When ``mcp_proxy`` is set, emit a stdio
    ``[mcp_servers.<id>]`` entry through the credential-process ``--mcp-proxy``
    shim instead (format source: https://developers.openai.com/codex/config-reference).
    """
    source_url = "https://learn.chatgpt.com/docs/amazon-bedrock"
    # model_ids (Claude tiers) intentionally unused: ChatGPT Work / Codex on
    # Bedrock run OpenAI models only (see source_url, "Supported models").
    _ = model_ids
    model = codex_runtime_model(aws_region)
    if model is None:
        model_line = "# model = <set an OpenAI inference-profile ID available in this Region>"
        residency_note = (
            "# The default model (GPT-6 Sol) has no AWS GovCloud profile. Set `model` to a\n"
            "# us-gov.openai.* profile listed on its model card after confirming availability.\n"
        )
    else:
        model_line = f'model = "{model}"'
        residency_note = ""
        if model.startswith("global."):
            residency_note = (
                f"# DATA RESIDENCY: from {aws_region} this default uses Global cross-Region\n"
                f"# inference, which can route requests to commercial AWS Regions worldwide.\n"
                f"# Confirm this meets your residency policy, or set a Geo profile from the model card.\n"
            )
    content = f"""# ChatGPT Work and Codex (desktop app, CLI, IDE extension) on Amazon Bedrock Runtime.
# Merge into ~/.codex/config.toml. Do not use the `amazon-bedrock` (Mantle) provider:
# the governed IAM policy denies bedrock-mantle because Mantle writes no invocation
# logs, so its usage could not be metered (ADR-0022, ADR-0037).
# Format source: {source_url}
#
# Authentication: the "{aws_profile_name}" AWS profile's credential_process handles
# OIDC SSO login, quota enforcement, and per-user session-name attribution.
# Make sure AWS_BEARER_TOKEN_BEDROCK is NOT set: Codex checks a Bedrock API key
# before the AWS SDK credential chain.

model_provider = "{CODEX_RUNTIME_PROVIDER}"
web_search = "disabled"
{residency_note}{model_line}

[model_providers.{CODEX_RUNTIME_PROVIDER}.aws]
profile = "{aws_profile_name}"
region = "{aws_region}"
"""
    mcp_instructions = ""
    if mcp_proxy:
        args = json.dumps(["--profile", aws_profile_name, "--mcp-proxy", mcp_proxy.gateway_url])
        content += f"""
# Web search via the AgentCore gateway (MCP, stdio shim). The credential-process
# binary proxies stdio<->streamable-HTTP and injects a fresh Authorization
# header per request — Codex's own header options (bearer_token_env_var,
# http_headers) are static for the process lifetime and die at token expiry.
# The installer resolves {CREDENTIAL_PROCESS_PLACEHOLDER} to the installed
# binary path; replace it manually if you copied this file first.
[mcp_servers.{WEBSEARCH_MCP_SERVER_NAME}]
command = "{mcp_proxy.credential_process_path}"
args = {args}
"""
        mcp_instructions = f"""
**Web search (MCP):** the snippet registers `{WEBSEARCH_MCP_SERVER_NAME}` as a stdio MCP
server through the credential-process `--mcp-proxy` shim, so the gateway bearer token is
refreshed per request (Codex's native `bearer_token_env_var`/`http_headers` are static and
break after the ~1h token TTL). Requires the solution installer to have resolved
`{CREDENTIAL_PROCESS_PLACEHOLDER}` in this file. Hosted web search stays disabled on
Bedrock Runtime.
"""
    env_content = f"""# Merge into ~/.codex/.env (%USERPROFILE%\\.codex\\.env on Windows), then restart
# the ChatGPT desktop app or IDE extension. They may not inherit shell variables.
# Format source: {source_url} ("Desktop app and IDE extension")
AWS_PROFILE={aws_profile_name}
AWS_REGION={aws_region}
"""
    residency_md = ""
    if model and model.startswith("global."):
        residency_md = (
            f"\n**Data residency:** from `{aws_region}` the default model `{model}` uses Global "
            "cross-Region inference, which can route requests to commercial AWS Regions worldwide. "
            "Set a Geo profile from the model card instead if one is listed for this Region.\n"
        )
    instructions = f"""### ChatGPT Work and Codex (desktop app, CLI, IDE extension)

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Merge `~/gip/harnesses/codex/config.toml` into `~/.codex/config.toml`.
3. For the ChatGPT desktop app or IDE extension, also merge
   `~/gip/harnesses/codex/codex.env` into `~/.codex/.env` and restart the app.
4. Confirm `/status` in Codex CLI shows provider `{CODEX_RUNTIME_PROVIDER}`, then send
   `Reply with exactly: bedrock-ok`.

**Administrators (optional):** `~/gip/harnesses/codex/requirements.toml` enforces
`model_provider = "{CODEX_RUNTIME_PROVIDER}"`, API-only login, no hosted web search and
no in-app updates. Users cannot override it. Deploy it to `/etc/codex/requirements.toml`
(macOS, Linux), `%ProgramData%\\OpenAI\\Codex\\requirements.toml` (Windows) or the
macOS MDM key `com.openai.codex` / `requirements_toml_base64`. It is a client-side
control; IAM remains the boundary.

**Requires the deployment to admit OpenAI models:** `allow_openai_models: true`
(auth stack parameter `AllowOpenAIModels=true`). Without it the IAM policy denies
OpenAI models. The `amazon-bedrock` (Mantle) provider is always denied. Budgets only
count ChatGPT Work / Codex usage with server-side metering in `max` mode
(`metering.enabled: true`, `metering.mode: max`), because Codex does not emit
Claude Code telemetry.
{residency_md}{mcp_instructions}"""
    return HarnessConfig(
        harness="codex",
        display_name="ChatGPT Work / Codex",
        filename="config.toml",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
        extra_files=(("codex.env", env_content), ("requirements.toml", CODEX_REQUIREMENTS_TOML)),
    )


def generate_aider_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate .aider.conf.yml for Aider on Bedrock.

    Format source: https://aider.chat/docs/llms/bedrock.html
    - Models use LiteLLM's "bedrock/<model-id>" naming; CRIS inference-profile
      IDs are passed as bedrock/us.anthropic.claude-... (the docs call out
      that inference-profile-only models REQUIRE the Inference Profile ID).
    - AWS auth via environment: AWS_PROFILE + AWS_REGION (also supported in a
      .env file per the same page). boto3 resolves the profile, which invokes
      our credential_process.

    MCP (web search): skipped — Aider has no MCP client support (nothing in
    the docs/options reference; upstream request Aider-AI/aider#2525 open
    since 2024-12). ``mcp_proxy`` is accepted for registry-signature parity
    and ignored.
    """
    _ = mcp_proxy
    source_url = "https://aider.chat/docs/llms/bedrock.html"
    default = _default_model(model_ids)
    model_line = f"model: bedrock/{default}" if default else "# model: bedrock/<cris-inference-profile-id>"
    content = f"""# Aider on Amazon Bedrock via LiteLLM "bedrock/<model-id>" naming.
# Place as .aider.conf.yml in your project root or home directory.
# Format source: {source_url}
#
# Authentication comes from the AWS SDK (boto3) credential chain. Export:
#   export AWS_PROFILE={aws_profile_name}
#   export AWS_REGION={aws_region}
# or put the same two lines (without 'export') in an .env file next to
# this config. The "{aws_profile_name}" profile's credential_process handles
# OIDC SSO login, quota enforcement, and session-name cost attribution.

{model_line}
"""
    tier_lines = "\n".join(
        f"# aider --model bedrock/{model_id}   # {tier} tier" for tier, model_id in model_ids.items()
    )
    if tier_lines:
        content += f"""
# Other tiers (pass via --model to override):
{tier_lines}
"""
    instructions = f"""### Aider

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Copy `~/gip/harnesses/aider/.aider.conf.yml` into your project root (or `~/`).
3. Point boto3 at the managed profile — either shell exports:

   ```bash
   export AWS_PROFILE={aws_profile_name}
   export AWS_REGION={aws_region}
   aider
   ```

   or the env-var alternative, a `.env` file in the project root:

   ```
   AWS_PROFILE={aws_profile_name}
   AWS_REGION={aws_region}
   ```

Models use LiteLLM `bedrock/<model-id>` naming with the same CRIS inference-profile IDs as
Claude Code (e.g. `bedrock/{default or "us.anthropic.claude-..."}`). Aider may prompt you to
`pip install boto3` on first Bedrock use.

**Web search (MCP):** not available — Aider has no MCP support (upstream request
Aider-AI/aider#2525 is still open), so the AgentCore web-search gateway cannot be wired here.
"""
    return HarnessConfig(
        harness="aider",
        display_name="Aider",
        filename=".aider.conf.yml",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
    )


def generate_pi_config(
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
) -> HarnessConfig:
    """Generate an env/launcher snippet for Pi on Bedrock.

    Format source: https://pi.dev/docs/latest/providers#amazon-bedrock
    - Pi has no dedicated Bedrock config file: auth is "Option 1: AWS Profile"
      via AWS_PROFILE (plus optional AWS_REGION, default us-east-1), and the
      provider/model are selected on the command line:
        pi --provider amazon-bedrock --model us.anthropic.claude-...-v1:0
    - CRIS (system-defined) inference-profile IDs are passed directly as the
      --model value; prompt caching is auto-enabled for recognizable Claude IDs.

    MCP (web search): skipped — Pi has no MCP by design (official README: "No
    MCP. Build CLI tools with READMEs (see Skills), or build an extension that
    adds MCP support."). ``mcp_proxy`` is accepted for registry-signature
    parity and ignored.
    """
    _ = mcp_proxy
    source_url = "https://pi.dev/docs/latest/providers#amazon-bedrock"
    default = _default_model(model_ids)
    launch_lines = "\n".join(
        f"#   pi --provider amazon-bedrock --model {model_id}   # {tier} tier" for tier, model_id in model_ids.items()
    )
    content = f"""# Pi on Amazon Bedrock — environment + launch reference.
# Source this file (or add the exports to your shell profile).
# Format source: {source_url}
#
# Pi has no Bedrock config file: credentials come from the AWS SDK chain
# ("Option 1: AWS Profile" in the docs) and the model is a CLI flag.
# The "{aws_profile_name}" profile's credential_process handles OIDC SSO login,
# quota enforcement, and session-name cost attribution.

export AWS_PROFILE={aws_profile_name}
export AWS_REGION={aws_region}

# Launch with a Bedrock CRIS inference-profile ID:
{launch_lines or "#   pi --provider amazon-bedrock --model <cris-inference-profile-id>"}
"""
    instructions = f"""### Pi

Config format: [{source_url}]({source_url})

1. Run the solution installer first (it creates the `{aws_profile_name}` AWS profile).
2. Source `~/gip/harnesses/pi/pi-bedrock.env` (or add its exports to your shell profile).
3. Launch Pi with the provider and model flags:

   ```bash
   pi --provider amazon-bedrock --model {default or "<cris-inference-profile-id>"}
   ```

Pi has no dedicated Bedrock config file — it reads `AWS_PROFILE`/`AWS_REGION` from the
environment ("Option 1: AWS Profile" in its docs) and takes the model as a CLI flag. CRIS
inference-profile IDs are passed directly; Pi auto-enables prompt caching for Claude model
IDs it recognizes.

**Web search (MCP):** not available — Pi has no MCP support by design (its README points to
extensions instead); users of the community `pi-mcp-adapter` extension can reuse the stdio
shim entry documented in `assets/docs/HARNESSES.md` (Platform tools section).
"""
    return HarnessConfig(
        harness="pi",
        display_name="Pi",
        filename="pi-bedrock.env",
        content=content,
        setup_instructions=instructions,
        source_url=source_url,
    )


# Registry: machine name -> generator. Order defines output order.
HARNESS_GENERATORS = {
    "opencode": generate_opencode_config,
    "codex": generate_codex_config,
    "pi": generate_pi_config,
    "aider": generate_aider_config,
}


def resolve_harness_selection(selection: str | None) -> list[str]:
    """Parse a --harnesses value into the list of EXTRA harnesses to generate.

    "claude-code" is always implicit and never returned (it is the default
    package output). "all" expands to every known extra harness. Unknown
    names raise ValueError.
    """
    if not selection:
        return []
    names: list[str] = []
    for raw in selection.split(","):
        name = raw.strip().lower()
        if not name or name == CLAUDE_CODE_HARNESS:
            continue
        if name == "all":
            for known in HARNESS_GENERATORS:
                if known not in names:
                    names.append(known)
            continue
        if name not in HARNESS_GENERATORS:
            valid = ", ".join([CLAUDE_CODE_HARNESS, *HARNESS_GENERATORS, "all"])
            raise ValueError(f"Unknown harness '{name}'. Valid values: {valid}")
        if name not in names:
            names.append(name)
    return names


def generate_harness_configs(
    harnesses: list[str],
    aws_profile_name: str,
    aws_region: str,
    model_ids: dict[str, str],
    mcp_proxy: McpProxyConfig | None = None,
    compatibility_model_id: str | None = None,
) -> list[HarnessConfig]:
    """Generate configs for the given extra harnesses (registry order).

    ``mcp_proxy`` (already gated by the caller: web search enabled, OIDC auth,
    URL resolved) threads the gateway MCP wiring into the generators that
    support it (OpenCode/Codex); Pi/Aider ignore it (no MCP support upstream).
    ``compatibility_model_id`` overrides the Sonnet model for Aider and Pi.
    """
    configs = []
    for name, generator in HARNESS_GENERATORS.items():
        if name not in harnesses:
            continue
        harness_models = model_ids
        # F-022/F-025: Current Aider and Pi releases need a compatible fallback.
        if name in {"aider", "pi"} and compatibility_model_id:
            harness_models = {**model_ids, "sonnet": compatibility_model_id}
        configs.append(
            generator(
                aws_profile_name,
                aws_region,
                harness_models,
                mcp_proxy=mcp_proxy,
            )
        )
    return configs


def build_harnesses_readme(
    configs: list[HarnessConfig], aws_profile_name: str, aws_region: str, model_ids: dict[str, str]
) -> str:
    """Assemble harnesses/README.md from per-harness setup instructions."""
    model_rows = "\n".join(f"| {tier} | `{model_id}` |" for tier, model_id in model_ids.items())
    sections = "\n---\n\n".join(cfg.setup_instructions.strip() + "\n" for cfg in configs)
    return f"""# Using Other Coding Harnesses with This Deployment

Every config in this folder points its harness at the **`{aws_profile_name}`** AWS profile
(region `{aws_region}`) that the installer writes into `~/.aws/config`. That profile's
`credential_process` is this solution's credential-process binary, so ANY harness that
resolves credentials through the AWS SDK default chain gets, with no harness-specific
auth work:

The installer places resolved, customer-usable copies under `~/gip/harnesses`
(`%USERPROFILE%\\gip\\harnesses` on Windows). Use those installed copies rather than
the package-source templates.

| Guarantee | How |
|---|---|
| **SSO login** | Same OIDC browser flow + cached/refreshed tokens as Claude Code |
| **Quota enforcement** | Quota is checked inside credential-process *before* credentials are vended; blocked users get no credentials at all |
| **Per-user cost attribution** | STS session name embeds the user identity; costs appear per user in CUR (`line_item_iam_principal`) |
| **Model/region guardrails** | IAM policy on the assumed role (Anthropic-model allow-list, region conditions) applies to every Bedrock call |

Model IDs are the same Bedrock cross-region inference (CRIS) profile IDs that the
packaged Claude Code settings use:

| Tier | Model ID |
|---|---|
{model_rows}

**What does NOT carry over:** Claude Code's OTEL telemetry dashboards, the managed-settings
default model and optional model restriction (`availableModels`), and the Claude apps gateway are
Claude-Code-only. Other harnesses can call any model the IAM allow-list permits. See
`assets/docs/HARNESSES.md` in the solution repository for the full capability matrix.

---

{sections}"""
