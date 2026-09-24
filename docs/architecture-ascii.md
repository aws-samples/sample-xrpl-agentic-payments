# AgentCore + XRPL Cross-Border Transfer POC

Standalone ASCII architecture export. All payment assets, rates, sanctions
decisions, and fiat payouts shown here are demo fixtures on XRPL Testnet.

## 1. User, application, and AgentCore planning plane

```text
                                     AWS us-west-2

  +--------------------+        +----------------------+       +------------------+
  | Authenticated user |<------>| Amazon Cognito       |       | Amazon Bedrock   |
  | Web browser        |  JWT   | User Pool            |       | Sonnet model     |
  +---------+----------+        +----------------------+       +---------+--------+
            | HTTPS                                                       ^
            v                                                             |
  +----------------------------+       AG-UI + SSE + JWT       +----------+-----------+
  | Next.js web application    |------------------------------>| AgentCore Runtime    |
  |                            |                               |                      |
  | - AG-UI chat client        |<------------------------------| - POST /invocations  |
  | - Application-owned cards  |        streamed events        | - GET /ping          |
  | - History and settings     |                               | - Strands agent      |
  | - Fixture administration   |                               | - trusted owner_sub  |
  +-------------+--------------+                               +----+--------+-------+
                |                                                   |        |
                | authenticated REST                                |        | opt-in only
                | quote / intent / approval / status                 |        v
                |                                                   |   +------------------+
                |                                                   |   | AgentCore Memory |
                |                                                   |   | preferences only |
                |                                                   |   +------------------+
                |                                                   |
                |                                                   | SigV4 + owner_sub
                |                                                   v
                |                                     +-------------+-------------+
                |                                     | AgentCore Gateway         |
                |                                     | MCP tool surface          |
                |                                     +-------------+-------------+
                |                                                   |
                |                                     +-------------v-------------+
                |                                     | AgentCore Policy          |
                |                                     | ENFORCE / default deny    |
                |                                     +-------------+-------------+
                |                                                   |
                |                    +------------------------------+-------------------+
                |                    |                |                 |               |
                |                    v                v                 v               v
                |           +---------------+ +---------------+ +---------------+ +---------------+
                |           | Lambda target | | Lambda target | | Lambda target | | Lambda target |
                |           | list          | | get transfer  | | create intent | | get transfer  |
                |           | corridors     | | quote         | |               | | status        |
                |           +-------+-------+ +-------+-------+ +-------+-------+ +-------+-------+
                |                   |                 |                 |                 |
                +-------------------+-----------------+-----------------+-----------------+
                                                    |
                                                    v
                                      +-------------+-------------+
                                      | Amazon API Gateway        |
                                      | HTTP API /v1/*            |
                                      +-------------+-------------+
                                                    |
                                                    v
                                      +-------------+-------------+
                                      | Transfer API Lambda       |
                                      | deterministic domain API  |
                                      +---------------------------+
```

Only the four Gateway tools above are available to the model. The Runtime
cannot approve or execute a transfer, read payment tables, retrieve seeds or
signed blobs, invoke signer functions, or start a workflow.

## 2. Approval and durable execution plane

```text
  Browser approval card
  (exact terms + approval hash + idempotency key)
               |
               | POST /v1/transfers/{id}/approve
               v
  +-------------------------+       atomic transaction       +--------------------------+
  | Transfer API Lambda     |------------------------------->| DynamoDB transfer table  |
  | owner and expiry checks |                                |                          |
  +-------------------------+                                | - quote                  |
                                                             | - transfer state         |
                                                             | - approval commitment    |
                                                             | - execution outbox       |
                                                             +------------+-------------+
                                                                          |
                                                                          | DynamoDB Stream
                                                                          v
                                                             +------------+-------------+
                                                             | Outbox starter Lambda    |
                                                             | idempotent execution ID  |
                                                             +------------+-------------+
                                                                          |
                                                                          v
                                                             +------------+-------------+
                                                             | AWS Step Functions       |
                                                             | durable transfer workflow|
                                                             +------------+-------------+
                                                                          |
                 +-------------------------+--------------------------------+----------------------+
                 |                         |                                |                      |
                 v                         v                                v                      v
      +----------------------+  +----------------------+         +----------------------+ +---------------------+
      | XRP fee signer       |  | XRP fee reconciler   |         | Payment signer       | | Payment reconciler  |
      | persist then submit  |  | validate or resubmit |         | persist then submit  | | strict ledger check |
      +----------+-----------+  +----------+-----------+         +----------+-----------+ +----------+----------+
                 |                         |                                |                      |
                 +------------+------------+                                +-----------+----------+
                              |                                                         |
                              v                                                         v
                 +------------+---------------------------------------------------------+----------+
                 | DynamoDB execution-artifact table                                               |
                 | separately KMS encrypted; signed blob, tx hash, sequence, ledger expiry         |
                 | signer: read/write | reconciler: read-only | Runtime and API: no access          |
                 +------------+---------------------------------------------------------+----------+
                              |                                                         |
                              +--------------------------+------------------------------+
                                                         |
                                                         v
                                               +---------+----------+
                                               | XRPL Testnet       |
                                               | JSON-RPC + ledger  |
                                               +---------+----------+
                                                         |
                                      +------------------+------------------+
                                      |                                     |
                                      v                                     v
                           +----------+-----------+              +----------+-----------+
                           | Direct wallet lane  |              | Simulated fiat lane  |
                           | exact MXN delivery  |              | payout wallet settles|
                           +----------------------+              +----------+-----------+
                                                                          |
                                                                          v
                                                              +-----------+----------+
                                                              | Payout finalizer     |
                                                              | SIM-* reference only |
                                                              +----------------------+
```

The approval write and execution-outbox write are one transaction. DynamoDB
TTL is cleanup only; application conditions enforce quote and approval expiry.
An ambiguous submit becomes `SUBMISSION_UNKNOWN`, and reconciliation resubmits
the same persisted blob until validation or `LastLedgerSequence` expiry.

## 3. XRPL settlement lanes

```text
  XRP x402 service fee

  +----------------------+     exact 0.001 XRP Payment      +----------------------+
  | Fee-payer wallet     |--------------------------------->| Fee-merchant wallet  |
  | seed: Secrets Manager|                                  | public address only  |
  +----------------------+                                  +----------------------+

  Exact-output cross-currency transfer

  +----------------------+     bounded SendMax      +-----------------------------+
  | Execution wallet     |------------------------->| XRPL path liquidity         |
  | seed: Secrets Manager|                          | order book / AMM fixtures    |
  +----------------------+                          +--------------+--------------+
                                                                  |
                                              exact delivered MXN |
                                          +-----------------------+----------------------+
                                          |                                              |
                                          v                                              v
                              +-----------+----------+                        +-----------+----------+
                              | Recipient wallet    |                        | Payout wallet       |
                              | direct Testnet lane |                        | simulated-fiat lane |
                              +---------------------+                        +----------------------+

  Issued-currency fixtures

  +----------------------+      trust lines, issued USD/MXN, offers      +----------------------+
  | Testnet USD issuer   |<-------------------------------------------->| Testnet MXN issuer   |
  +----------------------+                                              +----------------------+
```

Settlement is declared only when the transaction has `validated=true`,
`tesSUCCESS`, the expected hash, source, destination, memo, non-partial-payment
flags, and the exact delivered amount.

## 4. Security, infrastructure, and operations

```text
  +----------------------+  encrypts  +----------------------+  encrypts  +----------------------+
  | AWS KMS keys         |----------->| Transfer table       |            | Artifact table       |
  | separate key scopes  |----------->|                      |            |                      |
  +----------------------+            +----------------------+            +----------------------+

  +----------------------+            +----------------------+            +----------------------+
  | Secrets Manager      |----------->| Signer roles only    |            | IAM least privilege  |
  | Testnet seeds        |            | no Runtime access    |            | per component        |
  +----------------------+            +----------------------+            +----------------------+

  +----------------------+            +----------------------+            +----------------------+
  | Amazon ECR           |----------->| AgentCore Runtime    |----------->| CloudWatch / X-Ray   |
  | ARM64 runtime image  |            | container            | telemetry  | logs and traces      |
  +----------------------+            +----------------------+            +----------------------+

  +----------------------+            +----------------------+
  | TypeScript CDK       |----------->| AWS CloudFormation   |
  | infrastructure code  |            | deployment           |
  +----------------------+            +----------------------+

  +----------------------+  MCP tool sync (IAM SigV4)   +---------------------+
  | AWS Agent Registry   |<---------------------------> | AgentCore Gateway   |
  | 1 registry, 3 records|      the only live call      | MCP tool surface    |
  +----------+-----------+                              +----------------------+
             |
             | static data set at deploy time — no live call
             v
  Runtime ARN (custom record) + xrpl-agent-wallet / xrpl-payments SKILL.md (skill records)
```

## 5. End-to-end sequence

```text
  1. User signs in with Cognito.
  2. Browser sends AG-UI conversation through the server-side AG-UI proxy (/api/agui).
  3. Runtime calls only Policy-authorized Gateway discovery and intent tools.
  4. Browser receives an application-owned quote card.
  5. User creates an intent, then explicitly approves exact immutable terms.
  6. API transactionally stores approval and an execution outbox record.
  7. DynamoDB Stream starts one idempotent Step Functions execution.
  8. Workflow settles one XRP x402 fee.
  9. Workflow settles one exact-output cross-currency XRPL Payment.
 10. Reconciler verifies the validated ledger result and delivered amount.
 11. Direct-wallet delivery completes, or simulated payout emits a SIM-* reference.
 12. Browser polls authoritative status and recovers after refresh or disconnect.
```

