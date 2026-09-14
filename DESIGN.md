# XRPL Agentic Payments — Design Document v2

## AI Agents on AWS making Agentic Payments with x402 on the XRP Ledger

**Author:** Sunita Koppar (skoppar), Aman Tiwari (twaman)
**Date:** July 23, 2026
**Target:** Ripple Swell 2026 (Oct 27-29, NYC)
**Status:** Design finalized — ready for implementation

> **Read this first: this is the design, not the as-built system.** It records what
> was intended on the date above, and the implementation diverged from it in three
> ways that change how most of the following sections should be read:
>
> 1. **There is no AgentCore Harness**, and it was dropped on purpose: it put the
>    \$10,000 limit and "never skip compliance" in a system prompt rather than in
>    code. The agent tier is five scoped Strands agents constructed in-process by
>    `run_multi_agent_payment()` in `src/agents/orchestrator.py`, sequenced by
>    plain Python, with those rules re-implemented as `if` statements over a
>    validated request. Wherever a section below says "the Harness", read "the
>    orchestrator and its five agents". The callout in 4.1 sets the two designs
>    side by side rule for rule; read it before §5, §6, §7 or §9, all of which are
>    written in terms of the Harness.
> 2. **There is no AgentCore Runtime.** `src/mcp_server/server.py` implements all
>    eight tools and nothing deploys it; see the callout in 4.3.
> 3. **The Gateway fronts eight Lambda targets, not a Runtime**; see the callout
>    in 4.2.
>
> For what is actually built, the five slides under `docs/` are generated from the
> code and kept honest to it — start with `docs/architecture.png`, whose targets
> T1-T3 are exactly the parts of this design that are drawn but not built.

---

## 0. AWS Account & Credentials

| Property | Value |
|----------|-------|
| AWS Account | `<YOUR_AWS_ACCOUNT_ID>` |
| Region | `us-west-2` (Oregon) |
| IAM User | `<your-iam-user>` |
| Infrastructure as Code | AWS CDK (TypeScript) |
| CDK Directory | `infra/` (relative to repo root) |
| CDK Version | 2.261+ |
| Domain | `<your-subdomain>.<your-domain>` |

**Credentials:** AWS CLI configured via `~/.aws/credentials`. All CDK deploys target this account/region. All boto3 calls use `region_name='us-west-2'`.

---

## 1. Use Case

> "AI agents are beginning to transact, pay for services, and settle value autonomously, creating demand for financial infrastructure built for machine-to-machine commerce."
> — Ripple (ripple.com/insights/xrpl-ai-starter-kit)

**The use case:** AI agents running on AWS cloud making agentic payments with x402 on the XRP Ledger.

**What this means:** An AI agent hosted on Amazon Bedrock AgentCore receives a natural language payment instruction, reasons through compliance and routing, and executes a real payment on the XRPL — settling value in 3-5 seconds using the x402 protocol for machine-to-machine commerce.

**Why this matters for a Swell 2026 technical session:**

Ripple has built the settlement layer (XRPL) and the payment protocol (x402). AWS has built the agent hosting layer (Bedrock AgentCore). This prototype shows them working together — answering the question every financial institution in the audience will have: *"Where do my AI agents run, and how do they pay?"*

---

## 1.1 Current State (How Cross-Border Payments Work Today)

1. A human operator initiates a payment in a banking system
2. A compliance officer manually reviews the recipient (hours to days in a queue)
3. A treasury analyst checks foreign exchange rates and selects a route
4. A payments ops team submits the instruction to SWIFT or a correspondent bank
5. Settlement takes 3-5 business days through multiple intermediaries
6. A back-office team reconciles and confirms receipt

**Cost:** $25-45 per transaction + hidden FX spreads
**Speed:** 3-5 business days
**Availability:** Weekday business hours only
**Visibility:** None until arrival

---

## 1.2 Future State (AI Agents Transacting on XRPL)

1. An AI agent receives a payment instruction (natural language or API)
2. The agent screens the recipient against sanctions lists (milliseconds)
3. The agent queries the XRPL DEX for real-time rates (no hidden spreads)
4. The agent selects the optimal route and pays via x402 (HTTP-native)
5. The XRPL consensus validates the transaction (3-5 seconds, deterministic finality)
6. The agent confirms settlement and provides cryptographic proof

**Cost:** 10 drops (~$0.000023) per transaction
**Speed:** Under 10 seconds end-to-end
**Availability:** 24/7/365
**Visibility:** Every step observable in real-time

---

## 1.3 Who Uses This at Production Scale

| User | Role | Why |
|------|------|-----|
| **Financial institutions** (banks, payment providers) | Deploy and operate the agents in their own AWS account | They process the payments, hold the licenses, own the compliance obligation |
| **Corporate treasury** | Consume agent payments for cross-border AP/AR | They have thousands of payments/month that cost $45 each on SWIFT today |
| **Fintechs / payment aggregators** | Build agent-native products on XRPL | They need speed, low cost, and programmability |
| **AI agent developers** | Build agents that need to pay for services | x402 lets their agents pay for APIs, compute, and data on-demand without billing integrations |

The institution decides WHERE the agents run (their AWS account). Ripple provides WHAT they settle on (XRPL). x402 defines HOW they pay (HTTP-native protocol).

---

## 1.4 Where AWS Fits with Ripple

| Ripple provides | AWS provides |
|----------------|-------------|
| XRP Ledger (settlement network) | Amazon Bedrock AgentCore (agent hosting) |
| RLUSD stablecoin (value transfer) | Amazon Bedrock (AI model inference) |
| x402 protocol (payment standard) | AgentCore Gateway (tool routing) |
| Native DEX (FX liquidity) | AgentCore Observability (metrics, traces) |
| 3-5 sec deterministic finality | IAM, KMS, CloudTrail (security, audit) |
| XRPL AI Starter Kit (developer tools) | EKS, ALB, Route 53 (web frontend) |

**The prototype shows the full stack:** AWS provides the compute + AI + security + observability. Ripple provides the settlement + protocol + liquidity. Together, they enable agents to transact.

---

## 1.5 x402 Protocol on XRPL (How Agents Pay)

x402 is an open protocol for HTTP-native machine-to-machine payments. It extends HTTP 402 (Payment Required) into a complete autonomous payment workflow.

**Flow:**
1. Agent requests a protected resource (e.g., premium market data)
2. Server returns HTTP 402 with payment requirements (price, XRPL address, facilitator URL)
3. Agent signs an XRPL Payment transaction and submits to the network
4. Agent sends the tx hash to a facilitator for verification
5. Agent retries the request with the signed receipt in `X-PAYMENT` header
6. Server verifies receipt and delivers the resource

**Why XRPL for x402:**
- Deterministic finality — agent knows in 3-5 seconds if payment confirmed (no ambiguity)
- Predictable fees — no gas auctions, no fee estimation
- RLUSD support — dollar-denominated stable payments
- 14 years of continuous operation since 2012

---

## 2. Ripple's XRPL AI Starter Kit vs This Prototype

Ripple launched the XRPL AI Starter Kit (June 9, 2026) which provides:
- **XRPL Docs MCP Server** — documentation access for LLMs (not transactional)
- **XRPL Agent Wallet Skill** — Claude Code skill file teaching wallet creation
- **XRPL Payments Skill** — Claude Code skill file with payment knowledge
- **x402 integration guidance** — how to use the `x402-xrpl` package

**What Ripple's kit does:** Teaches AI agents how to interact with XRPL.
**What Ripple's kit does NOT do:** Actually host the agents, sign transactions, or provide operational tool endpoints.

**What this prototype adds:**
- **Agent hosting** — AI agent running on AgentCore (managed compute, observability)
- **Executable MCP tools** — real `submit_payment`, `get_balance`, `check_transaction` endpoints
- **End-to-end pipeline** — compliance → FX → routing → execution → settlement
- **x402 payments in action** — agent paying for premium tools (sanctions screening, market data)
- **Production architecture** — security, audit, governance on AWS

This is the operational layer that turns Ripple's developer toolkit into a deployable system.

---

## 3. Architecture

```
┌──────────────┐
│   Web UI     │
│  (EKS/ALB)  │
└──────┬───────┘
       │ InvokeHarness (event stream)
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Amazon Bedrock AgentCore                         │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  Harness (Payment Pipeline Agent)                          │   │
│  │  Model: Claude Sonnet 4.5 (us. cross-region)               │   │
│  │  Framework: Strands Agents (managed by AgentCore)          │   │
│  │  System Prompt: 5-step sequential payment flow             │   │
│  │  Execution Role: xrpl-agentic-payments-harness-role        │   │
│  │                                                             │   │
│  │  [Reasoning] → [Tool Call] → [Tool Result] → [Reasoning]  │   │
│  └───────────────────┬───────────────────────────────────────┘   │
│                      │ MCP tools/call                             │
│  ┌───────────────────▼───────────────────────────────────────┐   │
│  │  Gateway                                                    │   │
│  │  Auth: AWS_IAM                                             │   │
│  │  Target: → Runtime (MCP Server)                            │   │
│  └───────────────────┬───────────────────────────────────────┘   │
│                      │                                            │
│  ┌───────────────────▼───────────────────────────────────────┐   │
│  │  Runtime (XRPL MCP Server)                                  │   │
│  │  Deployment: S3 code package (Python 3.13)                 │   │
│  │  Network: PUBLIC (outbound HTTPS to XRPL RPC)              │   │
│  │  Protocol: MCP (streamable-http, stateless)                │   │
│  │  Tools: 8 (see below)                                       │   │
│  │                                                             │   │
│  │  xrpl-py → JSON-RPC → XRPL Testnet                        │   │
│  └───────────────────┬───────────────────────────────────────┘   │
│                      │                                            │
└──────────────────────┼────────────────────────────────────────────┘
                       │ HTTPS (JSON-RPC)
                       ▼
              ┌─────────────────┐
              │  XRP Ledger     │
              │  Testnet        │
              │  (RLUSD, DEX)   │
              │  3-5s finality  │
              └─────────────────┘
```

---

## 4. AWS Components — Complete Configuration

### 4.1 AgentCore Harness

> **As built there is no Harness, and dropping it was deliberate.** No stack
> creates one, no code invokes one, and the word appears in no file but this one,
> so every value in the table below — plus its execution role in 4.5 and its log
> group in 4.6 — describes something that does not exist.
>
> The reason it was dropped is in §6. The Harness design was ONE model holding all
> eight tools at once, with the safety rules written as English sentences in its
> system prompt: *"Never skip compliance. Never execute before compliance clears.
> Never execute amounts over \$10,000 without flagging."* That makes the model the
> enforcement point for a system that signs real XRPL payments. A sentence in a
> prompt is not an authorisation boundary — it is a request, and it fails whenever
> the model is wrong, is steered by a hostile tool response, or simply skips a
> step. It also cannot be unit-tested.
>
> What was built instead is five Strands agents in `src/agents/orchestrator.py`,
> sequenced by `run_multi_agent_payment()`, with each of those rules moved out of
> the prompt and into Python:
>
> | §6 rule, as a prompt instruction | As built |
> |---|---|
> | "Never execute before compliance clears" | Agent 4 is not *constructed* unless `screen_sanctions` returned CLEAR (`orchestrator.py:873`, `:947`), so when the run halts no object in the process is holding `execute_payment` |
> | "If amount exceeds \$10,000, flag for human approval" | `requires_human_approval()` re-checks the server-validated `PaymentRequest` in Python — `>=`, so exactly \$10,000 is held, where "exceeds" would have let it through — and fires on that check **or** on the routing tool's own flag (`:920-921`) |
> | "Call `submit_payment` with the destination, amount and currency" | The `execute_payment` wrapper takes those three arguments from the model and **discards all three**, substituting the validated request's values and `source_wallet="execution"` |
> | "Report the transaction hash" | The hash, `tesSUCCESS`, `validated`, destination and `delivered_amount` are read from the tool payload (`:995-996`); the model's prose is never parsed |
>
> The single-model design also had no tool scoping to offer: `submit_payment` was
> in scope from the first token, so an injection anywhere in the run could reach
> it. Five scoped `tools=[...]` lists are the boundary that replaces it — Agent 2
> cannot send money because sending money is not in its list.
>
> **What the change costs**, since it is not free: no managed Runtime or Harness
> means no platform session isolation, no Runtime log group or metrics (4.6), and
> the agent tier is yours to host — the EKS pod in 4.7, or a local process. It is
> five model calls with five system prompts rather than one conversation, so more
> tokens and more round trips. And the control flow is deliberately **not** agentic:
> the model reasons inside each step and decides none of the sequence. For a system
> that signs payments, that is the trade worth making.
>
> See `docs/agent-pipeline.png`.

| Property | Value |
|----------|-------|
| Name | `xrpl-agentic-payments-payment-agent` |
| Execution Role | `arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-harness-role` |
| Model | See Section 5 |
| System Prompt | See Section 6 |
| Tools | AgentCore Gateway (`xrpl-agentic-payments-gateway`) |
| Max Iterations | 15 (enough for 5 steps with retries) |
| Max Tokens | 8192 |
| Timeout Seconds | 120 |
| Memory | Disabled (stateless per payment) |

**Harness Endpoint:**
- Name: `DEFAULT`
- ARN: `arn:aws:bedrock-agentcore:us-west-2:<YOUR_AWS_ACCOUNT_ID>:harness/<id>/harness-endpoint/DEFAULT`

### 4.2 AgentCore Gateway

| Property | Value |
|----------|-------|
| Name | `xrpl-agentic-payments-gateway` |
| ID | `<gateway-id>` (generated at deploy time — read from `.env`, not source) |
| Auth | AWS_IAM |
| Target | MCP Server Runtime (see 4.3) |
| Listing Mode | DYNAMIC (auto-discovers tools from MCP server) |

> **As built, this differs.** `infra/lib/tools-stack.ts` creates the gateway with
> **eight Lambda targets**, one per tool in `functions/`, each carrying an inline
> `toolSchema` — not one target pointing at the Runtime, and so not DYNAMIC
> listing. Consequences worth knowing before reading the rest of this section:
> tools are exposed as `<targetName>___<toolName>` (see `TOOL_TARGETS` in
> `src/agentcore_client.py`), adding a tool means adding a target rather than
> just editing the MCP server, and the Gateway validates arguments against the
> declared schema before any Lambda runs. The Runtime MCP server in 4.3 still
> implements all eight tools, but nothing deploys it — see the callout in 4.3 —
> so it is not what a payment calls and there is no Runtime for a target to point
> at. The as-built request path is in `README.md` and `docs/architecture.png`.

### 4.3 AgentCore Runtime (MCP Server)

> **As built, this does not exist.** Every value in this section is the intended
> configuration, not a description of something running. No code or script in the
> repo calls `CreateAgentRuntime`: `infra/lib/infra-stack.ts:215` grants the
> permission and `deploy/package.sh` builds the archive, but nothing uploads the
> zip to `mcp-server/v1.zip` or creates the Runtime, so `AGENTCORE_RUNTIME_ARN`
> names nothing. `src/mcp_server/server.py` is real and implements all eight
> tools; it is a module with no deployment. Two consequences: the web app's
> observability tab queries Runtime metrics and the
> `/aws/bedrock-agentcore/runtimes/` log group in 4.6 and gets empty results
> because neither is ever created, and the eight tools reach XRPL through the
> Lambda targets in 4.2 instead. This is target T1 on `docs/architecture.png`.

| Property | Value |
|----------|-------|
| Name | `xrpl-agentic-payments-mcp-server` |
| Deployment | S3 Code Package |
| S3 Bucket | `xrpl-agentic-payments-artifacts-<YOUR_AWS_ACCOUNT_ID>` |
| S3 Key | `mcp-server/v1.zip` |
| Managed Runtime | `PYTHON_3_13` |
| Entry Point | `["python", "-m", "src.mcp_server.server"]` |
| Network Mode | PUBLIC |
| Role | `arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-agent-role` |
| Protocol | MCP (streamable-http, stateless_http=True) |
| Port | 8000 (path: /mcp) |

**Environment Variables:**
```
MCP_TRANSPORT=streamable-http     # read by src/mcp_server/server.py
# XRPL_NETWORK=testnet                                    # not read by any code
# XRPL_RPC_URL=https://s.altnet.rippletest.net:51234      # not read by any code
```

Only `MCP_TRANSPORT` has an effect. The server takes its RPC endpoint from
`_metadata.rpc_url` in the wallets secret, falling back to a hardcoded testnet
URL — so the two commented values are aspirational, and the configuration logic
behind them is not built yet. See `.env.example` for the same note.

**Tools exposed (8):**

| Tool | Category | Writes to XRPL? | Description |
|------|----------|-----------------|-------------|
| `screen_sanctions` | Compliance | No | OFAC SDN screening |
| `get_orderbook` | Market Data | No | XRPL DEX order book query |
| `get_paths` | Market Data | No | Payment path finding |
| `path_find` | Routing | No | XRPL `ripple_path_find` — candidate paths for an amount |
| `submit_payment` | Execution | **Yes** | Sign + submit XRPL Payment transaction |
| `check_transaction` | Settlement | No | Verify tx finality by hash |
| `get_balance` | Settlement | No | Account balance query |
| `get_trust_lines` | Settlement | No | Trust line relationships |

These are the same eight tools `infra/lib/tools-stack.ts` exposes as Lambda
targets, so the tool surface does not change when a payment takes the Lambda path
instead. Note that route *selection* is not among them: `routing_analyze` is a
local Python function in `src/agents/orchestrator.py`, so Agent 3 reaches no tool
at all. See `docs/agent-pipeline.png`.

### 4.4 S3 Bucket (Artifacts)

| Property | Value |
|----------|-------|
| Bucket Name | `xrpl-agentic-payments-artifacts-<YOUR_AWS_ACCOUNT_ID>` |
| Region | us-west-2 |
| Purpose | MCP server code zip + model invocation logs |
| Versioning | Enabled |
| Encryption | SSE-S3 |

### 4.5 IAM Roles

**Harness Execution Role:** `xrpl-agentic-payments-harness-role`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:inference-profile/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:GetGateway"
      ],
      "Resource": "arn:aws:bedrock-agentcore:us-west-2:<YOUR_AWS_ACCOUNT_ID>:gateway/*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-west-2:<YOUR_AWS_ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/*"
    }
  ]
}
```

Trust policy: `bedrock-agentcore.amazonaws.com`

**Runtime Execution Role:** `xrpl-agentic-payments-agent-role`
- ECR pull, CloudWatch Logs, X-Ray, CloudWatch Metrics
- Outbound HTTPS to XRPL testnet (PUBLIC network mode)

**Bedrock Logging Role:** `xrpl-agentic-payments-bedrock-logging-role`
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`
- `s3:PutObject` for large payload delivery

### 4.6 CloudWatch Configuration

> **As built, none of these three log groups exists.** The Runtime and Harness are
> not deployed (4.1, 4.3) so their log groups are never created, and nothing calls
> `PutModelInvocationLoggingConfiguration`, so Bedrock writes no model-invocation
> log either. What CloudWatch actually holds is what Lambda puts there by itself:
> one log group per tool under `/aws/lambda/`, plus `Invocations` / `Errors` /
> `Duration`. This repo emits no custom metric (nothing calls `put_metric_data`),
> instruments no X-Ray segment despite the `xray:PutTraceSegments` grant at
> `infra/lib/infra-stack.ts:125`, and creates no alarm or dashboard. `infra/` also
> creates no CloudTrail Trail, so tool invocations are data events and go
> unrecorded until data-event logging is turned on. See `docs/architecture.png`
> step 10.

| Resource | Log Group |
|----------|-----------|
| Runtime logs | `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` |
| Harness logs | `/aws/bedrock-agentcore/harness/<harness-id>` |
| Model invocations | `/aws/bedrock/xrpl-agentic-payments/model-invocations` |

**Metrics namespace:** `AWS/Bedrock-AgentCore`
- Dimensions: `Resource` (ARN), `Operation`, `Name`

### 4.7 Web UI (Frontend)

| Property | Value |
|----------|-------|
| Compute | Amazon EKS (`xrpl-agentic-payments-v2` cluster) |
| Node Type | Graviton3 c7g (Karpenter managed) |
| Application | FastAPI + WebSocket |
| Container | `<YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/xrpl-agentic-payments-webapp` |
| Domain | `<your-subdomain>.<your-domain>` |
| ALB | TLS 1.3, WebSocket stickiness, 3600s idle timeout |
| ACM Cert | `*.<your-domain>` wildcard |

### 4.8 XRPL Testnet (External)

| Property | Value |
|----------|-------|
| Network | XRPL Testnet |
| RPC Endpoint | `https://s.altnet.rippletest.net:51234` |
| Protocol | JSON-RPC over HTTPS |
| Currency | USD (RLUSD on testnet, 3-char code) |
| RLUSD Issuer | `rKqUaEeqAZznZP5UzDwYfhwukc2DbF5JKj` (treasury wallet) |
| Execution Wallet | `rHuy1KwEA8DaTRgRfu9rDw66Qo8ES9VU2T` |
| Destination (test) | `r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg` |
| Finality | 3-5 seconds (deterministic consensus) |
| TX Cost | 10 drops (0.00001 XRP) |

---

## 5. Model Selection

| Model | ID (cross-region inference profile) | Use |
|-------|-----|-----|
| **Claude Sonnet 4.5** | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Harness primary model |
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Future: lightweight tool-call steps |

**Why `us.` prefix (cross-region inference):**
- Routes across `us-east-1`, `us-east-2`, `us-west-2` automatically
- Provides capacity resilience (no single-region throttling)
- Same model quality, just load-balanced

**Harness model configuration:**
```python
{
    "bedrockModelConfig": {
        "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "apiFormat": "converse_stream",
        "maxTokens": 4096,
        "temperature": 0.1
    }
}
```

**Why `converse_stream`:**
- The Harness uses Bedrock's Converse API internally
- Streaming enabled — responses stream token-by-token to the client
- Tool use is native (model returns `tool_use` blocks, Harness executes, feeds `tool_result` back)
- `reasoningContent` deltas provide chain-of-thought visibility

---

## 6. System Prompt (Harness)

> **Not the prompt in use.** This is the single-model prompt the Harness design
> called for, and it is the reason the Harness was dropped — the STEP ordering and
> the RULES block are enforcement written as English. As built there are five
> prompts, one per agent (`COMPLIANCE_SYSTEM_PROMPT` … `SETTLEMENT_SYSTEM_PROMPT`
> in `src/agents/orchestrator.py`), and none of them is load-bearing for safety:
> the sequence is a Python function and the gates are `if` statements. See the
> callout in 4.1.

```
You are the XRPL Agentic Payments agent, executing cross-border payments on the XRP Ledger.

You follow a strict 5-step sequential process for every payment:

STEP 1 - COMPLIANCE: Screen the recipient against sanctions lists.
- Call screen_sanctions with the recipient name and country.
- If status is BLOCKED, stop immediately and report the block.
- If status is CLEAR, proceed to Step 2.

STEP 2 - FX INTELLIGENCE: Query available payment paths.
- Call get_paths with the source wallet, destination, amount, and currency.
- Report the number of paths found and the recommended path.

STEP 3 - ROUTING: Analyze and approve the route.
- For direct USD-to-USD payments, approve immediately.
- For cross-currency payments, evaluate cost and path hops.
- If amount exceeds $10,000, flag for human approval (do not execute).

STEP 4 - EXECUTION: Submit the payment to the XRP Ledger.
- Call submit_payment with the destination, amount, and currency.
- Report the transaction hash.

STEP 5 - SETTLEMENT: Confirm finality on the ledger.
- Call check_transaction with the transaction hash.
- Confirm the transaction is validated (irreversible).
- Report the ledger index and settlement timestamp.

RULES:
- Never skip compliance. Never execute before compliance clears.
- Never execute amounts over $10,000 without flagging.
- Always confirm settlement after execution.
- Report each step's outcome clearly.
- If any step fails, stop and report the failure with the reason.

WALLET CONTEXT:
- Source wallet: rHuy1KwEA8DaTRgRfu9rDw66Qo8ES9VU2T
- RLUSD issuer: rKqUaEeqAZznZP5UzDwYfhwukc2DbF5JKj
- Currency: USD (RLUSD stablecoin on XRPL testnet)
```

---

## 7. XRPL Interaction Model

**The Harness (AI) never talks to XRPL directly. The data flow:**

```
Harness → Gateway → MCP Server → xrpl-py → XRPL RPC → XRP Ledger
```

The MCP Server is the adapter between AI and blockchain:

| Operation | XRPL API Method | On-chain effect |
|-----------|----------------|-----------------|
| `submit_payment` | `submit_and_wait` (Payment tx) | Debits sender, credits receiver with RLUSD |
| `get_balance` | `account_info` + `account_lines` | Read-only — no transaction |
| `check_transaction` | `tx` (lookup by hash) | Read-only — confirms validation status |
| `get_orderbook` | `book_offers` | Read-only — DEX order book snapshot |
| `get_paths` | `ripple_path_find` | Read-only — available payment paths |
| `get_trust_lines` | `account_lines` | Read-only — trust relationships |

**Only `submit_payment` writes to the ledger.** All other tools are read-only queries. This means:
- Read operations: free, instant
- Write operations: 10 drops fee (~$0.000023), 3-5 sec for finality
- Wallet seed in MCP server signs transactions locally before submitting

---

## 8. Inference Endpoint

The Harness model config uses `apiFormat: "converse_stream"` which routes through the `bedrock-runtime` endpoint (Converse API). This is distinct from the newer `bedrock-mantle` endpoint (Responses API).

**Why `converse_stream` (not `bedrock-mantle`):**
- Harness manages the agent loop internally — doesn't need Mantle's stateful conversation
- Converse API has the most mature tool use + streaming support
- Chain-of-thought reasoning via `reasoningContent` deltas
- The Harness's `InvokeHarness` API is always streaming — it wraps Converse internally

**`bedrock-mantle` is available** (`bedrock-mantle.us-west-2.api.aws`) for future use if we need the Responses API's async inference or stateful conversations.

---

## 9. Observability

### Automatic (no code changes needed)
From AgentCore SAA v3.0: "The service automatically captures and publishes runtime metrics (invocations, latency, errors, throttles, CPU/memory usage) to Amazon CloudWatch."

| Metric | Namespace | Dimensions |
|--------|-----------|------------|
| Invocations | `AWS/Bedrock-AgentCore` | Resource (ARN), Operation, Name |
| Latency | `AWS/Bedrock-AgentCore` | Same |
| Errors | `AWS/Bedrock-AgentCore` | Same |
| Throttles | `AWS/Bedrock-AgentCore` | Same |
| CPU/Memory | `AWS/Bedrock-AgentCore` | Resource, Service |
| Sessions | `AWS/Bedrock-AgentCore` | AggregateOperation |

### Model Invocation Logging (to enable)
```python
bedrock.put_model_invocation_logging_configuration(
    loggingConfig={
        "cloudWatchConfig": {
            "logGroupName": "/aws/bedrock/xrpl-agentic-payments/model-invocations",
            "roleArn": "arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/xrpl-agentic-payments-bedrock-logging-role",
            "largeDataDeliveryS3Config": {
                "bucketName": "xrpl-agentic-payments-artifacts-<YOUR_AWS_ACCOUNT_ID>",
                "keyPrefix": "model-invocation-logs/"
            }
        }
    }
)
```

### Chain-of-Thought Reasoning
The `InvokeHarness` stream includes `reasoningContent` deltas — the model's internal thinking streamed to the client in real-time. This provides:
- Transparency into WHY the agent made each decision
- Visible in the UI as reasoning text before each tool call
- No extra configuration — it's part of the stream format

### What the UI Displays
| Signal | Source | Display |
|--------|--------|---------|
| Agent reasoning | `reasoningContent` deltas | Streamed text in chat |
| Tool calls | `tool_use` content blocks | Pipeline node activation |
| Tool results | `tool_result` content blocks | Pipeline node completion |
| Settlement proof | Final text content | TX hash + XRPL explorer link |
| Token usage | `metadata` event | Observability tab |
| Latency | Client-side timing | Value props bar |

---

## 10. Well-Architected: Agentic AI Lens

| Pillar | How addressed |
|--------|---------------|
| **Operational Excellence** | Managed AgentCore (no self-managed compute). Versioned Harness with endpoints. CloudWatch observability automatic. CDK for reproducible deploys. |
| **Security** | IAM execution roles (least privilege). Gateway auth (AWS_IAM). Session isolation (Firecracker microVMs). No secrets in code. PUBLIC network mode for testnet only. |
| **Reliability** | `timeoutSeconds: 120` prevents hung agents. `maxIterations: 15` prevents runaway loops. Cross-region inference profile for model capacity. S3 code deploy for fast recovery. |
| **Performance** | `converse_stream` for lowest latency to first token. Single Harness (no chaining overhead). `temperature: 0.1` for deterministic decisions. |
| **Cost** | Pay per invocation (no idle compute). Haiku available for simple steps (future). Right-sized `maxTokens`. No over-provisioned infrastructure. |
| **Sustainability** | Fully managed services. No idle EC2. Graviton for web frontend. |

---

## 11. CDK Infrastructure

**File:** `infra/lib/agentcore-stack.ts` (new)

Deploys:
1. S3 bucket for MCP server code
2. IAM roles (Harness, Runtime, Logging)
3. AgentCore Runtime (S3 code package)
4. AgentCore Gateway + Target (pointing to Runtime)
5. AgentCore Harness (model + system prompt + gateway tool)
6. Harness Endpoint (DEFAULT)
7. Model invocation logging configuration
8. CloudWatch dashboard

**Deploy command:**
```bash
cd infra
npx cdk deploy XrplAgenticPaymentsAgentCoreStack --no-rollback --require-approval never \
  --context domainName=example.com \
  --context certificateArn=arn:aws:acm:us-west-2:<account>:certificate/<id>
```

`domainName` and `certificateArn` are required for **every** `cdk` invocation,
not just the web stacks: they are read in `bin/infra.ts` while the app is being
constructed, so synthesis fails without them regardless of which stack is
named. TLS is mandatory because the deployed site's first request is a login
form — see the deployment section of `README.md`.

---

## 12. Deployment Phases

### Phase 1: MCP Server on AgentCore Runtime
1. Create S3 bucket (`xrpl-agentic-payments-artifacts-<YOUR_AWS_ACCOUNT_ID>`)
2. Package MCP server code as zip (include `xrpl-py`, `mcp`, dependencies)
3. Upload zip to S3
4. Create/Update AgentCore Runtime with `codeConfiguration` pointing to S3
5. Verify: `invoke_agent_runtime` with `tools/list` returns 8 tools
6. Verify: `invoke_agent_runtime` with `tools/call` for each tool
7. Fix any issues (asyncio conflicts, missing deps, network access)

### Phase 2: Gateway + Harness
8. Create/Update Gateway Target pointing to Runtime endpoint
9. Verify: Gateway discovers tools dynamically
10. Create Harness with model config + system prompt + gateway tool
11. Create Harness Endpoint (DEFAULT)
12. Verify: `invoke_harness` with "Send $500 to Acme GmbH in Germany"
13. Confirm full pipeline executes (compliance → FX → routing → execution → settlement)
14. Confirm XRPL transaction is real (check on testnet explorer)

### Phase 3: Observability
15. Enable model invocation logging
16. Verify CloudWatch metrics appear in `AWS/Bedrock-AgentCore` namespace
17. Verify runtime logs in `/aws/bedrock-agentcore/runtimes/` log group
18. Create CloudWatch dashboard with key metrics

### Phase 4: Web UI
19. Update webapp to call `InvokeHarness` (replace local orchestrator)
20. Stream `contentBlockDelta` events via WebSocket to browser
21. Display reasoning, tool calls, and settlement proof
22. Build Observability tab from CloudWatch metrics
23. Test end-to-end: user → UI → Harness → Gateway → Runtime → XRPL → UI

### Phase 5: Polish
24. Architecture page with real deployed component ARNs
25. Overview page with value proposition
26. Performance testing (target: <30s end-to-end)
27. Error handling (blocked payments, insufficient balance, timeouts)
28. Record demo video as backup for Swell

---

## 13. What Stays from Current Prototype

| Component | Keep | Change |
|-----------|------|--------|
| `src/mcp_server/server.py` | Yes | Repackage as S3 zip (same code) |
| `config/wallets.json` | Yes | Bundle in zip (or Secrets Manager for prod) |
| XRPL testnet wallets | Yes | Already funded with RLUSD |
| EKS cluster | Yes | Web frontend only |
| Web UI (FastAPI) | Partially | Rewrite to call InvokeHarness, not local orchestrator |
| `src/agents/orchestrator.py` | **Delete** | Replaced by Harness |
| Strands SDK usage | **Delete** | Harness uses Strands internally (managed) |
| AgentCore Runtime (container) | **Replace** | Switch from ECR container to S3 code package |
| Gateway | **Update** | Add target pointing to new Runtime |

**Status note:** as of today `src/agents/orchestrator.py` is still the live
implementation and the Harness migration is incomplete — `AGENTCORE_HARNESS_ARN`
is unset and `agentcore.json` has `"harnesses": []`.

---

*Design complete. Implementation begins with Phase 1.*
