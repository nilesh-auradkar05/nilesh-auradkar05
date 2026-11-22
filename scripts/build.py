#!/usr/bin/env python3
"""Render the profile README as a set of self-contained SVG cards, in light and dark themes.

GitHub renders README images inside an <img> sandbox: no external fonts, images or scripts
load from inside an SVG. So every card embeds its own font subset (only the glyphs it uses)
and, optionally, the avatar as a data URI. Each card is rendered twice and served through
<picture> + prefers-color-scheme, so the README follows the viewer's GitHub theme.

Usage:
    python scripts/build.py            # build from data/ on disk
    python scripts/build.py --live     # refresh contributions + avatar from GitHub first
                                       # (needs GITHUB_TOKEN or GH_STATS_TOKEN in env)
Edit profile.json, never README.md or assets/cards/* by hand.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import logging
import os
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
CARD_DIR = ROOT / "assets" / "cards"
DATA_DIR = ROOT / "data"
log = logging.getLogger("build")
W = 1000  # every full-width card shares this viewBox width


# ───────────────────────────── themes ─────────────────────────────
@dataclass(frozen=True)
class Theme:
    """One restrained accent; everything else is neutral. Values track GitHub's own Primer palette
    so cards sit naturally on the page in either theme."""
    name: str
    bg: str           # card background (matches GitHub canvas)
    surface: str      # inset panels
    border: str
    border_soft: str
    ink: str          # primary text
    ink2: str         # body text
    muted: str        # meta / labels
    accent: str       # rules, markers
    accent_ink: str   # accent-coloured text (meets contrast on bg)
    accent_bg: str    # tinted fill for highlighted items
    accent_line: str  # border for highlighted items
    accent_fill: str  # solid fill (link pills); text on it is white
    ok: str           # availability dot
    heat: tuple[str, str, str, str, str]


LIGHT = Theme("light", bg="#ffffff", surface="#f6f8fa", border="#d0d7de", border_soft="#e6eaef",
              ink="#1f2328", ink2="#424a53", muted="#656d76", accent="#0969da", accent_ink="#0550ae",
              accent_bg="#ddf4ff", accent_line="#54aeff", accent_fill="#0969da", ok="#1a7f37",
              heat=("#ebedf0", "#b6d7f7", "#6cb0f0", "#2f81e0", "#0a4fa6"))
DARK = Theme("dark", bg="#0d1117", surface="#161b22", border="#30363d", border_soft="#21262d",
             ink="#e6edf3", ink2="#adbac7", muted="#7d8590", accent="#2f81f7", accent_ink="#58a6ff",
             accent_bg="#0c2d6b", accent_line="#1f6feb", accent_fill="#1f6feb", ok="#3fb950",
             heat=("#161b22", "#0c2d6b", "#1158c7", "#388bfd", "#79c0ff"))
T: Theme = DARK  # active theme; set per render pass in build()

# face key -> (file, css family, weight, style)
FACES = {
    "serif":   ("instrument-serif-latin-400-normal.woff2", "IS", 400, "normal"),
    "serif-i": ("instrument-serif-latin-400-italic.woff2", "IS", 400, "italic"),
    "mono":    ("jetbrains-mono-latin-400-normal.woff2", "JB", 400, "normal"),
    "mono-m":  ("jetbrains-mono-latin-500-normal.woff2", "JB", 500, "normal"),
    "mono-b":  ("jetbrains-mono-latin-700-normal.woff2", "JB", 700, "normal"),
    "sans":    ("inter-latin-400-normal.woff2", "IN", 400, "normal"),
    "sans-m":  ("inter-latin-500-normal.woff2", "IN", 500, "normal"),
    "sans-sb": ("inter-latin-600-normal.woff2", "IN", 600, "normal"),
}
FALLBACK = {"IS": "Georgia, serif", "JB": "ui-monospace, Menlo, monospace", "IN": "system-ui, sans-serif"}

BASE_CSS = """
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.pulse{animation:pulse 2.6s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


class Fonts:
    """Glyph metrics + per-card woff2 subsetting."""

    def __init__(self) -> None:
        self._tt: dict[str, TTFont] = {}
        self._cache: dict[tuple[str, frozenset], str] = {}
        for key, (fname, *_rest) in FACES.items():
            path = FONT_DIR / fname
            if not path.exists():
                raise FileNotFoundError(f"missing font {path}")
            self._tt[key] = TTFont(str(path))

    def width(self, text: str, face: str, size: float, ls: float = 0.0) -> float:
        tt = self._tt[face]
        cmap, hmtx = tt.getBestCmap(), tt["hmtx"]
        upm = tt["head"].unitsPerEm
        notdef = hmtx["space"][0] if "space" in hmtx.metrics else upm // 2
        adv = sum(hmtx[cmap[ord(c)]][0] if ord(c) in cmap else notdef for c in text)
        return adv * size / upm + ls * max(len(text) - 1, 0)

    def wrap(self, text: str, face: str, size: float, max_w: float) -> list[str]:
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if self.width(trial, face, size) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def face_css(self, face: str, chars: set[str]) -> str:
        key = (face, frozenset(chars))
        if key not in self._cache:
            fname, fam, weight, style = FACES[face]
            opts = subset.Options()
            opts.flavor = "woff2"
            opts.layout_features = ["kern", "liga"]
            opts.name_IDs = []
            opts.notdef_outline = True
            font = TTFont(str(FONT_DIR / fname))
            sub = subset.Subsetter(opts)
            sub.populate(text="".join(sorted(chars | {" "})))
            sub.subset(font)
            buf = io.BytesIO()
            font.flavor = "woff2"
            font.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode()
            self._cache[key] = (
                f"@font-face{{font-family:{fam};font-weight:{weight};font-style:{style};"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
            )
        return self._cache[key]


@dataclass
class Svg:
    fonts: Fonts
    w: float
    h: float
    title: str
    parts: list[str] = field(default_factory=list)
    used: dict[str, set[str]] = field(default_factory=dict)
    _ids: int = 0

    def uid(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, face="sans", size=14, fill=None, anchor="start", ls=0.0, opacity=None) -> float:
        """Draw text; returns rendered width (for inline layout)."""
        fill = fill or T.ink
        self.used.setdefault(face, set()).update(s)
        _f, fam, weight, style = FACES[face]
        attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'font-family="{fam}, {FALLBACK[fam]}"',
                 f'font-size="{size}"', f'fill="{fill}"']
        if weight != 400:
            attrs.append(f'font-weight="{weight}"')
        if style != "normal":
            attrs.append('font-style="italic"')
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if ls:
            attrs.append(f'letter-spacing="{ls}"')
        if opacity is not None:
            attrs.append(f'opacity="{opacity}"')
        self.add(f"<text {' '.join(attrs)}>{escape(s)}</text>")
        return self.fonts.width(s, face, size, ls)

    def runs(self, x, y, runs: list[tuple[str, str, str]], size: float) -> float:
        """Inline runs of (text, face, fill) on one baseline; returns end x."""
        for s, face, fill in runs:
            x += self.text(x, y, s, face=face, size=size, fill=fill)
        return x

    def render(self) -> str:
        css = "".join(self.fonts.face_css(f, c) for f, c in sorted(self.used.items()))
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w:g}" height="{self.h:g}" '
            f'viewBox="0 0 {self.w:g} {self.h:g}" role="img" aria-label="{escape(self.title)}">'
            f"<title>{escape(self.title)}</title><style>{css}{BASE_CSS}</style>{''.join(self.parts)}</svg>"
        )


# ───────────────────────────── primitives ─────────────────────────────
def frame(s: Svg, rx: float = 12) -> None:
    """Card background + hairline border, shared by every SVG."""
    s.add(f'<rect x="0.5" y="0.5" width="{s.w-1}" height="{s.h-1}" rx="{rx}" fill="{T.bg}" stroke="{T.border}"/>')


def panel(s: Svg, x, y, w, h, fill=None, rx=8, stroke=None) -> None:
    s.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill or T.surface}" '
          f'stroke="{stroke or T.border_soft}"/>')


def header(s: Svg, y: float, idx: str, label: str, lead: str, accent: str, meta: str = "") -> float:
    """Section header: '01 / ABOUT' + serif headline with an italic second half. Returns bottom y."""
    s.text(40, y + 30, idx, face="mono-b", size=11, fill=T.accent_ink, ls=1.5)
    s.text(40 + f_w(s, idx, "mono-b", 11, 1.5) + 10, y + 30, f"/  {label}", face="mono-m", size=11, fill=T.muted, ls=1.8)
    s.runs(39, y + 76, [(lead, "serif", T.ink), (accent, "serif-i", T.ink2)], size=40)
    if meta:
        s.text(W - 40, y + 72, meta, face="mono", size=10, fill=T.muted, anchor="end", ls=1.5)
    s.add(f'<rect x="40" y="{y+94}" width="{W-80}" height="1" fill="{T.border_soft}"/>')
    s.add(f'<rect x="40" y="{y+93.5}" width="32" height="2" fill="{T.accent}"/>')
    return y + 106


def f_w(s: Svg, text, face, size, ls=0.0) -> float:
    return s.fonts.width(text, face, size, ls)


def chip(s: Svg, x, y, label: str, face="mono-m", size=10.5, fill=None, bg=None, stroke=None, pad=10, h=24) -> float:
    w = s.fonts.width(label, face, size, 0.4) + pad * 2
    s.add(f'<rect x="{x}" y="{y}" width="{w:.1f}" height="{h}" rx="5" fill="{bg or T.surface}" stroke="{stroke or T.border}"/>')
    s.text(x + pad, y + h / 2 + size * 0.36, label, face=face, size=size, fill=fill or T.ink2, ls=0.4)
    return w


def para(s: Svg, x, y, text, face="sans", size=14, fill=None, max_w=500, lh=21, max_lines=None) -> float:
    lines = s.fonts.wrap(text, face, size, max_w)
    if max_lines and len(lines) > max_lines:
        raise ValueError(f"text overflows {max_lines} lines at {max_w}px: {text[:60]}…")
    for i, ln in enumerate(lines):
        s.text(x, y + i * lh, ln, face=face, size=size, fill=fill or T.ink2)
    return y + (len(lines) - 1) * lh


def stages(s: Svg, fx, fw, top, bot, nodes: list[str], highlight: set[int]) -> None:
    """Vertical pipeline of labelled stages joined by a rail; highlighted stages get the accent tint."""
    nh = 30
    step = (bot - top - nh) / max(len(nodes) - 1, 1)
    lx = fx + 16
    s.add(f'<path d="M{lx} {top+nh/2}V{top+nh/2+step*(len(nodes)-1):.1f}" stroke="{T.border}" stroke-width="1.5"/>')
    for i, node in enumerate(nodes):
        ny = top + i * step
        on = i in highlight
        s.add(f'<rect x="{fx}" y="{ny:.1f}" width="{fw}" height="{nh}" rx="6" '
              f'fill="{T.accent_bg if on else T.bg}" stroke="{T.accent_line if on else T.border}"/>')
        s.add(f'<circle cx="{lx}" cy="{ny+nh/2:.1f}" r="3.5" fill="{T.accent if on else T.muted}"/>')
        s.text(fx + 32, ny + 19.5, f"{i+1:02d}", face="mono", size=10, fill=T.accent_ink if on else T.muted)
        s.text(fx + 58, ny + 19.5, node, face="mono-m", size=12, fill=T.accent_ink if on else T.ink)


def save(s: Svg, name: str) -> str:
    out = CARD_DIR / T.name / f"{name}.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(s.render(), encoding="utf-8")
    log.debug("wrote %s/%-26s %6.1f KB", T.name, out.name, out.stat().st_size / 1024)
    return name


# ───────────────────────────── cards ─────────────────────────────
def card_hero(f: Fonts, p: dict, avatar_b64: str | None) -> str:
    s = Svg(f, W, 440, f"{p['name_first']} {p['name_last']} — {p['roles'].title()}")
    frame(s)
    s.add(f'<rect x="0.5" y="0.5" width="{W-1}" height="4" rx="2" fill="{T.accent}"/>')

    s.text(W - 48, 52, "README.MD", face="mono", size=10, fill=T.muted, anchor="end", ls=1.5)
    chip(s, 48, 34, p["prompt_chip"], face="mono-m", size=11.5, fill=T.ink2, h=28, pad=12)

    s.text(50, 112, p["eyebrow"], face="mono-m", size=11.5, fill=T.accent_ink, ls=2.2)
    s.text(44, 200, p["name_first"], face="serif", size=96, fill=T.ink)
    x = s.text(44, 284, p["name_last"], face="serif-i", size=96, fill=T.ink)
    s.text(44 + x - 6, 284, ".", face="serif", size=96, fill=T.accent)  # italic advance overhangs
    s.text(50, 330, p["roles"], face="mono-b", size=11.5, fill=T.ink, ls=1.6)
    s.text(50, 366, p["tagline"], face="serif-i", size=22, fill=T.ink2)
    s.add(f'<rect x="50" y="388" width="32" height="1.5" fill="{T.accent}"/>')
    s.text(94, 392, p["meta"], face="mono", size=10.5, fill=T.muted, ls=1.2)

    # portrait: arch frame
    cx, top, aw, ah = 812, 72, 200, 256
    r = aw / 2
    arch = f"M{cx-r} {top+ah}V{top+r}A{r} {r} 0 0 1 {cx+r} {top+r}V{top+ah}Z"
    clip = s.uid("pc")
    s.add(f'<clipPath id="{clip}"><path d="{arch}"/></clipPath>')
    if avatar_b64:
        s.add(f'<image href="data:image/jpeg;base64,{avatar_b64}" x="{cx-r}" y="{top}" width="{aw}" height="{ah}" '
              f'preserveAspectRatio="xMidYMid slice" clip-path="url(#{clip})"/>')
    else:
        s.add(f'<path d="{arch}" fill="{T.surface}"/>')
        initials = p["name_first"][0] + p["name_last"][0]
        s.text(cx, top + ah * 0.64, initials, face="serif-i", size=104, fill=T.muted, anchor="middle")
    s.add(f'<path d="{arch}" fill="none" stroke="{T.border}" stroke-width="1.5"/>')
    s.text(cx - r, top + ah + 22, "FIG. 01", face="mono", size=9.5, fill=T.muted, ls=1.4)
    s.text(cx + r, top + ah + 22, "N. AURADKAR", face="mono", size=9.5, fill=T.muted, ls=1.4, anchor="end")
    lab = p["status"]
    tw = f.width(lab, "mono-b", 10, 1.2) + 34
    sx = cx - tw / 2
    sy = top + ah + 36
    s.add(f'<rect x="{sx:.1f}" y="{sy}" width="{tw:.1f}" height="24" rx="12" fill="{T.surface}" stroke="{T.border}"/>')
    s.add(f'<circle class="pulse" cx="{sx+14:.1f}" cy="{sy+12}" r="3.5" fill="{T.ok}"/>')
    s.text(sx + 24, sy + 16, lab, face="mono-b", size=10, fill=T.ink, ls=1.2)
    return save(s, "hero")


def card_pill(f: Fonts, label: str, value: str, name: str) -> str:
    lw = f.width(label, "mono-b", 10.5, 1.2) + 24
    vw = f.width(value.upper(), "mono-b", 10.5, 0.8) + 24
    s = Svg(f, lw + vw, 30, f"{label}: {value}")
    s.add(f'<rect x="0.5" y="0.5" width="{lw+vw-1}" height="29" rx="6" fill="{T.surface}" stroke="{T.border}"/>')
    s.add(f'<path d="M{lw} 0.5H{lw+vw-6.5}A6 6 0 0 1 {lw+vw-0.5} 6.5V23.5A6 6 0 0 1 {lw+vw-6.5} 29.5H{lw}Z" fill="{T.accent_fill}"/>')
    s.text(12, 19.5, label, face="mono-b", size=10.5, fill=T.ink2, ls=1.2)
    s.text(lw + 12, 19.5, value.upper(), face="mono-b", size=10.5, fill="#ffffff", ls=0.8)
    return save(s, name)


def card_about(f: Fonts, p: dict, idx: str) -> str:
    a = p["about"]
    lh = 23
    n = len(a["lines"]) + 1
    body_h = 46 + n * lh + 18
    s = Svg(f, W, 106 + 16 + body_h + 28, f"About {p['name_first']}")
    frame(s)
    y = header(s, 0, idx, "ABOUT", "Hello, I'm ", f"{p['name_first']}.", meta="WHOAMI") + 16
    tw = 610
    panel(s, 24, y, tw, body_h)
    s.add(f'<path d="M24 {y+34}H{24+tw}" stroke="{T.border_soft}"/>')
    for i in range(3):
        s.add(f'<circle cx="{44+i*16}" cy="{y+17}" r="5" fill="{T.border}"/>')
    s.text(24 + tw / 2, y + 21, a["terminal_title"], face="mono", size=11, fill=T.muted, anchor="middle")
    ty = y + 34 + 30
    for i, (sig, txt) in enumerate(a["lines"]):
        if sig == "$":
            s.text(48, ty + i * lh, "$", face="mono-b", size=13, fill=T.accent_ink)
            s.text(66, ty + i * lh, txt, face="mono-m", size=13, fill=T.ink)
        else:
            s.text(66, ty + i * lh, txt, face="mono", size=12.5, fill=T.ink2)
    last = ty + len(a["lines"]) * lh
    s.text(48, last, "$", face="mono-b", size=13, fill=T.accent_ink)
    s.add(f'<rect x="66" y="{last-12}" width="8" height="15" fill="{T.muted}"/>')
    sx = 24 + tw + 16
    sw = W - 24 - sx
    panel(s, sx, y, sw, body_h)
    s.text(sx + 24, y + 30, "CURRENTLY", face="mono-b", size=10, fill=T.accent_ink, ls=1.8)
    s.add(f'<path d="M{sx+24} {y+44}H{sx+sw-24}" stroke="{T.border_soft}"/>')
    yy = y + 72
    for k, v in a["sidebar"]:
        s.text(sx + 24, yy, k, face="mono-m", size=9.5, fill=T.muted, ls=1.6)
        end = para(s, sx + 24, yy + 22, v, face="sans-m", size=15, fill=T.ink, max_w=sw - 48, lh=20, max_lines=2)
        yy = end + 34
    if yy - 34 > y + body_h:
        raise ValueError("about sidebar overflows; shorten sidebar values")
    return save(s, "about")


def card_record(f: Fonts, p: dict, stats: dict, idx: str) -> str:
    th = 190
    s = Svg(f, W, 106 + 16 + th + 28, "On the record")
    frame(s)
    y = header(s, 0, idx, "ON THE RECORD", "Numbers, ", "not adjectives.", meta="SOURCED FROM REPO EVALS + GITHUB") + 16
    tiles = p["record"]
    gap = 16
    tw = (W - 48 - gap * (len(tiles) - 1)) / len(tiles)
    for i, t in enumerate(tiles):
        x = 24 + i * (tw + gap)
        panel(s, x, y, tw, th)
        s.add(f'<rect x="{x+22}" y="{y}" width="28" height="2" fill="{T.accent}"/>')
        s.text(x + 22, y + 32, f"0{i+1}", face="mono-m", size=10, fill=T.muted, ls=1.6)
        val = t["value"].replace("{contributions}", f"{stats['total']:,}")
        vx = s.text(x + 20, y + 100, val, face="serif", size=62, fill=T.ink)
        if t["unit"]:
            s.text(x + 26 + vx, y + 100, t["unit"], face="serif-i", size=26, fill=T.ink2)
        para(s, x + 22, y + 134, t["label"], face="sans", size=13, fill=T.ink2, max_w=tw - 44, lh=18, max_lines=3)
    return save(s, "record")


def card_section(f: Fonts, idx, label, lead, accent, meta, name) -> str:
    s = Svg(f, W, 116, f"{lead}{accent}")
    frame(s)
    header(s, 0, idx, label, lead, accent, meta)
    return save(s, name)


def card_project(f: Fonts, pr: dict, n: int) -> str:
    H = 420
    s = Svg(f, W, H, f"{pr['title']} — {pr['subtitle']}")
    frame(s)
    s.text(56, 66, pr["kicker"], face="mono-b", size=10.5, fill=T.accent_ink, ls=1.6)
    s.text(54, 120, pr["title"], face="serif", size=50, fill=T.ink)
    s.text(56, 154, pr["subtitle"], face="serif-i", size=21, fill=T.ink2)
    end = para(s, 56, 192, pr["desc"], face="sans", size=14.5, fill=T.ink2, max_w=560, lh=22, max_lines=4)
    my = max(end + 58, 300)
    mw = 560 / len(pr["metrics"])
    for i, (v, lab) in enumerate(pr["metrics"]):
        mx = 56 + i * mw
        if i:
            s.add(f'<path d="M{mx-14} {my-30}V{my+14}" stroke="{T.border_soft}"/>')
        s.text(mx, my, v, face="serif", size=34, fill=T.ink)
        s.text(mx, my + 20, lab.upper(), face="mono", size=9, fill=T.muted, ls=1.0)
    cy = my + 40
    s.text(56, cy + 16, "STACK", face="mono-b", size=9.5, fill=T.muted, ls=1.6)
    cx = 108
    for t in pr["stack"]:
        w = f.width(t, "mono-m", 10, 0.4) + 18
        if cx + w > 640:
            break
        cx += chip(s, cx, cy, t, size=10, pad=9, h=22) + 6
    fx, fw = 668, 272
    panel(s, fx - 16, 40, fw + 32, H - 80)
    s.text(fx, 70, "PIPELINE", face="mono-b", size=9.5, fill=T.muted, ls=1.8)
    s.text(fx + fw, 70, f"{len(pr['flow'])} STAGES", face="mono", size=9.5, fill=T.muted, ls=1.4, anchor="end")
    stages(s, fx, fw, 90, H - 70, pr["flow"], {len(pr["flow"]) - 1})
    s.text(56, H - 30, "VIEW REPOSITORY  →", face="mono-b", size=9.5, fill=T.accent_ink, ls=1.4)
    return save(s, f"project-{pr['slug']}")


def card_publication(f: Fonts, pub: dict, n: int) -> str:
    """Paper card: metadata + highlighted author list + summary on the left, method schematic on the right.
    Height is derived from content, so long titles or summaries grow the card instead of overflowing."""
    s = Svg(f, W, 0, f"{pub['title']} — arXiv:{pub['arxiv']}")
    LX, LW = 56, 570
    s.text(LX, 66, pub["kicker"], face="mono-b", size=10.5, fill=T.accent_ink, ls=1.5)
    y = 110
    last_title = y
    for i, ln in enumerate(f.wrap(pub["title"], "serif", 33, LW)):
        last_title = y + i * 37
        s.text(LX - 2, last_title, ln, face="serif", size=33, fill=T.ink)
    y = last_title + 34
    runs = []
    for i, a in enumerate(pub["authors"]):
        runs.append((a, "sans-sb", T.ink) if a == pub["self"] else (a, "sans", T.ink2))
        if i < len(pub["authors"]) - 1:
            runs.append((", ", "sans", T.ink2))
    if sum(f.width(t, face, 13) for t, face, _ in runs) > LW:
        raise ValueError("author list too wide for one line; abbreviate first names in profile.json")
    s.runs(LX, y, runs, size=13)
    # underline own name: the one emphasis that matters on a co-authored paper
    xs = LX + sum(f.width(t, face, 13) for t, face, _ in runs[:runs.index((pub["self"], "sans-sb", T.ink))])
    s.add(f'<rect x="{xs:.1f}" y="{y+4}" width="{f.width(pub["self"], "sans-sb", 13):.1f}" height="1.5" fill="{T.accent}"/>')
    y = para(s, LX, y + 34, pub["summary"], face="sans", size=14.5, fill=T.ink2, max_w=LW, lh=22, max_lines=6)
    cx, cy = LX, y + 26
    for t in pub["tags"]:
        w = f.width(t, "mono-m", 10, 0.4) + 18
        if cx + w > LX + LW:
            break
        cx += chip(s, cx, cy, t, size=10, pad=9, h=22) + 6
    H = max(cy + 22 + 66, 420)
    s.text(LX, H - 30, "READ ON ARXIV  →", face="mono-b", size=9.5, fill=T.accent_ink, ls=1.4)

    fx, fw = 668, 272
    body, s.parts = s.parts, []
    s.h = H
    frame(s)
    panel(s, fx - 16, 40, fw + 32, H - 80)
    s.parts += body
    s.text(fx, 70, "MEMTRIM", face="mono-b", size=9.5, fill=T.ink, ls=1.8)
    s.text(fx + fw, 70, "SCHEMATIC", face="mono", size=9.5, fill=T.muted, ls=1.4, anchor="end")
    stages(s, fx, fw, 90, H - 104, pub["flow"], set(pub.get("flow_highlight", [])))
    s.add(f'<path d="M{fx} {H-88}H{fx+fw}" stroke="{T.border_soft}"/>')
    s.add(f'<rect x="{fx}" y="{H-77}" width="8" height="8" rx="2" fill="{T.accent_bg}" stroke="{T.accent_line}"/>')
    s.text(fx + 15, H - 70, "MEMTRIM STAGE", face="mono", size=9, fill=T.muted, ls=1.2)
    s.text(fx + fw, H - 70, "NO RETRAINING", face="mono", size=9, fill=T.muted, ls=1.2, anchor="end")
    s.text(fx, H - 54, pub["flow_caption"], face="mono", size=9, fill=T.muted, ls=1.2)
    return save(s, f"paper-{pub['arxiv'].replace('.', '-')}")


def bibtex(pub: dict) -> str:
    authors = " and ".join(f"{a.split()[-1]}, {' '.join(a.split()[:-1])}" for a in pub["authors"])
    return (f"@misc{{{pub['bibkey']},\n  title         = {{{pub['title']}}},\n  author        = {{{authors}}},\n"
            f"  year          = {{{pub['year']}}},\n  eprint        = {{{pub['arxiv']}}},\n  archivePrefix = {{arXiv}},\n"
            f"  primaryClass  = {{{pub['primary_class']}}},\n  url           = {{https://arxiv.org/abs/{pub['arxiv']}}}\n}}")


def card_bench(f: Fonts, b: dict, i: int) -> str:
    w, h = 324, 206
    s = Svg(f, w, h, f"{b['title']} — {b['desc']}")
    frame(s, rx=10)
    s.text(22, 34, f"{i:02d} · {b['kicker']}", face="mono-b", size=9.5, fill=T.accent_ink, ls=1.4)
    s.text(21, 74, b["title"], face="serif", size=30, fill=T.ink)
    para(s, 22, 104, b["desc"], face="sans", size=13, fill=T.ink2, max_w=w - 44, lh=19, max_lines=3)
    s.add(f'<path d="M22 {h-40}H{w-22}" stroke="{T.border_soft}"/>')
    s.text(22, h - 18, b["lang"].upper(), face="mono", size=9.5, fill=T.muted, ls=1.2)
    s.text(w - 22, h - 18, "→", face="mono-b", size=12, fill=T.accent_ink, anchor="end")
    return save(s, f"bench-{i:02d}")


def card_toolkit(f: Fonts, p: dict, idx: str) -> str:
    groups = p["toolkit"]
    cols, gap, bh = 3, 16, 164
    rows = -(-len(groups) // cols)
    s = Svg(f, W, 106 + 16 + rows * bh + (rows - 1) * gap + 28, "The tools I reach for")
    frame(s)
    y0 = header(s, 0, idx, "TOOLKIT", "The tools I ", "reach for.", meta="DAILY DRIVERS") + 16
    bw = (W - 48 - gap * (cols - 1)) / cols
    for i, g in enumerate(groups):
        x = 24 + (i % cols) * (bw + gap)
        y = y0 + (i // cols) * (bh + gap)
        panel(s, x, y, bw, bh)
        s.text(x + 20, y + 30, f"{i+1:02d}  {g['label']}", face="mono-b", size=9.5, fill=T.accent_ink, ls=1.4)
        lines: list[str] = []  # wrap between items only, so a separator never starts or ends a line
        for item in g["items"]:
            trial = f"{lines[-1]} · {item}" if lines else item
            if lines and f.width(trial, "serif", 24) <= bw - 40:
                lines[-1] = trial
            else:
                lines.append(item)
        if len(lines) > 3:
            raise ValueError(f"toolkit group {g['label']} wraps past 3 lines")
        for j, ln in enumerate(lines):
            s.text(x + 20, y + 66 + j * 28, ln, face="serif", size=24, fill=T.ink)
        s.add(f'<path d="M{x+20} {y+bh-36}H{x+bw-20}" stroke="{T.border_soft}"/>')
        s.text(x + 20, y + bh - 16, g["note"].upper(), face="mono", size=9, fill=T.muted, ls=1.1)
    return save(s, "toolkit")


def heat_stats(days: list[dict], today: dt.date) -> dict:
    total = sum(d["count"] for d in days)
    active = sum(1 for d in days if d["count"] > 0)
    best = max(days, key=lambda d: d["count"]) if days else {"count": 0, "date": ""}
    longest, run, start, lstart, lend = 0, 0, None, None, None
    for d in days:
        if d["count"] > 0:
            run += 1
            start = start or d["date"]
            if run > longest:
                longest, lstart, lend = run, start, d["date"]
        else:
            run, start = 0, None
    current = 0
    for d in reversed(days):
        if d["date"] == today.isoformat() and d["count"] == 0:
            continue  # today isn't over yet
        if d["count"] == 0:
            break
        current += 1
    return {"total": total, "active": active, "best": best["count"], "best_date": best["date"],
            "longest": longest, "lstart": lstart, "lend": lend, "current": current}


def card_heatmap(f: Fonts, days: list[dict], st: dict, idx: str) -> str:
    ph = 264
    s = Svg(f, W, 106 + 16 + ph + 28, f"{st['total']} GitHub contributions in the last year")
    frame(s)
    y = header(s, 0, idx, "COMMIT LOG", "A year of ", "building.", meta="REFRESHED DAILY FROM GITHUB") + 16
    panel(s, 24, y, W - 48, ph)
    s.text(48, y + 34, "GITHUB ACTIVITY · LAST 12 MONTHS", face="mono-b", size=9.5, fill=T.accent_ink, ls=1.5)
    s.runs(48, y + 66, [("Shipped in public, ", "serif", T.ink), ("one square at a time.", "serif-i", T.ink2)], size=24)
    items = [(f"{st['total']:,}", "CONTRIBUTIONS"), (str(st["active"]), "ACTIVE DAYS"),
             (str(st["longest"]), "LONGEST STREAK"), (str(st["best"]), "BEST DAY")]
    x = W - 48
    for v, lab in reversed(items):
        lw = max(f.width(lab, "mono", 8.5, 1.0), f.width(v, "serif", 30))
        s.text(x, y + 44, v, face="serif", size=30, fill=T.ink, anchor="end")
        s.text(x, y + 62, lab, face="mono", size=8.5, fill=T.muted, anchor="end", ls=1.0)
        x -= lw + 28
    first = dt.date.fromisoformat(days[0]["date"])
    origin = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # back to Sunday
    cell, g = 13, 3.2
    gx, gy = 92, y + 114
    months_seen = set()
    for d in days:
        date = dt.date.fromisoformat(d["date"])
        col = (date - origin).days // 7
        row = (date.weekday() + 1) % 7
        cx, cy = gx + col * (cell + g), gy + row * (cell + g)
        lvl = max(0, min(4, d["level"]))
        stroke = f' stroke="{T.border_soft}"' if lvl == 0 else ""
        s.add(f'<rect x="{cx:.1f}" y="{cy:.1f}" width="{cell}" height="{cell}" rx="2.5" fill="{T.heat[lvl]}"{stroke}>'
              f'<title>{d["count"]} on {date:%b %d}</title></rect>')
        key = (date.year, date.month)
        if date.day <= 7 and row == 0 and key not in months_seen:
            months_seen.add(key)
            s.text(cx, gy - 10, f"{date:%b}", face="mono", size=9, fill=T.muted, ls=0.6)
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        s.text(48, gy + row * (cell + g) + 10, lab, face="mono", size=9, fill=T.muted)
    fy = gy + 7 * (cell + g) + 26
    if st["longest"]:
        a = dt.date.fromisoformat(st["lstart"]); b = dt.date.fromisoformat(st["lend"])
        s.text(48, fy, f"LONGEST STREAK · {st['longest']} DAYS · {a:%d %b %Y} – {b:%d %b %Y}".upper(),
               face="mono", size=9, fill=T.muted, ls=1.2)
    lx = W - 48 - 5 * (cell + g) - 34
    s.text(lx - 8, fy, "LESS", face="mono", size=9, fill=T.muted, anchor="end", ls=1.0)
    for i, c in enumerate(T.heat):
        stroke = f' stroke="{T.border_soft}"' if i == 0 else ""
        s.add(f'<rect x="{lx + i*(cell+g):.1f}" y="{fy-10}" width="{cell}" height="{cell}" rx="2.5" fill="{c}"{stroke}/>')
    s.text(W - 48, fy, "MORE", face="mono", size=9, fill=T.muted, anchor="end", ls=1.0)
    return save(s, "heatmap")


def card_progress_row(f: Fonts, r: dict, i: int) -> str:
    h = 78
    s = Svg(f, W, h, f"{r['title']} — {r['desc']}")
    frame(s, rx=10)
    s.text(36, 47, f"{i:02d}", face="mono-m", size=14, fill=T.muted)
    s.add(f'<path d="M80 18V{h-18}" stroke="{T.border_soft}"/>')
    s.text(100, 34, r["title"], face="serif", size=25, fill=T.ink)
    s.text(100, 58, r["desc"], face="sans", size=13, fill=T.ink2)
    tag = r["tag"]
    tw = f.width(tag, "mono-b", 9.5, 1.2) + 20
    tx = W - 150 - tw
    s.add(f'<rect x="{tx:.1f}" y="27" width="{tw:.1f}" height="22" rx="4" fill="{T.surface}" stroke="{T.border}"/>')
    s.text(tx + 10, 41.5, tag, face="mono-b", size=9.5, fill=T.ink2, ls=1.2)
    s.add(f'<circle cx="{W-124}" cy="38" r="3.5" fill="{T.accent}"/>')
    s.text(W - 114, 42, "IN PROGRESS", face="mono-m", size=9, fill=T.muted, ls=1.0)
    s.text(W - 24, 43, "→", face="mono-b", size=13, fill=T.accent_ink, anchor="end")
    return save(s, f"wip-{i:02d}")


def card_connect(f: Fonts, p: dict, built: str, idx: str) -> str:
    ph = 220
    s = Svg(f, W, 106 + 16 + ph + 28, "Let's build something that ships")
    frame(s)
    y = header(s, 0, idx, "CONNECT", "Let's build something ", "that ships.", meta=p["status"]) + 16
    panel(s, 24, y, W - 48, ph)
    x = s.text(W / 2 - 6, y + 106, p["footer_signature"], face="serif-i", size=80, fill=T.ink, anchor="middle")
    s.text(W / 2 + x / 2 - 10, y + 106, ".", face="serif", size=80, fill=T.accent)
    s.text(W / 2, y + 144, p["footer_line"], face="sans-m", size=15, fill=T.ink2, anchor="middle")
    s.text(W / 2, y + 174, p["footer_meta"], face="mono", size=10, fill=T.muted, anchor="middle", ls=2)
    s.text(W / 2, y + 202, f"LAST BUILD · {built}", face="mono", size=9, fill=T.muted, anchor="middle", ls=1.6)
    return save(s, "connect")


# ───────────────────────────── data ─────────────────────────────
LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}
GQL = """query($login:String!){user(login:$login){avatarUrl(size:420)
contributionsCollection{contributionCalendar{weeks{contributionDays{date contributionCount contributionLevel}}}}}}"""


def refresh_live(login: str) -> None:
    token = os.environ.get("GH_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("--live needs GITHUB_TOKEN or GH_STATS_TOKEN")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": GQL, "variables": {"login": login}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-readme"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if payload.get("errors"):
        raise SystemExit(f"GraphQL error: {payload['errors']}")
    user = payload["data"]["user"]
    days = [{"date": d["date"], "count": d["contributionCount"], "level": LEVELS.get(d["contributionLevel"], 0)}
            for w in user["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    (DATA_DIR / "contributions.json").write_text(json.dumps(days))
    log.info("refreshed %d days, %d contributions", len(days), sum(d["count"] for d in days))
    try:
        with urllib.request.urlopen(user["avatarUrl"], timeout=30) as r:
            (DATA_DIR / "avatar.jpg").write_bytes(r.read())
        log.info("refreshed avatar")
    except OSError as e:  # avatar is cosmetic; never fail the build on it
        log.warning("avatar download failed: %s", e)


def load_avatar(p: dict) -> str | None:
    path = DATA_DIR / "avatar.jpg"
    if p.get("portrait", "avatar") != "avatar" or not path.exists():
        return None
    try:
        from PIL import Image  # optional: normalise to a small JPEG
        im = Image.open(path).convert("RGB")
        im.thumbnail((420, 420))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=86)
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        return base64.b64encode(path.read_bytes()).decode()


# ───────────────────────────── README ─────────────────────────────
def pic(name: str, alt: str, width: str | None = "100%", height: str | None = None) -> str:
    """Theme-aware image: GitHub serves the dark or light card via prefers-color-scheme."""
    size = f'width="{width}"' if width else f'height="{height}"'
    alt = escape(alt, {'"': "&quot;"})
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="assets/cards/dark/{name}.svg">'
            f'<source media="(prefers-color-scheme: light)" srcset="assets/cards/light/{name}.svg">'
            f'<img src="assets/cards/light/{name}.svg" {size} alt="{alt}"></picture>')


def render_all(f: Fonts, p: dict, days: list[dict], st: dict, today: dt.date, avatar: str | None) -> list[str]:
    """Render every card for the active theme and return the README lines."""
    gh = f"https://github.com/{p['username']}"
    pills = [f'<a href="{l["url"]}">'
             + pic(card_pill(f, l["label"], l["value"], f"pill-{l['label'].lower()}"), f"{l['label']}: {l['value']}",
                   width=None, height="30") + "</a>"
             for l in p["links"] if l["url"] and l["value"]]
    pills_html = "&nbsp;".join(pills)

    out = ["<!-- Generated by scripts/build.py — edit profile.json, not this file. -->", ""]
    P = lambda *xs: out.extend(['<p align="center">', *xs, "</p>", ""])
    counter = iter(range(1, 100))
    nx = lambda: f"{next(counter):02d}"  # section numbers follow render order

    P(pic(card_hero(f, p, avatar), f"{p['name_first']} {p['name_last']}. {p['roles']}. {p['tagline']}"))
    if pills:
        P(pills_html)
    P(pic(card_about(f, p, nx()), "About: " + " ".join(t for s_, t in p["about"]["lines"] if s_ == ">")))
    P(pic(card_record(f, p, st, nx()), "On the record: " + "; ".join(
        f"{t['value'].replace('{contributions}', str(st['total']))} {t['unit']} {t['label']}" for t in p["record"])))
    pubs = p.get("publications", [])
    if pubs:
        P(pic(card_section(f, nx(), "PUBLICATIONS", "Research, ", "on arXiv.",
                           f"{len(pubs)} PREPRINT{'S' if len(pubs) > 1 else ''} · AGENTS · INFERENCE", "h-papers"), "Publications"))
        for n, pub in enumerate(pubs, 1):
            P(f'<a href="https://arxiv.org/abs/{pub["arxiv"]}">'
              + pic(card_publication(f, pub, n), f"{pub['title']}. {', '.join(pub['authors'])}. arXiv:{pub['arxiv']}. {pub['summary']}")
              + "</a>")
        out.extend(["<details>", "<summary><b>Cite</b> (BibTeX)</summary>", "", "```bibtex",
                    *"\n\n".join(bibtex(x) for x in pubs).split("\n"), "```", "", "</details>", ""])
    P(pic(card_section(f, nx(), "SELECTED WORK", "Systems, ", "not notebooks.", "FLAGSHIP BUILDS", "h-work"), "Selected work"))
    for n, pr in enumerate(p["projects"], 1):
        P(f'<a href="{gh}/{pr["repo"]}">' + pic(card_project(f, pr, n), f"{pr['title']}: {pr['desc']}") + "</a>")
    P(pic(card_section(f, nx(), "RESEARCH BENCH", "Papers, ", "re-derived in code.", "POST-TRAINING · INFERENCE · AGENTS", "h-bench"), "Research bench"))
    for row in range(0, len(p["bench"]), 3):
        P("".join(f'<a href="{gh}/{b["repo"]}">' + pic(card_bench(f, b, row + j + 1), f"{b['title']}: {b['desc']}", "32.6%") + "</a>"
                  for j, b in enumerate(p["bench"][row:row + 3])))
    P(pic(card_toolkit(f, p, nx()), "Toolkit: " + "; ".join(f"{g['label']}: {', '.join(g['items'])}" for g in p["toolkit"])))
    P(pic(card_heatmap(f, days, st, nx()), f"{st['total']} contributions in the last year, {st['active']} active days, longest streak {st['longest']} days"))
    P(pic(card_section(f, nx(), "IN PROGRESS", "What's ", "compiling now.", "ACTIVE REPOSITORIES", "h-wip"), "In progress"))
    for i, r in enumerate(p["in_progress"], 1):
        P(f'<a href="{gh}/{r["repo"]}">' + pic(card_progress_row(f, r, i), f"{r['title']}: {r['desc']}") + "</a>")
    P(pic(card_connect(f, p, today.isoformat(), nx()), "Let's build something that ships."))
    if pills:
        P(pills_html)
    return out


def build(live: bool) -> None:
    global T
    p = json.loads((ROOT / "profile.json").read_text())
    if live:
        refresh_live(p["username"])
    days = json.loads((DATA_DIR / "contributions.json").read_text())
    today = dt.date.today()
    st = heat_stats(days, today)
    if CARD_DIR.exists():
        shutil.rmtree(CARD_DIR)
    f = Fonts()
    avatar = load_avatar(p)
    readmes = []
    for theme in (LIGHT, DARK):
        T = theme
        readmes.append(render_all(f, p, days, st, today, avatar))
    assert readmes[0] == readmes[1], "README must not depend on theme"
    (ROOT / "README.md").write_text("\n".join(readmes[0]), encoding="utf-8")
    files = list(CARD_DIR.rglob("*.svg"))
    log.info("README.md written · %d cards × 2 themes · %.0f KB total",
             len(files) // 2, sum(x.stat().st_size for x in files) / 1024)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="refresh contribution data and avatar from GitHub first")
    args = ap.parse_args()
    try:
        build(args.live)
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        sys.exit(1)
