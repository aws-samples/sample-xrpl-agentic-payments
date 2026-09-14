#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""XRPL Agentic Payments — SLIDE 5/5: the x402 exchange, message by message.

Regenerate:

    python3 diagrams/render_x402_sequence.py

WHAT THIS SLIDE ANSWERS: what is actually on the wire between a buyer agent and a
merchant agent, in order, and which of those messages touches the ledger.

Slides 1-4 are component and lifecycle views of THIS repository. This one is a
protocol view: it would look the same in any correct x402 v2 implementation, and
it is drawn because the ordering is counter-intuitive in a way that prose keeps
failing to fix. Three things in it surprise nearly everyone:

  - The buyer SIGNS but never SUBMITS. The merchant's facilitator submits. What
    crosses the wire is an authorisation, not a receipt and not funds.
  - /verify never touches the ledger, so it is free and repeatable. /settle is
    the only message that moves money, and it happens AFTER the work is done.
  - The buyer echoes back the terms it is paying for, so the merchant has to
    check that echo against its own published price. A facilitator verifies a
    payment against the requirement it is HANDED — hand it the buyer's cheaper
    terms and it will call a one-drop payment perfectly valid.

WHY THIS IS NOT A diagram_lib SPEC: diagram_lib lays tiles on a grid and connects
them, which is the right model for "where do the components sit". A sequence
diagram's vertical axis is TIME on a lifeline, and the same participant appears at
every step — expressed as a diagram_lib grid it would need one tile per message
and would stop being a sequence diagram. So this file carries its own small
engine, and reuses diagram_lib's Ruler, styling and overlap check so the two
renderers stay visually consistent.

Layout is derived, not hand-tuned. Two rules do the work:

  - Lifeline pitch comes from the measured width of the widest message LABEL that
    has to fit between two lifelines, so editing a label cannot silently push
    text over a neighbour. Notes are then wrapped to whatever span they landed
    in — deriving the pitch from notes as well produced four-inch columns.
  - Row height is summed from its measured parts (label, arrow, wrapped note)
    rather than taken as a fraction of a guessed row, so a note that wraps to
    three lines makes its own room instead of colliding with the next message.

Every claim is read off src/payments/x402.py, src/payments/xrpl_exact.py,
src/payments/merchant.py, src/payments/facilitator.py and the live-verified
expectations in tests/test_x402_xrpl_live.py.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib                                              # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from diagram_lib import CAT, DPI, FS, MASK, Ruler, _wrap_note, text_overlaps  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Message text is the densest text in the picture and sits between two lifelines,
# so it gets its own sizes rather than borrowing the tile-caption sizes.
FS_MSG = 6.8
FS_INLINE = 6.1
FS_HEAD = 7.4
FS_PHASE = 6.6

GREY = "#5a6b7b"
INK = "#1b1b1b"

# Geometry, inches. Everything not listed here is measured.
LOOP_W = 0.46        # width of a self-message bracket
SELF_TEXT_W = 2.90   # wrap width for self-message text, which hangs to the right
ARROW_PAD = 0.30     # clearance each side of a label within its span
MARGIN = 0.42
PAD_TOP = 0.05       # row top to label
GAP_LABEL_ARROW = 0.09
GAP_ARROW_NOTE = 0.05
ROW_GAP = 0.19
LOOP_H = 0.26


@dataclass
class Participant:
    """One lifeline. `sub` names the code that plays this role, where we own it.

    deferred: this participant is implemented but has never run. Drawn hollow, so
           the eye cannot mistake it for a leg of the exchange that was observed
           working. check_deferred enforces that it only ever appears on "alt"
           messages, which is what stops the picture quietly promoting it.
    """
    key: str
    label: str
    sub: str = ""
    cat: str = "compute"
    deferred: bool = False


# Line style carries the kind of message; the participant's colour only says who
# sent it. RESPONSES lists the styles that answer a call rather than make one, and
# ALT_STYLES the ones belonging to a path that has never executed.
RESPONSES = ("return", "refuse", "alt-return")
ALT_STYLES = ("alt", "alt-return")
ALT_COLOUR = "#8a97a4"


@dataclass
class Msg:
    """One message between two lifelines.

    style: "solid" a call/request | "return" a response (dashed, the UML
           convention) | "refuse" a response that ends the exchange without the
           resource being served | "alt"/"alt-return" the same two things on a
           path that is implemented but has never run (dotted, grey).
    tag:   overrides the automatic step number. Used for the alternate rail, whose
           messages are numbered 4a/4b because they REPLACE steps 4-6 rather than
           following them — letting them consume numbers of their own would imply
           an exchange of 18 messages, when no single run makes more than 16.
    emphasis: draws the label boxed in red. Reserved for the messages that are
           security-critical or irreversible — a diagram where everything is
           emphasised emphasises nothing.
    note: a second, smaller line under the arrow. For the caveat that makes the
           message honest, not for a restatement of the label.
    """
    src: str
    dst: str
    label: str
    style: str = "solid"
    note: str = ""
    emphasis: bool = False
    tag: str = ""


@dataclass
class SelfMsg:
    """Work a participant does without sending anything — a decision or a signature.

    These matter here more than in most sequence diagrams: the place this protocol
    is most often got wrong is a SelfMsg (match the terms), and drawing it as an
    ordinary call would hide the fact that no network hop is involved in the
    refusal it produces.
    """
    actor: str
    label: str
    note: str = ""
    emphasis: bool = False
    tag: str = ""


@dataclass
class Phase:
    """A horizontal band label marking a stage of the exchange."""
    label: str


@dataclass
class SeqSpec:
    title: str
    participants: list
    steps: list
    subtitle: str = ""
    note: str = ""
    legend: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)
    note_w: float = 11.5


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------

PARTICIPANTS = [
    # Far left on purpose, so the two SIGNING RAILS bookend the picture: AgentCore
    # on one side, the XRP Ledger on the other, with the buyer, merchant and
    # facilitator — the parts both rails share — in between. Hollow because it has
    # never run; see the HONEST STATUS paragraph in the note.
    Participant("AGENTCORE", "AgentCore Payments",
                "AgentCorePaymentProvider · ProcessPayment · EVM + Solana only · "
                "never executed", "agent", deferred=True),
    Participant("BUYER", "Buyer agent",
                "X402Client + XrplExactProvider · holds the XRPL seed", "agent"),
    Participant("MERCHANT", "Merchant agent",
                "Merchant.require_payment · owns the price", "compute"),
    Participant("FACIL", "Facilitator",
                "x402.org/facilitator · /verify and /settle", "ml"),
    Participant("XRPL", "XRP Ledger",
                "testnet · xrpl:1 · 3-5s finality", "ext"),
]

# The label on each arrow is WHAT IS ON THE WIRE. Where a message carries an x402
# header, the header is named, because "sends payment" is exactly the vagueness
# this slide exists to remove.
STEPS = [
    Phase("CHALLENGE — the merchant names the price, and can refuse"),
    Msg("BUYER", "MERCHANT", "GET /orderbook\n(no PAYMENT-SIGNATURE)",
        note="the buyer does not know the price yet"),
    Msg("MERCHANT", "BUYER",
        "402  PAYMENT-REQUIRED\nexact · xrpl:1 · 1000 drops · payTo rDest…",
        style="refuse",
        note="NO WORK DONE. Terms travel in the header, not the body"),

    Phase("AUTHORISE — the buyer signs, and does not submit"),
    SelfMsg("BUYER", "select a provider by `network`",
            note="xrpl:* → XrplExactProvider · eip155:*/solana → AgentCore Payments. "
                 "This one line is the entire integration point"),

    Phase("ALT — had `network` been eip155:* or solana:*, steps 4-6 would be "
          "replaced by these two, and nothing else in the exchange would change"),
    Msg("BUYER", "AGENTCORE",
        "ProcessPayment  paymentType: CRYPTO_X402\n"
        "cryptoX402.payload = the merchant's requirement, verbatim",
        style="alt", tag="4a",
        note="verbatim because AgentCore reads amount, asset, payTo and network "
             "out of that document. A payload with our own field names is accepted "
             "and produces a proof the merchant cannot verify"),
    Msg("AGENTCORE", "BUYER",
        "status: COMPLETED\npaymentOutput.cryptoX402.payload",
        style="alt-return", tag="4b",
        note="the signing key never reaches the agent — that is the reason to want "
             "this rail. Not idempotent: clientToken is what stops a retry "
             "becoming a second payment"),

    Phase("…continuing on the XRPL rail — the one this repo has actually run"),
    Msg("BUYER", "XRPL", "autofill Sequence + Fee, read the validated ledger index",
        note="READ ONLY — the buyer never submits anything"),
    Msg("XRPL", "BUYER", "ledger_index", style="return"),
    SelfMsg("BUYER", "sign the Payment · DO NOT SUBMIT",
            note="LastLedgerSequence = ledger_index + ceil(timeout/5) + 2, "
                 "overriding xrpl-py's autofill default of +20"),
    Msg("BUYER", "MERCHANT",
        "GET /orderbook  PAYMENT-SIGNATURE\n{accepted: …, payload: {signedTxBlob}}",
        note="the signed blob is a bearer instrument until LastLedgerSequence passes"),

    Phase("CHECK — twice, and the first check is the merchant's own"),
    SelfMsg("MERCHANT", "match the echoed `accepted`\nagainst the published price",
            emphasis=True,
            note="scheme · network · amount · asset · payTo. Mismatch → 402, and "
                 "the facilitator is never consulted"),
    Msg("MERCHANT", "FACIL", "POST /verify\n{paymentPayload, paymentRequirements}",
        note="the merchant sends ITS OWN requirement, never the buyer's echo"),
    Msg("FACIL", "MERCHANT", "isValid: true · payer: rExec…", style="return",
        note="NO LEDGER WRITE — /verify is free and repeatable"),

    Phase("DELIVER — work first, then money"),
    SelfMsg("MERCHANT", "do the work",
            note="only now: verifying before working would give the result away, "
                 "settling first would charge for a result that may not exist"),
    Msg("MERCHANT", "FACIL", "POST /settle", emphasis=True,
        note="THIS MOVES MONEY. Called once, after the result exists"),
    Msg("FACIL", "XRPL", "submit signedTxBlob"),
    Msg("XRPL", "FACIL", "validated · tesSUCCESS · tx hash", style="return"),
    Msg("FACIL", "MERCHANT", "success · transaction", style="return"),
    Msg("MERCHANT", "BUYER", "200 + the data  PAYMENT-RESPONSE",
        style="return", note="the payer learns the tx hash here, not before"),
]

# Step numbers are assigned by position, so prose that cites one drifts the moment
# a message is inserted — which is exactly what happened on the first render of
# this file, silently. Each entry below is checked against the label of the step
# that currently holds that number, so a renumbering breaks the build rather than
# the reader.
REFS = {
    1: "GET /orderbook",
    3: "select a provider",
    4: "autofill",
    5: "ledger_index",
    6: "sign the Payment",
    8: "match the echoed",
    9: "POST /verify",
    10: "isValid",
    12: "POST /settle",
    13: "submit signedTxBlob",
    16: "200 + the data",
}


# ---------------------------------------------------------------------------
# Checks. Same contract as diagram_lib: a non-empty list blocks the render, so a
# picture asserting something false is never written to disk.
# ---------------------------------------------------------------------------

def _numbered(spec):
    """The steps that take an automatic number, in order — what the reader counts.

    Tagged steps are excluded: they belong to the alternate rail and carry 4a/4b,
    so they must not shift the numbering of the main line.
    """
    return [s for s in spec.steps if not isinstance(s, Phase) and not s.tag]


def _all_text(spec):
    out = [spec.title, spec.subtitle, spec.note]
    for p in spec.participants:
        out += [p.label, p.sub]
    for s in spec.steps:
        out += [s.label, getattr(s, "note", "")]
    out += [t for _s, _c, t in spec.legend]
    return [s for s in out if s]


def check_endpoints(spec):
    keys = {p.key for p in spec.participants}
    out = []
    for s in spec.steps:
        if isinstance(s, Msg):
            for end in (s.src, s.dst):
                if end not in keys:
                    out.append(f"message {s.label[:32]!r} references undeclared "
                               f"participant {end!r}")
            if s.src == s.dst:
                out.append(f"message {s.label[:32]!r} has src == dst; use SelfMsg")
        elif isinstance(s, SelfMsg) and s.actor not in keys:
            out.append(f"self-message {s.label[:32]!r} references undeclared "
                       f"participant {s.actor!r}")
    return out


def check_dollar_mathtext(spec):
    """See diagram_lib.check_dollar_mathtext — a PAIR of '$' renders as maths."""
    return [f"unescaped '$' in {s[:50]!r} — write \\$"
            for s in _all_text(spec) if re.search(r"(?<!\\)\$", s)]


def check_glyphs(spec):
    """Characters the render font lacks, which draw as tofu boxes."""
    try:
        from fontTools.ttLib import TTFont
        from matplotlib.font_manager import FontProperties, findfont
    except ImportError:
        return []
    font = TTFont(findfont(FontProperties(family="DejaVu Sans")))
    covered = set()
    for table in font["cmap"].tables:
        covered |= set(table.cmap)
    return [f"glyph {ch!r} (U+{ord(ch):04X}) in {s[:44]!r} missing from DejaVu Sans"
            for s in _all_text(spec)
            for ch in sorted({c for c in s if ord(c) > 127 and ord(c) not in covered})]


def check_ordering(spec):
    """A response arriving before anything was sent to that participant.

    Cheap to write, and it catches a real editing mistake: reordering the steps and
    leaving a response above the call it answers, which reads as the merchant
    answering a question nobody asked.
    """
    outstanding: set = set()
    out = []
    for s in spec.steps:
        if not isinstance(s, Msg):
            continue
        if s.style in RESPONSES:
            if (s.dst, s.src) not in outstanding:
                out.append(f"{s.src}->{s.dst} {s.label[:32]!r} is a "
                           f"{s.style} with no preceding call from {s.dst}")
            outstanding.discard((s.dst, s.src))
        else:
            outstanding.add((s.src, s.dst))
    return out


def check_deferred(spec):
    """A participant that has never run must not appear on a message that has.

    The point of drawing AgentCore Payments at all is to show where the second rail
    attaches. The risk in drawing it is that a reader takes a solid arrow as
    evidence the rail works. So: every message touching a deferred participant has
    to be an "alt" style, and every alt message has to touch one — otherwise the
    dotted styling has drifted away from the thing it is claiming.
    """
    deferred = {p.key for p in spec.participants if p.deferred}
    out = []
    for s in spec.steps:
        if isinstance(s, Msg):
            ends = {s.src, s.dst}
            if ends & deferred and s.style not in ALT_STYLES:
                out.append(f"{s.src}->{s.dst} {s.label[:32]!r} touches deferred "
                           f"{sorted(ends & deferred)} but is drawn as "
                           f"{s.style!r}, which reads as a leg that has run")
            if s.style in ALT_STYLES and not ends & deferred:
                out.append(f"{s.src}->{s.dst} {s.label[:32]!r} is drawn as "
                           f"{s.style!r} but touches no deferred participant")
        elif isinstance(s, SelfMsg) and s.actor in deferred:
            out.append(f"self-message {s.label[:32]!r} runs on deferred "
                       f"{s.actor!r}, which cannot have done any work")
    for p in spec.participants:
        if p.deferred and not any(
                isinstance(s, Msg) and p.key in (s.src, s.dst) for s in spec.steps):
            out.append(f"deferred participant {p.key!r} has a lifeline but no "
                       f"messages, so the slide never says what it would do")
    return out


def check_step_refs(spec):
    """Prose that cites "step N" must cite the step it means.

    Two halves: every declared ref must still match the label of the step now
    holding that number, and every step the note cites must be both in range and
    declared — an undeclared citation is one that nothing is checking.
    """
    steps = _numbered(spec)
    out = []
    for n, fragment in sorted(spec.refs.items()):
        if not 1 <= n <= len(steps):
            out.append(f"refs[{n}] is out of range (1..{len(steps)})")
        elif fragment.lower() not in steps[n - 1].label.lower():
            out.append(f"refs[{n}] expects {fragment!r} but step {n} is "
                       f"{steps[n - 1].label.splitlines()[0]!r}")
    # "step 9", "steps 4 and 5", "steps 12-16" — collect every integer that the
    # word "step(s)" introduces, including the far end of a range.
    cited = set()
    for head, rest in re.findall(r"steps? (\d+)((?:\s*(?:-|and|to)\s*\d+)*)",
                                 spec.note):
        cited.add(int(head))
        cited |= {int(m) for m in re.findall(r"\d+", rest)}
    for n in sorted(cited):
        if not 1 <= n <= len(steps):
            out.append(f"note cites step {n}, which does not exist "
                       f"(1..{len(steps)})")
        elif n not in spec.refs:
            out.append(f"note cites step {n} but refs has no entry for it, so "
                       f"nothing checks that the citation stays correct")
    return out


CHECKS = [check_endpoints, check_dollar_mathtext, check_glyphs, check_ordering,
          check_step_refs, check_deferred]


def _stroke(style):
    """(linestyle, colour override) for a message style. None = keep the sender's."""
    if style in ALT_STYLES:
        return (0, (1, 2.2)), ALT_COLOUR
    if style == "refuse":
        return (0, (4, 2.6)), CAT["security"]
    if style == "return":
        return (0, (4, 2.6)), None
    return "solid", None


# ---------------------------------------------------------------------------
# Layout + draw
# ---------------------------------------------------------------------------

def _widest(r, text, fontsize):
    if not text:
        return 0.0
    return max(r.size(line, fontsize)[0] for line in text.split("\n"))


def _text_h(r, text, fontsize, spacing=1.32):
    if not text:
        return 0.0
    n = len(text.split("\n"))
    return r.size("Ag", fontsize)[1] * (1 + spacing * (n - 1))


def _pitch(r, spec):
    """Lifeline pitch, from the widest LABEL that must fit inside one span.

    Deliberately ignores notes and self-message text. Notes are secondary and get
    wrapped to whatever span they land in; self-message text hangs to the right of
    its bracket and is allowed to reach past the next lifeline, which is the usual
    convention and is policed by the overlap check instead. Feeding either into
    the pitch produced four-inch columns and arrows too long to read.
    """
    idx = {p.key: i for i, p in enumerate(spec.participants)}
    need = [_widest(r, p.label, FS_HEAD) + 0.30 for p in spec.participants]
    for s in spec.steps:
        if isinstance(s, Msg):
            span = max(abs(idx[s.dst] - idx[s.src]), 1)
            need.append((_widest(r, s.label, FS_MSG) + ARROW_PAD * 2) / span)
    return max(need)


def draw(spec, out_path):
    r = Ruler()
    keys = [p.key for p in spec.participants]
    idx = {k: i for i, k in enumerate(keys)}
    pitch = _pitch(r, spec)

    head_label_h = max(_text_h(r, p.label, FS_HEAD) for p in spec.participants)
    # Wrapped once, here, and reused for both the margin maths and the drawing.
    # Wrapping at the pre-spread pitch is deliberate: spreading only widens the
    # pitch, so a caption sized against the narrow one can never overflow later.
    subs = {p.key: r.wrap(p.sub, FS_INLINE, pitch * 0.92) for p in spec.participants}
    head_sub_h = max(_text_h(r, subs[p.key], FS_INLINE) for p in spec.participants)
    head_h = head_label_h + head_sub_h + 0.34

    note_lines = _wrap_note(r, spec.note, spec.note_w) if spec.note else []
    note_h = (r.size("Ag", FS["note"])[1] * (1 + 1.55 * (len(note_lines) - 1))
              if note_lines else 0.0)
    legend_h = (r.size("Ag", FS["sub"])[1] + 0.26) if spec.legend else 0.0
    title_h = r.size("Ag", FS["title"], "bold")[1]
    sub_h = r.size("Ag", FS["sub"])[1] if spec.subtitle else 0.0

    # Width. The margins have to clear whatever is WIDEST at the outer lifelines —
    # header box or the caption under it, which is usually the caption — not the
    # lifelines they hang from. Getting this wrong pushed the rightmost lifeline
    # off the canvas on the first render and clipped the leftmost caption on the
    # second. The bottom note block is wrapped to its own width and can be wider
    # than the whole exchange, so take the larger of the two and then spread the
    # lifelines to fill it rather than leaving the picture lopsided.
    def _reach(p):
        return max(_widest(r, p.label, FS_HEAD), _widest(r, subs[p.key], FS_INLINE)) / 2

    lead = max(_reach(spec.participants[0]), LOOP_W) + 0.10
    trail = _reach(spec.participants[-1]) + 0.10
    if any(isinstance(s, SelfMsg) and idx[s.actor] == len(keys) - 1
           for s in spec.steps):
        trail = max(trail, LOOP_W + SELF_TEXT_W + 0.10)
    text_w = max([_widest(r, ln, FS["note"]) for ln in note_lines] or [0.0])
    if spec.legend:
        text_w = max(text_w, sum(0.52 + r.size(t, FS["sub"])[0] + 0.55
                                 for _s, _c, t in spec.legend))
    chrome = MARGIN + lead + trail + MARGIN
    W = max(chrome + (len(keys) - 1) * pitch, text_w + 2 * MARGIN)
    if len(keys) > 1:
        pitch = (W - chrome) / (len(keys) - 1)
    X = {k: MARGIN + lead + i * pitch for i, k in enumerate(keys)}

    # Height. Each row is summed from its measured parts; nothing is a fraction of
    # a guessed row, so a note that wraps to three lines makes its own room.
    rows = []
    for s in spec.steps:
        if isinstance(s, Phase):
            rows.append((s, _text_h(r, s.label, FS_PHASE) + 0.30, "", ""))
            continue
        if isinstance(s, SelfMsg):
            lab = r.wrap(s.label, FS_MSG, SELF_TEXT_W)
            nt = r.wrap(s.note, FS_INLINE, SELF_TEXT_W) if s.note else ""
            h = (PAD_TOP + 0.04 + max(LOOP_H, _text_h(r, lab, FS_MSG))
                 + (GAP_ARROW_NOTE + _text_h(r, nt, FS_INLINE) if nt else 0.0)
                 + ROW_GAP)
        else:
            lab = s.label
            avail = max(abs(X[s.dst] - X[s.src]) - 0.24, 1.7)
            nt = r.wrap(s.note, FS_INLINE, avail) if s.note else ""
            h = (PAD_TOP + _text_h(r, lab, FS_MSG) + GAP_LABEL_ARROW
                 + (GAP_ARROW_NOTE + _text_h(r, nt, FS_INLINE) if nt else 0.0)
                 + ROW_GAP)
        rows.append((s, h, lab, nt))

    body_dy = note_h + legend_h + 0.40
    H = body_dy + sum(h for _s, h, _l, _n in rows) + head_h + title_h + sub_h + 0.72

    fig = plt.figure(figsize=(W, H), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(MARGIN, H - 0.26, spec.title, fontsize=FS["title"], weight="bold",
            va="top")
    if spec.subtitle:
        ax.text(MARGIN, H - 0.26 - title_h - 0.10, spec.subtitle,
                fontsize=FS["sub"], color="#555555", va="top")

    head_top = H - 0.26 - title_h - sub_h - 0.34
    life_top = head_top - head_h
    life_bottom = body_dy - 0.16

    # Lifelines first: everything else paints over them.
    for p in spec.participants:
        colour = ALT_COLOUR if p.deferred else CAT.get(p.cat, GREY)
        x = X[p.key]
        ax.plot([x, x], [life_bottom, life_top],
                linestyle=(0, (1, 3)) if p.deferred else (0, (3, 4)),
                linewidth=1.0, color=colour, alpha=0.55, zorder=1)
        box_w = _widest(r, p.label, FS_HEAD) + 0.30
        # Hollow for a rail that has never run: filled tiles are the ones observed
        # working, and that distinction has to survive being read at a glance.
        ax.add_patch(FancyBboxPatch(
            (x - box_w / 2, life_top + 0.06), box_w, head_label_h + 0.18,
            boxstyle="round,pad=0.03",
            facecolor="white" if p.deferred else colour, edgecolor=colour,
            linestyle=(0, (2.6, 1.8)) if p.deferred else "solid",
            linewidth=1.4, zorder=4))
        ax.text(x, life_top + 0.15 + head_label_h / 2, p.label, ha="center",
                va="center", fontsize=FS_HEAD, weight="bold",
                color=colour if p.deferred else "white", zorder=5)
        if p.sub:
            ax.text(x, life_top + head_label_h + 0.36, subs[p.key], ha="center",
                    va="bottom", fontsize=FS_INLINE, color="#555555",
                    linespacing=1.3, zorder=5)

    y = life_top
    n = 0
    for step, h, lab, nt in rows:
        top = y - PAD_TOP
        y -= h

        if isinstance(step, Phase):
            band = y + h * 0.30
            ax.plot([MARGIN, W - MARGIN], [band, band], linewidth=0.7,
                    color="#c9c9c9", zorder=1)
            ax.text(MARGIN + 0.04, band + 0.04, step.label, ha="left",
                    va="bottom", fontsize=FS_PHASE, weight="bold",
                    style="italic", color=GREY, zorder=6, bbox=MASK)
            continue

        # A tagged step keeps its own label (4a/4b) and does not advance the count,
        # because the alternate rail replaces steps 4-6 rather than adding to them.
        if step.tag:
            marker = step.tag
        else:
            n += 1
            marker = str(n)
        emph_box = dict(boxstyle="round,pad=0.16", facecolor="#fff4f4",
                        edgecolor=CAT["security"], linewidth=0.9)

        if isinstance(step, SelfMsg):
            x = X[step.actor]
            colour = CAT.get(
                next(p.cat for p in spec.participants if p.key == step.actor), GREY)
            loop_top = top - 0.04
            loop_bot = loop_top - LOOP_H
            ax.plot([x, x + LOOP_W, x + LOOP_W, x],
                    [loop_top, loop_top, loop_bot, loop_bot], linewidth=1.15,
                    color=colour, zorder=3, solid_joinstyle="miter")
            ax.add_patch(FancyArrowPatch(
                (x + LOOP_W * 0.62, loop_bot), (x + 0.02, loop_bot),
                arrowstyle="-|>", mutation_scale=9, linewidth=1.15, color=colour,
                zorder=3, shrinkA=0, shrinkB=0))
            tx = x + LOOP_W + 0.12
            ax.text(tx, (loop_top + loop_bot) / 2, f"{marker} · {lab}", ha="left",
                    va="center", fontsize=FS_MSG, color=INK,
                    weight="bold" if step.emphasis else "normal",
                    linespacing=1.32, zorder=6,
                    bbox=emph_box if step.emphasis else MASK)
            if nt:
                ax.text(tx, loop_bot - GAP_ARROW_NOTE, nt, ha="left", va="top",
                        fontsize=FS_INLINE, color="#5f5f5f", style="italic",
                        linespacing=1.3, zorder=6, bbox=MASK)
            continue

        x0, x1 = X[step.src], X[step.dst]
        colour = CAT.get(
            next(p.cat for p in spec.participants if p.key == step.src), GREY)
        dashes, override = _stroke(step.style)
        colour = override or colour
        label_h = _text_h(r, lab, FS_MSG)
        arrow_y = top - label_h - GAP_LABEL_ARROW
        sign = 1 if x1 > x0 else -1
        ax.add_patch(FancyArrowPatch(
            (x0 + sign * 0.02, arrow_y), (x1 - sign * 0.02, arrow_y),
            arrowstyle="-|>", mutation_scale=10, linewidth=1.2, color=colour,
            zorder=3, shrinkA=0, shrinkB=0,
            linestyle=dashes))
        ax.text((x0 + x1) / 2, top - label_h / 2, f"{marker} · {lab}", ha="center",
                va="center", fontsize=FS_MSG,
                color=ALT_COLOUR if step.style in ALT_STYLES else INK,
                weight="bold" if step.emphasis else "normal",
                linespacing=1.32, zorder=6,
                bbox=emph_box if step.emphasis else MASK)
        if nt:
            ax.text((x0 + x1) / 2, arrow_y - GAP_ARROW_NOTE, nt, ha="center",
                    va="top", fontsize=FS_INLINE, color="#5f5f5f", style="italic",
                    linespacing=1.3, zorder=6, bbox=MASK)

    if spec.legend:
        lx, ly = MARGIN, note_h + 0.30 + legend_h / 2
        for style, colour, text in spec.legend:
            ax.add_patch(FancyArrowPatch(
                (lx, ly), (lx + 0.42, ly), arrowstyle="-|>", mutation_scale=10,
                linewidth=1.2, color=colour, zorder=4,
                linestyle=_stroke(style)[0]))
            ax.text(lx + 0.52, ly, text, fontsize=FS["sub"], color="#333333",
                    va="center", zorder=4)
            lx += 0.52 + r.size(text, FS["sub"])[0] + 0.55

    if spec.note:
        ax.text(MARGIN, note_h + 0.20, "\n".join(note_lines), fontsize=FS["note"],
                color="#333333", va="top", linespacing=1.55, zorder=7)
    r.close()

    overlaps = text_overlaps(fig, ax)
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)
    return overlaps


def render(spec, out_path):
    for check in CHECKS:
        problems = check(spec)
        if problems:
            print(f"  {check.__name__} FAILED:")
            for p in problems:
                print(f"    {p}")
            return 1
    overlaps = draw(spec, out_path)
    print(f"wrote {out_path} ({os.path.getsize(out_path) // 1024} KB)")
    if overlaps:
        print(f"  {len(overlaps)} OVERLAPPING TEXT PAIR(S):")
        for a, b in overlaps:
            print(f"    {a!r}  <->  {b!r}")
        return 1
    print("  all checks pass, no overlapping text")
    print("  NOW READ THE PNG — checks cannot catch a wrong protocol.")
    return 0


SPEC = SeqSpec(
    title="SLIDE 5/5 — x402 on XRPL: the exchange, message by message",
    subtitle="Buyer agent to merchant agent, in wire order. The buyer signs and "
             "never submits; the merchant's facilitator submits. Each arrow is "
             "coloured by the lifeline that SENT it. Previous: docs/wallets.png",
    participants=PARTICIPANTS,
    steps=STEPS,
    refs=REFS,
    # Line style carries the meaning; colour only identifies the sender, so the
    # samples are drawn neutral. Colouring them per-participant, as the first cut
    # did, read as "requests are purple" — which is false the moment the merchant
    # is the one making the request, at steps 9 and 12.
    legend=[
        ("solid", GREY, "request — the label is what is on the wire"),
        ("dashed", GREY, "response"),
        ("dashed", CAT["security"], "refusal — a 402 with no resource served"),
    ],
    note=(
        "WHY THIS ORDER, AND NOT THE OBVIOUS ONE:\n"
        "•  THE INTUITIVE FLOW DOES NOT WORK. If the buyer submitted the payment "
        "itself and handed over a tx hash, it would have spent money before "
        "knowing the resource would be served, and the merchant would have to go "
        "and check that the hash corresponds to a payment it can actually claim. "
        "x402 moves the AUTHORISATION instead: the buyer authorises exactly one "
        "payment, and the merchant decides whether to execute it. That is why "
        "step 6 signs without submitting, and why nothing is written to the "
        "ledger until step 13.\n"
        "•  Steps 4 and 5 are the buyer's only contact with the ledger, and they "
        "are a read. The wallet that signs never needs permission to submit.\n"
        "•  A SIGNED BLOB IS A BEARER INSTRUMENT. Anyone holding it can submit it "
        "until LastLedgerSequence passes, which is why step 6 clamps that window "
        "to ceil(maxTimeoutSeconds / 5) + 2 ledgers — roughly a minute — instead "
        "of accepting xrpl-py's autofill default of +20. Too high is rejected as "
        "lastledgersequence_too_large; too low expires in flight, because the "
        "bound is measured when the FACILITATOR looks, not when the buyer signs.\n"
        "\n"
        "STEP 8 IS THE ONE PEOPLE LEAVE OUT:\n"
        "•  The buyer echoes back the requirement it chose, as `accepted`. A "
        "facilitator verifies a payment against the requirement it is HANDED — so "
        "hand it the buyer's own cheaper terms and it will report isValid on a "
        "one-drop payment, because in isolation that payment is perfectly "
        "consistent. Only the merchant knows what it asked for.\n"
        "•  So Merchant._match_offered compares the echo against the published "
        "price on scheme, network, amount, asset and payTo, and step 9 then sends "
        "the merchant's OWN requirement object — never the buyer's echo — so no "
        "field the buyer invented can influence the verdict. "
        "tests/test_x402_xrpl_live.py proves both halves against the real "
        "facilitator: the forged payment verifies on its own terms, and is still "
        "refused here.\n"
        "•  A merchant that skips step 8 is exploitable while looking correct, "
        "because every individual component still reports success.\n"
        "\n"
        "VERIFY IS FREE, SETTLE IS NOT:\n"
        "•  Step 9 touches no ledger, which is what step 10 is reporting. It is "
        "safe to call repeatedly and costs nothing, and that is what makes it "
        "usable as the gate.\n"
        "•  Step 12 is the only message that moves money, and it is sent AFTER the "
        "work exists. Settling first would charge for a result that may never be "
        "produced; working before verifying would give it away.\n"
        "•  The residual risk is the window between step 12 and step 16: if the "
        "merchant dies there, the buyer has paid and holds nothing. No two-party "
        "protocol without an escrow closes that window — it is why `exact` keeps "
        "amounts small and expiry short. Named here so nobody has to rediscover "
        "it.\n"
        "\n"
        "THE TWO RAILS — STEP 3 CHOOSES ONE, AND THAT IS THE WHOLE INTEGRATION:\n"
        "•  AgentCore Payments settles EVM and Solana — its API enumerates "
        "CryptoWalletNetwork = ['ETHEREUM','SOLANA'] and contains no XRPL, XRP or "
        "RLUSD identifier anywhere — so it cannot sign an XRPL transaction, and "
        "XrplExactProvider handles xrpl:* instead. Registering both providers on "
        "one X402Client is what \"AgentCore Payments with XRPL\" means here: one "
        "protocol, two rails, neither pretending to do the other's job.\n"
        "•  The dotted rail on the left is the whole of the difference. Steps 4a "
        "and 4b REPLACE steps 4 to 6 — no ledger read, no local signing, because "
        "the key lives in the connector's vault and the agent never holds it. "
        "Steps 1, 2 and 7 to 16 are byte-for-byte the same work on either rail, "
        "which is why the merchant does not need to know or care which one the "
        "buyer used. On the AgentCore rail steps 13 and 14 land on Base, Ethereum "
        "or Solana rather than XRPL.\n"
        "•  Same protocol does NOT mean same trust model. On the XRPL rail the "
        "agent holds a seed and can sign anything it likes, bounded only by the "
        "amount it chose to sign for; on the AgentCore rail it holds a session and "
        "an instrument, and the limits on that session are what bound it. That is "
        "the argument for the second rail, and it is why the first one is testnet "
        "only.\n"
        "•  HONEST STATUS: the XRPL rail above is live-verified — every field rule "
        "in it corresponds to a rejection actually observed from the reference "
        "facilitator, and steps 1 to 10 pass against XRPL testnet. Nothing in the "
        "test suite calls /settle, so steps 12 to 16 are implemented and "
        "unexercised. The AgentCore rail is written against the API model but has "
        "never run: it needs an AWS Marketplace subscription to the Coinbase "
        "Wallets listing, a funded wallet, and a human visiting a delegation URL."
    ),
)

if __name__ == "__main__":
    # Written twice for the same reason the other four slides are: webapp/
    # templates/architecture.html serves them, and a hand-copied PNG is a PNG that
    # goes stale the first time the spec changes.
    out = os.path.abspath(os.path.join(HERE, "..", "docs", "x402-sequence.png"))
    rc = render(SPEC, out)
    if rc == 0:
        dst = os.path.abspath(os.path.join(HERE, "..", "webapp", "static",
                                           "assets", "x402-sequence.png"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(out, dst)
        print(f"copied -> {dst}")
    raise SystemExit(rc)
