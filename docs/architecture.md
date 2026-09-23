# Architecture and safety boundary

![AgentCore and XRPL cross-border transfer architecture](assets/architecture.png)

_Regenerate with `pip install -r diagrams/requirements.txt && python3 diagrams/render_architecture.py` (icons: see [`diagrams/icons/SOURCE.md`](../diagrams/icons/SOURCE.md))._

[Standalone ASCII architecture](architecture-ascii.md)

The POC separates conversation, durable authorization, signing, settlement
verification, and payout finalization. An LLM can help a user discover a
corridor, obtain a quote, create an intent, and read status. It cannot approve
or execute a transfer.

```text
Browser
  │ Cognito JWT
  ▼
Next.js AG-UI BFF ────────────► Transfer REST API ──► DynamoDB transfer table
  │ AG-UI + JWT                        │ approval             │ stream outbox
  ▼                                    │                      ▼
AgentCore Runtime                      │                Step Functions
  │ IAM + trusted owner_sub            │                   │       │
  ▼                                    │                   ▼       ▼
AgentCore Gateway + Policy             │                Signer   Reconciler
  │ four planning/status tools         │                   │       │
  └────────────────────────────────────┘                   ▼       ▼
                                                      XRPL Testnet
```

The signer persists a deterministic signed transaction in the separately
encrypted artifact table before broadcast. A network timeout after signing is
`SUBMISSION_UNKNOWN`, not failure. The reconciler has only public wallet
identities and read-only artifact access. It declares settlement only after a
validated `tesSUCCESS` Payment matches the prepared hash, source, destination,
workflow memo, approved `Amount`, and exact delivered amount. Every pending
reconciliation cycle resubmits the persisted signed blob unchanged until
validation or `LastLedgerSequence` expiry.

## AgentCore boundaries

- Runtime: native AG-UI on port 8080 with `POST /invocations`, `GET /ping`,
  Cognito JWT validation, model invocation, read-only preference recall, and
  Gateway invocation.
- Gateway: exactly four tools—corridors, quotes, intent creation, and status.
  The Runtime injects the authenticated Cognito subject; model arguments cannot
  replace it.
- Policy: enforcement mode with only four explicit Runtime-to-tool permits.
  Everything else is denied by absence of a permit.
- Memory: off by default and limited to structured corridor, payout, and
  slippage preferences under `/preferences/{actorId}/`.
- Payments: AgentCore Payments is not used because XRPL is not a supported
  instrument. `XrplX402Adapter` is an isolated replacement point.

## Approval commitment

The SHA-256 approval commitment canonicalizes and binds the contract version,
authenticated owner, transfer ID, payout mode, source and destination amounts
and issued assets, issuers, recipient and destination tag, selected path,
bounded `SendMax`, slippage, XRP x402 fee, quote expiry, and quote/transfer
nonces. Transfer intent IDs are deterministic per owner and quote, so a retried
Gateway or REST invocation returns the existing intent instead of creating
another chargeable transfer. Approval and execution-outbox creation are one
DynamoDB transaction.
TTL is cleanup only; every approval and execution boundary checks expiry.

## Product UI

AG-UI streams conversation and invokes only application-owned quote, approval,
fee progress, settlement, receipt, and failure components. Those components
poll the REST status API while nonterminal, so browser refreshes and completed
Runtime sessions do not affect execution. Authentication, history, settings,
and fixture administration are ordinary Next.js screens.

Shared AG-UI state and the model-facing Gateway projection reject unknown keys
and credential/signing/PII-shaped fields. They never include seeds, signed
blobs, bank details, JWTs, approval credentials, paths, or recipient wallet
addresses. The owner-authenticated REST projection separately supplies the
exact approval terms to the application-owned card, including recipient
address/tag, issuer identities, slippage, expiry, and commitment. The approval
button calls the authenticated REST endpoint directly. The browser uses a
native AG-UI client; no third-party chat framework or telemetry is involved.

## Explicit demo boundary

Only XRPL Testnet is accepted in code and infrastructure. USD and MXN are
private Testnet issued-currency fixtures; sanctions outcomes and local-fiat
payouts are simulated. Mainnet, real custody, production KYC/AML, and real bank
movement are out of scope.
