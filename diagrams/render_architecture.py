#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""XRPL Agentic Payments — SLIDE 1 of 5: component architecture.

Regenerate:

    python3 diagrams/render_architecture.py

WHAT THIS SLIDE ANSWERS: where every component runs, which side of the AWS
account boundary it is on, and in what ORDER the hops happen. Every arrow
carries a step number and every number is explained in the legend under the
picture, so the diagram can be read without this file.

The five slides, in reading order:

    1. docs/architecture.png    — this one: components and the numbered hops
    2. docs/payment-flow.png    — one payment, stage by stage, end to end
    3. docs/agent-pipeline.png  — inside the agent tier: the five agents
    4. docs/wallets.png         — why there are six wallets, and what each is
    5. docs/x402-sequence.png   — the x402 exchange itself, message by message

Represents the request path AS THE CDK STACKS BUILD IT — every solid tile here
is provisioned by code in infra/, and the note's "WHERE THIS RUNS" block says
which of those stacks is actually standing in the account and what changes when
the web tier runs locally instead. A tile is solid because infra/ creates it, and
dashed (T*) because nothing in the repo creates it at all; the two must not be
conflated, which is why the title no longer claims "deployed".

The agent tier is five scoped Strands agents sequenced by a deterministic Python
function (src/agents/orchestrator.py:run_multi_agent_payment) running INSIDE the
web app process — not on AgentCore. AgentCore Runtime is drawn as target T1
because DESIGN.md plans that migration and NOTHING IN THE REPO CREATES A RUNTIME:
src/mcp_server/server.py is a second implementation of the same eight tools and
deploy/package.sh builds its archive, but no code or script ever calls
CreateAgentRuntime or uploads the zip. T1 says Runtime rather than the managed
Harness on purpose: see the RUNTIME node below. KMS-backed signing is likewise a
target (src/signing/kms_signer.py), and so is Cognito (T3), which has no
implementation in the repo at all.

AgentCore Payments is on the request path (arrow 5) because every tool call goes
through PaymentGate.charge() before the Gateway hop. It is drawn as a built hop
because the call is really made, with the caveat stated in the legend: no payment
manager is wired into the pod, so the charge is logged rather than settled.

Writes BOTH docs/architecture.png and webapp/static/assets/architecture.png —
they are the same picture served from two places and used to drift apart.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diagram_lib import CAT, Edge, Node, Spec, Zone, render  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# COLUMNS ENCODE THE TRUST BOUNDARY: col 0 (user) and col 6 (XRPL) are outside
# the AWS account; cols 1-5 are inside it. ROW 1 is the request spine, read left
# to right. Row 0 holds services the spine calls upward; row 2, downward.
NODES = {
    # --- request spine (row 1) ---
    "USER":    Node(0, 1, "user.png", "User", "browser", "user"),
    "ALB":     Node(1, 1, "alb.png", "Application Load Balancer",
                    "HTTPS only · :80 301-redirects", "network"),
    # One web tier, FastAPI. eks-stack.ts builds this image and
    # deploy/build_webapp_image.sh packages it; there is no second front end.
    "WEBAPP":  Node(2, 1, "eks.png", "Web app (EKS / Graviton)",
                    "FastAPI · authenticated entry point", "compute"),
    # The agents run in this same pod, in the FastAPI process. Drawn as its own
    # tile because it is a distinct responsibility, but it is inside the web tier
    # boundary, NOT inside AgentCore.
    "AGENTS":  Node(3, 1, "harness.png", "Orchestrator + 5 agents",
                    "deterministic Python, one scoped Strands agent per step",
                    "agent"),
    # In the request path, not decoration. Eight targets, one per tool Lambda,
    # each with an inline tool schema the Gateway validates arguments against
    # (infra/lib/tools-stack.ts). AWS_IAM inbound auth rather than CUSTOM_JWT,
    # because the caller is the pod and it already has an AWS identity.
    "GATEWAY": Node(4, 1, "gateway.png", "AgentCore Gateway",
                    "MCP · AWS_IAM (SigV4) · 8 Lambda targets", "agent"),
    "TOOLS":   Node(5, 1, "lambda.png", "Lambda tools (x8)",
                    "screen / route / submit_payment / settle", "compute"),
    "XRPL":    Node(6, 1, "◈", "XRP Ledger Testnet",
                    "RLUSD + DEX · 3-5s finality", "ext"),

    # --- services called upward (row 0) ---
    # A TARGET, not a deployed hop, and with no implementation behind it: nothing
    # in the repo verifies a Cognito token, and infra/ provisions no user pool.
    # The deployed FastAPI app authenticates with a signed session cookie against
    # the credentials in xrpl-agentic-payments/web-auth. Drawn as T3 so the
    # picture does not promise a managed identity provider that no request touches.
    "COGNITO": Node(2, 0, "cognito.png", "Amazon Cognito",
                    "JWT auth · not implemented · no user pool in infra/",
                    "security", status="target"),
    "BEDROCK": Node(3, 0, "bedrock.png", "Amazon Bedrock",
                    "Claude Sonnet 4.5 inference", "ml"),
    # Captioned as LOGS, not "metrics + traces". What exists without anyone
    # configuring it is the Lambda log groups and Lambda's own metrics; nothing in
    # this repo emits a custom metric (no put_metric_data anywhere), instruments
    # X-Ray, or creates a dashboard or an alarm. See note item 10.
    "CW":      Node(4, 0, "cloudwatch.png", "Amazon CloudWatch",
                    "Lambda logs + Lambda metrics · no alarms, no custom metrics",
                    "mgmt"),
    # Deliberately NOT captioned "audit trail". Every AWS account has the 90-day
    # Event history for MANAGEMENT events without configuring anything, and that
    # is all this tile is: infra/ creates no Trail, so Lambda invocations (data
    # events) are not captured. Overstating this one is how a diagram ends up
    # promising an audit trail that nobody would find when they went looking.
    "CTRAIL":  Node(5, 0, "cloudtrail.png", "AWS CloudTrail",
                    "management events · no trail\nprovisioned by infra/", "mgmt"),

    # --- services called downward (row 2) ---
    # ON THE REQUEST PATH, and previously missing from this slide entirely. Every
    # tool call goes through it: _call_tool (src/agents/orchestrator.py) calls
    # invoke_tool_with_payment, which runs PaymentGate.charge() BEFORE the Gateway
    # hop. The tile is solid because that call really is attempted on every
    # premium tool; what it does NOT do today is settle, and the subtitle has to
    # say so — PAYMENT_MANAGER_ARN is unset in the pod, so the charge degrades to
    # a logged "simulated" receipt. See note item 5.
    "PAYMENTS": Node(2, 2, "agentcore.png", "AgentCore Payments",
                     "x402 tool charges · Coinbase connector · logged, not "
                     "settled today", "agent"),
    # RUNTIME, not Harness. Both are real AgentCore components, but the managed
    # Harness is a DECLARATIVE agent loop — you hand it a model, tools and a
    # prompt and it owns the orchestration. This orchestrator cannot hand that
    # over: the run order is a fixed five-step Python sequence and the
    # authorisation boundary is a Decimal comparison against
    # HUMAN_APPROVAL_THRESHOLD_USD, which by design is not the model's to make.
    # AWS documents exactly this case as "code-defined agents on AgentCore"
    # rather than the Harness (see the Bedrock Agents Classic migration guide:
    # use the Harness "unless you have a specific reason to own the loop
    # yourself", e.g. custom orchestration or multi-agent supervisor patterns).
    # microVMs, not Instances: a payment finishes in seconds, so nothing here
    # needs the 14-day sessions, GPUs or shared filesystem that the Instances
    # compute type exists to provide.
    "RUNTIME": Node(3, 2, "runtime.png", "AgentCore Runtime",
                    "code-defined agents · microVM per session", "agent",
                    status="target"),
    # The only place wallet seeds live. They are no longer baked into any
    # container image or the AgentCore archive, so both the web tier and the
    # Lambda tools read them from here at runtime.
    #
    # SECRETS sits in column 4 and KMS in column 5 (not the other way round)
    # because BOTH the web tier in column 2 and the tools in column 5 read this
    # secret. From column 5 the arrow from the web tier had to cross three
    # unrelated tiles to reach it; from column 4 both arrows are short and
    # cross nothing. KMS has only the one caller, so it takes the far column.
    "SECRETS": Node(4, 2, "secrets.png", "AWS Secrets Manager",
                    "wallet seeds (tools only) · web login · not in any image",
                    "security"),
    "KMS":     Node(5, 2, "kms.png", "AWS KMS",
                    "HSM signing", "security", status="target"),
}

# EVERY ARROW IS NUMBERED, and every number is spelled out in SPEC.note. The
# numbers are the order the hops actually happen in for one payment, which is
# why the CSI mount is 0: it happens once at pod start-up, before any request
# exists. Keep the two lists in step — a number on an arrow with no entry in
# the legend is worse than no number at all.
EDGES = [
    # request spine
    Edge("USER", "ALB", "1 · HTTPS / WSS", nudge_y=0.10),
    Edge("ALB", "WEBAPP", "2 · route", nudge_y=0.10),
    Edge("WEBAPP", "AGENTS", "3 · validated\nPaymentRequest", nudge_y=0.20),
    Edge("AGENTS", "GATEWAY", "6 · MCP tools/call", nudge_y=0.10),
    Edge("GATEWAY", "TOOLS", "7 · invoke", nudge_y=0.10),
    # Nudged left of the midpoint: at the midpoint the second line runs into the
    # dashed "external" boundary around the ledger.
    Edge("TOOLS", "XRPL", "9 · JSON-RPC\nsubmit_payment", nudge_x=-0.30,
         nudge_y=0.22),
    # upward service hops
    Edge("AGENTS", "BEDROCK", "4 · inference", nudge_x=-0.38),
    # From the TOOLS column, not from the Gateway. This arrow used to leave the
    # Gateway labelled "telemetry", and there is no such flow to point at: the
    # account has the eight /aws/lambda/xrpl-agentic-payments-* log groups and no
    # gateway log group at all, because nothing in infra/ configures AgentCore
    # observability. What CloudWatch really receives is what Lambda sends it for
    # free. See note item 10.
    Edge("TOOLS", "CW", "10 · Lambda logs\n+ metrics", style="dashed",
         nudge_x=-0.30, nudge_y=0.30),
    Edge("TOOLS", "CTRAIL", "11 · audit", style="dashed", nudge_x=0.36),
    # downward service hops
    # The hop this slide used to omit. Solid because it is attempted on every
    # premium tool call, ahead of arrow 6 — see note item 5 for what "attempted"
    # buys today.
    # Label pushed down the arrow, away from the midpoint: at the midpoint it
    # lands on T1's label, which rides the vertical AGENTS->RUNTIME arrow.
    Edge("AGENTS", "PAYMENTS", "5 · x402 charge\n(process_payment)",
         nudge_x=-0.70, nudge_y=0.05),
    Edge("TOOLS", "SECRETS", "8 · load\nexecution seed", nudge_x=0.06,
         nudge_y=0.30),
    # A DIFFERENT secret from the one above. The web tier mounts the login
    # credentials; it does NOT mount the wallet seeds, which the tools read
    # directly (arrow 8). It used to mount wallets.json, which put the treasury
    # and execution seeds in a long-lived multi-tenant process that has no
    # reason to sign — the two public addresses it actually wanted are plain env
    # vars now. The label rides 70% of the way along the arrow to keep clear of
    # the RUNTIME label at the midpoint.
    Edge("WEBAPP", "SECRETS", "0 · CSI mount\nweb login only", style="dashed",
         nudge_x=1.05, nudge_y=-0.46),
    # Targets get letters, not numbers: they are not steps in today's flow.
    Edge("AGENTS", "RUNTIME", "T1 · migration\ntarget", style="target",
         nudge_x=-0.52, nudge_y=-0.22),
    Edge("TOOLS", "KMS", "T2 · sign", style="target", nudge_x=0.30),
    Edge("WEBAPP", "COGNITO", "T3 · Cognito JWT\n(not implemented)", style="target",
         nudge_x=-0.46),
]

ZONES = [
    Zone(["USER"], "User's machine", CAT["ext"], pad=0.30, dash=(0, (3, 3))),
    Zone(["XRPL"], "XRP Ledger Testnet  ·  external", CAT["ext"], pad=0.30,
         dash=(0, (3, 3))),
    Zone(["ALB", "WEBAPP", "COGNITO", "AGENTS", "BEDROCK", "GATEWAY", "CW",
          "CTRAIL", "TOOLS", "SECRETS", "KMS", "RUNTIME", "PAYMENTS"],
         "AWS account  ·  us-west-2", "#232f3e", pad=0.62, dash=(0, (7, 4)),
         contains_others=True),
    # The agents are inside the web tier, not inside AgentCore. That distinction
    # is the whole point of this revision: an earlier version of this diagram
    # drew them as a deployed managed component, which they are not.
    Zone(["ALB", "WEBAPP", "AGENTS"], "Web tier  ·  EKS pod (Graviton)",
         CAT["compute"], pad=0.22),
    Zone(["GATEWAY"], "Amazon Bedrock AgentCore", CAT["agent"], pad=0.22),
]

SPEC = Spec(
    title="SLIDE 1/5 — XRPL Agentic Payments: component architecture "
          "(as the CDK stacks build it)",
    subtitle="Five scoped AI agents making payments on the XRP Ledger, calling "
             "tools through Amazon Bedrock AgentCore. Testnet prototype.   "
             "Numbers on the arrows are the order the hops happen — read the "
             "legend below.   Next: docs/payment-flow.png",
    nodes=NODES,
    edges=EDGES,
    zones=ZONES,
    inside_cols={1, 2, 3, 4, 5},
    icon_dir=os.path.join(HERE, "icons"),
    cx={0: 1.4, 1: 4.0, 2: 6.9, 3: 9.9, 4: 12.8, 5: 15.6, 6: 18.3},
    cy={0: 6.7, 1: 4.2, 2: 1.7},
    legend=[
        ("solid", "#232f3e", "numbered request path — built, and created by a "
                             "stack in infra/"),
        ("dashed", CAT["ext"], "credential / control / audit hop — carries no "
                               "payment data"),
        ("target", CAT["security"], "T1-T3 TARGET — not built: agents on "
                                    "AgentCore Runtime, KMS signing, Cognito"),
    ],
    note=(
        "DATAFLOW — what each number on the diagram is:\n"
        "  0   Pod start-up, before any request exists. The Secrets Store CSI "
        "driver materialises the WEB LOGIN secret at /mnt/secrets-store. Note "
        "what is NOT on this arrow: the wallet seeds. They used to be mounted "
        "here too, at /app/config/wallets.json, so that the orchestrator could "
        "read two public addresses out of the file that also holds every private "
        "key. Those two addresses are plain environment variables now "
        "(XRPL_EXECUTION_ADDRESS, XRPL_RLUSD_ISSUER) and no seed reaches the web "
        "tier at all.\n"
        "  1   Browser opens HTTPS, then a WSS WebSocket to /ws/chat. Port 80 "
        "exists only to 301-redirect; the session cookie is Secure + HttpOnly.\n"
        "  2   ALB routes to the web tier pod on EKS Graviton.\n"
        "  3   The request is authenticated before anything else runs — a signed "
        "session cookie, checked against the credentials in Secrets Manager "
        "(xrpl-agentic-payments/web-auth); the WebSocket handshake is "
        "authenticated AND Origin-checked BEFORE it is accepted. Then the web app "
        "VALIDATES the request into a PaymentRequest (src/payments/request.py) and "
        "calls the orchestrator in-process. Validates, not parses: the prose is "
        "picked apart in the BROWSER (webapp/templates/index.html regexes for "
        "amount, address, name, country) and the socket receives explicit JSON "
        "fields. parse_payment_request takes that object, requires destination, "
        "amount, currency and recipient_name, defaults nothing, and refuses "
        "anything it cannot verify — no server code reads the sentence. Every "
        "later gate reads the validated object, never the model's restatement.\n"
        "  4   Each of the five agents calls Claude Sonnet 4.5 on Amazon "
        "Bedrock through one shared cached client.\n"
        "  5   Before a PRICED tool runs, the orchestrator charges for it through "
        "AgentCore Payments: PaymentGate.charge() calls process_payment with "
        "paymentType=CRYPTO_X402 against a payment session capped by "
        "maxSpendAmount, settling over the Coinbase connector that "
        "scripts/setup_agentcore_payments.py provisions (credential provider → "
        "payment manager → connector). Priced today: get_orderbook and get_paths "
        "at \\$0.003, screen_sanctions at \\$0.01; the other five tools are free. "
        "READ THE LIMIT: the EKS pod sets no PAYMENT_MANAGER_ARN and its role "
        "carries no bedrock-agentcore payment permissions, so the session cannot "
        "be created and every charge falls back to status=\"simulated\" — logged "
        "for reconciliation while the tool call proceeds. It is a metering hop, "
        "not a gate: a failed charge has never blocked a payment. This is also "
        "the ONLY x402 in the system — the ledger payment at arrow 9 is a plain "
        "XRPL Payment transaction, not an x402 settlement.\n"
        "  6   An agent calls a tool as an MCP tools/call — JSON-RPC over HTTPS "
        "to the Gateway's /mcp endpoint, SigV4-signed with the pod's own IAM "
        "identity (Pod Identity), authorised by bedrock-agentcore:InvokeGateway. "
        "There is no boto3 invoke_gateway operation; a gateway is an MCP "
        "endpoint, so src/agentcore_client.py signs the POST itself.\n"
        "  7   The Gateway validates the arguments against the target's tool "
        "schema and invokes the matching Lambda tool (8 tools, ARM64, shared "
        "xrpl-py layer). Tools are namespaced `<target>___<tool>`, which "
        "functions/shared.py strips. This hop is what an earlier revision of "
        "this diagram drew before it existed: the stack granted "
        "bedrock-agentcore permission to invoke the Lambdas and created no "
        "gateway, so the tools ran on the Runtime-hosted MCP server instead and "
        "these eight functions were deployed and unreachable.\n"
        "  8   submit_payment is the only tool that needs a seed, and the only one "
        "that USES one: it signs with the `execution` wallet. Read the limit "
        "before calling this least privilege — the load is not per-tool and not "
        "per-invoke. functions/shared.py fetches the WHOLE wallets secret at "
        "MODULE IMPORT (cold start, hardcoded id and region, shared.py:55-60), so "
        "all seven seeds sit in the memory of each of the seven tool Lambdas that "
        "import it — not just submit_payment's. Only screen_sanctions, which needs "
        "no wallet, never loads them. Slide 4 covers what that means.\n"
        "  9   The tool talks to XRPL testnet over JSON-RPC: read-only queries "
        "for most tools, a signed Payment transaction for submit_payment.\n"
        "  10  What CloudWatch actually holds, which is less than an "
        "observability tile implies. Lambda writes a log group per tool and emits "
        "Invocations / Errors / Duration on its own; that is the whole of it. "
        "infra/ configures no AgentCore observability, and the account has no "
        "gateway log group — only the eight /aws/lambda/xrpl-agentic-payments-* "
        "groups. No code in this repo calls put_metric_data, so there is not one "
        "custom metric; no alarm and no dashboard is created; and although the "
        "agent role is granted xray:PutTraceSegments "
        "(infra/lib/infra-stack.ts:125), nothing instruments X-Ray, so no trace is "
        "produced. The web app's observability tab is honest about this by "
        "accident: it queries AWS/Bedrock-AgentCore Invocations and a Logs "
        "Insights query over the Runtime log group, and since no Runtime exists "
        "(T1) both come back empty. What IS true is the containment claim — the "
        "agent has no tool that can write to or edit CloudWatch.\n"
        "  11  CloudTrail records the AWS API calls the tools make. Read the "
        "limit before relying on this: management events land in the account's "
        "90-day Event history with no configuration, but infra/ provisions no "
        "Trail, so Lambda tool INVOCATIONS are data events and are not captured "
        "until data-event selectors are turned on. Both this arrow and 10 are "
        "dashed for the same reason: they are what the platform gives you, not "
        "something this repo built.\n"
        "  T1  TARGET: move the agent tier onto AgentCore Runtime as "
        "CODE-DEFINED agents, microVM compute type. Not the managed Harness: the "
        "Harness owns the agent loop, and here the run order and the \\$10,000 "
        "approval gate are deliberately plain Python, not the model's to decide. "
        "Not the Instances compute type either — a payment takes seconds, so "
        "14-day sessions and GPUs buy nothing. What the migration BUYS is one "
        "isolated microVM per session: today every user's payment runs in the "
        "same interpreter as every other user's. What it COSTS is re-plumbing "
        "the in-process event bus that feeds /ws/chat through the Runtime's "
        "streaming API. Nothing is built, and not partly built: no code or script "
        "in this repo calls CreateAgentRuntime. infra-stack.ts:215 grants the "
        "permission and deploy/package.sh builds the archive, but the zip is never "
        "uploaded and the API is never called, so AGENTCORE_RUNTIME_ARN names "
        "nothing and the web UI's infrastructure panel correctly reports "
        "NOT_DEPLOYED. src/mcp_server/server.py — a second implementation of these "
        "same eight tools — is what a Runtime WOULD host, and no payment has ever "
        "called it.\n"
        "  T2  TARGET: sign in KMS so no seed is ever in application memory "
        "(scaffold in src/signing/kms_signer.py — KMS mode raises "
        "NotImplementedError, and nothing imports the module yet). Signing is "
        "local today.\n"
        "  T3  TARGET: Cognito JWT auth, with nothing behind it yet — no code in "
        "the repo verifies an ID token and infra/ provisions no user pool. The "
        "deployed app authenticates with the signed session cookie on arrow 3 "
        "instead.\n"
        "\n"
        "WHERE THIS RUNS — a solid tile means infra/ creates it, which is not the "
        "same as it standing right now:\n"
        "•  infra/ has four stacks. The tool path is two of them and they are the "
        "two that must exist: XrplAgenticPaymentsStack (roles, artifacts bucket, "
        "OFAC data) and XrplAgenticPaymentsToolsStack (the Gateway, its eight "
        "targets, the eight ARM64 Lambdas). Those two carry the whole of arrows "
        "6-9.\n"
        "•  The web tier — ALB, the Graviton pod, Karpenter, the CSI mount at "
        "arrow 0 — is XrplAgenticPaymentsEksV2, and XrplAgenticPaymentsWebStack is "
        "a single-EC2 alternative to it. Both are fully written; a cluster is also "
        "the only thing here that costs money by the hour, so this drawing does not "
        "assume one is up.\n"
        "•  RUNNING IT LOCALLY IS THE SAME PICTURE FROM ARROW 3 RIGHTWARD. "
        "`uvicorn webapp.app:app` puts the FastAPI process and all five agents on "
        "your machine, so arrows 1-2 lose the ALB, arrow 0 disappears (the login "
        "comes from XRPL_AGENTIC_USERNAME / XRPL_AGENTIC_PASSWORD in .env instead "
        "of the CSI mount), and nothing else changes: the same SigV4 calls reach "
        "the same deployed Gateway with your own credentials in place of Pod "
        "Identity. There is deliberately no offline mode — agentcore_client.py "
        "raises without AGENTCORE_GATEWAY_URL rather than faking a tool result — "
        "so even a local run needs those two stacks deployed.\n"
        "\n"
        "WHAT THE PICTURE ASSERTS:\n"
        "•  The five agents are sequenced by plain Python "
        "(run_multi_agent_payment), not by an LLM supervisor, and they run "
        "inside the web app process — that is why they are drawn in the web "
        "tier box and NOT inside the AgentCore box.\n"
        "•  Each agent is constructed with its own tools=[...] list, so an "
        "agent cannot call a tool outside its step. Enforced in application "
        "code, not by Cedar policies. See slide 3.\n"
        "•  A payment cannot execute until sanctions screening returns CLEAR, "
        "and at or over \\$10,000 the run halts for a human before the Execution "
        "agent is even constructed. See slide 2 for both gates.\n"
        "•  Of the 8 tools, only submit_payment writes to the ledger — it is "
        "the only one granted a signing role.\n"
        "•  The tools an agent calls are the Lambdas in column 5, reached "
        "through the Gateway. The repo also contains a second implementation of "
        "the same eight tools — the MCP server on AgentCore Runtime "
        "(src/mcp_server/server.py), drawn as a target because nothing in the "
        "repo deploys it. It is not on this arrow because it is not what a "
        "payment calls.\n"
        "•  The only x402 in the system meters TOOL CALLS (arrow 5), and it is "
        "not what settles the payment: the ledger transfer at arrow 9 is a plain "
        "XRPL Payment. Nothing in the flow can be blocked by a failed charge.\n"
        "•  Wallet seeds live in Secrets Manager only: no container image and no "
        "AgentCore archive contains them, and the web tier does not mount them "
        "either — the two public addresses it needs are env vars. Only "
        "submit_payment SIGNS with a seed, but every tool Lambda that imports "
        "functions/shared.py loads all seven at cold start, so read arrow 8 for "
        "what that boundary is and is not. Slide 4 covers which wallets exist and "
        "why."
    ),
)

# Both copies are served to users, so both are generated from this one spec.
OUTPUTS = [
    os.path.abspath(os.path.join(HERE, "..", "docs", "architecture.png")),
    os.path.abspath(os.path.join(HERE, "..", "webapp", "static", "assets",
                                 "architecture.png")),
]

if __name__ == "__main__":
    rc = render(SPEC, OUTPUTS[0])
    if rc == 0:
        for dst in OUTPUTS[1:]:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(OUTPUTS[0], dst)
            print(f"copied -> {dst}")
    raise SystemExit(rc)
