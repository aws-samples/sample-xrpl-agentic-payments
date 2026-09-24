# Deployment and live acceptance

## Prerequisites

- AWS credentials authorized for CDK deployment in your chosen region,
  including ECR, CodeBuild, and S3
- Node.js 22.12+ (or 24/26), Python 3.13, `uv`, AWS CLI, and CDK bootstrap
- Bedrock model access for the configured Sonnet inference profile

No local Docker or other container engine is required. `deploy.sh` builds and
pushes the Runtime image with AWS CodeBuild, so this works from a machine with
no container engine at all.

**Region:** `AWS_DEFAULT_REGION` in `.env` is the only place a default region
lives (`us-west-2`); edit it to deploy elsewhere. Claude Sonnet 4.5 is invoked
through the `us.` cross-region inference profile, which fans out only to
`us-east-1`, `us-east-2`, and `us-west-2` regardless of which of those three
you deploy to — deploying outside them needs model access and an inference
profile for that region, and AgentCore Runtime, Gateway, Memory, and Policy
available there too.

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
npx cdk bootstrap "aws://ACCOUNT_ID/$(grep '^AWS_DEFAULT_REGION=' .env | cut -d= -f2-)"
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

## 3. Observability

The stack enables tracing on the Runtime, Gateway, and Memory by default —
`tracingEnabled: true` on the Runtime, and explicit trace/log delivery wired
up for Gateway and Memory, which don't have Runtime's shortcut. None of it
produces visible traces until you complete a one-time, per-account-and-region
setup: CloudWatch Transaction Search, which is what lets X-Ray deliver trace
data into CloudWatch in the first place.

```bash
uv run python scripts/verify_and_set_env.py --verify-only  # nothing to do here, just a reminder .env has a region
./scripts/enable_observability.sh
```

This is safe to re-run and changes only your account's X-Ray configuration —
not anything specific to this stack. If the account is shared with other
workloads that manage their own X-Ray setup, coordinate before running it.
Skip it and logs still work; only traces need it.

Where things land:

- **Runtime** spans go to its own log group,
  `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`. `GET /ping` and
  `POST /invocations` requests are visible there immediately (see
  [Confirming an agent was invoked](#confirming-an-agent-was-invoked) below);
  traces need the account setup above.
- **Gateway** and **Memory** each get a dedicated CloudWatch log group for
  `APPLICATION_LOGS` — printed as the `GatewayLogGroupName` and
  `MemoryLogGroupName` stack outputs — plus a shared X-Ray trace destination.
- All three show up together in the
  [CloudWatch GenAI Observability dashboard](https://console.aws.amazon.com/cloudwatch/home#gen-ai-observability)
  once Transaction Search is on. This dashboard is also what
  [Amazon CloudWatch Omni](https://aws.amazon.com/blogs/mt/introducing-amazon-cloudwatch-omni-observability-for-the-ai-era/)
  reads from — AgentCore's existing observability carries forward automatically,
  with no extra setup beyond what's above.

### Confirming an agent was invoked

Two log groups are created per Runtime (a naming artifact of the AgentCore
service, not a bug): an uppercase `...-DEFAULT` and a lowercase `...-default`.
Only one receives traffic — check both if one looks empty.

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-default --region us-west-2 --since 1h
```

Or, in the [CloudWatch Logs Insights console](https://console.aws.amazon.com/cloudwatch/home#logsV2:logs-insights),
run this query against that log group to isolate real chat traffic from
`/ping` health checks:

```
fields @timestamp, @message | filter @message like /invocations/ | sort @timestamp desc
```

## 4. Agent Registry

The stack registers the Gateway, the Runtime, and both Claude Code skills in
an AWS Agent Registry catalog (see
[docs/architecture.md#agent-registry](architecture.md#agent-registry)). AWS
creates every record in `DRAFT` status regardless of the registry's
auto-approval configuration — auto-approval decides the outcome of a
submission, it doesn't submit for you. Run this once after every deploy that
creates or changes a record:

```bash
./scripts/submit_registry_records_for_approval.sh
```

`DRAFT` records are invisible to search — skipping this step means the
registry exists but nothing in it is discoverable. Then see it actually
used, cold, by a consumer that has never seen this stack's Gateway URL or
ARNs — only the registry ID:

```bash
uv run python scripts/demo_registry_discovery.py
```

It searches the registry by natural language, pulls back the full Gateway
record (URL and, where sync permits allow it, tool definitions), invokes
`list_supported_corridors` live with a narrowly-scoped IAM role, and pulls
the `xrpl-agent-wallet` skill's full content straight out of the registry.

## 5. Acceptance

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
