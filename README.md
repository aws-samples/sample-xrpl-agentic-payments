# AgentCore + XRPL Cross-Border Transfer POC

A safety-governed cross-border remittance sample built with Amazon Bedrock
AgentCore, Strands Agents, AG-UI, AWS Step Functions, DynamoDB,
Amazon Cognito, and XRPL Testnet.

The sample demonstrates exact-output issued-currency transfers with bounded
`SendMax`, an XRP-denominated x402 service fee, direct Testnet wallet delivery,
and simulated local-fiat payout. The language model can discover corridors,
obtain quotes, create transfer intents, and read status. It cannot approve,
sign, or execute a payment.

> **Testnet demonstration only**
>
> All currencies, rates, sanctions decisions, and fiat payouts are demo
> fixtures. The code and infrastructure reject non-Testnet XRPL configuration.
> Mainnet, real custody, production KYC/AML, and real bank movement are outside
> this sample's scope.

![AgentCore and XRPL architecture](docs/assets/architecture.png)

_Regenerate with `pip install -r diagrams/requirements.txt && python3 diagrams/render_architecture.py` (icons: see [`diagrams/icons/SOURCE.md`](diagrams/icons/SOURCE.md))._

## Quickstart

There are three levels of testing. Each one adds more of the real system. Levels 1
and 2 need no AWS account and move no funds.

| Level | What it proves | Needs | Time |
|-------|----------------|-------|------|
| 1. Verify | Lint, unit tests, builds, and CDK synth all pass | Python, `uv`, Node | ~5 min |
| 2. Local API | Quote → intent → approval works, and a tampered approval is refused | Level 1 | ~2 min |
| 3. End to end | The agent quotes, you approve, and XRPL Testnet settles | AWS sandbox, Docker | ~30 min |

### Level 1: verify the source

```bash
git clone https://github.com/aws-samples/sample-xrpl-agentic-payments.git
cd sample-xrpl-agentic-payments
uv sync --extra dev
npm ci
cp .env.example .env
./scripts/verify.sh
```

Success: the script exits 0 after Ruff, Pytest, TypeScript, Vitest, the
Next.js build, `npm audit`, and `cdk synth`.

### Level 2: drive the REST API locally

This runs the transfer API in memory, using a demo identity in place of Cognito.
It exercises quoting and the approval commitment. It does not sign or submit
anything to XRPL.

Terminal 1:

```bash
set -a; source .env; set +a
ALLOW_DEMO_AUTH=true uv run uvicorn xrpl_agentcore.api:app --port 8000
```

Terminal 2. Port 8000 serves only the API; `/` returns `{"detail":"Not Found"}` by design.

```bash
api() { curl -s -H 'X-Demo-User: sample-user' -H 'Content-Type: application/json' "$@"; }

api http://localhost:8000/v1/corridors

quote_id=$(api -X POST http://localhost:8000/v1/quotes -d '{
  "corridor_id": "usd-mxn-testnet", "destination_amount": "10",
  "payout_mode": "LOCAL_FIAT_SIMULATED", "recipient_name": "Demo Recipient",
  "recipient_country": "MX", "payout_alias": "fixture-bank-token"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["quote_id"])')

intent=$(api -X POST http://localhost:8000/v1/transfers -d "{\"quote_id\": \"$quote_id\"}")
transfer_id=$(echo "$intent" | python3 -c 'import json,sys; print(json.load(sys.stdin)["transfer_id"])')
approval_hash=$(echo "$intent" | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_hash"])')

# Approve with the exact commitment: expect "status": "APPROVED"
api -X POST "http://localhost:8000/v1/transfers/$transfer_id/approve" \
  -d "{\"approval_hash\": \"$approval_hash\", \"idempotency_key\": \"quickstart-0001\"}"
```

Success: the intent comes back `AWAITING_APPROVAL`, then `APPROVED`. An approval
whose `approval_hash` does not match the quoted terms returns `409`.

### Level 3: end to end on AWS and XRPL Testnet

Use a disposable sandbox account in `us-west-2`. This creates billable AWS
resources and three Secrets Manager secrets. See
[Prerequisites](#prerequisites) for Docker and Bedrock model access.

```bash
export AWS_PROFILE=<sandbox-profile> AWS_DEFAULT_REGION=us-west-2

# 1. Create Testnet wallets, trust lines, and liquidity; store 3 signer seeds.
uv run python scripts/provision_testnet.py --write-secrets

# 2. Verify the ledger state and write the public wallet addresses to .env.
uv run python scripts/verify_and_set_env.py

# 3. Deploy. deploy.sh reads the addresses from .env.
# First time per account: npx cdk bootstrap aws://<account-id>/us-west-2
./scripts/deploy.sh | tee /tmp/xrpl-deploy.log

# 4. Point the web app at the deployment.
{ grep -E '^(AGENTCORE_RUNTIME_URL|NEXT_PUBLIC_[A-Z_]+)=' /tmp/xrpl-deploy.log
  grep -E '^NEXT_PUBLIC_DEMO_(RECIPIENT_ADDRESS|PAYOUT_ALIAS)=' .env; } > web/.env.local

# 5. Create a sign-in user and start the app.
POC_USER_EMAIL=demo@example.com POC_USER_PASSWORD='<strong-disposable-password>' \
  ./scripts/create_poc_user.sh
npm run dev --workspace web
```

`deploy.sh` refuses an uncommitted tree. Commit first, or prefix it with
`ALLOW_DIRTY_DEPLOY=true`.

Then open [http://localhost:3000](http://localhost:3000) and sign in:

1. Select **Simulated fiat**. The assistant returns a quote card.
2. Reply `create the transfer intent`. The assistant renders an approval card
   showing the exact terms.
3. Select **Approve and execute** on the card. The model cannot do this step.
4. Watch the card move through `FEE_PAID` → `SUBMITTED` → `SETTLED` →
   `COMPLETED`. The card links the x402 fee and payment transaction hashes to
   testnet.xrpl.org.

To repeat the same checks without the browser, see [Live acceptance](#live-acceptance).
Remove everything with [Cleanup](#cleanup).

Each section below explains a step in more detail.

## What the sample includes

- Native AG-UI AgentCore Runtime with `POST /invocations`, `GET /ping`, SSE,
  and a Strands agent.
- AgentCore Gateway exposing exactly four planning and status tools.
- AgentCore Policy in enforcement mode with default-deny behavior.
- Cognito authentication and trusted owner identity propagation.
- Opt-in AgentCore Memory limited to structured user preferences.
- Canonical decimal amounts and SHA-256 quote and approval commitments.
- Transactional approval plus execution outbox creation.
- Idempotent Step Functions execution independent of the model session.
- Separately KMS-encrypted transfer and signed-artifact DynamoDB tables.
- Persist-before-broadcast signing and same-blob XRPL resubmission.
- Strict validated-ledger reconciliation and partial-payment rejection.
- Replaceable `XrplX402Adapter` for the XRP service-fee lane.
- AG-UI application-owned quote, approval, progress, receipt, and failure
  components with refresh-safe status recovery.
- Disposable XRPL Testnet issuer, trust-line, liquidity, wallet, deployment,
  and acceptance scripts.

## Architecture and trust boundaries

```text
Browser -> Next.js AG-UI BFF ------> AgentCore Runtime -> Gateway + Policy
   |                                                        |
   +-> authenticated REST API -> DynamoDB outbox             |
                                  |                          |
                                  v                          |
                            Step Functions                   |
                             |          |                    |
                             v          v                    |
                          Signer     Reconciler --------------+
                             |          |
                             +-----> XRPL Testnet
```

Approval always uses the authenticated REST API, never an AG-UI event or model
tool result. Runtime has no access to signer seeds, signed blobs, payment
tables, Lambda execution functions, or Step Functions start permissions.

- [Detailed architecture and safety model](docs/architecture.md)
- [Standalone ASCII architecture](docs/architecture-ascii.md)
- [Deployment details](docs/deployment.md)
- [Research and source mapping](docs/research.md)
- [Recorded live Testnet evidence](docs/live-testnet-evidence.md)

## Repository layout

| Path | Purpose |
| --- | --- |
| `web/` | Next.js, native AG-UI client (`@ag-ui/client`), Cognito, and application-owned cards |
| `src/xrpl_agentcore/` | FastAPI, AgentCore Runtime, domain, signing, reconciliation, and x402 adapter |
| `infra/` | TypeScript CDK infrastructure and least-privilege assertions |
| `scripts/` | Verification, fixture provisioning, deployment, and live acceptance |
| `config/` | Committable example corridor plus ignored generated fixture config |
| `tests/` | Python domain, API, security, idempotency, and XRPL tests |
| `docs/` | Architecture, deployment, research, and acceptance evidence |

## Prerequisites

For local tests:

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 22.12 or newer
- npm

For full deployment and live Testnet acceptance:

- An AWS sandbox account with credentials for CDK, IAM, KMS, DynamoDB, Lambda,
  API Gateway, Cognito, Step Functions, ECR, Secrets Manager, Amazon Bedrock,
  and Amazon Bedrock AgentCore.
- Bedrock model access for the configured Sonnet inference profile.
- AWS CLI v2.
- Docker with Buildx and Linux ARM64 cross-build support.
- CDK bootstrap permission in `us-west-2`.
- Network access to the XRPL Testnet faucet and JSON-RPC endpoint.

The deployment script intentionally supports only `us-west-2`.

## Install and verify locally

Clone the repository and install locked dependencies:

```bash
git clone https://github.com/aws-samples/sample-xrpl-agentic-payments.git
cd sample-xrpl-agentic-payments

uv sync --extra dev
npm ci
```

Create local configuration:

```bash
cp .env.example .env
```

`.env`, `web/.env.local`, generated wallet files, fixture corridor
configuration, CDK output, dependencies, and build artifacts are ignored by
Git. Never add wallet seeds, signed transaction blobs, AWS credentials,
Cognito tokens, or production PII to the repository.

Run the complete source verification gate:

```bash
./scripts/verify.sh
```

The gate runs Ruff, Python formatting checks, Pytest, TypeScript checks,
Vitest, production builds, npm audit, and CDK synthesis.

## Run the local API fixture mode

The API can run without DynamoDB by leaving `TRANSFER_TABLE_NAME` empty. Source
the local environment and enable the explicitly marked demo identity:

```bash
set -a
source .env
set +a

ALLOW_DEMO_AUTH=true uv run uvicorn xrpl_agentcore.api:app --reload --port 8000
```

In another terminal:

```bash
curl http://localhost:8000/health
curl -H "X-Demo-User: sample-user" http://localhost:8000/v1/corridors
```

This mode is suitable for deterministic API and domain development. It does
not execute the deployed Step Functions, signer, or XRPL settlement workflow.

## Run the local AG-UI Runtime health check

Starting the Runtime and calling `/ping` does not invoke a model:

```bash
set -a
source .env
set +a

ALLOW_DEMO_AUTH=true uv run python -m xrpl_agentcore.agent_runtime
```

In another terminal:

```bash
curl http://localhost:8080/ping
```

Real agent invocations additionally require valid AWS credentials,
`AGENTCORE_GATEWAY_URL`, Bedrock model access, and the deployed Gateway targets.

## Deploy the complete AWS and XRPL Testnet sample

Use a disposable AWS sandbox. The deployment creates billable AWS resources,
and fixture provisioning creates or updates three Secrets Manager secrets.

### 1. Select the AWS account and region

```bash
export AWS_PROFILE=<sandbox-profile>
export AWS_DEFAULT_REGION=us-west-2

aws sts get-caller-identity
```

### 2. Provision disposable XRPL Testnet fixtures

```bash
uv run python scripts/provision_testnet.py --write-secrets
uv run python scripts/verify_and_set_env.py
```

Provisioning creates eight disposable Testnet wallets, USD and MXN issuers,
trust lines, balances, and USD/MXN order-book liquidity. It writes:

- `.testnet-fixtures.json`: mode `0600`, contains wallet seeds, ignored by Git.
- `config/corridors.json`: generated deployment fixture, ignored by Git.
- Secrets Manager entries for execution, fee-payer, and fee-merchant seeds.

Each wallet has one role. The first six are stack parameters; only the three
signing wallets have seeds in Secrets Manager.

| Wallet | Role | Deployment value |
|--------|------|------------------|
| `execution` | Sender. Holds USD and signs the exact-output Payment with bounded `SendMax`. | `XRPL_EXECUTION_ADDRESS` (seed in Secrets Manager) |
| `payout` | Destination for simulated local-fiat payout, standing in for the payout partner. | `XRPL_PAYOUT_ADDRESS` |
| `fee_payer` | Pays the x402 XRP service fee, kept separate from the transfer principal. | `XRPL_FEE_PAYER_ADDRESS` (seed in Secrets Manager) |
| `fee_merchant` | Receives the service fee, and refunds it if the transfer fails. | `XRPL_FEE_MERCHANT_ADDRESS` (seed in Secrets Manager) |
| `usd_issuer` | Issues the Testnet USD token. | `XRPL_USD_ISSUER_ADDRESS` |
| `mxn_issuer` | Issues the Testnet MXN token. | `XRPL_MXN_ISSUER_ADDRESS` |
| `recipient` | Destination for direct-wallet delivery in the web starter prompt. | `NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS` |
| `liquidity` | Market maker. Places the USD/MXN offer the payment path crosses. | none |

The roles cannot be merged. XRPL rejects a Payment to its own account, so
the fee payer and fee merchant must differ. An issuer cannot hold its own token,
so neither issuer can double as the sender or a destination.

`scripts/verify_and_set_env.py` re-reads the validated ledger state for every
fixture. Only if all checks pass does it write the seven public addresses in the
table above into `.env`. If `.env` does not exist yet, it creates it from
`.env.example`. Other lines in `.env` are kept, the file is written with mode
`0600`, and seeds are never written to it. Nothing needs to be exported by hand:
`scripts/deploy.sh` reads the six stack parameters from `.env`. A value you
export in the shell still takes precedence. To check the ledger without
touching `.env`, add `--verify-only`.

### 3. Bootstrap CDK and deploy

```bash
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
npx cdk bootstrap "aws://${AWS_ACCOUNT_ID}/us-west-2"
./scripts/deploy.sh
```

`scripts/deploy.sh` verifies a clean Git tree, builds the ARM64 Lambda layer,
runs the full verification gate, deploys support resources, pushes the ARM64
Runtime image to ECR, and enables the AgentCore Runtime in a second CDK pass.

The script prints these frontend values:

```text
AGENTCORE_RUNTIME_URL=...
NEXT_PUBLIC_API_BASE_URL=...
NEXT_PUBLIC_COGNITO_USER_POOL_ID=...
NEXT_PUBLIC_COGNITO_CLIENT_ID=...
```

### 4. Configure and start the web application

Create `web/.env.local` using the deployment outputs and fixture recipient:

```dotenv
AGENTCORE_RUNTIME_URL=<printed-runtime-url>
NEXT_PUBLIC_API_BASE_URL=<printed-api-url>
NEXT_PUBLIC_COGNITO_USER_POOL_ID=<printed-user-pool-id>
NEXT_PUBLIC_COGNITO_CLIENT_ID=<printed-user-pool-client-id>
NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS=<fixture-recipient-address>
NEXT_PUBLIC_DEMO_PAYOUT_ALIAS=fixture-bank-token
```

The `AGENTCORE_RUNTIME_URL` value is server-only. Never prefix it with
`NEXT_PUBLIC_`, and never expose AWS credentials to the browser.

Create or reset a POC Cognito user:

```bash
export POC_USER_EMAIL=demo@example.com
export POC_USER_PASSWORD='<strong-disposable-password>'
./scripts/create_poc_user.sh
```

Start the application:

```bash
npm run dev --workspace web
```

Open [http://localhost:3000](http://localhost:3000), sign in, and use either
one-click starter:

- **Direct wallet** requests a complete 5 MXN quote using the configured
  fixture recipient address.
- **Simulated fiat** requests a complete 10 MXN quote using the configured
  payout alias.

Both starters stop after rendering a structured quote. The user must separately
request intent creation and explicitly select **Approve and execute** on the
application-owned approval card.

## Live acceptance

Before executing payments, independently verify the fixture ledger state:

```bash
uv run python scripts/verify_and_set_env.py --verify-only
```

The direct XRPL engine acceptance moves only disposable Testnet assets:

```bash
uv run python scripts/live_xrpl_acceptance.py
```

For deployed REST acceptance, obtain a Cognito access token and run:

```bash
COGNITO_ACCESS_TOKEN="$COGNITO_ACCESS_TOKEN" \
uv run python scripts/live_acceptance.py \
  --api-url "$NEXT_PUBLIC_API_BASE_URL" \
  --recipient-address "$NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS"
```

The acceptance script replays approvals and requires one idempotent workflow,
one x402 fee hash, and one transfer hash per lane.

## Important safety invariants

- Runtime can call only corridor, quote, intent-creation, and status tools.
- Approval binds owner, transfer ID, payout mode, assets, issuers, recipient
  and tag, route, `SendMax`, slippage, fee, expiry, and nonce.
- Approval and outbox creation are transactional and idempotent.
- Signers persist encrypted signed artifacts before broadcast.
- Ambiguous submission enters `SUBMISSION_UNKNOWN`; it is never immediately
  treated as failure.
- Reconciliation resubmits only the same signed blob and declares settlement
  only after strict validated-ledger checks.
- AG-UI state excludes wallet seeds, signed blobs, bank data, JWTs, approval
  credentials, full recipient details, and other sensitive values.
- Application-owned components render known financial events; model-generated
  HTML or JavaScript is never rendered.

## Cleanup

Destroy the CDK stack when the sandbox is no longer needed:

```bash
npx cdk destroy XrplAgentCorePoc \
  --app "npx ts-node --prefer-ts-exts infra/bin/app.ts"
```

The fixture script creates signer secrets outside the stack. Review and delete
the three `xrpl-agentcore/testnet/*-seed` secrets separately after confirming
that no other test deployment uses them. Testnet wallets and ledger
transactions remain public and cannot be deleted.

## License

Apache-2.0. See [LICENSE](LICENSE).
