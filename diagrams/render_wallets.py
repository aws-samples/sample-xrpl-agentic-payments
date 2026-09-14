#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""XRPL Agentic Payments — SLIDE 4 of 5: why there are six wallets.

Regenerate:

    python3 diagrams/render_wallets.py

WHAT THIS SLIDE ANSWERS: "six wallets for five agents — why?" It is the question
the other three slides invite and none of them answer, because the answer is
about the LEDGER, not about AWS.

Short version: two of the six are load-bearing for different reasons (an issuer
must exist for RLUSD to exist at all, and the account that signs payments must
not be the account that can create money), and the other four are one on-ledger
identity per agent. A seventh account, `destination`, appears here because
scripts/setup_trust_lines.py creates it — it is the end-to-end test recipient.

Every number is read off scripts/provision_wallets.py (WALLET_NAMES) and
scripts/setup_trust_lines.py (CURRENCY_CODE, ISSUANCE, the trust-line limit),
and the runtime usage claims off functions/shared.py:55-60 (the cold-start load),
functions/submit_payment.py, src/agents/orchestrator.py and
src/mcp_server/server.py.

The slide is deliberately honest about the gap: the per-agent split is an
attribution boundary today, not a custody boundary. Two separate reasons, and the
second is the one that is easy to miss — every seed sits in ONE secret, and
functions/shared.py loads that secret WHOLE at module import, so each tool Lambda
holds all seven seeds regardless of which one it needs. Only `execution` ever
signs.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diagram_lib import CAT, Edge, Node, Spec, Zone, render  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# ROW 0 is the issuer, ROW 1 is one account per agent, ROW 2 is the counterparty
# that receives a payment. Reading down the picture IS the direction value moves:
# treasury issues, an agent account holds, the destination receives.
#
# The agent-tier colours match slide 3 on purpose — compliance is red on both
# slides, FX is teal on both — so "which wallet belongs to which agent" needs no
# cross-referencing.
NODES = {
    # Kept to two short lines on purpose: six arrows leave this tile downward
    # and every one of them crosses the caption block beneath it, so a long
    # caption here is six arrows through a paragraph.
    "TREASURY": Node(2, 0, "$", "treasury  ·  RLUSD ISSUER",
                     "DefaultRipple set · every account below trusts it",
                     "storage"),

    "EXEC":  Node(0, 1, "◈", "execution",
                  "Agent 4 · 10,000 RLUSD · THE ONLY SEED THAT SIGNS · every "
                  "payment leaves from here", "compute"),
    "FX":    Node(1, 1, "◈", "fx_agent",
                  "Agent 2 · 100 RLUSD · DEX order-book queries need no funds; "
                  "the balance is for tool fees", "ml"),
    "COMP":  Node(2, 1, "◈", "compliance",
                  "Agent 1 · 100 RLUSD · sanctions screening fees", "security"),
    "ROUTE": Node(3, 1, "◈", "routing",
                  "Agent 3 · 100 RLUSD · path discovery", "agent"),
    "MON":   Node(4, 1, "◈", "monitor",
                  "Agent 5 · 100 RLUSD · settlement subscriptions", "mgmt"),

    "DEST":  Node(1, 2, "◈", "destination  ·  7th account",
                  "not one of the six · created by setup_trust_lines.py · "
                  "0 RLUSD, trust line ready · the end-to-end test recipient",
                  "ext"),
}

# ARROW DIRECTION IS THE PAYMENT DIRECTION, not the trust direction. On XRPL the
# holder submits the TrustSet (holder -> issuer) and the issuer then submits a
# Payment (issuer -> holder); drawing both would double every arrow, so these
# are the issuance Payments and the trust lines are stated in the note.
EDGES = [
    Edge("TREASURY", "EXEC", "issues\n10,000 RLUSD", nudge_y=0.34),
    Edge("TREASURY", "FX", "100", nudge_x=-0.34),
    Edge("TREASURY", "COMP", "100", nudge_x=0.28),
    Edge("TREASURY", "ROUTE", "100", nudge_x=0.34),
    Edge("TREASURY", "MON", "100", nudge_y=0.34),
    # Trust line but no issuance: the destination starts empty, which is the
    # whole point of having it — a payment to it is observable from zero.
    # Pushed three quarters of the way down its own arrow: at the midpoint the
    # label lands beside the compliance tile and reads as compliance's label.
    Edge("TREASURY", "DEST", "trust line only\n0 RLUSD", style="dashed",
         nudge_x=-0.90, nudge_y=-1.75),
    Edge("EXEC", "DEST", "the payment\n(slide 2, stage 8)", nudge_x=-0.80),
]

ZONES = [
    Zone(["EXEC", "FX", "COMP", "ROUTE", "MON"],
         "one XRPL account per agent  ·  own identity on the ledger, own fee "
         "balance", CAT["agent"], pad=0.24),
    Zone(["TREASURY", "EXEC", "FX", "COMP", "ROUTE", "MON", "DEST"],
         "all seven seeds live in ONE secret: xrpl-agentic-payments/wallets  ·  "
         "every tool Lambda loads all of it at cold start  ·  written locally to "
         "config/wallets.json (0600, git-ignored), never in a container image or "
         "the AgentCore archive",
         "#232f3e", pad=0.62, dash=(0, (7, 4)), contains_others=True),
]

SPEC = Spec(
    title="SLIDE 4/5 — XRPL Agentic Payments: why there are six wallets",
    subtitle="Six accounts on XRPL testnet, plus a seventh for testing. What "
             "each one is for, and which of them the running system actually "
             "uses.   Previous: docs/agent-pipeline.png   "
             "Next: docs/x402-sequence.png",
    nodes=NODES,
    edges=EDGES,
    zones=ZONES,
    icon_dir=os.path.join(HERE, "icons"),
    cx={0: 2.3, 1: 5.9, 2: 9.5, 3: 13.1, 4: 16.7},
    cy={0: 9.4, 1: 5.8, 2: 2.2},
    legend=[
        ("solid", CAT["storage"], "Payment — RLUSD issued from the treasury, or "
                                  "sent by the execution wallet"),
        ("dashed", CAT["storage"], "trust line only — the account can hold "
                                   "RLUSD but has none"),
    ],
    note=(
        "WHY SIX, WHEN THERE ARE ONLY FIVE AGENTS — four separate reasons:\n"
        "  1  RLUSD does not exist on XRPL testnet, so something has to issue "
        "it. An issued currency on XRPL is a balance owed BY an account, so the "
        "treasury account exists to be that issuer: setup_trust_lines.py sets "
        "DefaultRipple on it, every other account opens a trust line to it "
        "(limit 1,000,000), and it issues the starting balances. On testnet the "
        "3-character code is USD, not RLUSD — XRPL codes are 3 characters or 40 "
        "hex digits.\n"
        "  2  The account that SIGNS outbound payments must not be the account "
        "that can CREATE money. That is why execution is separate from "
        "treasury: compromise the execution seed and the attacker can spend "
        "10,000 RLUSD; compromise the issuer and they can mint. Splitting these "
        "two is the single most valuable separation available on a ledger, and "
        "it is the reason the count starts at two rather than one.\n"
        "  3  One account per agent gives every agent its own on-ledger "
        "identity, so ledger history attributes activity to a specific agent "
        "without trusting any log the agent itself wrote, and its own small fee "
        "balance, so an agent that misbehaves spends its own 100 RLUSD rather "
        "than the treasury's. Combined with the SourceTag and the JSON memos on "
        "each payment (slide 2, stage 8), an auditor can reconstruct which "
        "agent did what from the ledger alone.\n"
        "  4  destination is the 7th account and is NOT one of the six: "
        "setup_trust_lines.py funds it and opens its trust line so the "
        "end-to-end test can pay a real account that starts at 0 RLUSD. Paying "
        "a stranger's address would work on-ledger but proves nothing, because "
        "the balance change could not be checked.\n"
        "\n"
        "WHAT THE RUNNING SYSTEM ACTUALLY USES — the honest part:\n"
        "•  Exactly one seed SIGNS anything: `execution`. The orchestrator's "
        "wrapper hardcodes source_wallet=\"execution\" so the model cannot choose "
        "another, submit_payment defaults to the same, and the KMS scaffold names "
        "it too. The treasury's seed is used only by the setup script, though its "
        "ADDRESS is read at runtime as the RLUSD issuer.\n"
        "•  But one seed signing is NOT one seed being read, and this is the gap "
        "the picture exists to show. functions/shared.py fetches the whole secret "
        "at MODULE IMPORT and parses it into a dict, so every tool Lambda that "
        "imports it — seven of the eight; only screen_sanctions does not — holds "
        "ALL SEVEN SEEDS in memory from cold start, whether or not the tool has "
        "any use for one. get_balance has the treasury's seed in scope while it "
        "reads a balance.\n"
        "•  The four per-agent wallets are provisioned, trust-lined and funded, "
        "but no runtime code selects them yet — get_wallet(name) can load any of "
        "them, and nothing does. They are the identities the design calls for, "
        "waiting on the code that spends from them.\n"
        "•  So the per-agent split is an ATTRIBUTION boundary today, not a custody "
        "boundary, and the two bullets above are why: one secret, no per-tool "
        "scoping of which seeds a function can see, and signing in application "
        "memory. Per-wallet KMS keys (src/signing/kms_signer.py — the KMS path "
        "raises NotImplementedError and nothing imports the module) are what would "
        "turn this into a real separation of duties. That is target T2 on slide 1. "
        "Splitting the secret per wallet, so a Lambda can only fetch the seed it "
        "signs with, is the cheaper half of the same idea and is also not done.\n"
        "•  Every account is also faucet-funded with XRP, which is not "
        "optional: XRPL charges a base reserve per account and an extra reserve "
        "per trust line, and every transaction burns a small XRP fee. An "
        "account holding only RLUSD cannot transact.\n"
        "•  Testnet only. These seeds control nothing real, which is why they "
        "can be written to disk at all. Re-running provision_wallets.py creates "
        "only the names that are missing, so a rate-limited faucet cannot "
        "orphan a funded wallet whose seed was never saved."
    ),
)

if __name__ == "__main__":
    # Also published into the web app's assets: architecture.html serves all
    # five slides, and hand-copying is how the two copies drift.
    out = os.path.abspath(os.path.join(HERE, "..", "docs", "wallets.png"))
    rc = render(SPEC, out)
    if rc == 0:
        dst = os.path.abspath(os.path.join(HERE, "..", "webapp", "static",
                                           "assets", "wallets.png"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(out, dst)
        print(f"copied -> {dst}")
    raise SystemExit(rc)
