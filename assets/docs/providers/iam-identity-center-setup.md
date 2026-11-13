# AWS IAM Identity Center Setup Guide

This guide covers setting up the Governed Inference Platform using AWS IAM Identity Center (formerly AWS SSO) as the authentication method.

## How It Works

IDC uses your existing AWS SSO credentials — no external IdP or JWT tokens needed.

- **Authentication:** Developers run `aws sso login` — Claude Code picks up ambient AWS credentials automatically via the standard credential chain. When quota enforcement is enabled, the credential-process binary resolves the permission-set credentials instead, so the quota decision runs before credentials are returned.
- **Monitoring:** The local OTEL sidecar collector sends metrics to CloudWatch's native OTLP endpoint using the developer's SSO session credentials (SigV4). User identity is baked as a static resource attribute in the collector config at distribution time.
- **Attribution:** User email is resolved from the IAM Identity Center ARN session name (e.g. `user@company.com`) during `gip init` or `gip package` and embedded directly in the collector configuration.

The developer bundle depends on whether quota monitoring is enabled:

| Quota monitoring | Bundle contents | Quota enforcement |
|---|---|---|
| Disabled | Collector binary only — no credential-process, no otel-helper | None. Ambient SSO credentials reach Bedrock without a quota decision. |
| Enabled | Collector binary and credential-process | SigV4 quota check runs before credentials are returned. |

## When to Choose IAM Identity Center vs OIDC

### Choose IAM Identity Center (IDC) when:
- Your organization already uses AWS SSO/Identity Center for AWS access management
- You want to leverage existing AWS permission sets and user groups
- You need native AWS authentication without external identity providers
- You want simplified setup for AWS-first environments

### Choose OIDC when:
- Your organization uses external identity providers (Okta, Azure AD, etc.)
- You need JWT-based ALB authorization for the OTEL proxy
- You want token-based session management with refresh tokens

## What's NOT Supported with IAM Identity Center

⚠️ **Differences from OIDC:**

1. **Quota via SigV4**: Per-user quota enforcement works via IAM SigV4 authentication (not JWT). The quota Lambda takes the user from the session name of the IAM Identity Center role (`AWSReservedSSO_…`) that signs the check; a request signed by any other role or IAM user is denied. Requires the quota-monitoring stack to be deployed, and switches the developer bundle from collector-only to collector plus credential-process (see [How It Works](#how-it-works)).
2. **Per-User OTEL Attribution**: Works automatically — credential-process writes user email to the OTEL cache from the STS caller ARN. Requires email as the IDC session name (the default).
3. **No ALB JWT Authorization**: The OTEL proxy (central mode) cannot validate requests via JWT. Use sidecar mode, or accept unauthenticated OTEL ingestion in proxy mode.

## Prerequisites

Before starting, ensure you have:

- AWS IAM Identity Center enabled in your AWS account
- A permission set for developers (recommended name: `BedrockDeveloperAccess`). Its Bedrock access comes from the policy that `gip deploy` creates ([step 6](#6-attach-the-bedrock-access-policy-to-the-permission-set)).
- Users assigned to the permission set
- AWS CLI v2 installed and configured

## Step-by-Step Deployment

### 1. Initialize Configuration

Run the Claude Code setup wizard and select IAM Identity Center:

```bash
poetry run gip init
```

When prompted for authentication method, choose:
```
❯ AWS IAM Identity Center (SSO)
```

### 2. Provide Identity Center Details

You'll be prompted for:

- **Start URL**: Your Identity Center portal URL (e.g., `https://company.awsapps.com/start`)
- **SSO Region**: The AWS region where Identity Center is configured
- **Account ID**: Your 12-digit AWS account number
- **Permission Set**: The name of your permission set (default: `BedrockDeveloperAccess`)

### 3. AWS Configuration

The wizard will generate an AWS config block and offer to append it to `~/.aws/config`:

```ini
[profile gip]
sso_session = gip-MyPool
sso_account_id = 123456789012
sso_role_name = BedrockDeveloperAccess
region = us-east-1

[sso-session gip-MyPool]
sso_start_url = https://company.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
```

### 4. Authenticate with AWS SSO

Before deploying, authenticate using the AWS CLI:

```bash
aws sso login --profile gip
```

Verify your identity:

```bash
aws sts get-caller-identity --profile gip
```

### 5. Deploy Infrastructure

Deploy the Claude Code infrastructure:

```bash
poetry run gip deploy
```

This will:
- Deploy the quota monitoring stack when quota enforcement is enabled (skipped when it is not)
- Deploy the Bedrock access policy, `<identity_pool_name>-BedrockAccessPolicy` (the stack creates no IAM role: users call Bedrock with their permission set's credentials)
- Deploy monitoring and dashboard stacks (if enabled)

### 6. Attach the Bedrock access policy to the permission set

The auth stack's policy holds the model allow-list (`RestrictToAnthropicModels`, `allow_openai_models`), the Region scope (`allowed_bedrock_regions`), the Mantle deny, the Claude Desktop bearer-token statement and the monitoring statements. It reaches your users only when the permission set references it. Do this once, after the auth stack exists in every account the permission set is assigned to: IAM Identity Center fails to assign a permission set whose customer managed policy is missing in an account ([Use IAM policies in permission sets](https://docs.aws.amazon.com/singlesignon/latest/userguide/howtocmp.html)). `gip deploy auth` prints these commands with your policy name. Run them with credentials for the IAM Identity Center management or delegated administrator account:

```bash
aws sso-admin attach-customer-managed-policy-reference-to-permission-set \
  --instance-arn <INSTANCE_ARN> --permission-set-arn <PERMISSION_SET_ARN> \
  --customer-managed-policy-reference Name=<identity_pool_name>-BedrockAccessPolicy,Path=/
aws sso-admin provision-permission-set --instance-arn <INSTANCE_ARN> \
  --permission-set-arn <PERMISSION_SET_ARN> --target-type ALL_PROVISIONED_ACCOUNTS
```

Or, in the IAM Identity Center console, open the permission set, add the policy name under **Customer managed policies**, and re-provision it.

Then remove any broader Bedrock allow from the permission set, such as the `bedrock:InvokeModel*` on `*` example under [Permission Set Configuration](#permission-set-configuration). IAM allows what any attached policy allows, so a broader allow overrides the policy's model allow-list; the deny statements apply either way. Later `gip deploy auth` runs update the policy in place, and the permission set picks up the change without another step. This flow has not been live-tested.

## Extending SSO Session Duration

By default, AWS SSO sessions expire after 8-12 hours. To extend this:

1. **In AWS Console**: Go to IAM Identity Center → Settings → Session settings
2. **Update Session Duration**: Set to maximum allowed (up to 7 days for programmatic access)
3. **Apply to Permission Sets**: Ensure your permission set inherits these settings

Example session settings:
- **Programmatic access**: 7 days
- **AWS Management Console access**: 12 hours

## Per-User Cost Attribution

IDC users get per-user OTEL attribution automatically. The user email is baked into the collector configuration at distribution time (`gip package` resolves it from `aws sts get-caller-identity`). Dashboard widgets (Token Usage by User, Active Users) work without additional configuration.

**Requirement:** IAM Identity Center must use email as the session name (the default). The ARN format must be:
`arn:aws:sts::ACCOUNT:assumed-role/RoleName/user@company.com`

> **Note:** Unlike the OIDC path (which extracts identity dynamically from JWT tokens at runtime), the IDC path uses a static email set at distribution time. This works well for single-user machines. If a user's email changes, regenerate their package with `gip package`.

### Verifying Attribution

```bash
# Confirm email is in your ARN
aws sts get-caller-identity --profile gip
# Look for: "Arn": "arn:aws:sts::123456789012:assumed-role/.../user@company.com"

# Check the OTEL cache was written (after first credential-process invocation)
cat ~/.gip/otel-cache/*.json
# Should show: {"x-user-email": "user@company.com", ...}
```

If your ARN shows a session ID instead of email, update your IDC session name configuration in the IAM Identity Center console under Settings → Session settings.

For additional cost tracking via CloudTrail:

### 1. Enable CloudTrail

```yaml
# Add to your monitoring configuration
CloudTrailEnabled: true
CloudTrailS3Bucket: my-company-cloudtrail-bucket
```

### 2. Query CloudTrail Logs

Use Athena or CloudWatch Logs Insights to query Bedrock API calls:

```sql
-- Athena query for user-specific Bedrock usage
SELECT 
    useridentity.sessioncontext.sessionissuer.principalid as user_id,
    eventname,
    COUNT(*) as api_calls,
    DATE_TRUNC('day', eventtime) as date
FROM cloudtrail_logs
WHERE 
    eventsource = 'bedrock.amazonaws.com'
    AND eventname LIKE 'InvokeModel%'
    AND eventtime >= current_timestamp - interval '30' day
GROUP BY 1,2,4
ORDER BY date DESC, api_calls DESC;
```

### 3. Create Custom Dashboards

Use the user identity from CloudTrail to create cost allocation reports and usage dashboards.

## Troubleshooting

### Common Issues

1. **"Profile not found" errors**
   ```bash
   # Verify your profile exists
   aws configure list-profiles
   
   # Test SSO authentication
   aws sso login --profile gip
   ```

2. **"Access denied" for Bedrock**
   - Verify your permission set includes Bedrock permissions
   - Check that the deployed IAM role has the correct policies attached
   - Ensure you're using the correct AWS region for Bedrock access

3. **CloudFormation deployment failures**
   ```bash
   # Check stack status
   aws cloudformation describe-stacks --stack-name YourStackName
   
   # View stack events for errors
   aws cloudformation describe-stack-events --stack-name YourStackName
   ```

4. **SSO session expired**
   ```bash
   # Re-authenticate
   aws sso login --profile gip
   
   # Verify credentials are refreshed
   aws sts get-caller-identity --profile gip
   ```

### Bedrock Access Policy Not Applied

If users can call models the policy should deny, or can't call models it allows, check that the policy exists in the account and that the permission set references it:

```bash
# The policy the auth stack created (stack output PolicyArn)
aws iam get-policy --policy-arn <POLICY_ARN>

# In the IAM Identity Center administrator account: the permission set's references
aws sso-admin list-customer-managed-policy-references-in-permission-set \
  --instance-arn <INSTANCE_ARN> --permission-set-arn <PERMISSION_SET_ARN>
```

Earlier versions of `bedrock-auth-idc.yaml` also created a role (`BedrockIDCFederatedRole` by default) that nothing in this sample assumed. The next `gip deploy auth` deletes it.

### Permission Set Configuration

Use the customer managed policy from [step 6](#6-attach-the-bedrock-access-policy-to-the-permission-set). If you give the permission set an inline policy instead, it decides what users can call. A minimal one, with no model allow-list or Region scope, is:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel*",
                "bedrock:GetFoundationModel",
                "bedrock:ListFoundationModels"
            ],
            "Resource": "*"
        }
    ]
}
```

#### Sidecar telemetry and Claude Desktop

For IAM Identity Center profiles, credential-process returns the permission set's role credentials directly. The policy from [step 6](#6-attach-the-bedrock-access-policy-to-the-permission-set) carries the statements for sidecar telemetry and Claude Desktop (the OTLP statement only when `MonitoringMode=sidecar`). With an inline policy instead, add them to it:

- `AllowCloudWatchOtlpMetrics` lets the sidecar collector export OTLP metrics. Without it CloudWatch rejects the export with HTTP 403 (`not authorized to perform: cloudwatch:PutMetricData`). Add it only for sidecar monitoring: it lets the user write metrics under any identity (see [Quota Monitoring](../QUOTA_MONITORING.md#current-limitations)).
- `AllowBedrockShortTermBearerToken` lets Claude Desktop use the short-term Amazon Bedrock API key that `credential-process --desktop` returns.

They are the statements of the same names in `bedrock-auth-okta.yaml`. Replace `<ACCOUNT_ID>` with the account the permission set is assigned to and the Region list with your allowed Bedrock Regions. In other partitions, use that partition's ARN prefix (for example `arn:aws-us-gov:`).

```json
{
    "Sid": "AllowCloudWatchOtlpMetrics",
    "Effect": "Allow",
    "Action": ["cloudwatch:PutMetricData"],
    "Resource": "arn:aws:cloudwatch:*:<ACCOUNT_ID>:dataset/default",
    "Condition": {
        "Null": {"cloudwatch:namespace": "true"}
    }
},
{
    "Sid": "AllowBedrockShortTermBearerToken",
    "Effect": "Allow",
    "Action": "bedrock:CallWithBearerToken",
    "Resource": "*",
    "Condition": {
        "StringEquals": {
            "aws:RequestedRegion": ["<REGION_1>", "<REGION_2>"],
            "bedrock:BearerTokenType": "SHORT_TERM"
        }
    }
}
```

The OTLP grant lets any user of the permission set write metrics under any identity; see the sidecar caveat in [Quota Monitoring](../QUOTA_MONITORING.md#current-limitations).

#### ChatGPT Work and Codex (OpenAI models)

`allow_openai_models: true` adds `AllowOpenAIInvokeRegional` and `AllowOpenAIInvokeGlobal` to the stack's Bedrock access policy ([ADR-0037](../adr/0037-openai-models-on-bedrock-runtime.md)), so a permission set that references it ([step 6](#6-attach-the-bedrock-access-policy-to-the-permission-set)) gets them at the next `gip deploy auth`. With an inline policy instead, the permission set decides: the broad `bedrock:InvokeModel*` on `*` example above already allows OpenAI models; a narrower inline policy needs the two statements below, which are the statements of the same names in `bedrock-auth-okta.yaml`. Use the same placeholders as above. They were live-tested with generic OIDC federation, not with IAM Identity Center.

```json
{
    "Sid": "AllowOpenAIInvokeRegional",
    "Effect": "Allow",
    "Action": "bedrock:InvokeModel",
    "Resource": [
        "arn:aws:bedrock:*::foundation-model/openai.*",
        "arn:aws:bedrock:*:*:inference-profile/*.openai.*",
        "arn:aws:bedrock:*:<ACCOUNT_ID>:project/default"
    ],
    "Condition": {
        "StringEquals": {"aws:RequestedRegion": ["<REGION_1>", "<REGION_2>"]}
    }
},
{
    "Sid": "AllowOpenAIInvokeGlobal",
    "Effect": "Allow",
    "Action": "bedrock:InvokeModel",
    "Resource": "arn:aws:bedrock:::foundation-model/openai.*"
}
```

The same applies to `DenyBedrockMantleEndpoint`, the statement that keeps ChatGPT Work and Codex off the Mantle (`bedrock-mantle`) endpoint, which writes no invocation logs ([ADR-0022](../adr/0022-mantle-metering-deferred-tripwire.md)). It is part of the stack's Bedrock access policy, so it applies once the permission set references that policy. With an inline policy instead: the example permission set above allows no `bedrock-mantle` actions, so Mantle is denied by default. Add the explicit deny so it stays denied if the permission set is broadened later, for example with the AWS managed policy `AmazonBedrockFullAccess`, which allows `bedrock-mantle:*` (SCP-2 in the [multi-account guide](../MULTI_ACCOUNT.md#scp-2-deny-bedrock-mantle-org-wide-unmeterable-endpoint) denies Mantle organization-wide):

```json
{
    "Sid": "DenyBedrockMantleEndpoint",
    "Effect": "Deny",
    "Action": "bedrock-mantle:*",
    "Resource": "*"
}
```

## Migration from OIDC to IDC

To switch an existing OIDC profile to IDC:

1. **Backup current configuration**:
   ```bash
   cp ~/.gip/profiles/myprofile.json ~/.gip/profiles/myprofile-backup.json
   ```

2. **Re-run init with IDC**:
   ```bash
   poetry run gip init --profile myprofile
   ```
   Select "AWS IAM Identity Center (SSO)" when prompted.

3. **Redeploy infrastructure**:
   ```bash
   poetry run gip deploy
   ```
   The auth stack will be updated to use the IDC template instead of OIDC.

## Next Steps

After successful deployment:

- **Test Authentication**: Create and run a simple Claude Code script
- **Monitor Usage**: Use CloudWatch dashboards for system monitoring
- **Set Up Alerts**: Configure SNS notifications for system health
- **Train Users**: Share SSO login instructions with your team

For quota monitoring and per-user controls, consider using the OIDC authentication method instead.