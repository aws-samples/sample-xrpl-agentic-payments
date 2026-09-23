# Research decisions

Reviewed 2026-09-21. The implementation deliberately combines deterministic
payment services with an agent-facing planning surface; it does not grant the
agent a wallet.

## XRPL

- [XRPL Agent Wallet Skill](https://xrpl.org/docs/agents/xrpl-agent-wallet-skill)
  informs the wallet abstraction and Testnet fixture workflow. In this POC,
  wallet seeds are held by Secrets Manager and are available only to the
  reserved-concurrency signer Lambda.
- [XRPL Payments Skill](https://xrpl.org/docs/agents/xrpl-payments-skill)
  informs transaction construction and validation. The implementation adds
  approval commitments, persist-before-broadcast artifacts, unchanged-blob
  retries, and an independent reconciler.
- [Agentic payments with x402](https://xrpl.org/docs/agents/agentic-payments-x402)
  informs `XrplX402Adapter`: versioned `exact` challenges and transaction-hash
  proofs paid in XRP on the x402 CAIP-style Testnet identifier `xrpl:1`. Prices
  are encoded in drops and the adapter emits v2 `PAYMENT-REQUIRED`,
  `PAYMENT-SIGNATURE`, and `PAYMENT-RESPONSE` envelopes. Settlement remains
  locally verified for this POC rather than trusting a hosted facilitator. The
  transfer lane uses the wallet skill's `20260530` attribution tag; the x402
  fee uses a distinct merchant-declared endpoint tag.
- [Cross-currency payments](https://xrpl.org/docs/concepts/payment-types/cross-currency-payments)
  are implemented as exact-output Payments: `Amount` is the exact destination
  issued currency and `SendMax` is the approval-bound source ceiling. Partial
  Payment flags are rejected.

The signer and reconciler are intentionally ordinary deterministic services,
not model tools. This preserves the useful wallet/payment concepts while
preventing prompt-driven signing.

## Amazon Bedrock AgentCore

- [InvokeAgentRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html)
  defines the direct HTTPS streaming endpoint used by the server-side
  AG-UI proxy route (`/api/agui`). A Cognito access token is forwarded as a Bearer token.
- [Runtime invocation guidance](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
  supports streaming responses, endpoint qualifiers, and durable session IDs.
- [Geographic cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html)
  requires access to the `us.` inference profile and its model in each routed
  US destination. The Runtime role names only the Sonnet 4.5 profile plus its
  `us-east-1`, `us-east-2`, and `us-west-2` foundation-model ARNs; model access
  is conditioned on invocation through that profile.
- [AgentCore example policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies.html)
  defines the stable `AgentCore::IamEntity` form used to permit only the
  Runtime's assumed role to invoke each named Gateway action.

Runtime exposes the native AG-UI container contract (`POST /invocations`,
`GET /ping`, SSE, port 8080). Gateway is IAM-authenticated and Policy is in
enforcement mode. Cognito supplies the end-user subject, which Runtime injects
into every Gateway call. Memory is opt-in and preference-only.

AgentCore Payments is not used: its current instrument abstraction does not
provide XRPL settlement. The x402 implementation is behind a replaceable
adapter so a supported managed instrument can be introduced later without
changing approval or workflow contracts.

## AG-UI placement

The browser runs a native AG-UI client (`@ag-ui/client`) against a server-side
Next.js proxy route (`/api/agui`), which forwards AG-UI to AgentCore Runtime. AG-UI is used for conversation and application-owned
financial cards. Approval remains a Cognito-authorized REST mutation, and every
nonterminal card polls the durable transfer API. Model-generated HTML and
execution actions are absent from the component allowlist.
