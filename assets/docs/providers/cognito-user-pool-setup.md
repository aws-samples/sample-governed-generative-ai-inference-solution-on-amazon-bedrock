# AWS Cognito User Pool Setup Guide

This guide explains how to set up an AWS Cognito User Pool for use with Claude Code authentication. The User Pool can be used on its own or with your own external OIDC identity provider.

> Using a Cognito user pool as the identity provider for Claude Apps Gateway
> instead? See [Amazon Cognito as the gateway identity provider](../APPS_GATEWAY.md#amazon-cognito-as-the-gateway-identity-provider).

## Overview

The CloudFormation template creates a Cognito User Pool with:
- OAuth2 Authorization Code flow
- Proper token validity settings
- Support for external OIDC providers
- Pre-configured attribute mappings

## Prerequisites

- AWS CLI configured with appropriate credentials
- Permissions to create Cognito User Pools and IAM roles
- A unique domain prefix for your Cognito domain

## Quick Start

### 1. Deploy the User Pool

```bash
# Clone the repository
git clone <repository-url>
cd sample-governed-generative-ai-inference-solution-on-amazon-bedrock

# Deploy the User Pool stack
aws cloudformation deploy \
  --template-file deployment/infrastructure/cognito-user-pool-setup.yaml \
  --stack-name gip-user-pool \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    UserPoolName=gip-auth \
    DomainPrefix=my-unique-domain-prefix \
    CallbackURLs=http://localhost:8400/callback
```

### 2. Get the Configuration Values

```bash
# Get User Pool ID
aws cloudformation describe-stacks \
  --stack-name gip-user-pool \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text

# Get Client ID
aws cloudformation describe-stacks \
  --stack-name gip-user-pool \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text

# Get Domain
aws cloudformation describe-stacks \
  --stack-name gip-user-pool \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolDomain`].OutputValue' \
  --output text
```

### 3. Configure Claude Code

```bash
# Initialize the platform with your User Pool
poetry run gip init

# When prompted, enter:
# - Provider Domain: <your-domain-prefix>.auth.<region>.amazoncognito.com
# - User Pool ID: <from step 2>
# - Client ID: <from step 2>
```

### 4. Deploy the Identity Pool

```bash
# Deploy the authentication infrastructure
poetry run gip deploy --type auth
```

## Configuration Options

### Basic Parameters

- `UserPoolName`: Name for your User Pool (default: gip-auth)
- `DomainPrefix`: Unique prefix for Cognito domain (required)
- `CallbackURLs`: OAuth2 callback URLs (default: http://localhost:8400/callback)
- `LogoutURLs`: OAuth2 logout URLs (default: http://localhost:8400/logout)

### External OIDC Identity Provider Parameters (Optional)

Set these to let users sign in through your own OIDC identity provider as well as with
Cognito user names. The option is off while `ExternalOidcIssuerUrl` is empty, which is the default.

- `ExternalOidcIssuerUrl`: the provider's issuer URL, the `issuer` value of its
  `/.well-known/openid-configuration` document (must start with `https://`)
- `ExternalOidcClientId`: client ID of the app you register in the provider for this user pool; the
  app must allow the `openid`, `email` and `profile` scopes, which the user pool requests
- `ExternalOidcClientSecret`: client secret of that app (`NoEcho`)
- `ExternalOidcProviderName`: name of the provider in the user pool (default `ExternalOIDC`), shown on
  the managed login page; 3 to 32 letters, digits, dots or hyphens, starting and ending with a letter or
  digit

The stack refuses to create or update when `ExternalOidcIssuerUrl` is set without the client ID and
secret. This option has no entry in [LIVE_VALIDATION.md](../LIVE_VALIDATION.md): the repository's
live validation does not cover it, so test sign-in through your provider before you rely on it.

## User Pool Configuration

The template creates a User Pool with the following settings:

### Sign-in Options
- Username with email alias
- Email as required attribute
- preferred_username as required attribute

### Security Settings
- Self-registration disabled
- Password policy: 8+ chars, upper/lower/numbers/symbols
- MFA optional (can be configured per user)
- Token revocation enabled
- Prevent user existence errors enabled
- A changed email address takes effect only after the user confirms the code sent to it
  (`UserAttributeUpdateSettings`); until then the original address stays active and in the tokens.
  Deployments can use the email claim as the user's identity, so a user can't take over a
  colleague's address. A user who controls a second address can still verify it and move to it.

### Token Validity
- Authentication flow session: 3 minutes
- Refresh token: 600 minutes (10 hours)
- Access token: 10 minutes
- ID token: 60 minutes

### OAuth2 Configuration
- Authorization code flow only
- Scopes: openid, email, profile
- No implicit grant flow

## Adding Users

Since self-registration is disabled, you need to create users manually:

### Via AWS Console
1. Navigate to Cognito > User pools > Your pool
2. Click "Create user"
3. Enter username and temporary password
4. User will need to change password on first login

### Via AWS CLI
```bash
aws cognito-idp admin-create-user \
  --user-pool-id <your-user-pool-id> \
  --username <username> \
  --user-attributes Name=email,Value=user@example.com \
  --temporary-password <temp-password>
```

### Changing a User's Email Address

An administrator changes it. Marking the new address as verified in the same call makes it active
at once, without a code:

```bash
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <your-user-pool-id> \
  --username <username> \
  --user-attributes Name=email,Value=new@example.com Name=email_verified,Value=true
```

Without `email_verified`, Cognito sends a code to the new address and keeps the original one
active until the user confirms it. For users who sign in through the external OIDC provider,
change the address in the provider. Cognito's attribute-mapping documentation says that it updates
mapped attributes from the provider when the user signs in, and that a mapped email address is
unverified unless the provider's `email_verified` claim is mapped (this template doesn't map it).
The documentation doesn't say how the verify-before-update setting applies to a mapped `email`, so
check the user's attributes after their next sign-in.

## Integrating External Identity Providers

### Your OIDC Identity Provider

1. Register an app in your identity provider for this user pool, as a confidential web client:
   - Redirect URI: `https://<domain-prefix>.auth.<region>.amazoncognito.com/oauth2/idpresponse`
     (the stack output `IdpResponseEndpoint`)
   - Scopes: `openid`, `email` and `profile`. Email is a required attribute of this user pool, so
     the provider must release the `email` claim.
   - Restrict which of your users may sign in (groups or app assignment) in the provider.

2. Update the stack with the provider's values:

   ```bash
   aws cloudformation deploy \
     --template-file deployment/infrastructure/cognito-user-pool-setup.yaml \
     --stack-name gip-user-pool \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides \
       ExternalOidcIssuerUrl=https://login.example.com \
       ExternalOidcClientId=<client-id> \
       ExternalOidcClientSecret=<client-secret>
   ```

   The stack creates the identity provider, maps the standard OIDC claims (`sub` to
   `preferred_username`, and `email`, `given_name` and `family_name` to the attributes of the same
   name), and adds the provider to both app clients. The `ExternalOidcSetupInstructions` output
   repeats the redirect URI and the provider name.

3. Sign in once through the managed login page with a user of your provider, and confirm the user
   appears in the user pool with an email address.

### Other Ways to Add a Provider

You can also add an OpenID Connect provider to the user pool in the Cognito console, and then add it
to the app clients' identity providers. A provider added that way is outside the stack, and a later
stack update that changes an app client sets its identity providers back to the template's list, so
prefer the parameters above.

## Troubleshooting

### Group-Level Dashboards Show Default Values

If CloudWatch dimensions show `department: "unspecified"` or `team: "default-team"`, your Cognito tokens may not include custom attributes. Enable the Pre-Token-Generation trigger:

```bash
# Redeploy with custom claim injection enabled
aws cloudformation deploy \
  --template-file deployment/infrastructure/cognito-user-pool-setup.yaml \
  --stack-name gip-user-pool \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    EnableCustomClaimInjection=true
```

Then ensure users have custom attributes set:
```bash
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <pool-id> \
  --username user@example.com \
  --user-attributes \
    Name=custom:department,Value=engineering \
    Name=custom:team,Value=platform
```

Users must re-authenticate for the new claims to appear in their tokens.

> **Note:** This is only needed for pure Cognito deployments. If you use an external IdP (Okta, Azure AD, Google), those providers already emit standard claims via OIDC.

### Domain Already Exists
If you get a domain conflict error, choose a different `DomainPrefix`. Cognito domains must be globally unique.

### Missing Outputs
Ensure the stack deployment completed successfully:
```bash
aws cloudformation describe-stacks \
  --stack-name gip-user-pool \
  --query 'Stacks[0].StackStatus'
```

### Authentication Issues
Check that:
1. User exists in the User Pool
2. Callback URL matches exactly
3. App client has correct identity providers

## Cleanup

To remove the User Pool:
```bash
aws cloudformation delete-stack --stack-name gip-user-pool
```

Note: This will delete all users and configurations. Back up any important data first.