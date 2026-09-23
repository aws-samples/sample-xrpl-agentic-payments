# Deployment and live acceptance

## Prerequisites

- AWS credentials authorized for CDK deployment in `us-west-2`, including ECR,
  CodeBuild, and S3
- Node.js 22.12+ (or 24/26), Python 3.13, `uv`, AWS CLI, and CDK bootstrap
- Bedrock model access for the configured Sonnet inference profile

No local Docker or other container engine is required. `deploy.sh` builds and
pushes the Runtime image with AWS CodeBuild, so this works from a machine with
no container engine at all.

## 1. Provision disposable Testnet fixtures

```bash
uv run python scripts/provision_testnet.py --write-secrets
```

This funds eight new Testnet wallets, establishes USD/MXN trust lines, issues
fixture balances, enables Default Ripple on both fixture issuers, creates a
USD/MXN order-book offer, writes
`config/corridors.json`, and stores the three signer seeds in Secrets Manager.
The complete seed set is written to ignored `.testnet-fixtures.json` mode 0600.
Never commit that file.

Independently re-read the validated ledger state, then write the public
addresses to `.env`:

```bash
uv run python scripts/verify_and_set_env.py
```

After every check passes, this writes the six stack-parameter addresses plus
`NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS` into `.env` (mode `0600`; seeds are never
written). If `.env` does not exist yet, it is created from `.env.example`.
Other lines already in `.env` are left untouched. `scripts/deploy.sh` reads
these six values from `.env`, so nothing needs to be exported by hand; a value
already exported in the shell still takes precedence. Use `--verify-only` to
check the ledger without writing `.env`.

The signer and reconciler can be exercised directly against those disposable
wallets before AWS deployment:

```bash
uv run python scripts/live_xrpl_acceptance.py
```

This proves the real x402 fee and both XRPL payment destinations, but it does
not replace the Cognito, AgentCore, DynamoDB, or Step Functions acceptance
below.

## 2. Bootstrap and deploy

```bash
npx cdk bootstrap aws://ACCOUNT_ID/us-west-2
./scripts/deploy.sh
```

Deployment is intentionally two-phase. The first pass creates ECR, a
CodeBuild project, and its S3 build-source bucket, along with the rest of the
supporting services, with `DeployAgentRuntime=false`. The script zips
`Dockerfile.runtime`, `pyproject.toml`, `README.md`, and `src/` — the same
files `.dockerignore` allows into the build context — uploads that to the
bucket, and runs a CodeBuild build (privileged mode, ARM64) that builds and
pushes the image, entirely in AWS. Only then does the second pass enable
Runtime, referencing the tag CodeBuild just pushed. This is why the phases
can't merge: a Runtime can't reference an ECR tag that does not exist yet.

The first pass reruns until the Runtime exists — including after a prior run
failed partway (for example, a CodeBuild run that failed, or `deploy.sh`
interrupted before starting one) — so a retry can pick up new or changed base
resources. Once the Runtime exists, that pass is skipped so a rebuild never
regresses a healthy stack back to no-Runtime while the replacement image is
being pushed.

After the Runtime pass, `deploy.sh` calls `scripts/write_web_env.py` to write
`web/.env.local`. It reads `ApiUrl`, `UserPoolId`, and `UserPoolClientId`
straight from the stack's CloudFormation outputs, builds
`AGENTCORE_RUNTIME_URL` from the `RuntimeArn` output following the documented
`InvokeAgentRuntime` HTTPS contract:

```text
https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{URL_ENCODED_ARN}/invocations?qualifier=default
```

and reads `NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS` and
`NEXT_PUBLIC_DEMO_PAYOUT_ALIAS` from `.env` — the disposable `recipient`
address that `scripts/verify_and_set_env.py` wrote there, so the one-click
direct-wallet prompt stays inside the current fixture set instead of binding
the sample to another deployment's wallet.

To (re)write `web/.env.local` without redeploying, run it directly:

```bash
uv run python scripts/write_web_env.py
npm run dev --workspace web
```

`AGENTCORE_RUNTIME_URL` is server-only. Never prefix it with `NEXT_PUBLIC_`,
and never expose AWS credentials to the browser.

Create or reset a Cognito POC user, then sign in through the application:

```bash
POC_USER_EMAIL="demo@example.com" \
POC_USER_PASSWORD="replace-with-a-strong-poc-password" \
./scripts/create_poc_user.sh
```

The Next.js server forwards the user's access token to the JWT-protected
Runtime. Do not expose AWS credentials to the browser.

## 3. Acceptance

In the UI:

1. Ask conversationally for a 100 MXN direct-wallet quote.
2. Inspect the structured quote and create the intent.
3. Approve through the application-owned card.
4. Refresh while execution is nonterminal and confirm recovery.
5. Confirm one validated XRP fee hash and one validated transfer hash link.
6. Repeat using simulated local-fiat payout and confirm a `SIM-*` reference.

The deterministic REST lane can be repeated with a Cognito access token:

```bash
COGNITO_ACCESS_TOKEN="$COGNITO_ACCESS_TOKEN" \
uv run python scripts/live_acceptance.py \
  --api-url "$NEXT_PUBLIC_API_BASE_URL" \
  --recipient-address "$XRPL_RECIPIENT_ADDRESS"
```

The script deliberately replays each approval with the same idempotency key and
requires both lanes to complete with fee/payment hashes. Inspect CloudWatch and
the artifact table only through signer/reconciler roles; the Runtime role is
designed to have no table, workflow, Lambda, or Secrets Manager permission.
