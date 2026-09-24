#!/usr/bin/env python3
"""Component architecture of the deployed XrplAgentCorePoc stack.

    python3 diagrams/render_architecture.py      # -> docs/assets/architecture.png

Ground truth is infra/lib/xrpl-agentcore-stack.ts plus the Python handlers it
wires up. Update this spec when either changes, then READ the PNG — the checks
catch geometry, not a wrong architecture.

Icons are official vendor marks and are not committed; see icons/SOURCE.md to
rebuild them.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from diagram_lib import CAT, Edge, Node, Spec, Zone, render  # noqa: E402

# COLUMNS ENCODE THE TRUST BOUNDARY: 0-1 the operator's machine (the Next.js app
# is not deployed by the stack), 2-9 the AWS account, 10 the public XRPL Testnet.
# ROWS: 0-1 conversation plane, 2 the authenticated REST spine, 3-4 execution.
NODES = {
    "USER":    Node(0, 2, "user.png", "Sender", "browser", "user"),
    "WEB":     Node(1, 2, "client.png", "Next.js AG-UI client",
                    "runs locally (localhost:3000); AG-UI BFF route "
                    "forwards the bearer JWT", "user"),

    "COGNITO": Node(2, 0, "cognito.png", "Amazon Cognito",
                    "user pool, no self sign-up — the identity front door",
                    "security"),
    "HTTPAPI": Node(2, 2, "apigw.png", "HTTP API",
                    "Cognito JWT authorizer on every /v1 route", "network"),
    "ECR":     Node(2, 4, "ecr.png", "Amazon ECR",
                    "ARM64 Runtime image, scan on push", "compute"),

    "BEDROCK": Node(3, 0, "bedrock.png", "Amazon Bedrock",
                    "Claude Sonnet 4.5 inference profile", "ml"),
    "RUNTIME": Node(3, 1, "runtime.png", "AgentCore Runtime",
                    "Strands agent, native AG-UI; Cognito authorizer; "
                    "deployed in phase 2", "agent"),
    "APIFN":   Node(3, 2, "lambda.png", "Transfer API Lambda",
                    "FastAPI: corridors, quotes, transfers, APPROVE, "
                    "preferences", "compute"),
    "KMS":     Node(3, 4, "kms.png", "AWS KMS",
                    "3 rotating CMKs: transfers, artifacts + ECR, memory",
                    "security"),

    "MEMORY":  Node(4, 0, "memory.png", "AgentCore Memory",
                    "opt-in preferences only, /preferences/{actorId}/", "agent"),

    "POLICY":  Node(5, 0, "policy.png", "AgentCore Policy",
                    "Cedar, ENFORCE, default-deny: 4 permits", "agent"),
    "GATEWAY": Node(5, 1, "gateway.png", "AgentCore Gateway",
                    "MCP, IAM (SigV4) auth — 4 tools, none can sign",
                    "agent"),

    "REGISTRY": Node(7, 0, "▣", "AWS Agent Registry",
                    "1 registry · 3 records: Gateway (MCP, live-synced), "
                    "Runtime (custom), 2 skills (from SKILL.md)", "agent"),
    "TOOLS":   Node(6, 1, "lambda.png", "4 tool Lambdas",
                    "corridors, quote, create intent, status", "compute"),
    "TABLE":   Node(6, 2, "dynamodb.png", "Transfer table",
                    "quotes, intents, state; stream = execution outbox",
                    "database"),

    "OUTBOX":  Node(7, 3, "lambda.png", "Outbox Lambda",
                    "stream filter: INSERT of EXECUTE_APPROVED_TRANSFER",
                    "compute"),
    "SFN":     Node(8, 3, "stepfunctions.png", "Step Functions",
                    "execution name = transfer_id (idempotent start)", "mgmt"),

    "SECRETS": Node(9, 1, "secrets.png", "Secrets Manager",
                    "3 Testnet seeds, pre-provisioned", "security"),
    "SIGNER":  Node(9, 2, "lambda.png", "Signer Lambda",
                    "fee, payment, refund; reserved concurrency 1", "compute"),
    "ARTIFACT": Node(9, 3, "dynamodb.png", "Artifact table",
                     "signed blobs, own CMK; written BEFORE broadcast",
                     "database"),
    "RECON":   Node(9, 4, "lambda.png", "Reconciler Lambda",
                    "no seeds; read-only artifacts", "compute"),

    "XRPL":    Node(10, 3, "◈", "XRPL Testnet",
                    "s.altnet.rippletest.net", "ext"),
}

EDGES = [
    Edge("USER", "WEB", "0 uses"),
    Edge("WEB", "COGNITO", "1 sign in (SRP) → JWT", nudge_x=-0.55, nudge_y=0.35),

    # Conversation plane: the model can plan, never approve or sign.
    Edge("WEB", "RUNTIME", "2 chat: AG-UI + JWT", nudge_x=-0.35, nudge_y=0.05),
    Edge("RUNTIME", "COGNITO", "validate JWT", style="dashed", nudge_x=0.25),
    Edge("RUNTIME", "BEDROCK", "3 model", nudge_x=0.30),
    Edge("RUNTIME", "MEMORY", "read prefs", style="dashed", nudge_x=0.35,
         nudge_y=-0.10),
    Edge("RUNTIME", "GATEWAY", "4 MCP tool call + trusted owner_sub",
         nudge_y=0.12),
    Edge("GATEWAY", "POLICY", "authorize", style="dashed", nudge_x=0.40),
    Edge("REGISTRY", "GATEWAY", "MCP tool sync (IAM SigV4)", style="dashed",
         nudge_x=-0.30, nudge_y=-0.15),
    Edge("GATEWAY", "TOOLS", "5", nudge_y=0.10),
    Edge("TOOLS", "TABLE", "6 quote, intent", nudge_x=0.50),

    # Approval: the only path to money, and it bypasses the model entirely.
    Edge("WEB", "HTTPAPI", "7 APPROVE (card button) + JWT", nudge_y=0.12),
    Edge("HTTPAPI", "COGNITO", "validate JWT", style="dashed", nudge_x=-0.40,
         nudge_y=-0.40),
    Edge("HTTPAPI", "APIFN", "", nudge_y=0.10),
    Edge("APIFN", "TABLE", "8 one transaction: APPROVED + outbox item",
         nudge_y=0.12),
    Edge("APIFN", "MEMORY", "write prefs (opt-in)", style="dashed",
         nudge_x=0.55, nudge_y=0.55),

    # Execution plane: durable, model-independent.
    Edge("TABLE", "OUTBOX", "9 stream", nudge_x=-0.25),
    Edge("OUTBOX", "SFN", "10 start", nudge_y=0.10),
    Edge("SFN", "SIGNER", "11 submit tasks", nudge_x=-0.55),
    Edge("SFN", "RECON", "13 reconcile tasks", nudge_x=-0.55),
    Edge("SIGNER", "SECRETS", "seeds", style="dashed", nudge_x=0.28),
    Edge("SIGNER", "ARTIFACT", "persist signed blob", nudge_x=0.60),
    Edge("SIGNER", "XRPL", "12 x402 XRP fee, then Payment",
         nudge_x=0.55, nudge_y=0.25),
    Edge("RECON", "ARTIFACT", "read", style="dashed", nudge_x=0.22),
    Edge("RECON", "XRPL", "14 validated ledger / same-blob resubmit",
         nudge_x=0.60, nudge_y=-0.30),
    Edge("SIGNER", "TABLE", "state transitions", style="dashed", nudge_y=0.12),
]

ZONES = [
    Zone(["USER", "WEB"], "Outside AWS — operator's machine", CAT["ext"],
         pad=0.30, dash=(0, (3, 3))),
    Zone(["XRPL"], "Outside AWS — public ledger", CAT["ext"], pad=0.30,
         dash=(0, (3, 3))),
    Zone(["COGNITO", "HTTPAPI", "ECR", "BEDROCK", "RUNTIME", "APIFN", "KMS",
          "MEMORY", "POLICY", "GATEWAY", "REGISTRY", "TOOLS", "TABLE",
          "OUTBOX", "SFN", "SECRETS", "SIGNER", "ARTIFACT", "RECON"],
         "AWS account  ·  us-west-2  ·  one CloudFormation stack (XrplAgentCorePoc)",
         "#232f3e", pad=0.62, dash=(0, (7, 4)), contains_others=True),
    Zone(["BEDROCK", "RUNTIME", "MEMORY", "POLICY", "GATEWAY", "TOOLS"],
         "Conversation plane — plans and reads; cannot approve or sign",
         CAT["agent"], pad=0.26),
    Zone(["OUTBOX", "SFN", "SECRETS", "SIGNER", "ARTIFACT", "RECON"],
         "Execution plane — the only holder of seeds", CAT["security"],
         pad=0.26),
]

SPEC = Spec(
    title="AgentCore + XRPL cross-border transfer — deployed architecture",
    subtitle=("As built by infra/lib/xrpl-agentcore-stack.ts. Numbers give the "
              "order of one transfer; XRPL Testnet only, fiat payout simulated."),
    nodes=NODES,
    edges=EDGES,
    zones=ZONES,
    inside_cols={2, 3, 4, 5, 6, 7, 8, 9},
    icon_dir=os.path.join(HERE, "icons"),
    # Wider gaps at cols 1|2 and 9|10: those are the trust boundaries, and the
    # outside zones must not touch the account zone.
    cx={0: 1.2, 1: 3.8, 2: 7.2, 3: 9.8, 4: 12.4, 5: 15.0, 6: 17.6, 7: 20.2,
        8: 22.8, 9: 25.4, 10: 28.9},
    legend=[
        ("solid", "#232f3e", "request / execution path (numbered in order)"),
        ("dashed", CAT["ext"], "auth, credential, or side-effect hop"),
    ],
    note=(
        "How to read it:\n"
        "•  1-6  The agent can only list corridors, quote, create an UNAPPROVED intent and read "
        "status. The Runtime injects owner_sub from the validated JWT; the model cannot set it. "
        "Policy permits those 4 tools for the Runtime role, plus one narrow read-only exception "
        "(list_supported_corridors) for the registry demo consumer role — see below.\n"
        "•  7-8  Approval never goes through the model: the approval card calls the REST API "
        "directly, and the approve route writes APPROVED plus the outbox item in one DynamoDB "
        "transaction, bound to a SHA-256 commitment over every approved term.\n"
        "•  9-14  Step Functions runs independently of any chat session: mark fee pending → pay the "
        "x402 XRP service fee → submit the exact-output Payment (bounded SendMax) → reconcile against "
        "a validated ledger → complete payout. On failure after the fee, it refunds the fee.\n"
        "•  Signer and Reconciler both write conditional state transitions to the transfer table; "
        "only the Signer's arrow is drawn. Runtime is created only when DeployAgentRuntime=true "
        "(after its image is in ECR). KMS encrypts both tables, Memory and ECR.\n"
        "•  Agent Registry is a catalog, not a request-path hop: it attempts to sync the Gateway's "
        "MCP tool definitions live (IAM SigV4), but that sync role has no Policy Engine permit "
        "either, so the synced tool list comes back empty — confirmed live, a disclosed limitation, "
        "not fixed here. The Runtime and skill records are static data set at deploy time (Runtime "
        "ARN, and each skill's SKILL.md) — no edge is drawn for those."
    ),
)

if __name__ == "__main__":
    out = os.path.join(HERE, "..", "docs", "assets", "architecture.png")
    raise SystemExit(render(SPEC, os.path.normpath(out)))
