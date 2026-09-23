#!/usr/bin/env python3
"""Reusable component-architecture diagram renderer with self-verification.

Import this, declare a spec, call render(). matplotlib only — works offline, no
Node, no Docker, and no diagram content leaves the machine.

The point of this library is not drawing. It is that the layout is DERIVED from
measured text rather than hand-tuned, and that a set of checks refuses to emit a
PNG that makes a false claim about the system.

    from diagram_lib import Spec, Node, Edge, Zone, render
    spec = Spec(title=..., nodes={...}, edges=[...], zones=[...])
    raise SystemExit(render(spec, "out.png"))

Data coordinates are INCHES throughout: the axes are set so 1 data unit == 1 inch,
which makes measured text extents (points / 72) directly usable as geometry.
See example_spec.py for a complete spec.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg                     # noqa: E402
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

DPI = 170

# AWS-console service category colours. Extend freely; keys are yours to choose.
CAT = {
    "compute":  "#ed7100",
    "ml":       "#01a88d",
    "agent":    "#7b27ff",   # Bedrock AgentCore, matches the official icon set
    "security": "#dd344c",
    "storage":  "#7aa116",
    "database": "#2e27ad",
    "network":  "#8c4fff",
    "mgmt":     "#e7157b",
    "devtools": "#c925d1",
    "ext":      "#5a6b7b",   # outside your cloud account
    "user":     "#232f3e",   # people
}


@dataclass
class Node:
    """One component tile.

    col/row index into the grid. The grid is the layout contract:
      - COLUMNS should encode the trust boundary (see Spec.inside_cols), so the
        account/VPC zone can be drawn truthfully.
      - ROWS should encode tiers; put the primary request path on one row and read
        it left to right.
      - Leave a cell EMPTY if it sits on the line between two connected tiles.
        check_arrows_through_tiles() enforces this.

    art: filename resolved under Spec.icon_dir; anything else is drawn as a glyph.
         Restrict glyphs to what the render font ships — check_glyphs() enforces it.
    status: "built" (solid) | "target" | "deferred" (both dashed + tagged).
            Mixing built and planned components without tagging them is the most
            common way an architecture diagram misleads.
    """
    col: int
    row: int
    art: str
    caption: str
    sub: str = ""
    cat: str = "compute"
    status: str = "built"


@dataclass
class Edge:
    """One connection.

    style: "solid" (request path) | "dashed" (credential/control hop) |
           "target" (exists only in the target architecture)
    nudge_x/y shift the LABEL ONLY, to dodge a neighbouring label. They never
    move structure — that stays derived.
    curve: matplotlib arc3 rad. Bows the arrow so it can route AROUND a tile that
           sits on the chord between its endpoints. Use this rather than letting a
           straight arrow pass under an unrelated component, which reads as a
           connection that does not exist.
    """
    src: str
    dst: str
    label: str = ""
    style: str = "solid"
    nudge_x: float = 0.0
    nudge_y: float = 0.0
    curve: float = 0.0


@dataclass
class Zone:
    """A dashed boundary around a set of nodes.

    The rectangle is DERIVED from the measured extents of `members`, so it always
    encloses exactly them as captions and the grid change. Containment is read by
    humans as membership, so this is a claim, not decoration.

    contains_others: set True for an outer zone (account, VPC) that legitimately
    encloses inner zones; check_zone_intrusions() then skips it.
    """
    members: list
    label: str
    colour: str = "#232f3e"
    pad: float = 0.24
    dash: tuple = (0, (6, 4))
    contains_others: bool = False


@dataclass
class Spec:
    title: str
    nodes: dict
    edges: list
    zones: list = field(default_factory=list)
    subtitle: str = ""
    note: str = ""
    legend: list = field(default_factory=list)   # (style, colour, text)
    icon_dir: str = "icons"
    # Columns considered inside your cloud account. If set, check_trust_boundary()
    # asserts every node's column agrees with the account zone's membership.
    inside_cols: set = field(default_factory=set)
    account_zone_prefix: str = "AWS account"
    tile: float = 0.74
    col_w: float = 2.15          # caption wrap width, inches
    cx: dict = field(default_factory=dict)   # col -> x centre, inches
    cy: dict = field(default_factory=dict)   # row -> y centre, inches

    def grid(self):
        """Fill in any missing column/row centres with even spacing."""
        cols = sorted({n.col for n in self.nodes.values()})
        rows = sorted({n.row for n in self.nodes.values()})
        cx = dict(self.cx) or {}
        cy = dict(self.cy) or {}
        for i, c in enumerate(cols):
            cx.setdefault(c, 1.2 + i * (self.col_w + 0.45))
        for i, rw in enumerate(rows):
            cy.setdefault(rw, 1.4 + (len(rows) - 1 - i) * 2.1)
        return cx, cy


FS = dict(title=13.5, sub=6.5, cap=7.4, edge=6.4, zone=7.0, note=7.0,
          glyph=10.0, tag=5.6)
MASK = dict(boxstyle="square,pad=0.12", facecolor="white", edgecolor="none")


class Ruler:
    """Measures rendered text in inches (data units are inches, so the numbers
    it returns are directly usable as layout geometry)."""

    def __init__(self):
        self.fig = plt.figure(figsize=(8, 8), dpi=DPI)
        self.rend = self.fig.canvas.get_renderer()

    def size(self, s, fontsize, weight="normal"):
        t = self.fig.text(0, 0, s, fontsize=fontsize, weight=weight)
        bb = t.get_window_extent(self.rend)
        t.remove()
        return bb.width / DPI, bb.height / DPI

    def wrap(self, s, fontsize, width, weight="normal"):
        words, lines, cur = s.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and self.size(trial, fontsize, weight)[0] > width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return "\n".join(lines)

    def block(self, s, fontsize, width, weight="normal", spacing=1.3):
        """Wrapped text plus the (width, height) of the block it occupies."""
        if not s:
            return "", 0.0, 0.0
        wrapped = self.wrap(s, fontsize, width, weight)
        lines = wrapped.split("\n")
        line_h = self.size("Ag", fontsize, weight)[1]
        widest = max(self.size(ln, fontsize, weight)[0] for ln in lines)
        return wrapped, widest, line_h * (1 + spacing * (len(lines) - 1))

    def close(self):
        plt.close(self.fig)


def _extent(r, spec, key, cx, cy):
    """Measured bbox of a node: its tile plus both caption blocks."""
    n = spec.nodes[key]
    x, y = cx[n.col], cy[n.row]
    _, cap_w, cap_h = r.block(n.caption, FS["cap"], spec.col_w, "bold")
    _, sub_w, sub_h = r.block(n.sub, FS["sub"], spec.col_w)
    half = max(spec.tile, cap_w, sub_w) / 2
    return (x - half, y - spec.tile / 2 - 0.14 - cap_h - sub_h,
            x + half, y + spec.tile / 2)


def _zone_rects(r, spec, cx, cy, zone_h):
    ext = {k: _extent(r, spec, k, cx, cy) for k in spec.nodes}
    rects = []
    for z in spec.zones:
        boxes = [ext[k] for k in z.members]
        rects.append((min(b[0] for b in boxes) - z.pad,
                      min(b[1] for b in boxes) - z.pad,
                      max(b[2] for b in boxes) + z.pad,
                      max(b[3] for b in boxes) + z.pad + zone_h, z))
    return ext, rects


def _anchor(x0, y0, x1, y1, half):
    """Point where the segment leaves a square tile of half-width `half`."""
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return x0, y0
    s = half / max(abs(dx), abs(dy))
    return x0 + dx * s, y0 + dy * s


# --------------------------------------------------------------------------
# Checks. Each returns a list of problems; non-empty blocks the render.
# When you find a defect by LOOKING at the PNG and it is mechanically
# expressible, add a check here so it cannot regress.
# --------------------------------------------------------------------------

def check_account_ids(spec):
    """Any 12-digit run — i.e. an AWS account ID — in the diagram text.

    Diagrams get pasted into decks and tickets. The region is useful context; the
    account number is only useful to someone enumerating your resources.
    """
    strings = [z.label for z in spec.zones] + [spec.note, spec.title, spec.subtitle]
    for n in spec.nodes.values():
        strings += [n.caption, n.sub]
    strings += [e.label for e in spec.edges]
    return [f"account ID {m} appears in the diagram text"
            for s in strings for m in re.findall(r"\b\d{12}\b", s or "")]


def check_endpoints(spec):
    """Edges naming a node that does not exist — a typo silently drops a hop."""
    return [f"edge {e.src}->{e.dst} references undeclared node "
            f"{e.src if e.src not in spec.nodes else e.dst}"
            for e in spec.edges
            if e.src not in spec.nodes or e.dst not in spec.nodes]


def check_trust_boundary(spec):
    """Nodes whose column contradicts the account boundary the zones assert.

    Placing a node in the wrong column makes the dashed account rectangle enclose
    something not in the account — the error most likely to mislead a reader about
    the security model.
    """
    if not spec.inside_cols:
        return []
    inside = {k for z in spec.zones if z.label.startswith(spec.account_zone_prefix)
              for k in z.members}
    if not inside:
        return []
    out = []
    for key, n in spec.nodes.items():
        in_col = n.col in spec.inside_cols
        if in_col != (key in inside):
            out.append(f"{key}: column says "
                       f"{'in-account' if in_col else 'external'} but it is "
                       f"{'listed in' if key in inside else 'absent from'} "
                       f"the account zone")
    return out


def check_zone_intrusions(spec):
    """Tiles falling inside a zone box they are not a member of.

    Readers take containment literally: a tile inside the AgentCore box IS part of
    AgentCore. This check exists because a real draft drew CloudWatch inside an
    AgentCore boundary, asserting CloudWatch is an AgentCore component rather than
    a separate service it reports into.
    """
    r = Ruler()
    cx, cy = spec.grid()
    zone_h = r.size("Ag", FS["zone"], "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, spec, cx, cy, zone_h)
    r.close()
    hits = []
    for x0, y0, x1, y1, z in rects:
        if z.contains_others:
            continue
        for key, n in spec.nodes.items():
            if key in z.members:
                continue
            x, y = cx[n.col], cy[n.row]
            if x0 < x < x1 and y0 < y < y1:
                hits.append(f"{key} sits inside zone {z.label!r} but is not a member")
    return hits


def check_zone_overlaps(spec):
    """Sibling zone boxes whose rectangles intersect.

    Two boundaries crossing each other implies a shared region — components in
    both — which is almost never what the architecture means. Genuine nesting is
    expressed with contains_others=True and is exempt.
    """
    r = Ruler()
    cx, cy = spec.grid()
    zone_h = r.size("Ag", FS["zone"], "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, spec, cx, cy, zone_h)
    r.close()
    def contains(outer, inner):
        return (outer[0] <= inner[0] and outer[1] <= inner[1]
                and outer[2] >= inner[2] and outer[3] >= inner[3])

    hits = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            za, zb = a[4], b[4]
            if not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]):
                continue                                  # disjoint
            if set(za.members) & set(zb.members):
                continue                                  # shared members
            # Full containment is legitimate nesting. PARTIAL overlap is not,
            # even for a contains_others zone: an external zone clipping the
            # account box makes the external component look in-account, which is
            # precisely the claim the boundary exists to deny.
            if (za.contains_others and contains(a, b)) or \
               (zb.contains_others and contains(b, a)):
                continue
            hits.append(f"zones {za.label!r} and {zb.label!r} overlap without "
                        f"nesting (move a column, or reduce pad)")
    return hits


def check_arrows_through_tiles(spec):
    """Edges whose drawn path crosses a tile that is not an endpoint.

    The text-overlap check cannot see this: the arrow is drawn UNDER an unrelated
    component, which reads as a connection that does not exist. Curved edges are
    traced along the same quadratic Bezier matplotlib's `arc3,rad=` produces, so
    bowing an edge around a tile is verified, not assumed.
    """
    cx, cy = spec.grid()
    pos = {k: (cx[n.col], cy[n.row]) for k, n in spec.nodes.items()}
    pad = spec.tile / 2 + 0.05
    hits = []
    for e in spec.edges:
        if e.src not in pos or e.dst not in pos:
            continue          # check_endpoints reports this
        x0, y0 = pos[e.src]
        x1, y1 = pos[e.dst]
        ctlx = (x0 + x1) / 2 + e.curve * (y1 - y0)
        ctly = (y0 + y1) / 2 - e.curve * (x1 - x0)
        for key, (bx, by) in pos.items():
            if key in (e.src, e.dst):
                continue
            for i in range(1, 100):
                t = i / 100
                u = 1 - t
                px = u * u * x0 + 2 * u * t * ctlx + t * t * x1
                py = u * u * y0 + 2 * u * t * ctly + t * t * y1
                if abs(px - bx) < pad and abs(py - by) < pad:
                    hits.append(f"edge {e.src}->{e.dst} crosses tile {key} "
                                f"(leave that cell empty, or set curve=)")
                    break
    return hits


def check_art(spec):
    """Icon files referenced by a node but absent from the icon dir."""
    return [f"{k}: icon file {n.art} not found in {spec.icon_dir}/"
            for k, n in spec.nodes.items()
            if n.art.endswith(".png")
            and not os.path.exists(os.path.join(spec.icon_dir, n.art))]


def check_glyphs(spec):
    """Tile glyphs absent from the render font, which draw as tofu boxes.

    matplotlib only WARNS about these, so check up front. Many plausible symbols
    are missing from DejaVu Sans (U+1F511 KEY, U+26BF, U+1F5DD all are).
    """
    try:
        from fontTools.ttLib import TTFont
        from matplotlib.font_manager import FontProperties, findfont
    except ImportError:
        return []
    font = TTFont(findfont(FontProperties(family="DejaVu Sans")))
    covered = set()
    for table in font["cmap"].tables:
        covered |= set(table.cmap)
    return [f"{k}: glyph {ch!r} (U+{ord(ch):04X}) missing from DejaVu Sans"
            for k, n in spec.nodes.items() if not n.art.endswith(".png")
            for ch in n.art if ord(ch) > 127 and ord(ch) not in covered]


CHECKS = [check_endpoints, check_art, check_glyphs, check_account_ids,
          check_trust_boundary, check_zone_intrusions, check_zone_overlaps,
          check_arrows_through_tiles]


def text_overlaps(fig, ax):
    """Pairs of text objects whose rendered boxes intersect.

    Measures GLYPHS, not the opaque masks drawn behind labels — so a label with a
    mask over a line still counts as clean, which is correct, but two labels
    overlapping each other is always reported.
    """
    rend = fig.canvas.get_renderer()
    items = [(t.get_text().replace("\n", " / ")[:44],
              t.get_window_extent(rend).expanded(0.98, 0.86)) for t in ax.texts]
    hits = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1].overlaps(items[j][1]):
                hits.append((items[i][0], items[j][0]))
    return hits


def draw(spec, out_path):
    r = Ruler()
    cx, cy = spec.grid()
    zone_h = r.size("Ag", FS["zone"], "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, spec, cx, cy, zone_h)

    title_h = r.size("Ag", FS["title"], "bold")[1]
    sub_h = r.size("Ag", FS["sub"])[1] if spec.subtitle else 0.0
    note_lines = spec.note.split("\n") if spec.note else []
    note_h = (r.size("Ag", FS["note"])[1] * (1 + 1.55 * (len(note_lines) - 1))
              if note_lines else 0.0)
    legend_h = (r.size("Ag", FS["sub"])[1] + 0.26) if spec.legend else 0.0
    head_h = title_h + sub_h + 0.34

    if rects:
        body_dx = 0.40 - min(rc[0] for rc in rects)
        # A zone label starts at its left edge and can run wider than the zone
        # itself, so the canvas must clear the LABEL, not just the rectangle.
        right = max(max(rc[2] for rc in rects),
                    max(rc[0] + 0.12 + r.size(rc[4].label, FS["zone"], "bold")[0]
                        for rc in rects))
        top = max(rc[3] for rc in rects)
    else:
        boxes = [_extent(r, spec, k, cx, cy) for k in spec.nodes]
        body_dx = 0.40 - min(b[0] for b in boxes)
        right, top = max(b[2] for b in boxes), max(b[3] for b in boxes)

    body_dy = note_h + legend_h + 0.34
    W = right + body_dx + 0.40
    H = top + body_dy + head_h + 0.30

    fig = plt.figure(figsize=(W, H), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(0.40, H - 0.26, spec.title, fontsize=FS["title"], weight="bold",
            va="top")
    if spec.subtitle:
        ax.text(0.40, H - 0.26 - title_h - 0.10, spec.subtitle,
                fontsize=FS["sub"], color="#555555", va="top")

    def P(key):
        n = spec.nodes[key]
        return cx[n.col] + body_dx, cy[n.row] + body_dy

    for x0, y0, x1, y1, z in rects:
        x0, x1 = x0 + body_dx, x1 + body_dx
        y0, y1 = y0 + body_dy, y1 + body_dy
        ax.add_patch(FancyBboxPatch(
            (x0, y0), x1 - x0, y1 - y0, boxstyle="round,pad=0.04",
            facecolor="none", edgecolor=z.colour, linewidth=1.3,
            linestyle=z.dash, alpha=0.75, zorder=1))
        ax.text(x0 + 0.12, y1 - 0.08, z.label, fontsize=FS["zone"],
                style="italic", weight="bold", color=z.colour, va="top",
                zorder=2, bbox=MASK)

    # Arrows first, so tiles and captions paint over their ends.
    for e in spec.edges:
        x0, y0 = P(e.src)
        x1, y1 = P(e.dst)
        sx, sy = _anchor(x0, y0, x1, y1, spec.tile / 2 + 0.05)
        ex, ey = _anchor(x1, y1, x0, y0, spec.tile / 2 + 0.05)
        colour = (CAT.get("agent") if e.style == "target"
                  else CAT.get(spec.nodes[e.src].cat, "#232f3e"))
        ax.add_patch(FancyArrowPatch(
            (sx, sy), (ex, ey), arrowstyle="-|>", mutation_scale=11,
            linewidth=1.25, color=colour, alpha=0.95, zorder=3,
            linestyle="solid" if e.style == "solid" else "dashed",
            connectionstyle=f"arc3,rad={e.curve}", shrinkA=0, shrinkB=0))
        if e.label:
            ax.text((sx + ex) / 2 + e.nudge_x, (sy + ey) / 2 + e.nudge_y,
                    e.label, ha="center", va="center", fontsize=FS["edge"],
                    color="#2b2b2b", zorder=6, bbox=MASK)

    for key, n in spec.nodes.items():
        x, y = P(key)
        colour = CAT.get(n.cat, "#232f3e")
        art = os.path.join(spec.icon_dir, n.art) if n.art.endswith(".png") else None
        # Official icons are line art and need a light tile to sit on; glyph
        # tiles stay filled with the category colour.
        ax.add_patch(FancyBboxPatch(
            (x - spec.tile / 2, y - spec.tile / 2), spec.tile, spec.tile,
            boxstyle="round,pad=0.02",
            facecolor="#f7f4ff" if art else colour,
            edgecolor=colour, linewidth=1.6,
            linestyle="solid" if n.status == "built" else "dashed", zorder=5))
        if art:
            pad = 0.10
            ax.imshow(mpimg.imread(art), aspect="auto", zorder=6,
                      extent=(x - spec.tile / 2 + pad, x + spec.tile / 2 - pad,
                              y - spec.tile / 2 + pad, y + spec.tile / 2 - pad))
        else:
            ax.text(x, y, n.art, ha="center", va="center", fontsize=FS["glyph"],
                    color="white", weight="bold", zorder=6)
        if n.status != "built":
            # Left of the tile's top edge: the right side is where a vertical
            # arrow's head lands, and the tag would sit on top of it.
            ax.text(x - spec.tile / 2, y + spec.tile / 2 + 0.05,
                    n.status.upper(), ha="left", va="bottom",
                    fontsize=FS["tag"], weight="bold", color=colour, zorder=7)
        cap_t, _cw, cap_h = r.block(n.caption, FS["cap"], spec.col_w, "bold")
        ax.text(x, y - spec.tile / 2 - 0.10, cap_t, ha="center", va="top",
                fontsize=FS["cap"], weight="bold", color="#111111", zorder=6,
                linespacing=1.3, bbox=MASK)
        if n.sub:
            sub_t, _sw, _sh = r.block(n.sub, FS["sub"], spec.col_w)
            ax.text(x, y - spec.tile / 2 - 0.14 - cap_h, sub_t, ha="center",
                    va="top", fontsize=FS["sub"], color="#555555", zorder=6,
                    linespacing=1.3, bbox=MASK)

    if spec.legend:
        lx, ly = 0.40, note_h + 0.30 + legend_h / 2
        for style, colour, text in spec.legend:
            ax.add_patch(FancyArrowPatch(
                (lx, ly), (lx + 0.42, ly), arrowstyle="-|>", mutation_scale=11,
                linewidth=1.25, color=colour, zorder=4,
                linestyle="solid" if style == "solid" else "dashed"))
            ax.text(lx + 0.52, ly, text, fontsize=FS["sub"], color="#333333",
                    va="center", zorder=4)
            lx += 0.52 + r.size(text, FS["sub"])[0] + 0.55

    if spec.note:
        ax.text(0.40, note_h + 0.20, spec.note, fontsize=FS["note"],
                color="#333333", va="top", linespacing=1.55, zorder=7)
    r.close()

    overlaps = text_overlaps(fig, ax)
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)
    return overlaps


def render(spec, out_path, checks=CHECKS):
    """Run every check, then draw. Returns a process exit code.

    Checks run BEFORE drawing so a spec asserting something false never produces
    a PNG at all — there is no half-correct artifact to accidentally circulate.
    """
    for check in checks:
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
    print("  NOW READ THE PNG — checks cannot catch a wrong architecture.")
    return 0
