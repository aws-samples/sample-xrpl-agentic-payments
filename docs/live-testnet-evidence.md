# Live XRPL Testnet acceptance evidence

Verified on 2026-09-21 in `us-west-2` against XRPL network ID `1`. All
currencies, corridor checks, sanctions decisions, and fiat payouts are demo
fixtures. This evidence does not cover mainnet, real custody, production
KYC/AML, or real fiat movement.

## Deployed control plane

CloudFormation stack `XrplAgentCorePoc` was `UPDATE_COMPLETE` at
`2026-09-21T16:32:32Z`.

| Component | Live result |
| --- | --- |
| Transfer API | `https://n6jiga9of9.execute-api.us-west-2.amazonaws.com` |
| AgentCore Runtime | `XrplTransferAssistant-VlxkRw9jNt` was `READY`; deployed image tag `af17c0c` |
| AgentCore Gateway | `xrpl-transfer-gateway-iad3hrzevx` was `READY`, MCP protocol, Policy mode `ENFORCE` |
| Gateway targets | Corridors, quote, intent creation, and status targets were all `READY` |
| AgentCore Policy | Four allow policies were `ACTIVE`; an unmatched local IAM principal was denied by default |
| AgentCore Memory | `XrplTransferPreferences-7UDt4U59IL` was `ACTIVE` with a customer-managed KMS key |
| Durable execution | All three acceptance Step Functions executions were `SUCCEEDED` |
| Storage isolation | Transfer and signed-artifact records use separate customer-KMS-encrypted DynamoDB tables |

The Runtime role can invoke only the model, Gateway, and long-term-memory read
APIs. It has no DynamoDB, signing-secret, Lambda invocation, or Step Functions
start permission. Its Memory KMS access is scoped to the Memory key and
`bedrock-agentcore.us-west-2.amazonaws.com`.

## Browser and agent acceptance

A Cognito-authenticated browser session completed the application path:

`Browser → server-side AG-UI proxy (/api/agui) → AgentCore Runtime → Gateway → Policy → Lambda`

The assistant streamed a structured USD/MXN corridor response. It then handled
a complete conversational direct-wallet flow: quote exactly `5 MXN`, create an
unapproved intent, and render application-owned quote and approval cards. The
agent did not approve the transfer. The authenticated user selected **Approve
and execute**, and the card entered `FEE_PENDING`.

The browser was refreshed while that execution was nonterminal. The History
screen recovered transfer `tr_36ab3f9a41e783b2ec16d4ae93e79694` from the
authoritative REST API and displayed it as `COMPLETED` with both XRPL hashes. A
separate conversational status request recovered an earlier completed transfer
after its quote had expired. History also retained the direct and simulated
deterministic acceptance transfers and the simulated payout reference.

The user explicitly opted in to structured preference memory. AgentCore Memory
stored only:

```json
{
  "default_corridor_id": "usd-mxn-testnet",
  "default_payout_mode": "XRPL_WALLET",
  "default_slippage_bps": 100
}
```

A later browser conversation recalled exactly those three preferences without
calling a payment tool. No recipient, amount, wallet, bank detail, token,
approval credential, or conversation transcript was stored as a preference.

An invocation from the unmatched principal
`forbidden-local-principal` was rejected with `No policy applies to the request
(denied by default)`.

## Transfer and ledger acceptance

Every lane used an exact-output cross-currency Payment and a `0.001 XRP` x402
fee. The browser flow bound `5 MXN` to a `0.292755 USD` `SendMax`; the
deterministic direct and simulated pair bound `10 MXN` to a `0.585509 USD`
`SendMax`.

| Lane | Transfer | XRPL transaction | Ledger | Independent result | Delivered |
| --- | --- | --- | ---: | --- | --- |
| Browser direct wallet | `tr_36ab3f9a41e783b2ec16d4ae93e79694` | [x402 fee](https://testnet.xrpl.org/transactions/29A7DB26EADC77328B5B30F5D871778A33DB40688F6F378FC5FAF6CBE7BA1C29) | 20940230 | `validated=true`, `tesSUCCESS` | 1000 drops |
| Browser direct wallet | `tr_36ab3f9a41e783b2ec16d4ae93e79694` | [Cross-currency payment](https://testnet.xrpl.org/transactions/DF81CB33ED2C8E8254C9B682D7924D03368E3B4BBD351B3DCAEE13C579D4F76B) | 20940232 | `validated=true`, `tesSUCCESS` | exactly 5 MXN |
| Direct wallet | `tr_a872486e85325e98019bcbe33199a76b` | [x402 fee](https://testnet.xrpl.org/transactions/3D7520DCAEFAE5C23517FDFAED64626E1654513193DC6FB5AA7AD8EAF97BA5D1) | 20939723 | `validated=true`, `tesSUCCESS` | 1000 drops |
| Direct wallet | `tr_a872486e85325e98019bcbe33199a76b` | [Cross-currency payment](https://testnet.xrpl.org/transactions/E5F1B9F645D2BF23E88F8F2C6CD0B86BDBD4D69DA5DCEBD6B8B23209FC944A30) | 20939725 | `validated=true`, `tesSUCCESS` | exactly 10 MXN |
| Simulated fiat | `tr_b0433a75a019ffdcfd651cd473b2ac16` | [x402 fee](https://testnet.xrpl.org/transactions/FDFFBA6FC3787C805B9EDAE428233DDDD66B0CECBE588EDB46A916142CB7EA60) | 20939728 | `validated=true`, `tesSUCCESS` | 1000 drops |
| Simulated fiat | `tr_b0433a75a019ffdcfd651cd473b2ac16` | [Cross-currency payment](https://testnet.xrpl.org/transactions/945FE92CCC6C214F6A9D69CCCA518952816497AFB9AF111D2BA41E0C23C262E7) | 20939730 | `validated=true`, `tesSUCCESS` | exactly 10 MXN |

All three lanes reached `COMPLETED`. The simulated-fiat lane produced payout
reference `SIM-630152A3227E`.

Independent XRPL `tx` RPC calls verified all six hashes, validation flags,
ledger results, and delivered amounts. Application reconciliation additionally
verified the signed hash, source, destination, approval memo, non-partial
payment flags, and exact delivered issued-currency amount before settlement.

The deterministic REST acceptance replayed both approvals. Each replay
preserved one named Step Functions execution, one x402 fee hash, and one
transfer hash. No duplicate fee or transfer was produced.
