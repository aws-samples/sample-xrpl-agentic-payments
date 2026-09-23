# Control-flow sequence

Numbers match [`docs/architecture.md`](architecture.md) and its diagram —
step *N* here is the same hop as step *N* there. Solid arrows are the
numbered request path; dotted arrows are auth, credential, or side-effect
hops that happen alongside it. The four boxes are the trust boundaries: two
outside AWS (the operator's machine and the public XRPL Testnet), and AWS's
own conversation plane and execution plane, split exactly as in the component
diagram's zones.

_Regenerate the PNG with `npx -y @mermaid-js/mermaid-cli -i docs/sequence.md -o docs/assets/sequence.png` after editing the block below — see [Prerequisites](../README.md#prerequisites) for Node. Read the rendered PNG after regenerating; this file has no automated topology check._

```mermaid
sequenceDiagram
    autonumber off

    box rgb(240,240,240) Outside AWS — operator's machine
        participant U as Sender
        participant Web as Next.js AG-UI client
    end
    box rgb(214,234,248) AWS — conversation plane
        participant Cognito as Amazon Cognito
        participant Runtime as AgentCore Runtime
        participant Bedrock as Amazon Bedrock
        participant Memory as AgentCore Memory
        participant Gateway as Gateway + Policy
    end
    box rgb(212,239,223) AWS — REST + execution plane
        participant API as Transfer REST API
        participant DB as Transfer table
        participant Outbox as Outbox Lambda
        participant SFN as Step Functions
        participant Signer as Signer Lambda
        participant Artifact as Artifact table
        participant Recon as Reconciler Lambda
    end
    box rgb(235,225,250) Outside AWS — Testnet
        participant XRPL as XRPL Testnet
    end

    Note over U,Web: Sign-in
    U->>Web: 0 uses
    Web->>Cognito: 1 sign in (SRP) → JWT
    Cognito-->>Web: JWT

    Note over Web,Gateway: Conversation plane — the model can plan and read,<br/>never approve or sign
    Web->>Runtime: 2 chat: AG-UI + JWT
    Runtime-->>Cognito: validate JWT
    activate Runtime
    Runtime->>Bedrock: 3 model
    Bedrock-->>Runtime: plan
    Runtime-->>Memory: read prefs
    Runtime->>Gateway: 4 MCP tool call + trusted owner_sub
    Gateway-->>Gateway: authorize (Policy, ENFORCE)
    Gateway->>API: 5 invoke tool Lambda
    API->>DB: 6 quote, intent
    DB-->>API: quote / intent
    API-->>Gateway: result
    Gateway-->>Runtime: result
    Runtime-->>Web: quote card / approval card (AG-UI)
    deactivate Runtime

    Note over Web,DB: Approval — bypasses the model entirely
    Web->>API: 7 APPROVE (card button) + JWT
    API-->>Cognito: validate JWT
    activate API
    API->>DB: 8 one transaction: APPROVED + outbox item
    API-->>Memory: write prefs (opt-in)
    deactivate API

    Note over DB,SFN: Execution outbox — durable, model-independent
    DB->>Outbox: 9 stream
    Outbox->>SFN: 10 start
    activate SFN

    Note over SFN,XRPL: Settlement
    SFN->>Signer: 11 submit tasks
    activate Signer
    Signer-->>Signer: seeds (Secrets Manager)
    Signer->>Artifact: persist signed blob before broadcast
    Signer->>XRPL: 12 x402 XRP fee, then Payment
    XRPL-->>Signer: submitted / SUBMISSION_UNKNOWN
    Signer-->>DB: state transitions
    deactivate Signer

    SFN->>Recon: 13 reconcile tasks
    activate Recon
    Recon-->>Artifact: read persisted signed blob
    Recon->>XRPL: 14 validated ledger / same-blob resubmit
    XRPL-->>Recon: tesSUCCESS / retry
    Recon-->>DB: SETTLED / COMPLETED
    deactivate Recon
    deactivate SFN
```
