# Claude Apps Gateway on AWS

Claude Code and Claude Desktop use the same Claude Apps Gateway inference and
policy plane. This repository does **not** maintain a separate gateway
implementation. It pins the AWS Samples worked example to an exact upstream
commit and materializes it unchanged from:

<https://github.com/aws-samples/anthropic-on-aws/tree/main/claude-apps-gateway>

The pin is `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`: repository,
commit, each subtree's Git tree id, and the versioned `git-tree-sha1-v1`
verification format. The subtrees themselves are not tracked in this repository
([ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md));
`scripts/fetch-claude-apps-gateway.sh` fetches the pinned commit with plain
`git`, reconstructs the materialized paths/types/modes/targets/bytes as Git
trees, verifies their ids, and materializes:

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway/`

The upstream README, CDK application, tests, deployment scripts, architecture,
operational caveats, and configuration reference are authoritative. The sample
is reference material, not an AWS or Anthropic supported production artifact.

## Gateway or credential-process

Both architectures can run side by side in one account. Each row states one
fact per path; the links hold the detail.

| Question | Claude Apps Gateway | credential-process |
|---|---|---|
| Which clients it serves | Claude Code and Claude Desktop ([gateway overview](https://code.claude.com/docs/en/claude-apps-gateway)) | Claude Code, Claude Desktop and other AWS SDK clients, such as internal apps, ChatGPT Work and Codex, OpenCode, Pi and Aider ([HARNESSES.md](HARNESSES.md)) |
| When a spend block takes effect | On the next request: a developer over a cap gets HTTP `429` ([spend limits](https://code.claude.com/docs/en/claude-apps-gateway-spend-limits#how-enforcement-works)) | At credential issuance and the periodic re-check; credentials already issued keep working until they expire, bounded by the session length ([ADR-0009](adr/0009-enforcement-at-credential-issuance.md)) |
| Spend-limit scopes | User, IdP group and organization ([spend limits](https://code.claude.com/docs/en/claude-apps-gateway-spend-limits#set-a-cap)) | User, group and a default policy ([QUOTA_MONITORING.md](QUOTA_MONITORING.md#policy-types)) |
| Per-model spend limit | None: a cap's scope is a user, a group or the organization | None: a quota policy has no model field |
| Spend store unreachable | Enforcement fails open by default; `enforcement.fail_closed_on_error: true` makes it fail closed ([spend limits](https://code.claude.com/docs/en/claude-apps-gateway-spend-limits#postgres-availability)) | Fails closed by default: credential-process issues quota-enabled developers no new or refreshed credentials ([FAILURE_POSTURE.md](FAILURE_POSTURE.md)) |
| AWS cost attribution in CUR 2.0 | One gateway principal by default; per developer when the Bedrock upstream sets `assume_role` with `session_name` ([below](#aws-cost-attribution)) | Per federation mode: the session name carries the user with Direct STS and IAM Identity Center, not with Cognito Identity Pool; documented, not observed in a CUR 2.0 export by this sample's live validation ([by federation mode](#credential-process-by-federation-mode)) |
| AWS cost attribution in Cost Explorer | By activated tags, such as IAM principal tags (`iamPrincipal/<key>`), which the AWS page on IAM principal cost allocation applies to IAM users or roles; it describes no view by session name. Each upstream signs every developer it serves with one role, its `assume_role` role or the gateway's own principal, and `assume_role` sets no session tags, so role tags split spend at most by upstream, not by developer ([below](#aws-cost-attribution)) | Per user only through session tags, which each federation mode gets differently; documented, not observed in Cost Explorer by this sample's live validation ([by federation mode](#credential-process-by-federation-mode)) |
| What the spend figure is | The gateway's own USD estimate; reconcile against your provider's usage reporting ([spend limits](https://code.claude.com/docs/en/claude-apps-gateway-spend-limits#how-requests-are-priced)) | An estimate priced at this sample's rates from client telemetry, or the higher of client and invocation-log counts in `max` mode; reconcile against CUR 2.0 ([QUOTA_MONITORING.md](QUOTA_MONITORING.md)) |
| Identity providers | One OIDC issuer per gateway; no IAM Identity Center, SAML or LDAP (upstream `README.md`; for Cognito see [below](#amazon-cognito-as-the-gateway-identity-provider)) | OIDC (Okta, Microsoft Entra ID, Auth0, Google, generic OIDC, Amazon Cognito user pool) or IAM Identity Center ([providers](providers/README.md)) |
| What you operate | An ECS service, internal ALB and RDS PostgreSQL database from the upstream CDK | CloudFormation stacks from `gip deploy` and a client package on each machine from `gip package` |
| Support status | The upstream CDK is a worked example; its `cdk/README.md` lists changes to make before production | This repository is a sample; see the notice at the top of its README |

Server-side metering can also count gateway traffic toward a user's
credential-process quota; see
[QUOTA_MONITORING.md](QUOTA_MONITORING.md#traffic-through-claude-apps-gateway).

## Deploy

Materialize the pinned source first, then follow the upstream instructions
without local patches:

```bash
scripts/fetch-claude-apps-gateway.sh   # needs git, jq, and github.com access; exits non-zero on any mismatch
cd vendor/aws-samples/anthropic-on-aws/claude-apps-gateway/cdk
cp .env.example .env
# Fill the values documented by upstream.
# Stage the Linux claude binary first (upstream cdk/README.md, prerequisite 5):
# deploy.sh exits before creating AWS resources when it is missing.
npm ci
./scripts/deploy.sh
```

The fetch refuses to overwrite an existing materialized tree. To confirm a tree
is still the pinned upstream bytes run `scripts/fetch-claude-apps-gateway.sh
--verify` (your `.env`, `node_modules/`, and `cdk.out/` make it report a
difference, which is expected after a deploy); to start over run `--clean`
and fetch again.

`gip deploy gateway` is intentionally retired so local CloudFormation
parameters cannot drift from the upstream CDK. Use the upstream CDK and its
operations scripts for deploy, status, and teardown; GIP does not guess the
upstream stack's region or name.

## Claude Desktop

Claude Desktop is not a separate inference implementation:

1. The same gateway handles identity, policy, inference, spend controls, and
   telemetry.
2. A matching gateway policy opts Desktop in with a `desktop` block.
3. Desktop reads `<gateway>/user/bootstrap` when `bootstrapUrl` is delivered by
   MDM or imported configuration.

Use the gateway-native Desktop overlay documented in the upstream README when
that contract is sufficient. Use the AWS Samples companion add-on when you need
the separate PKCE configuration/MCP overlay, materialized by the same fetch:

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway-bootstrap/`

That add-on consumes the existing upstream gateway; it does not replace or
modify the inference plane.

## AWS cost attribution

By default the gateway signs every Bedrock request with its own principal, the
ECS task role in the upstream CDK, so AWS sees all developers' spend under one
IAM principal ([cost attribution on AWS](https://code.claude.com/docs/en/claude-apps-gateway-on-aws#cost-attribution); upstream
`docs/gotchas.md` §21). Two routes split it, and they combine:

- **Per developer, in CUR 2.0.** Set `assume_role` with `session_name: email`
  (or `sub`) on every Bedrock upstream. The gateway then assumes that role once
  per developer per hour, and each developer's requests reach AWS as
  `arn:aws:sts::<account>:assumed-role/<role>/<email>`
  ([per-developer AWS cost attribution](https://code.claude.com/docs/en/claude-apps-gateway-config#per-developer-aws-cost-attribution)).
  It needs the gateway on Claude Code v2.1.281 or later; the upstream sample
  sets the version in `CLAUDE_VERSION` in `cdk/scripts/setup.sh`. The upstream
  CDK doesn't configure `assume_role`, so you add the role and the
  `sts:AssumeRole` grant yourself
  ([per developer with `assume_role`](https://code.claude.com/docs/en/claude-apps-gateway-on-aws#per-developer-with-assume_role)).
  The gateway calls STS at request time, so its subnets need a path to
  `sts.<region>.amazonaws.com`; a NAT gateway provides one, and so does an STS
  interface VPC endpoint (same page). When the upstream CDK creates the VPC, the
  tasks run in private subnets with egress through its NAT gateway, and STS is
  not among the interface endpoints it adds (`cdk/lib/claude-gateway-stack.ts`),
  so STS calls leave through NAT. The stack's comment says its other AWS
  traffic uses those endpoints; add an STS endpoint if STS should too. With
  `vpcId` set, the stack uses an existing VPC, whose routing decides the path.
  The token count and one-token fallback for a request the client abandoned
  stay on the shared `claude-apps-gateway` session.
- **Per team.** Create one Bedrock application inference profile per team and
  model, tag it, activate the tag as a cost allocation tag, and pin each IdP
  group to its team's model ids
  ([per team with application inference profiles](https://code.claude.com/docs/en/claude-apps-gateway-on-aws#per-team-with-application-inference-profiles)).

To see the per-developer ARN, turn on **Include caller identity (IAM principal)
allocation data** in a CUR 2.0 Data Export; the ARN then appears in the
`line_item_iam_principal` column
([AWS: IAM principal cost allocation](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/iam-principal-cost-allocation.html)).
This sample's live validation has not observed the gateway's per-developer ARN
in a CUR 2.0 export.
Cost Explorer is a different answer: the same AWS page describes grouping and
filtering there by activated IAM principal tags (`iamPrincipal/<key>`), not by
session name, so query CUR 2.0 for per-developer gateway spend
([COST_ATTRIBUTION.md](COST_ATTRIBUTION.md)).

### Credential-process, by federation mode

For comparison, this is how the credential-process path attributes cost per
user in each federation mode it ships. CUR 2.0 records the caller's IAM ARN,
including the assumed-role session name, in `line_item_iam_principal`; Cost
Explorer does not show that column
([AWS: IAM principal cost allocation](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/iam-principal-cost-allocation.html); [COST_ATTRIBUTION.md §1](COST_ATTRIBUTION.md#1-built-in-per-user-tracking-no-idp-changes-required)).
Session tags appear in both CUR 2.0 and Cost Explorer once activated as cost
allocation tags; AWS lists an assumed-role session's tags as IAM principal
tags, with the `iamPrincipal/` prefix in CUR 2.0
([AWS: IAM principal cost allocation](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/iam-principal-cost-allocation.html); [AWS launch post](https://aws.amazon.com/blogs/machine-learning/introducing-granular-cost-attribution-for-amazon-bedrock/);
[COST_ATTRIBUTION.md](COST_ATTRIBUTION.md#activate-session-tags-as-cost-allocation-tags)).
This sample's live validation has not run that activation ([LV-1](LIVE_VALIDATION.md#lv-1-aip-cost-allocation-tag-flow-into-cost-explorer-and-cur) is blocked).

| Federation mode | Session name in CUR 2.0 `line_item_iam_principal` | Session tags for Cost Explorer and CUR 2.0 | Observed by this sample's live validation |
|---|---|---|---|
| Direct STS (OIDC, `FederationType=direct`; the `gip init` default, also for a Cognito user pool) | Built from the user's email, or the `sub` with `SessionNameBinding=sub` ([COST_ATTRIBUTION.md §1](COST_ATTRIBUTION.md#1-built-in-per-user-tracking-no-idp-changes-required), [binding](COST_ATTRIBUTION.md#binding-session-names-to-the-token-claim-sessionnamebinding)). Client-asserted with `SessionNameBinding=none`; IdP-signed and STS-enforced with `email` or `sub` ([trust levels](COST_ATTRIBUTION.md#attribution-trust-levels)) | Only if the IdP puts them in the ID token ([COST_ATTRIBUTION.md §3](COST_ATTRIBUTION.md#3-optional-session-tags-for-richer-per-user-attribution)) | Not observed: no session name or session tag read from billing data |
| Cognito Identity Pool (`FederationType=cognito`) | Set by Cognito, not derived from the user, so it does not identify the user ([QUOTA_MONITORING.md](QUOTA_MONITORING.md#identity-caveats)) | `UserEmail` and `UserId`, which Cognito maps server-side from the validated token's `email` and `sub` (`IdentityPoolPrincipalTag` in the `bedrock-auth-*.yaml` templates with `FederationType=cognito`, and in `cognito-identity-pool.yaml`; [trust levels](COST_ATTRIBUTION.md#attribution-trust-levels)) | Not observed: no session tag or session name read from billing data |
| IAM Identity Center | The IAM Identity Center user name; the IDC guide requires it to be the email, which is the default. Server-derived ([IDC guide](providers/iam-identity-center-setup.md#per-user-cost-attribution), [QUOTA_MONITORING.md](QUOTA_MONITORING.md#identity-caveats)) | Through ABAC attributes configured in IAM Identity Center ([COST_ATTRIBUTION.md](COST_ATTRIBUTION.md#idc-limitation)) | Not observed: no session name or session tag read from billing data |

The session-name column for IAM Identity Center follows from the AWS page and
the ARN format the IDC guide requires
(`assumed-role/RoleName/user@company.com`). No entry in
[LIVE_VALIDATION.md](LIVE_VALIDATION.md) reads a session name or session tag
from billing data. LV-1, the only billing check, which would also read the
session name next to application inference profile tags in CUR 2.0, is
blocked on payer-only tag activation.

## Amazon Cognito as the gateway identity provider

The upstream sample lists an Amazon Cognito user pool
(`https://cognito-idp.<region>.amazonaws.com/<pool-id>`) as an OIDC issuer in
`workshop/01-identity/README.md`. Three things to check:

- **Verified email.** The gateway always rejects an id_token whose
  `email_verified` claim is explicitly `false`, so check that the user pool's
  users have verified email addresses ([`oidc` reference](https://code.claude.com/docs/en/claude-apps-gateway-config#oidc)).
  Check the app client's attribute read access too: the AWS token endpoint
  page lists `invalid_grant` when the app client can't read every attribute in
  the requested scope, for example when the app requests the `email` scope and
  the app client can read `email` but not `email_verified`
  ([AWS: token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html)).
- **Groups claim.** The gateway reads group membership from the id_token claim
  that `oidc.groups_claim` names, default `groups`; the key can be a flat key or
  a JSON Pointer ([`oidc` reference](https://code.claude.com/docs/en/claude-apps-gateway-config#oidc)). This sample's own quota code
  reads Cognito groups from the `cognito:groups` claim
  (`quota_check/index.py`), which is the value to give `groups_claim` for
  `allowed_groups` and group policies.
- **Scopes.** The gateway requests `[openid, profile, email, offline_access]`
  by default, and its reference says that dropping `offline_access` disables
  refresh tokens ([`oidc` reference](https://code.claude.com/docs/en/claude-apps-gateway-config#oidc)). The upstream sample says
  that the default set fails the first `/login` against Cognito with
  `invalid_scope`, and that the app client must allow all three of
  `openid`, `email` and `profile`. Its `cdk/gateway.yaml.template` ships the
  override `scopes: [openid, email, profile]` commented out, with an
  instruction to uncomment it; `cdk/gateway.yaml.example` gives the same scopes
  in a comment; `workshop/01-identity/README.md` says to set them. AWS
  documents that Cognito returns a refresh token whenever the grant type is
  `authorization_code`: requesting `offline_access` has no effect, and omitting
  it does not suppress the refresh token
  ([AWS: token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html)).
  The two sources differ on what requesting `offline_access` does: the AWS
  page says it has no effect, and the upstream sample says the default set,
  which includes it, fails the first `/login`. This sample has tested neither the `invalid_scope` failure nor that
  refresh-token behavior. Taken together, leaving `offline_access` out of
  `scopes` is consistent with the AWS page and avoids the failure the upstream
  sample reports. The upstream sample keeps `session.ttl_hours` at 1 on the
  strength of the AWS behavior; whether the gateway still refreshes sessions
  without `offline_access`, against its reference's statement, has not been
  checked.

This sample's live validation has not exercised Cognito as the gateway's
identity provider; its Cognito runs cover the credential-process path.

## Updates

Propose a new pin, then materialize and verify it:

```bash
scripts/sync-claude-apps-gateway.sh       # rewrites UPSTREAM.json (and the LICENSE copy) for upstream main
scripts/fetch-claude-apps-gateway.sh --clean
scripts/fetch-claude-apps-gateway.sh      # fetches the new pin; fails closed on any mismatch
```

The `sync-claude-apps-gateway.yml` workflow does the same on demand. It does not
run on a schedule: a maintainer starts it from the repository's **Actions** tab or
with `gh workflow run sync-claude-apps-gateway.yml`. It executes the upstream
CDK and shell tests against the materialized pin and opens a review pull
request that changes only `UPSTREAM.json` (plus the `LICENSE` copy) and links
the upstream diff. It never auto-merges or auto-deploys upstream changes.
Pull requests that touch the pin or the scripts run
`check-claude-apps-gateway-mirror.yml`, which fetches and verifies the pin.

**Rollback.** The pin is the only state: check out the previous
`vendor/aws-samples/anthropic-on-aws/UPSTREAM.json` (for example
`git checkout <previous-commit> -- vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`),
run `scripts/fetch-claude-apps-gateway.sh --clean`, then
`scripts/fetch-claude-apps-gateway.sh` to materialize the prior upstream
commit. A newer pin can synthesize a real CloudFormation delta for an existing
gateway stack; run `cdk diff` from the materialized directory before deploying
an update, and keep the previous pin available for the rollback window.

## Migrating a legacy GIP stack

The previous local CloudFormation gateway and bootstrap templates have been
removed from the repository (not included in this sample). To
retrieve the template of a deployed legacy stack, use
`aws cloudformation get-template --stack-name <name>`; teardown needs no
template file (`aws cloudformation delete-stack --stack-name <name>`). Do not
attempt an in-place CloudFormation-to-CDK update of the stateful
PostgreSQL-backed gateway.

1. Record the legacy stack outputs, client configuration, IdP settings, and
   current database identifier. Create and verify a manual pre-cutover RDS
   snapshot in addition to the template's snapshot-on-delete policy.
2. Register separate upstream OIDC callbacks and deploy the upstream CDK on a
   separate private hostname. Do not reuse the production hostname during the
   pilot.
3. Treat gateway sessions, device grants, and spend state as non-migrating.
   The new PostgreSQL store starts independently; do not copy internal tables
   between implementations.
4. Move a pilot cohort's MDM settings to the upstream hostname. Validate fresh
   sign-in, inference, policy selection, telemetry, spend controls, Desktop
   `/user/bootstrap`, and any companion PKCE/MCP overlay. Keep the legacy stack
   and hostname unchanged through a defined rollback window.
5. Cut over remaining MDM settings only after the pilot passes. Rollback means
   restoring the prior MDM hostname while the legacy service and IdP callbacks
   are still available.
6. After the rollback window, remove the legacy bootstrap stack first, then the
   legacy gateway. `gip destroy bootstrap` and `gip destroy gateway` verify the
   live template signature before deletion. Confirm the final RDS snapshot is
   available and restorable before removing remaining rollback state.

Upstream stacks, build buckets, CodeBuild projects, and other external build
artifacts follow the upstream teardown order and are never deleted by GIP.
