# XRPL Agentic Payments — Ripple Agentic AI Payments Prototype

## Complete Project Documentation

**Author:** Anil Nadiminti (anilnadi), Sunita Koppar (skoppar), Aman Tiwari (twaman)  
**Created:** Sept 21, 2026  
**Target:** Ripple Swell 2026 (Oct 27-29, NYC)  
**Status:** Phase 1 complete, Phase 2 in progress

---

## AWS Account & Region

- Account: `<YOUR_AWS_ACCOUNT_ID>`
- Primary Region: `us-west-2`
- IAM User: `<your-iam-user>`
- Domain: `<your-subdomain>.<your-domain>`

---

## Project Location

```
~/workspace/labs/ripple/xrpl-agentic-payments/
├── infra/                      # CDK stacks (TypeScript)
│   ├── lib/infra-stack.ts      # Core: IAM, ECR, Secrets
│   └── lib/web-stack.ts        # Web: VPC, ALB, EC2, Route53
├── src/
│   ├── mcp_server/server.py    # FastMCP XRPL tool server (8 tools)
│   ├── agents/
│   │   └── orchestrator.py     # Orchestrator + 5 specialized agents
│   ├── signing/kms_signer.py   # KMS signing module
│   └── payments/               # AgentCore Payments (x402)
├── webapp/
│   ├── app.py                  # FastAPI web app (chat + observability + architecture)
│   ├── templates/              # HTML templates (index, login, observability, architecture)
│   └── static/styles.css       # CSS
├── scripts/
│   ├── provision_wallets.py    # Creates 6 XRPL testnet wallets
│   ├── setup_trust_lines.py    # RLUSD trust lines + issuance
│   └── setup_agentcore_payments.py  # AgentCore Payments config
├── config/
│   ├── wallets.json            # XRPL wallet addresses + seeds (GITIGNORED)
│   ├── payments.json           # AgentCore payment manager IDs
│   └── runtime.json            # AgentCore runtime IDs
├── Dockerfile                  # MCP server container (AgentCore Runtime)
├── Dockerfile.webapp           # Web app container (EC2)
└── .venv/                      # Python 3.13 virtualenv
```

---

## CDK Stacks Deployed

> Every `cdk` command below also needs `--context domainName=<your-domain>` and
> `--context certificateArn=<acm-arn>`. They are read in `bin/infra.ts` as the
> app is constructed, so synthesis fails without them no matter which stack is
> named. TLS is not optional: the deployed site's first request is a login form.
> See the deployment section of `README.md`.

### Stack 1: XrplAgenticPaymentsStack (Core Infrastructure)

```bash
cd ~/workspace/labs/ripple/xrpl-agentic-payments/infra
npx cdk deploy XrplAgenticPaymentsStack --no-rollback --require-approval never
```

**Resources:**

| Resource | ARN / ID | Purpose |
|----------|----------|---------|
| IAM Role | `arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-payments-role` | AgentCore Payments |
| IAM Role | `arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-agent-role` | AgentCore Runtime execution |
| Secret | `arn:aws:secretsmanager:us-west-2:<YOUR_AWS_ACCOUNT_ID>:secret:xrpl-agentic-payments/coinbase-cdp-<suffix>` | Coinbase CDP credentials |
| ECR Repo | `<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-mcp-server` | MCP server images |
| ECR Repo | `<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp` | Web app images |

### Stack 2: XrplAgenticPaymentsWebStack (Web Frontend)

```bash
cd ~/workspace/labs/ripple/xrpl-agentic-payments/infra
npx cdk deploy XrplAgenticPaymentsWebStack --no-rollback --require-approval never
```

**Resources:**

| Resource | ID / Value | Purpose |
|----------|-----------|---------|
| VPC | XrplAgenticPaymentsVPC (2 public subnets, 2 AZs) | Network isolation |
| ALB | `<alb-name>` | HTTPS load balancer |
| ALB DNS | `<alb-dns-name>` | - |
| Target Group | `<target-group-name>/<id>` | Routes to EC2:8080 |
| ASG | `XrplAgenticPaymentsWebStack-ASG<suffix>` | 1x t4g.small Graviton |
| EC2 Instance | `<instance-id>` | Runs webapp Docker container |
| Security Group (ALB) | `<alb-sg-id>` | HTTPS from allowed IPs only |
| Security Group (EC2) | `<ec2-sg-id>` | Port 8080 from ALB SG only |
| ACM Certificate | `arn:aws:acm:us-west-2:<YOUR_AWS_ACCOUNT_ID>:certificate/<cert-id>` | `*.<your-domain>` wildcard |
| Route 53 A Record | `<your-subdomain>.<your-domain>` → ALB (alias) | Custom domain |
| Hosted Zone | `<hosted-zone-id>` | <your-domain> |

---

## AgentCore Runtime (MCP Server in Cloud)

### Runtime Details

| Property | Value |
|----------|-------|
| Runtime ID | `<runtime-id>` |
| Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:<YOUR_AWS_ACCOUNT_ID>:runtime/<runtime-id>` |
| Status | READY |
| Protocol | MCP |
| Container | `<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-mcp-server:v6` |
| Port | 8000 (path: /mcp) |
| Transport | streamable-http, stateless_http=True |

AgentCore runtime, gateway, payment-manager, and connector ids are generated at
deploy time — they are not in source. Read them from your environment (`.env`),
not from this document.

### Tools Registered (8 total)

| Tool | Group | x402 Gated | Purpose |
|------|-------|-----------|---------|
| `submit_payment` | Payment | No | Send RLUSD on XRPL |
| `get_balance` | Payment | No | Check account balances |
| `path_find` | Payment | No | XRPL native pathfinding |
| `check_transaction` | Payment | No | Verify tx settlement |
| `get_trust_lines` | Payment | No | Token relationships |
| `get_orderbook` | Market | Yes ($0.003) | XRPL DEX order book |
| `get_paths` | Market | Yes ($0.003) | Ranked path comparison |
| `screen_sanctions` | Compliance | Yes ($0.01) | OFAC SDN screening |

### How to Invoke

```python
import boto3, json

data = boto3.client('bedrock-agentcore', region_name='us-west-2')
RUNTIME_ARN = 'arn:aws:bedrock-agentcore:us-west-2:<YOUR_AWS_ACCOUNT_ID>:runtime/<runtime-id>'

# List tools
payload = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
r = data.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=payload.encode(),
    contentType='application/json',
    accept='application/json, text/event-stream',
    mcpMethod='tools/list',
    mcpProtocolVersion='2024-11-05',
    runtimeSessionId='my-session-id-must-be-33-chars-min',
)
body = r['response'].read().decode()

# Call a tool
payload = json.dumps({
    'jsonrpc':'2.0','id':2,'method':'tools/call',
    'params':{'name':'screen_sanctions','arguments':{'entity_name':'Acme Corp','entity_country':'US'}}
})
r = data.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=payload.encode(),
    contentType='application/json',
    accept='application/json, text/event-stream',
    mcpMethod='tools/call',
    mcpProtocolVersion='2024-11-05',
    runtimeSessionId='my-session-id-must-be-33-chars-min',
)
```

### How to Update the MCP Server

```bash
cd ~/workspace/labs/ripple/xrpl-agentic-payments

# 1. Make code changes to src/mcp_server/server.py

# 2. Build new image
docker build --platform linux/arm64 -f Dockerfile -t <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-mcp-server:v7 .

# 3. Push to ECR
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com
DOCKER_CONTENT_TRUST=0 docker push <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-mcp-server:v7

# 4. Update runtime
python -c "
import boto3
ctrl = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
ctrl.update_agent_runtime(
    agentRuntimeId='<runtime-id>',
    agentRuntimeArtifact={'containerConfiguration':{'containerUri':'<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-mcp-server:v7'}},
    roleArn='arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-agent-role',
    networkConfiguration={'networkMode':'PUBLIC'},
    environmentVariables={'XRPL_NETWORK':'testnet','XRPL_RPC_URL':'https://s.altnet.rippletest.net:51234','MCP_TRANSPORT':'streamable-http'},
)
"
```

### Important: AgentCore Runtime MCP Contract

Per official AWS docs (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html):

- Container must listen on `0.0.0.0:8000/mcp`
- Transport: `streamable-http` with `stateless_http=True`
- Must accept `Mcp-Session-Id` header (platform-injected)
- ARM64 architecture required
- Use `from mcp.server.fastmcp import FastMCP` (from the `mcp` pip package)
- IAM role needs: CloudWatch Logs, X-Ray, ECR pull, bedrock-agentcore:* permissions

---

## AgentCore Gateway

| Property | Value |
|----------|-------|
| Gateway ID | `<gateway-id>` |
| Status | READY |
| Protocol | MCP |
| Auth | AWS_IAM |

---

## AgentCore Payments (x402)

| Property | Value |
|----------|-------|
| Payment Manager ID | `<payment-manager-id>` |
| Payment Manager Status | READY |
| Connector ID | `<connector-id>` |
| Connector Type | CoinbaseCDP |
| Credential Provider ARN | `arn:aws:bedrock-agentcore:us-west-2:<YOUR_AWS_ACCOUNT_ID>:token-vault/default/paymentcredentialprovider/xrpl-agentic-payments-coinbase` |
| Network | BASE (L2 for USDC) |
| Session Budget | $5.00 USDC per session |
| Expiry | 15 minutes |

### Coinbase CDP Credentials

- API Key file: `~/Downloads/cdp_api_key.json`
- Wallet Secret: `~/Downloads/cdp_wallet_secret.txt`
- Stored in: AWS Secrets Manager `xrpl-agentic-payments/coinbase-cdp`
- Delegated signing: Enabled

---

## XRPL Testnet Wallets

All wallets in `config/wallets.json`. Funded via faucet.

| Wallet | Address | Balance | Purpose |
|--------|---------|---------|---------|
| treasury (RLUSD issuer) | `rKqUaEeqAZznZP5UzDwYfhwukc2DbF5JKj` | ~100 XRP | Issues RLUSD |
| execution | `rHuy1KwEA8DaTRgRfu9rDw66Qo8ES9VU2T` | 10,000 USD + XRP | Signs/submits payments |
| fx_agent | `rUbTApSfUB76FXrPiPHwTJ77HBmG4ut8Q4` | 100 USD | DEX queries |
| compliance | `rKiZTkXTtH3QMcegsJ4M1XfuHLCQokVkFL` | 100 USD | Screening fees |
| routing | `rBavnsyiqoF1Nu2p6Fby3BFkCXmPZ4XW9c` | 100 USD | Path discovery |
| monitor | `rpqMmihbsUN8oYp74oPu4hHhsef9JiwgMs` | 100 USD | Tx subscriptions |
| destination (test) | `r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg` | ~300 USD | E2E test recipient |

- RLUSD Currency Code: `USD` (3-char on testnet)
- RLUSD Issuer: `rKqUaEeqAZznZP5UzDwYfhwukc2DbF5JKj` (treasury wallet)
- Network: `https://s.altnet.rippletest.net:51234`
- DefaultRipple: Enabled on treasury

### Reprovision Wallets (if needed)

```bash
cd ~/workspace/labs/ripple/xrpl-agentic-payments
source .venv/bin/activate
python scripts/provision_wallets.py    # Creates 6 new wallets
python scripts/setup_trust_lines.py   # Trust lines + RLUSD issuance
```

---

## Website: <your-subdomain>.<your-domain>

### Access

- URL: `https://<your-subdomain>.<your-domain>`
- Username: `aman`
- Password: `<set-your-own-password>`
- Auth: Session cookie (24h TTL)

### Architecture

- ALB (HTTPS 443) → EC2 t4g.small (port 8080) → Docker container
- ACM wildcard cert: `*.<your-domain>`
- Route 53 A record (alias to ALB)
- Security Group: Restricted to `73.202.213.109/32` (your IP)

### Pages

1. `/` — Conversational AI chat (WebSocket to multi-agent orchestrator)
2. `/observability` — Real-time agent metrics from CloudWatch
3. `/architecture` — Architecture diagram with AWS icons + Ripple logo + data flow

### How to Update the Web App

```bash
cd ~/workspace/labs/ripple/xrpl-agentic-payments

# 1. Make changes to webapp/ or src/

# 2. Build
docker build --platform linux/arm64 -f Dockerfile.webapp -t <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp:v6 .

# 3. Push
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com
DOCKER_CONTENT_TRUST=0 docker push <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp:v6

# 4. Deploy to EC2 via SSM
aws ssm send-command --region us-west-2 --instance-ids <instance-id> \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["docker pull <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp:v6 && docker stop xrpl-agentic-payments && docker rm xrpl-agentic-payments && docker run -d --restart=always --name xrpl-agentic-payments -p 8080:8080 -e XRPL_AGENTIC_USERNAME=<your-username> -e XRPL_AGENTIC_PASSWORD=<your-password> -e AWS_DEFAULT_REGION=us-west-2 <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp:v6"]'
```

### How to Change Allowed IPs (Security Group)

```bash
# Add a new IP
aws ec2 authorize-security-group-ingress --group-id <alb-sg-id> --region us-west-2 \
  --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=NEW_IP/32,Description="Description"}]'

# Remove an IP
aws ec2 revoke-security-group-ingress --group-id <alb-sg-id> --region us-west-2 \
  --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=OLD_IP/32}]'

# Open to all (for Swell demo)
aws ec2 authorize-security-group-ingress --group-id <alb-sg-id> --region us-west-2 \
  --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=0.0.0.0/0,Description="Swell demo - temporary"}]'
```

---

## Bedrock Model

| Property | Value |
|----------|-------|
| Model | Claude Sonnet 4.5 |
| Inference Profile | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Region | us-west-2 |
| API | ConverseStream (via Strands SDK) |

---

## E2E Tests Verified

| Test | Result | TX Hash |
|------|--------|---------|
| Send 100 USD (multi-agent orchestrator) | ✅ SETTLED | `8FD541E2BD98B49C9B773D08724C3EA2918F582A05B04718B59FD79A56C06A9D` |
| Sanctions block (Iran entity) | ✅ BLOCKED | N/A |
| AgentCore Runtime tools/list | ✅ 8 tools | N/A |

---

## Cost Allocation Tags (All Resources)

| Tag | Value |
|-----|-------|
| Project | XRPL Agentic Payments |
| Environment | development |
| Owner | skoppar |
| ManagedBy | cdk |
| CostCenter | ISV-DasGenAI-West-A |
| Customer | Ripple |
| UseCase | XrplAgenticPayments-AutonomousPayments |
| Phase | prototype |

---

## Security Finding (DyePack)

- Finding: `a512f1e8-8763-496a-bc77-b389f400e365`
- Issue: ALB public IP without infrastructure-level auth
- Fix Applied: ALB SG restricted to `73.202.213.109/32`
- Status: Remediated (pending next scan)

---

## Known Issues / TODO

1. **Docker Desktop proxy** — intermittent `write tcp: use of closed network connection` when pushing large images to ECR. Retry 2-3 times or use `DOCKER_CONTENT_TRUST=0`.

2. **Old runtime stale state** — A runtime can end up with corrupted state after multiple failed updates. When that happens, create a fresh runtime and point `.env` at the new id; the old one can be deleted after removing its endpoint.

3. **ALB listeners not created by CDK** — The CDK stack failed on the Route 53 A record (CNAME conflict), and when the stack was retried, the listeners weren't re-created. They were created manually. On a clean redeploy this won't recur.

4. **AgentCore CLI (agentcore deploy) CodeZip path** — Requires `opentelemetry-distro` in requirements.txt if using CodeZip build type. We use Container build type instead (manual ECR push).

5. **Performance** — t4g.small (2GB RAM) is tight for Strands agents calling Bedrock. Consider upgrading to t4g.medium for Swell demo.

---

## Phase 3 TODO (Remaining)

- [ ] Wire the orchestrator's agents to invoke tools via AgentCore Runtime (cloud) instead of locally
- [ ] Integrate AgentCore Payments x402 flow (create payment session, process micropayments)
- [ ] Move per-agent tool scoping into Cedar / Amazon Verified Permissions policies. Today scoping is enforced in application code only — by the tool list passed to each Strands `Agent` in `src/agents/orchestrator.py`. Cedar is a target, not built.
- [ ] KMS secp256k1 key for mainnet transaction signing
- [ ] Step Functions human-in-the-loop for >$10K payments
- [ ] Mastercard Verifiable Intent API integration (if access available)
- [ ] Performance tuning (target: <10s E2E)
- [ ] Record backup demo video
- [ ] Swell presentation deck

---

## Quick Commands Reference

```bash
# Activate virtualenv
cd ~/workspace/labs/ripple/xrpl-agentic-payments && source .venv/bin/activate

# Run multi-agent E2E locally
AWS_REGION=us-west-2 python src/agents/orchestrator.py

# Start local dashboard
python dashboard/server.py  # http://localhost:8765

# Deploy CDK (all stacks). domainName + certificateArn are mandatory.
cd infra && npx cdk deploy --all --no-rollback --require-approval never \
  --context domainName=example.com \
  --context certificateArn=arn:aws:acm:us-west-2:<account>:certificate/<id>

# Check AgentCore Runtime status
python -c "import boto3; ctrl=boto3.client('bedrock-agentcore-control',region_name='us-west-2'); print(ctrl.get_agent_runtime(agentRuntimeId='<runtime-id>')['status'])"

# SSH into EC2 (via SSM)
aws ssm start-session --target <instance-id> --region us-west-2

# View webapp container logs
aws ssm send-command --region us-west-2 --instance-ids <instance-id> \
  --document-name "AWS-RunShellScript" --parameters 'commands=["docker logs xrpl-agentic-payments --tail 50"]'
```

---

*Last updated: July 21, 2026*
