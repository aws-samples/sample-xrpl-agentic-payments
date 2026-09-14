#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""XRPL Agentic Payments — SLIDE 3 of 5: inside the agent tier.

Regenerate:

    python3 diagrams/render_agent_pipeline.py

WHAT THIS SLIDE ANSWERS: slide 1 shows WHERE the agent tier sits and slide 2
shows WHEN each agent runs; this one shows WHAT IS INSIDE the tier — all five
agents, the order they run in, the exact tools each one is allowed to call, and
what stops the pipeline.

The number on each agent tile is its position in the run, and it is the SAME
number the agent carries on slide 2 ("Agent 3" there is tile 3 here). The
per-agent legend under the picture lists every tool in each agent's
`tools=[...]` list, so the authorisation boundary can be read off the slide.

Every claim here is read off src/agents/orchestrator.py. In particular:
  - the sequence is a plain Python function, not an LLM supervisor delegating
  - each agent is constructed with its own `tools=[...]` list; that list IS the
    authorisation boundary today (Cedar / Verified Permissions is a target)
  - the Execution agent is not even constructed unless Compliance returned CLEAR,
    the amount is strictly under the $10,000 autonomous limit (the gate is `>=`,
    so exactly $10,000 is held for a human), AND routing returned APPROVED
  - that gate is evaluated in Python against the server-validated
    PaymentRequest, not against the amount the model restated back

WHAT GOES IN `tools=[...]` IS A LOCAL WRAPPER, NOT THE GATEWAY TOOL. This slide
used to print the Gateway tool names as if they were the tool list, and they are
not: each entry is a @tool-decorated Python function in orchestrator.py
(compliance_screen, fx_get_orderbook, fx_find_paths, routing_analyze,
execute_payment, monitor_transaction, monitor_balance) that emits its events and
then calls the Gateway tool underneath. The distinction is load-bearing rather
than pedantic — the wrapper is where execute_payment discards the arguments the
model passed and substitutes the validated request, which is only possible
because the model never gets to address the Gateway directly.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diagram_lib import CAT, Edge, Node, Spec, Zone, render  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# COLUMN 0 IS THE PIPELINE, read top to bottom: one row per agent, in execution
# order. Laid out vertically on purpose — every agent shares the same model and
# the same Gateway, and with the agents in a row those ten shared-resource edges
# crossed the pipeline itself into an unreadable tangle.
#
# Subtitles are ONE line with "·" separators, never an embedded newline: the
# renderer re-wraps every caption to the measured column width, so a hand-placed
# break is discarded and its two halves run together mid-sentence.
NODES = {
    "A1": Node(0, 0, "agentcore.png", "1 · Compliance",
               "compliance_screen → screen_sanctions · non-CLEAR aborts the "
               "payment", "security"),
    "A2": Node(0, 1, "agentcore.png", "2 · FX Intelligence",
               "fx_get_orderbook, fx_find_paths → get_orderbook, get_paths · "
               "read-only, cannot move money", "ml"),
    "A3": Node(0, 2, "agentcore.png", "3 · Routing",
               "routing_analyze · local Python only · no Gateway tool at all",
               "agent"),
    "A4": Node(0, 3, "agentcore.png", "4 · Execution",
               "execute_payment → submit_payment · the ONLY agent that writes "
               "to the ledger", "compute"),
    "A5": Node(0, 4, "agentcore.png", "5 · Settlement Monitor",
               "monitor_transaction, monitor_balance → check_transaction, "
               "get_balance · cannot pay", "mgmt"),

    # The tool path. Shared by every agent that has tools at all, so it is drawn
    # once at the vertical centre. Amazon Bedrock is deliberately NOT a tile: all
    # five agents share one cached client, so it belongs in the zone label rather
    # than as five more edges crossing the picture.
    # The x402 charge used to be captioned here, and it does not happen here: the
    # client runs PaymentGate.charge() BEFORE the Gateway hop (slide 1, arrow 5).
    # This tile says only what the Gateway itself does.
    "GATEWAY": Node(1, 2, "gateway.png", "AgentCore Gateway",
                    "AWS_IAM (SigV4) · 8 targets · validates args against the "
                    "tool schema", "agent"),
    "TOOLS":   Node(2, 2, "lambda.png", "Lambda tools (x8)",
                    "ARM64 · shared xrpl-py layer", "compute"),
    "XRPL":    Node(3, 2, "◈", "XRP Ledger Testnet",
                    "RLUSD + DEX · 3-5s finality", "ext"),
}

# The handoff labels are the actual gate conditions, not decoration: each one is
# a branch in run_multi_agent_payment() that can end the run.
EDGES = [
    Edge("A1", "A2", "CLEAR", nudge_x=-0.62),
    Edge("A2", "A3", "rates + paths", nudge_x=-0.62),
    # Every drawn dollar sign is escaped: matplotlib reads a PAIR of raw $ as
    # mathtext delimiters, so two amounts in one string render as maths.
    # check_dollar_mathtext() enforces this.
    Edge("A3", "A4", "APPROVED\n(< \\$10,000)", nudge_x=-0.62),
    Edge("A4", "A5", "tesSUCCESS\n+ tx hash", nudge_x=-0.62),

    # Four edges, not five: Agent 3 (Routing) is the one agent with an empty tool
    # list, so it has no path to the Gateway at all. That absence is the point.
    Edge("A1", "GATEWAY"),
    Edge("A2", "GATEWAY", "MCP tools/call", nudge_y=0.18),
    Edge("A4", "GATEWAY"),
    Edge("A5", "GATEWAY"),

    Edge("GATEWAY", "TOOLS", "invoke", nudge_y=0.14),
    Edge("TOOLS", "XRPL", "JSON-RPC", nudge_y=0.14),
]

ZONES = [
    # The zone IS the orchestrator: there is no separate supervisor tile to draw,
    # because the sequencing is a Python function, not an agent.
    Zone(["A1", "A2", "A3", "A4", "A5"],
         "run_multi_agent_payment()  ·  Claude Sonnet 4.5 on Amazon Bedrock, "
         "one cached client, five system prompts",
         CAT["agent"], pad=0.26),
    Zone(["XRPL"], "external", CAT["ext"], pad=0.28, dash=(0, (3, 3))),
]

SPEC = Spec(
    title="SLIDE 3/5 — XRPL Agentic Payments: inside the agent tier",
    subtitle="src/agents/orchestrator.py — five scoped Strands agents, run in "
             "this fixed order by plain Python. The numbers are the run order; "
             "the legend below lists every tool each agent may call.   "
             "Previous: docs/payment-flow.png   Next: docs/wallets.png",
    nodes=NODES,
    edges=EDGES,
    zones=ZONES,
    icon_dir=os.path.join(HERE, "icons"),
    cx={0: 2.4, 1: 7.4, 2: 11.6, 3: 15.7},
    cy={0: 13.0, 1: 10.4, 2: 7.8, 3: 5.2, 4: 2.6},
    legend=[
        ("solid", CAT["agent"], "agent handoff — the label is the gate "
                                "condition that must hold to proceed"),
        ("solid", "#232f3e", "tool call path — one Gateway, shared by the four "
                             "agents that have tools"),
    ],
    note=(
        "THE FIVE AGENTS AND THEIR EXACT TOOL LISTS — the number is the run "
        "order, and it is the same number this agent has on slide 2. Each name is "
        "the LOCAL @tool wrapper actually passed to the Strands Agent; the arrow "
        "gives the Gateway tool it calls, because the two are not the same thing "
        "and an agent can only reach the Gateway through its wrapper:\n"
        "  1  Compliance — tools=[compliance_screen] → screen_sanctions. Matches "
        "the recipient NAME and country against the OFAC SDN list (19,254 entries, "
        "loaded from S3 at cold start). Not the XRPL address: the tool takes an "
        "entity_address and the wrapper passes only name and country, so this is "
        "name screening. Anything other than CLEAR ends the run, and a screening "
        "error fails closed to BLOCKED.\n"
        "  2  FX Intelligence — tools=[fx_get_orderbook, fx_find_paths] → "
        "get_orderbook, get_paths. Reads the XRPL DEX order book and the payment "
        "paths. Both are read-only; there is no way to move money in this list. "
        "These are also the two priced tools this agent pays x402 for, at "
        "\\$0.003 each.\n"
        "  3  Routing — tools=[routing_analyze], a LOCAL Python function in "
        "orchestrator.py and not a Gateway tool. This is the one agent with no "
        "path to the Gateway at all, which is why only four arrows — not five — "
        "reach the Gateway tile. It re-derives the amount, currency and "
        "destination from the validated request, so its own returned decision is "
        "authoritative even when it contradicts the arguments the model handed "
        "it.\n"
        "  4  Execution — tools=[execute_payment] → submit_payment. The only "
        "agent, and submit_payment the only one of the eight tools, that writes to "
        "the ledger. The wrapper takes destination, amount and currency from the "
        "model and DISCARDS all three, sending the validated PaymentRequest's "
        "values and source_wallet=\"execution\" instead. It is constructed only "
        "after all three gates pass.\n"
        "  5  Settlement Monitor — tools=[monitor_transaction, monitor_balance] → "
        "check_transaction, get_balance. Confirms the outcome from the ledger. It "
        "can read balances and cannot pay anyone.\n"
        "\n"
        "How the five agents are kept apart:\n"
        "•  Tool scoping is the `tools=[...]` list passed to each Strands "
        "Agent. Agent 2 has no submit_payment in its list, so it cannot send "
        "money even if the model decides it should. This is enforced in "
        "application code — Cedar / Amazon Verified Permissions on the Gateway "
        "is a target, not built.\n"
        "•  Agent 3 (Routing) calls no external tool at all. It reasons over "
        "the path data Agent 2 already fetched, which is cached from the raw "
        "tool payload rather than re-queried.\n"
        "•  Three conditions each end the run before Agent 4 is CONSTRUCTED, so "
        "no object in the process holds execute_payment: a non-CLEAR sanctions "
        "result; an amount AT OR OVER the \\$10,000 autonomous limit (status "
        "'awaiting_approval' — held for a human, not sent, and exactly \\$10,000 "
        "is held); and a routing decision that is not APPROVED. The threshold gate "
        "is also belt-and-braces — it fires on the Python re-check OR on "
        "routing_analyze's own requires_human_approval flag, so an agent that "
        "never called the tool cannot produce an implicit approval by omission.\n"
        "•  Every gate reads the server-validated PaymentRequest "
        "(src/payments/request.py), not the amount or destination the model "
        "restated. The threshold is re-checked in Python after Agent 3 returns, "
        "and a mismatch between the two is an ERROR event.\n"
        "•  Terminal status is read from the cached tool payloads, not from the "
        "model's prose summary of what it did. Settlement is only confirmed "
        "when the ledger says validated AND tesSUCCESS AND the hash, "
        "destination and delivered_amount match what was submitted — a "
        "partial payment or a tec* failure in a validated ledger is not "
        "settled."
    ),
)

if __name__ == "__main__":
    # Also published into the web app's assets: architecture.html serves all
    # five slides, and hand-copying is how the two copies drift.
    out = os.path.abspath(os.path.join(HERE, "..", "docs",
                                       "agent-pipeline.png"))
    rc = render(SPEC, out)
    if rc == 0:
        dst = os.path.abspath(os.path.join(HERE, "..", "webapp", "static",
                                           "assets", "agent-pipeline.png"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(out, dst)
        print(f"copied -> {dst}")
    raise SystemExit(rc)
