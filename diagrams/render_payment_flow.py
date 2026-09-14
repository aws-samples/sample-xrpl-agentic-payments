#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""XRPL Agentic Payments — SLIDE 2 of 5: one payment, stage by stage.

Regenerate:

    python3 diagrams/render_payment_flow.py

WHAT THIS SLIDE ANSWERS: what actually happens, in order, between a person
typing "send 500 RLUSD to r..." and the ledger having settled it — including
the two places the run can stop without a payment.

Slide 1 (docs/architecture.png) shows WHERE the components run; this slide shows
WHEN each one is touched. THE TWO NUMBERINGS ARE NOT THE SAME and must not be
cross-read: slide 1 numbers HOPS between components (its 5 is the x402 charge),
this slide numbers STAGES of one payment's lifecycle (its 5 is routing). Each
stage below names the slide-1 component that performs it; that naming, not the
number, is the link between the two slides.

Read the tiles as a snake: row 1 left to right, row 2 right to left, row 3 left
to right. The two tiles hanging off the pipeline are the halts, and they are
drawn because a diagram that only shows the happy path is the diagram that gets
someone to trust an unreviewed $50,000 transfer.

Every claim is read off src/agents/orchestrator.py, src/payments/request.py,
functions/submit_payment.py, functions/check_transaction.py and webapp/app.py.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diagram_lib import CAT, Edge, Node, Spec, render  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# THE SNAKE IS THE LAYOUT CONTRACT: consecutive stages are always adjacent
# cells, so no handoff arrow has to cross an unrelated tile. Row 0 and the
# bottom-right cell are left free for the two halts, which is the only reason
# they can be drawn as short arrows off the stage that produces them.
# Subtitles are written as ONE line with "·" separators, never with embedded
# newlines: the renderer re-wraps every caption to the measured column width, so
# a hand-placed line break is discarded and its two halves run together into a
# sentence that was never proof-read.
NODES = {
    # --- row 1: request arrives and is screened (left to right) ---
    "S1":  Node(0, 1, "user.png", "1 · Instruction",
                "browser extracts the fields, sends JSON over WSS /ws/chat · "
                "authenticated, Origin-checked", "user"),
    "S2":  Node(1, 1, "eks.png", "2 · Validate",
                "server re-checks every field into a PaymentRequest · nothing "
                "is defaulted", "compute"),
    "S3":  Node(2, 1, "agentcore.png", "3 · Screen",
                "Agent 1 → screen_sanctions · OFAC SDN, 19,254 entries · name "
                "and country, not the address", "security"),
    "S4":  Node(3, 1, "agentcore.png", "4 · Quote",
                "Agent 2 → get_orderbook, get_paths · read-only",
                "ml"),
    "S5":  Node(4, 1, "agentcore.png", "5 · Route",
                "Agent 3 · no GATEWAY tool · reasons on Agent 2's cached data",
                "agent"),

    # --- row 2: authorised, signed, settled (right to left) ---
    "S6":  Node(4, 2, "◆", "6 · Authorize",
                "Python re-reads the validated request · at or over "
                "\\$10,000 it is held", "security"),
    # NOT "load key". Naming it a load put a per-payment fetch in the reader's
    # head, and there is none: functions/shared.py reads the secret at module
    # import, so on a warm Lambda this stage has already happened.
    "S7":  Node(3, 2, "secrets.png", "7 · Key in memory",
                "the Lambda already holds every seed · loaded whole at cold "
                "start, not per payment", "security"),
    "S8":  Node(2, 2, "lambda.png", "8 · Sign + submit",
                "Agent 4 → submit_payment · signed Payment, SourceTag, memos",
                "compute"),
    "S9":  Node(1, 2, "◈", "9 · Consensus",
                "XRPL validates in 3-5s · RLUSD as issued USD", "ext"),
    "S10": Node(0, 2, "agentcore.png", "10 · Confirm",
                "Agent 5 → check_transaction · validated AND tesSUCCESS",
                "mgmt"),

    # --- row 3: report back (left to right) ---
    "S11": Node(1, 3, "eks.png", "11 · Stream",
                "every step emitted to the browser as it happens", "compute"),
    "S12": Node(2, 3, "cloudwatch.png", "12 · Observe",
                "Lambda logs only · outside the agent's reach, and thinner than "
                "it looks", "mgmt"),

    # --- the two halts ---
    "ABORT": Node(2, 0, "✗", "HALT · sanctions hit",
                  "screening did not return CLEAR · nothing is submitted",
                  "security"),
    "HOLD":  Node(4, 3, "⚠", "HALT · awaiting approval",
                  "a human decides · Agent 4 is never constructed", "mgmt"),
}

# The label on each arrow is WHAT IS HANDED OVER, not a description of the next
# box. Where that handover is a gate condition, the label is the condition.
EDGES = [
    Edge("S1", "S2", "prompt", nudge_y=0.22),
    Edge("S2", "S3", "validated\nrequest", nudge_y=0.30),
    Edge("S3", "S4", "CLEAR", nudge_y=0.22),
    Edge("S4", "S5", "rates + paths", nudge_y=0.22),
    Edge("S5", "S6", "chosen path", nudge_x=0.62),
    Edge("S6", "S7", "APPROVED\n(under \\$10,000)", nudge_y=0.30),
    Edge("S7", "S8", "seed in\nLambda memory", nudge_y=0.30),
    Edge("S8", "S9", "signed tx blob", nudge_y=0.22),
    Edge("S9", "S10", "tx hash", nudge_y=0.22),
    Edge("S10", "S11", "settled", nudge_x=-0.62),
    Edge("S11", "S12", "run summary", nudge_y=0.22),

    # Halts. Dashed because nothing is handed on: the run ends.
    Edge("S3", "ABORT", "non-CLEAR", style="dashed", nudge_x=0.52),
    Edge("S6", "HOLD", "at or over\n\\$10,000", style="dashed", nudge_x=0.58),
]

SPEC = Spec(
    title="SLIDE 2/5 — XRPL Agentic Payments: one payment, stage by stage",
    subtitle="The lifecycle of a single instruction, in order, with both halts. "
             "Numbers are stages; the legend below says what each stage does "
             "and which code does it.   Previous: docs/architecture.png   "
             "Next: docs/agent-pipeline.png",
    nodes=NODES,
    edges=EDGES,
    icon_dir=os.path.join(HERE, "icons"),
    cx={0: 2.0, 1: 5.3, 2: 8.6, 3: 11.9, 4: 15.2},
    cy={0: 11.7, 1: 8.7, 2: 5.3, 3: 1.9},
    legend=[
        ("solid", CAT["agent"], "stage handoff — the label is what is passed on, "
                                "or the condition that must hold"),
        ("dashed", CAT["security"], "HALT — the run ends here and no payment is "
                                    "submitted"),
    ],
    note=(
        "THE TWELVE STAGES:\n"
        "  1   THE PROSE IS PARSED IN THE BROWSER, NOT ON THE SERVER, and this is "
        "the stage most often described wrongly. webapp/templates/index.html runs "
        "regexes over what you typed for the amount, the r-address, the recipient "
        "name and the country, and sends those as explicit JSON fields over the "
        "WebSocket at /ws/chat. The sentence rides along in `message` and no "
        "server code ever reads it. If a regex finds nothing the browser sends "
        "nothing and says which field it could not find — it used to substitute "
        "100 USD to a hardcoded address, which turned an unreadable sentence into "
        "a real payment. The handshake itself is rejected before it is accepted "
        "unless the session cookie verifies AND the Origin is allow-listed.\n"
        "  2   The server does not trust that extraction. parse_payment_request "
        "(src/payments/request.py) re-checks every field: Decimal amount — never "
        "float — that is finite, positive, at most 1,000,000 and within XRPL's "
        "precision; a currency in {USD, RLUSD, XRP}; a destination that is a valid "
        "classic r-address (an X-address is refused rather than silently stripped "
        "of its tag); and a recipient_name, which is REQUIRED because stage 3 has "
        "nothing to screen without it. Nothing is defaulted, so a missing field "
        "ends the run here and no agent is constructed.\n"
        "  3   Agent 1 (Compliance) calls screen_sanctions, which matches the "
        "recipient NAME and country against the OFAC SDN list. Read that "
        "carefully: the XRPL destination address is NOT screened — the tool "
        "accepts an entity_address parameter and the orchestrator does not pass "
        "one, so this is name screening, not on-chain address screening, and it "
        "would not catch a sanctioned party behind an unnamed address. The verdict "
        "is read from the cached tool payload, not from the model's prose, and a "
        "screening error fails closed to BLOCKED.\n"
        "  4   Agent 2 (FX Intelligence) calls get_orderbook and get_paths on "
        "the XRPL DEX. Its tool list contains no way to move money. These two, "
        "plus screen_sanctions in stage 3, are the three PRICED tools: each call "
        "runs the x402 charge on slide 1's arrow 5 first (\\$0.003, \\$0.003, "
        "\\$0.01), which today logs a simulated receipt and cannot block the "
        "call.\n"
        "  5   Agent 3 (Routing) reaches no Gateway tool and touches nothing "
        "outside the process. It does have a tool — routing_analyze — but that is "
        "a local Python function in the orchestrator, which is why this stage "
        "costs nothing and cannot fail on the network. It chooses between the "
        "paths Agent 2 already fetched.\n"
        "  6   THE AUTHORISATION GATE. Plain Python re-reads the "
        "server-validated PaymentRequest. USD and RLUSD are the threshold "
        "currency already; an XRP amount is converted at the `mid` from the stage-4 "
        "order book, and if no rate is on the context it is held rather than "
        "guessed — an unpriceable payment is exactly the case where a limit must "
        "not be assumed to hold. At or over \\$10,000 — the comparison is >=, so "
        "exactly \\$10,000 is held — the status becomes awaiting_approval and "
        "the run ends. A mismatch between the request and what the model "
        "restated is an ERROR event, not a warning.\n"
        "  7   The seed. Slide 1's arrow 8 has the caveat and it belongs here too: "
        "the wallet secret is not fetched for this payment. functions/shared.py "
        "reads it whole at MODULE IMPORT, so a warm Lambda already holds all seven "
        "seeds before your request arrives, and every tool that imports shared.py "
        "does — not only submit_payment. What is true per payment is narrower and "
        "still worth having: only submit_payment SIGNS, and the wrapper hardcodes "
        "source_wallet=\"execution\", so the model cannot choose to spend from the "
        "treasury. See slide 4.\n"
        "  8   Agent 4 (Execution) calls submit_payment, the only tool that "
        "writes to the ledger, through a wrapper that IGNORES WHAT THE MODEL "
        "ASKED FOR: execute_payment takes destination, amount and currency as "
        "arguments and then discards all three, sending the validated "
        "PaymentRequest's values instead. A model that has drifted, or been "
        "steered by a poisoned tool response, cannot redirect the funds or change "
        "the amount at the last step. The transaction carries SourceTag 20260530 "
        "and JSON memos (agent_id, session_id, action, task_id) so the payment is "
        "attributable on-chain.\n"
        "  9   XRPL consensus validates the transaction in 3-5 seconds. RLUSD "
        "on testnet is an issued currency under the 3-character code USD, "
        "issued by the treasury wallet.\n"
        "  10  Agent 5 (Settlement Monitor) calls check_transaction. Settled "
        "means ALL of: the ledger says validated, the result is tesSUCCESS, and "
        "the hash, destination and delivered_amount match what was submitted. A "
        "partial payment, or a tec* failure inside a validated ledger, is NOT "
        "settled.\n"
        "  11  Every stage above is emitted as an event on the same WebSocket "
        "as it happens, so the UI shows the run rather than replaying it "
        "afterwards. The outcome is then recorded in ONE process-local variable "
        "for the dashboard: the last run only, in memory, gone on restart and not "
        "shared between replicas. There is no payment history store in this "
        "repo.\n"
        "  12  The honest version of the observability box. What CloudWatch holds "
        "is what Lambda puts there by itself — a log group per tool and "
        "Invocations / Errors / Duration. This repo emits no custom metric "
        "(nothing calls put_metric_data), instruments no X-Ray trace, and creates "
        "no alarm or dashboard; the web app's observability tab queries AgentCore "
        "Runtime metrics and a Runtime log group, both of which return empty "
        "because no Runtime is deployed. CloudTrail is thinner still: infra/ "
        "creates no Trail, so the tool invocations are data events and go "
        "unrecorded until data-event logging is enabled. What survives all those "
        "caveats is the containment claim — no tool in any agent's list can write "
        "to or edit any of it. See slide 1, steps 10 and 11.\n"
        "\n"
        "THE TWO HALTS ARE THE POINT:\n"
        "•  Stage 3 and stage 6 both end the run BEFORE Agent 4 exists as an "
        "object in the process, so nothing in memory holds submit_payment. That "
        "is stronger than declining to call it.\n"
        "•  Both gates are evaluated in Python against the object built at "
        "stage 2. Neither trusts the amount, currency or destination the model "
        "restated back — a model that misreports the amount cannot widen its own "
        "authority.\n"
        "•  Nothing here is a policy engine yet: tool scoping is a Python list "
        "per agent, and Cedar / Amazon Verified Permissions on the Gateway is a "
        "target. See slide 3."
    ),
)

if __name__ == "__main__":
    # Written twice for the same reason slide 1 is: webapp/templates/
    # architecture.html serves these slides, and a hand-copied PNG is a PNG that
    # goes stale the first time the spec changes.
    out = os.path.abspath(os.path.join(HERE, "..", "docs", "payment-flow.png"))
    rc = render(SPEC, out)
    if rc == 0:
        dst = os.path.abspath(os.path.join(HERE, "..", "webapp", "static",
                                           "assets", "payment-flow.png"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(out, dst)
        print(f"copied -> {dst}")
    raise SystemExit(rc)
