#!/usr/bin/env python3
"""Render the profile README as a set of self-contained SVG cards.

GitHub renders README images inside an <img> sandbox: no external fonts, images
or scripts load from inside an SVG. So every card embeds its own font subset
(only the glyphs it uses) and, optionally, the avatar as a data URI.

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

# ───────────────────────────── design tokens ─────────────────────────────
INK = "#f7ecef"          # primary text
INK_2 = "#d9bfc8"        # secondary text
MUTED = "#9c7f89"        # tertiary / meta
PINK = "#ff5c8a"         # accent
PINK_SOFT = "#ff9bb8"    # accent text on dark
LINE = "rgba(255,92,138,0.28)"
LINE_SOFT = "rgba(255,92,138,0.14)"
BG = "#0b0508"
W = 1000                 # every full-width card shares this viewBox width
HEAT = ["#26111a", "#5a1430", "#8f1f48", "#cf2f63", "#ff6b95"]

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

    def text(self, x, y, s, face="sans", size=14, fill=INK, anchor="start", ls=0.0,
             opacity=None, cls=None, extra="") -> float:
        """Draw text; returns rendered width (for inline layout)."""
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
        if cls:
            attrs.append(f'class="{cls}"')
        if extra:
            attrs.append(extra)
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
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.w:g}" height="{self.h:g}" viewBox="0 0 {self.w:g} {self.h:g}" role="img" '
            f'aria-label="{escape(self.title)}"><title>{escape(self.title)}</title>'
            f"<style>{css}{BASE_CSS}</style>{DEFS}{''.join(self.parts)}</svg>"
        )


BASE_CSS = """
@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes sweep{from{transform:translateX(-420px)}to{transform:translateX(1400px)}}
.blink{animation:blink 1.1s steps(1) infinite}
.pulse{animation:pulse 2.4s ease-in-out infinite}
.spin{transform-box:fill-box;transform-origin:center;animation:spin 60s linear infinite}
.spin-r{transform-box:fill-box;transform-origin:center;animation:spin 90s linear infinite reverse}
.sweep{animation:sweep 7s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""

DEFS = f"""<defs>
<linearGradient id="panel" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="#22101a"/><stop offset="1" stop-color="#120709"/></linearGradient>
<radialGradient id="glow" cx="0.85" cy="0.1" r="0.9">
  <stop offset="0" stop-color="#ff2e6a" stop-opacity="0.22"/><stop offset="0.55" stop-color="#ff2e6a" stop-opacity="0.04"/>
  <stop offset="1" stop-color="#ff2e6a" stop-opacity="0"/></radialGradient>
<radialGradient id="hero" cx="0.78" cy="0.45" r="0.85">
  <stop offset="0" stop-color="#6e1432"/><stop offset="0.45" stop-color="#2f0b19"/><stop offset="1" stop-color="#12060b"/></radialGradient>
<linearGradient id="crimson" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="#ff5c8a"/><stop offset="1" stop-color="#b3164e"/></linearGradient>
<linearGradient id="crimsonText" x1="0" y1="0" x2="1" y2="0">
  <stop offset="0" stop-color="#ffd3df"/><stop offset="0.6" stop-color="#ff7aa2"/><stop offset="1" stop-color="#ff4d7e"/></linearGradient>
<linearGradient id="feature" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="#7a1636"/><stop offset="1" stop-color="#2a0a17"/></linearGradient>
<linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
  <stop offset="0" stop-color="{PINK}" stop-opacity="0.9"/><stop offset="0.35" stop-color="{PINK}" stop-opacity="0.25"/>
  <stop offset="1" stop-color="{PINK}" stop-opacity="0"/></linearGradient>
<linearGradient id="shine" x1="0" y1="0" x2="1" y2="0">
  <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.5" stop-color="#fff" stop-opacity="0.06"/>
  <stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
  <path d="M40 0H0V40" fill="none" stroke="#ff5c8a" stroke-opacity="0.05"/></pattern>
<filter id="grain"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch"/>
  <feColorMatrix values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.05 0"/></filter>
<filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter>
</defs>"""


# ───────────────────────────── primitives ─────────────────────────────
def frame(s: Svg, rx: float = 18) -> None:
    """Outer card background shared by every SVG."""
    s.add(f'<rect width="{s.w}" height="{s.h}" rx="{rx}" fill="{BG}"/>')
    s.add(f'<rect width="{s.w}" height="{s.h}" rx="{rx}" fill="url(#glow)"/>')
    s.add(f'<rect width="{s.w}" height="{s.h}" rx="{rx}" filter="url(#grain)" opacity="0.6"/>')
    s.add(f'<rect x="0.5" y="0.5" width="{s.w-1}" height="{s.h-1}" rx="{rx}" fill="none" stroke="{LINE_SOFT}"/>')


def panel(s: Svg, x, y, w, h, fill="url(#panel)", rx=12, ticks=True, stroke=LINE) -> None:
    s.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}"/>')
    s.add(f'<line x1="{x+rx}" y1="{y+0.5}" x2="{x+w-rx}" y2="{y+0.5}" stroke="#fff" stroke-opacity="0.07"/>')
    if ticks:
        L, o = 10, 6
        for cx, cy, dx, dy in ((x - o, y - o, 1, 1), (x + w + o, y - o, -1, 1),
                               (x - o, y + h + o, 1, -1), (x + w + o, y + h + o, -1, -1)):
            s.add(f'<path d="M{cx} {cy+dy*L}V{cy}H{cx+dx*L}" fill="none" stroke="{PINK}" stroke-opacity="0.55"/>')


def header(s: Svg, y: float, idx: str, label: str, lead: str, accent: str, meta: str = "") -> float:
    """Section header: '01  ABOUT' + serif headline with italic accent. Returns bottom y."""
    s.add(f'<rect x="40" y="{y+18}" width="8" height="8" fill="{PINK}"/>')
    s.text(58, y + 26, f"{idx}  {label}", face="mono-b", size=11, fill=PINK_SOFT, ls=2.2)
    x = s.runs(40, y + 74, [(lead, "serif", INK), (accent, "serif-i", PINK_SOFT)], size=44)
    if meta:
        s.text(W - 40, y + 70, meta, face="mono", size=10, fill=MUTED, anchor="end", ls=1.8)
    s.add(f'<rect x="40" y="{y+92}" width="{W-80}" height="1" fill="url(#rule)"/>')
    return y + 104


def chip(s: Svg, x, y, label: str, face="mono-m", size=10.5, fill=INK_2, bg="rgba(255,92,138,0.07)",
         stroke=LINE, pad=10, h=24) -> float:
    w = s.fonts.width(label, face, size, 0.6) + pad * 2
    s.add(f'<rect x="{x}" y="{y}" width="{w:.1f}" height="{h}" rx="5" fill="{bg}" stroke="{stroke}"/>')
    s.text(x + pad, y + h / 2 + size * 0.36, label, face=face, size=size, fill=fill, ls=0.6)
    return w


def para(s: Svg, x, y, text, face="sans", size=14, fill=INK_2, max_w=500, lh=21, max_lines=None) -> float:
    lines = s.fonts.wrap(text, face, size, max_w)
    if max_lines and len(lines) > max_lines:
        raise ValueError(f"text overflows {max_lines} lines at {max_w}px: {text[:60]}…")
    for i, ln in enumerate(lines):
        s.text(x, y + i * lh, ln, face=face, size=size, fill=fill)
    return y + (len(lines) - 1) * lh


def save(s: Svg, name: str) -> str:
    out = CARD_DIR / f"{name}.svg"
    out.write_text(s.render(), encoding="utf-8")
    log.info("wrote %-28s %6.1f KB", out.name, out.stat().st_size / 1024)
    return f"assets/cards/{name}.svg"


# ───────────────────────────── cards ─────────────────────────────
def card_hero(f: Fonts, p: dict, avatar_b64: str | None) -> str:
    s = Svg(f, W, 470, f"{p['name_first']} {p['name_last']} — {p['roles'].title()}")
    frame(s)
    px, py, pw, ph = 20, 20, 960, 430
    clip = s.uid("hc")
    s.add(f'<clipPath id="{clip}"><rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="14"/></clipPath>')
    s.add(f'<g clip-path="url(#{clip})">')
    s.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="url(#hero)"/>')
    s.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="url(#grid)"/>')
    s.add(f'<rect class="sweep" x="0" y="{py}" width="380" height="{ph}" fill="url(#shine)"/>')
    s.add("</g>")
    s.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="14" fill="none" stroke="{LINE}"/>')
    panel(s, px, py, pw, ph, fill="none", rx=14, stroke="none")

    # top bar
    s.text(W - 56, 56, "PROFILE / README.MD", face="mono", size=10, fill=MUTED, anchor="end", ls=1.8)
    cw = chip(s, 56, 42, p["prompt_chip"] + "  ", face="mono-m", size=11.5, fill=INK, bg="rgba(0,0,0,0.35)", h=28, pad=12)
    s.add(f'<rect class="blink" x="{56+cw-19:.1f}" y="49" width="7" height="14" fill="{PINK}"/>')

    # identity
    s.add(f'<circle class="pulse" cx="60" cy="118" r="4" fill="{PINK}"/>')
    s.text(74, 122, p["eyebrow"], face="mono-m", size=11.5, fill=PINK_SOFT, ls=2.6)
    s.text(52, 214, p["name_first"], face="serif", size=100, fill=INK)
    x = s.text(52, 300, p["name_last"], face="serif-i", size=100, fill="url(#crimsonText)")
    s.text(52 + x - 8, 300, ".", face="serif", size=100, fill=PINK)  # italic advance overhangs
    s.text(56, 346, p["roles"], face="mono-b", size=11.5, fill=INK, ls=1.8)
    s.text(56, 382, p["tagline"], face="serif-i", size=23, fill=INK_2)
    s.add(f'<rect x="56" y="402" width="40" height="1" fill="{PINK}"/>')
    s.text(106, 406, p["meta"], face="mono", size=10.5, fill=MUTED, ls=1.4)

    # portrait: arch frame with orbit rings
    cx, top, aw, ah = 806, 88, 206, 262
    r = aw / 2
    arch = f"M{cx-r} {top+ah}V{top+r}A{r} {r} 0 0 1 {cx+r} {top+r}V{top+ah}Z"
    s.add(f'<ellipse class="spin" cx="{cx}" cy="{top+ah/2}" rx="170" ry="170" fill="none" stroke="{PINK}" '
          f'stroke-opacity="0.22" stroke-dasharray="2 7"/>')
    s.add(f'<ellipse class="spin-r" cx="{cx}" cy="{top+ah/2}" rx="148" ry="148" fill="none" stroke="{PINK}" '
          f'stroke-opacity="0.12" stroke-dasharray="60 18 4 18"/>')
    s.add(f'<path d="{arch}" fill="#000" opacity="0.45" filter="url(#soft)" transform="translate(6 10)"/>')
    clip = s.uid("pc")
    s.add(f'<clipPath id="{clip}"><path d="{arch}"/></clipPath>')
    if avatar_b64:
        s.add(f'<image href="data:image/jpeg;base64,{avatar_b64}" x="{cx-r}" y="{top}" width="{aw}" height="{ah}" '
              f'preserveAspectRatio="xMidYMid slice" clip-path="url(#{clip})"/>')
        s.add(f'<path d="{arch}" fill="url(#crimson)" opacity="0.12"/>')
    else:
        s.add(f'<path d="{arch}" fill="url(#feature)"/>')
        s.add(f'<g clip-path="url(#{clip})"><rect x="{cx-r}" y="{top}" width="{aw}" height="{ah}" fill="url(#grid)"/></g>')
        initials = p["name_first"][0] + p["name_last"][0]
        s.text(cx, top + ah * 0.66, initials, face="serif-i", size=118, fill="url(#crimsonText)", anchor="middle")
    s.add(f'<path d="{arch}" fill="none" stroke="{PINK}" stroke-opacity="0.75"/>')
    s.add(f'<path d="M{cx-r-8} {top+ah+8}H{cx+r+8}" stroke="{PINK}" stroke-opacity="0.5"/>')
    s.text(cx - r, top + ah + 26, "FIG. 01", face="mono", size=9.5, fill=MUTED, ls=1.6)
    s.text(cx + r, top + ah + 26, "N. AURADKAR", face="mono", size=9.5, fill=MUTED, ls=1.6, anchor="end")
    # status pill
    lab = p["status"]
    tw = f.width(lab, "mono-b", 10, 1.4) + 34
    sx = cx - tw / 2
    s.add(f'<rect x="{sx:.1f}" y="{top+ah+40}" width="{tw:.1f}" height="24" rx="12" fill="rgba(255,92,138,0.12)" stroke="{LINE}"/>')
    s.add(f'<circle class="pulse" cx="{sx+14:.1f}" cy="{top+ah+52}" r="3.5" fill="#5dffa8"/>')
    s.text(sx + 24, top + ah + 56, lab, face="mono-b", size=10, fill=INK, ls=1.4)
    return save(s, "hero")


def card_pill(f: Fonts, label: str, value: str, name: str) -> str:
    lw = f.width(label, "mono-b", 10.5, 1.4) + 24
    vw = f.width(value.upper(), "mono-b", 10.5, 1.0) + 24
    s = Svg(f, lw + vw, 30, f"{label}: {value}")
    s.add(f'<rect width="{lw+vw}" height="30" rx="6" fill="#140810" stroke="{LINE}"/>')
    s.add(f'<path d="M{lw} 0H{lw+vw-6}A6 6 0 0 1 {lw+vw} 6V24A6 6 0 0 1 {lw+vw-6} 30H{lw}Z" fill="url(#crimson)"/>')
    s.text(12, 19.5, label, face="mono-b", size=10.5, fill=PINK_SOFT, ls=1.4)
    s.text(lw + 12, 19.5, value.upper(), face="mono-b", size=10.5, fill="#fff", ls=1.0)
    return save(s, name)


def card_about(f: Fonts, p: dict, idx: str) -> str:
    a = p["about"]
    lh = 23
    n = len(a["lines"]) + 1
    body_h = 46 + n * lh + 18
    s = Svg(f, W, 104 + 16 + body_h + 24, f"About {p['name_first']}")
    frame(s)
    y0 = header(s, 0, idx, "ABOUT", "Hello, I'm ", f"{p['name_first']}.", meta="WHOAMI · SESSION 01")
    y = y0 + 16
    # terminal
    tw = 610
    panel(s, 20, y, tw, body_h)
    s.add(f'<path d="M20 {y+34}H{20+tw}" stroke="{LINE_SOFT}"/>')
    for i, c in enumerate(("#ff5c8a", "#a3365a", "#4f1d31")):
        s.add(f'<circle cx="{40+i*16}" cy="{y+17}" r="5" fill="{c}"/>')
    s.text(20 + tw / 2, y + 21, a["terminal_title"], face="mono", size=11, fill=MUTED, anchor="middle")
    ty = y + 34 + 30
    for i, (sig, txt) in enumerate(a["lines"]):
        if sig == "$":
            s.text(44, ty + i * lh, "$", face="mono-b", size=13, fill=PINK)
            s.text(62, ty + i * lh, txt, face="mono-m", size=13, fill=INK)
        else:
            s.text(62, ty + i * lh, txt, face="mono", size=12.5, fill=INK_2)
    last = ty + len(a["lines"]) * lh
    s.text(44, last, "$", face="mono-b", size=13, fill=PINK)
    s.add(f'<rect class="blink" x="62" y="{last-12}" width="8" height="15" fill="{PINK}"/>')
    # sidebar
    sx, sw = 20 + tw + 20, W - 40 - tw - 20
    panel(s, sx, y, sw, body_h)
    s.text(sx + 24, y + 30, "CURRENTLY", face="mono-b", size=10, fill=PINK_SOFT, ls=2.2)
    s.add(f'<path d="M{sx+24} {y+44}H{sx+sw-24}" stroke="{LINE_SOFT}"/>')
    yy = y + 72
    for k, v in a["sidebar"]:
        s.text(sx + 24, yy, k, face="mono-m", size=9.5, fill=MUTED, ls=2)
        end = para(s, sx + 24, yy + 22, v, face="sans-m", size=15, fill=INK, max_w=sw - 48, lh=20, max_lines=2)
        yy = end + 34
    if yy - 34 > y + body_h:
        raise ValueError("about sidebar overflows; shorten sidebar values")
    return save(s, "about")


def card_record(f: Fonts, p: dict, stats: dict, idx: str) -> str:
    s = Svg(f, W, 104 + 16 + 196 + 24, "On the record")
    frame(s)
    y = header(s, 0, idx, "ON THE RECORD", "Numbers, ", "not adjectives.", meta="SOURCED FROM REPO EVALS + GITHUB") + 16
    tiles = p["record"]
    gap = 16
    tw = (W - 40 - gap * (len(tiles) - 1)) / len(tiles)
    for i, t in enumerate(tiles):
        x = 20 + i * (tw + gap)
        feature = i == 0
        panel(s, x, y, tw, 196, fill="url(#feature)" if feature else "url(#panel)", ticks=feature)
        s.text(x + 22, y + 32, f"0{i+1}", face="mono-m", size=10, fill=PINK_SOFT if feature else MUTED, ls=2)
        val = t["value"].replace("{contributions}", f"{stats['total']:,}")
        vx = s.text(x + 20, y + 104, val, face="serif", size=66, fill=INK)
        if t["unit"]:
            s.text(x + 26 + vx, y + 104, t["unit"], face="serif-i", size=28, fill=PINK_SOFT)
        para(s, x + 22, y + 138, t["label"], face="sans", size=13, fill=INK_2, max_w=tw - 44, lh=18, max_lines=3)
    return save(s, "record")


def card_section(f: Fonts, idx, label, lead, accent, meta, name) -> str:
    s = Svg(f, W, 112, f"{lead}{accent}")
    frame(s)
    header(s, 0, idx, label, lead, accent, meta)
    return save(s, name)


def card_project(f: Fonts, pr: dict, n: int) -> str:
    H = 420
    s = Svg(f, W, H, f"{pr['title']} — {pr['subtitle']}")
    frame(s)
    panel(s, 20, 20, W - 40, H - 40)
    # big ghost index
    s.text(612, 150, f"0{n}", face="serif-i", size=170, fill=PINK, opacity=0.06, anchor="end")
    s.text(56, 66, pr["kicker"], face="mono-b", size=10.5, fill=PINK_SOFT, ls=2)
    s.text(54, 122, pr["title"], face="serif", size=52, fill=INK)
    s.text(56, 156, pr["subtitle"], face="serif-i", size=21, fill=PINK_SOFT)
    end = para(s, 56, 194, pr["desc"], face="sans", size=14.5, fill=INK_2, max_w=560, lh=22, max_lines=4)
    # metrics
    my = max(end + 58, 300)
    mw = 560 / len(pr["metrics"])
    for i, (v, lab) in enumerate(pr["metrics"]):
        mx = 56 + i * mw
        if i:
            s.add(f'<path d="M{mx-14} {my-30}V{my+14}" stroke="{LINE_SOFT}"/>')
        s.text(mx, my, v, face="serif", size=36, fill=INK)
        s.text(mx, my + 20, lab.upper(), face="mono", size=9, fill=MUTED, ls=1.2)
    # stack chips
    cy = my + 40
    s.text(56, cy + 16, "BUILT", face="mono-b", size=9.5, fill=MUTED, ls=2)
    cx = 104
    for t in pr["stack"]:
        w = f.width(t, "mono-m", 10, 0.6) + 18
        if cx + w > 640:
            break
        cx += chip(s, cx, cy, t, size=10, pad=9, h=22) + 6
    # pipeline column
    fx, fw = 668, 272
    s.add(f'<rect x="{fx-16}" y="44" width="{fw+32}" height="{H-88}" rx="10" fill="rgba(0,0,0,0.25)" stroke="{LINE_SOFT}"/>')
    s.text(fx, 72, "PIPELINE", face="mono-b", size=9.5, fill=MUTED, ls=2.2)
    s.text(fx + fw, 72, f"{len(pr['flow'])} STAGES", face="mono", size=9.5, fill=MUTED, ls=1.6, anchor="end")
    nodes = pr["flow"]
    top, bot = 92, H - 66
    nh = 30
    step = (bot - top - nh) / max(len(nodes) - 1, 1)
    lx = fx + 16
    s.add(f'<path d="M{lx} {top+nh/2}V{top+nh/2+step*(len(nodes)-1)}" stroke="{PINK}" stroke-opacity="0.35" stroke-dasharray="3 4"/>')
    for i, node in enumerate(nodes):
        ny = top + i * step
        last = i == len(nodes) - 1
        s.add(f'<rect x="{fx}" y="{ny:.1f}" width="{fw}" height="{nh}" rx="7" '
              f'fill="{"url(#crimson)" if last else "#1d0c14"}" stroke="{LINE}"/>')
        s.add(f'<circle cx="{lx}" cy="{ny+nh/2:.1f}" r="3.5" fill="{"#fff" if last else PINK}"/>')
        s.text(fx + 32, ny + 19.5, f"{i+1:02d}", face="mono", size=10, fill="#ffd9e4" if last else MUTED)
        s.text(fx + 58, ny + 19.5, node, face="mono-m", size=12, fill="#fff" if last else INK)
    # pulse is positioned at stage 01 statically; motion is relative, so a paused animation still sits on the rail
    s.add(f'<circle cx="{lx}" cy="{top+nh/2}" r="4" fill="#fff"><animateMotion dur="3.6s" '
          f'repeatCount="indefinite" path="M0 0V{step*(len(nodes)-1):.1f}"/></circle>')
    s.text(fx + fw, H - 38, "VIEW REPOSITORY  ↗", face="mono-b", size=9.5, fill=PINK_SOFT, ls=1.8, anchor="end")
    return save(s, f"project-{pr['slug']}")


def card_publication(f: Fonts, pub: dict, n: int) -> str:
    """Paper card: metadata + highlighted author list + summary on the left, method schematic on the right.
    Height is derived from content, so long titles or summaries grow the card instead of overflowing."""
    s = Svg(f, W, 0, f"{pub['title']} — arXiv:{pub['arxiv']}")
    LX, LW = 56, 570  # no ghost index here: a two-line serif title sits where it would go
    s.text(LX, 66, pub["kicker"], face="mono-b", size=10.5, fill=PINK_SOFT, ls=1.8)
    y = 112
    for i, ln in enumerate(f.wrap(pub["title"], "serif", 34, LW)):
        s.text(LX - 2, y + i * 38, ln, face="serif", size=34, fill=INK)
        last_title = y + i * 38
    # authors: one baseline, self highlighted; fail loudly rather than overflow into the schematic
    y = last_title + 36
    runs = []
    for i, a in enumerate(pub["authors"]):
        sep = ", " if i < len(pub["authors"]) - 1 else ""
        runs.append((a, "sans-sb", PINK_SOFT) if a == pub["self"] else (a, "sans", INK_2))
        if sep:
            runs.append((sep, "sans", INK_2))
    if sum(f.width(t, face, 13) for t, face, _ in runs) > LW:
        raise ValueError("author list too wide for one line; abbreviate first names in profile.json")
    s.runs(LX, y, runs, size=13)
    y = para(s, LX, y + 34, pub["summary"], face="sans", size=14.5, fill=INK_2, max_w=LW, lh=22, max_lines=6)
    cx, cy = LX, y + 26
    for t in pub["tags"]:
        w = f.width(t, "mono-m", 10, 0.6) + 18
        if cx + w > LX + LW:
            break
        cx += chip(s, cx, cy, t, size=10, pad=9, h=22) + 6
    H = max(cy + 22 + 70, 420)
    s.text(LX, H - 38, "READ ON ARXIV  ↗", face="mono-b", size=9.5, fill=PINK_SOFT, ls=1.8)

    # method schematic (qualitative; no data plotted)
    fx, fw = 668, 272
    s.add(f'<rect x="{fx-16}" y="44" width="{fw+32}" height="{H-88}" rx="10" fill="rgba(0,0,0,0.25)" stroke="{LINE_SOFT}"/>')
    s.text(fx, 72, "MEMTRIM", face="mono-b", size=9.5, fill=PINK_SOFT, ls=2.2)
    s.text(fx + fw, 72, "SCHEMATIC", face="mono", size=9.5, fill=MUTED, ls=1.6, anchor="end")
    nodes, hi = pub["flow"], set(pub.get("flow_highlight", []))
    top, bot, nh = 92, H - 108, 30
    step = (bot - top - nh) / max(len(nodes) - 1, 1)
    lx = fx + 16
    s.add(f'<path d="M{lx} {top+nh/2}V{top+nh/2+step*(len(nodes)-1):.1f}" stroke="{PINK}" stroke-opacity="0.35" stroke-dasharray="3 4"/>')
    for i, node in enumerate(nodes):
        ny = top + i * step
        on = i in hi
        s.add(f'<rect x="{fx}" y="{ny:.1f}" width="{fw}" height="{nh}" rx="7" '
              f'fill="{"url(#crimson)" if on else "#1d0c14"}" stroke="{LINE}"/>')
        s.add(f'<circle cx="{lx}" cy="{ny+nh/2:.1f}" r="3.5" fill="{"#fff" if on else PINK}"/>')
        s.text(fx + 32, ny + 19.5, f"{i+1:02d}", face="mono", size=10, fill="#ffd9e4" if on else MUTED)
        s.text(fx + 58, ny + 19.5, node, face="mono-m", size=12, fill="#fff" if on else INK)
    s.add(f'<circle cx="{lx}" cy="{top+nh/2}" r="4" fill="#fff"><animateMotion dur="4s" '
          f'repeatCount="indefinite" path="M0 0V{step*(len(nodes)-1):.1f}"/></circle>')
    s.add(f'<path d="M{fx} {H-90}H{fx+fw}" stroke="{LINE_SOFT}"/>')
    s.add(f'<rect x="{fx}" y="{H-79}" width="7" height="7" fill="url(#crimson)"/>')
    s.text(fx + 14, H - 72, "MEMTRIM STAGE", face="mono", size=9, fill=MUTED, ls=1.4)
    s.text(fx + fw, H - 72, "NO RETRAINING", face="mono", size=9, fill=MUTED, ls=1.4, anchor="end")
    s.text(fx, H - 56, pub["flow_caption"], face="mono", size=9, fill=MUTED, ls=1.4)

    # frame + panel go underneath the content, now that the height is known
    body, s.parts, s.h = s.parts, [], H
    frame(s)
    panel(s, 20, 20, W - 40, H - 40)
    s.parts += body
    return save(s, f"paper-{pub['arxiv'].replace('.', '-')}")


def bibtex(pub: dict) -> str:
    authors = " and ".join(f"{a.split()[-1]}, {' '.join(a.split()[:-1])}" for a in pub["authors"])
    return (f"@misc{{{pub['bibkey']},\n  title         = {{{pub['title']}}},\n  author        = {{{authors}}},\n"
            f"  year          = {{{pub['year']}}},\n  eprint        = {{{pub['arxiv']}}},\n  archivePrefix = {{arXiv}},\n"
            f"  primaryClass  = {{{pub['primary_class']}}},\n  url           = {{https://arxiv.org/abs/{pub['arxiv']}}}\n}}")


def card_bench(f: Fonts, b: dict, i: int) -> str:
    w, h = 324, 206
    s = Svg(f, w, h, f"{b['title']} — {b['desc']}")
    frame(s, rx=14)
    s.add(f'<rect x="0" y="0" width="{w}" height="3" rx="1.5" fill="url(#rule)"/>')
    s.text(22, 34, f"{i:02d} · {b['kicker']}", face="mono-b", size=9.5, fill=PINK_SOFT, ls=1.8)
    s.text(21, 74, b["title"], face="serif", size=31, fill=INK)
    para(s, 22, 104, b["desc"], face="sans", size=13, fill=INK_2, max_w=w - 44, lh=19, max_lines=3)
    s.add(f'<path d="M22 {h-40}H{w-22}" stroke="{LINE_SOFT}"/>')
    s.text(22, h - 18, b["lang"].upper(), face="mono", size=9.5, fill=MUTED, ls=1.4)
    s.text(w - 22, h - 18, "↗", face="mono-b", size=12, fill=PINK, anchor="end")
    return save(s, f"bench-{i:02d}")


def card_toolkit(f: Fonts, p: dict, idx: str) -> str:
    groups = p["toolkit"]
    cols, gap, bh = 3, 16, 168
    rows = -(-len(groups) // cols)
    s = Svg(f, W, 104 + 16 + rows * bh + (rows - 1) * gap + 24, "The tools I reach for")
    frame(s)
    y0 = header(s, 0, idx, "TOOLKIT", "The tools I ", "reach for.", meta="DAILY DRIVERS, NOT KEYWORDS") + 16
    bw = (W - 40 - gap * (cols - 1)) / cols
    for i, g in enumerate(groups):
        x = 20 + (i % cols) * (bw + gap)
        y = y0 + (i // cols) * (bh + gap)
        panel(s, x, y, bw, bh, ticks=False)
        s.text(x + 20, y + 30, f"{i+1:02d}  {g['label']}", face="mono-b", size=9.5, fill=PINK_SOFT, ls=1.8)
        lines = f.wrap(" · ".join(g["items"]), "serif", 25, bw - 40)
        if len(lines) > 3:
            raise ValueError(f"toolkit group {g['label']} wraps past 3 lines")
        for j, ln in enumerate(lines):
            s.text(x + 20, y + 68 + j * 29, ln, face="serif", size=25, fill=INK)
        s.add(f'<path d="M{x+20} {y+bh-36}H{x+bw-20}" stroke="{LINE_SOFT}"/>')
        s.text(x + 20, y + bh - 16, g["note"].upper(), face="mono", size=9, fill=MUTED, ls=1.3)
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
    s = Svg(f, W, 104 + 16 + 268 + 24, f"{st['total']} GitHub contributions in the last year")
    frame(s)
    y = header(s, 0, idx, "COMMIT LOG", "A year of ", "building.", meta="REAL DATA · REFRESHED DAILY") + 16
    panel(s, 20, y, W - 40, 268)
    s.text(48, y + 34, "COMMIT LOG · REAL GITHUB ACTIVITY", face="mono-b", size=9.5, fill=PINK_SOFT, ls=2)
    s.runs(48, y + 66, [("Shipped in public, ", "serif", INK), ("one square at a time.", "serif-i", PINK_SOFT)], size=25)
    items = [(f"{st['total']:,}", "CONTRIBUTIONS"), (str(st["active"]), "ACTIVE DAYS"),
             (str(st["longest"]), "LONGEST STREAK"), (str(st["best"]), "BEST DAY")]
    x = W - 48
    for v, lab in reversed(items):
        lw = max(f.width(lab, "mono", 8.5, 1.2), f.width(v, "serif", 32))
        s.text(x, y + 44, v, face="serif", size=32, fill=INK, anchor="end")
        s.text(x, y + 62, lab, face="mono", size=8.5, fill=MUTED, anchor="end", ls=1.2)
        x -= lw + 30
    # grid
    first = dt.date.fromisoformat(days[0]["date"])
    origin = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # back to Sunday
    cell, g = 13, 3.2
    gx, gy = 92, y + 116
    months_seen = set()
    for d in days:
        date = dt.date.fromisoformat(d["date"])
        col = (date - origin).days // 7
        row = (date.weekday() + 1) % 7
        cx, cy = gx + col * (cell + g), gy + row * (cell + g)
        lvl = max(0, min(4, d["level"]))
        title = f"{d['count']} on {date:%b %d}"
        s.add(f'<rect x="{cx:.1f}" y="{cy:.1f}" width="{cell}" '
              f'height="{cell}" rx="2.5" fill="{HEAT[lvl]}"'
              f'{" stroke=%s" % chr(34) + LINE_SOFT + chr(34) if lvl == 0 else ""}><title>{title}</title></rect>')
        if lvl == 4:
            s.add(f'<rect x="{cx:.1f}" y="{cy:.1f}" width="{cell}" height="{cell}" rx="2.5" fill="{HEAT[4]}" '
                  f'opacity="0.55" filter="url(#soft)"/>')
        key = (date.year, date.month)
        if date.day <= 7 and row == 0 and key not in months_seen:
            months_seen.add(key)
            s.text(cx, gy - 10, f"{date:%b}", face="mono", size=9, fill=MUTED, ls=0.8)
    # decorative scan across the grid; cells are static so a paused animation hides nothing
    gw = 53 * (cell + g)
    clip = s.uid("gc")
    s.add(f'<clipPath id="{clip}"><rect x="{gx-4}" y="{gy-4}" width="{gw+8}" height="{7*(cell+g)+8}"/></clipPath>')
    s.add(f'<g clip-path="url(#{clip})"><rect class="sweep" x="-200" y="{gy-4}" width="160" '
          f'height="{7*(cell+g)+8}" fill="url(#shine)"/></g>')
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        s.text(48, gy + row * (cell + g) + 10, lab, face="mono", size=9, fill=MUTED)
    fy = gy + 7 * (cell + g) + 26
    if st["longest"]:
        a = dt.date.fromisoformat(st["lstart"]); b = dt.date.fromisoformat(st["lend"])
        s.text(48, fy, f"LONGEST STREAK · {st['longest']} DAYS · {a:%d %b %Y} – {b:%d %b %Y}".upper(),
               face="mono", size=9, fill=MUTED, ls=1.4)
    lx = W - 48 - 5 * (cell + g) - 34
    s.text(lx - 8, fy, "LESS", face="mono", size=9, fill=MUTED, anchor="end", ls=1.2)
    for i, c in enumerate(HEAT):
        s.add(f'<rect x="{lx + i*(cell+g):.1f}" y="{fy-10}" width="{cell}" height="{cell}" rx="2.5" fill="{c}"/>')
    s.text(W - 48, fy, "MORE", face="mono", size=9, fill=MUTED, anchor="end", ls=1.2)
    return save(s, "heatmap")


def card_progress_row(f: Fonts, r: dict, i: int) -> str:
    h = 78
    s = Svg(f, W, h, f"{r['title']} — {r['desc']}")
    frame(s, rx=14)
    s.text(36, 47, f"{i:02d}", face="serif-i", size=30, fill=PINK, opacity=0.55)
    tag = r["tag"]
    s.add(f'<path d="M88 18V{h-18}" stroke="{LINE_SOFT}"/>')
    s.text(108, 34, r["title"], face="serif", size=26, fill=INK)
    s.text(108, 58, r["desc"], face="sans", size=13, fill=INK_2)
    tw = f.width(tag, "mono-b", 9.5, 1.6) + 20
    tx = W - 150 - tw
    s.add(f'<rect x="{tx:.1f}" y="27" width="{tw:.1f}" height="22" rx="4" fill="rgba(255,92,138,0.08)" stroke="{LINE}"/>')
    s.text(tx + 10, 41.5, tag, face="mono-b", size=9.5, fill=PINK_SOFT, ls=1.6)
    s.add(f'<circle class="pulse" cx="{W-124}" cy="38" r="3.5" fill="{PINK}"/>')
    s.text(W - 114, 42, "BUILDING", face="mono-m", size=9.5, fill=MUTED, ls=1.6)
    s.text(W - 30, 43, "↗", face="mono-b", size=13, fill=PINK, anchor="end")
    return save(s, f"wip-{i:02d}")


def card_connect(f: Fonts, p: dict, built: str, idx: str) -> str:
    s = Svg(f, W, 104 + 16 + 230 + 24, "Let's build something that ships")
    frame(s)
    y = header(s, 0, idx, "CONNECT", "Let's build something ", "that ships.", meta=p["status"]) + 16
    clip = s.uid("fc")
    s.add(f'<clipPath id="{clip}"><rect x="20" y="{y}" width="{W-40}" height="230" rx="12"/></clipPath>')
    s.add(f'<g clip-path="url(#{clip})"><rect x="20" y="{y}" width="{W-40}" height="230" fill="url(#hero)"/>'
          f'<rect x="20" y="{y}" width="{W-40}" height="230" fill="url(#grid)"/></g>')
    panel(s, 20, y, W - 40, 230, fill="none")
    x = s.text(W / 2 - 6, y + 112, p["footer_signature"], face="serif-i", size=88, fill="url(#crimsonText)", anchor="middle")
    s.text(W / 2 + x / 2 - 14, y + 112, ".", face="serif", size=88, fill=PINK)
    s.text(W / 2, y + 150, p["footer_line"], face="sans-m", size=15, fill=INK, anchor="middle")
    s.text(W / 2, y + 180, p["footer_meta"], face="mono", size=10, fill=MUTED, anchor="middle", ls=2.4)
    s.text(W / 2, y + 210, f"LAST BUILD · {built}", face="mono", size=9, fill=PINK_SOFT, anchor="middle", ls=2, opacity=0.7)
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
def img(src: str, alt: str, width: str = "100%") -> str:
    return f'<img src="{src}" width="{width}" alt="{escape(alt, {chr(34): "&quot;"})}">'


def build(live: bool) -> None:
    p = json.loads((ROOT / "profile.json").read_text())
    if live:
        refresh_live(p["username"])
    days = json.loads((DATA_DIR / "contributions.json").read_text())
    today = dt.date.today()
    st = heat_stats(days, today)
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    for old in CARD_DIR.glob("*.svg"):
        old.unlink()
    f = Fonts()
    gh = f"https://github.com/{p['username']}"

    pills = []
    for i, l in enumerate(x for x in p["links"] if x["url"] and x["value"]):
        src = card_pill(f, l["label"], l["value"], f"pill-{l['label'].lower()}")
        pills.append(f'<a href="{l["url"]}">{img(src, l["label"] + ": " + l["value"], "auto")}</a>')
    pills_html = "&nbsp;".join(pills).replace('width="auto" ', 'height="30" ')

    out = ["<!-- Generated by scripts/build.py — edit profile.json, not this file. -->", ""]
    P = lambda *xs: out.extend(['<p align="center">', *xs, "</p>", ""])
    counter = iter(range(1, 100))
    nx = lambda: f"{next(counter):02d}"  # section numbers follow render order
    P(img(card_hero(f, p, load_avatar(p)), f"{p['name_first']} {p['name_last']}. {p['roles']}. {p['tagline']}"))
    if pills:
        P(pills_html)
    P(img(card_about(f, p, nx()), "About: " + " ".join(t for s_, t in p["about"]["lines"] if s_ == ">")))
    P(img(card_record(f, p, st, nx()), "On the record: " + "; ".join(
        f"{t['value'].replace('{contributions}', str(st['total']))} {t['unit']} {t['label']}" for t in p["record"])))
    pubs = p.get("publications", [])
    if pubs:
        P(img(card_section(f, nx(), "PUBLICATIONS", "Research, ", "on arXiv.",
                           f"{len(pubs)} PREPRINT{'S' if len(pubs) > 1 else ''} · AGENTS · INFERENCE", "h-papers"), "Publications"))
        for n, pub in enumerate(pubs, 1):
            P(f'<a href="https://arxiv.org/abs/{pub["arxiv"]}">'
              + img(card_publication(f, pub, n), f"{pub['title']}. {', '.join(pub['authors'])}. arXiv:{pub['arxiv']}. {pub['summary']}")
              + "</a>")
        out.extend(["<details>", "<summary><b>Cite</b> (BibTeX)</summary>", "", "```bibtex",
                    *("\n\n".join(bibtex(x) for x in pubs)).split("\n"), "```", "", "</details>", ""])
    P(img(card_section(f, nx(), "SELECTED WORK", "Systems, ", "not notebooks.", "FLAGSHIP BUILDS", "h-work"), "Selected work"))
    for n, pr in enumerate(p["projects"], 1):
        P(f'<a href="{gh}/{pr["repo"]}">' + img(card_project(f, pr, n), f"{pr['title']}: {pr['desc']}") + "</a>")
    P(img(card_section(f, nx(), "RESEARCH BENCH", "Papers, ", "re-derived in code.", "POST-TRAINING · INFERENCE · AGENTS", "h-bench"), "Research bench"))
    for row in range(0, len(p["bench"]), 3):
        cells = [f'<a href="{gh}/{b["repo"]}">' + img(card_bench(f, b, row + j + 1), f"{b['title']}: {b['desc']}", "32.6%") + "</a>"
                 for j, b in enumerate(p["bench"][row:row + 3])]
        P("".join(cells))
    P(img(card_toolkit(f, p, nx()), "Toolkit: " + "; ".join(f"{g['label']}: {', '.join(g['items'])}" for g in p["toolkit"])))
    P(img(card_heatmap(f, days, st, nx()), f"{st['total']} contributions in the last year, {st['active']} active days, longest streak {st['longest']} days"))
    P(img(card_section(f, nx(), "IN PROGRESS", "What's ", "compiling now.", "ACTIVE REPOSITORIES", "h-wip"), "In progress"))
    for i, r in enumerate(p["in_progress"], 1):
        P(f'<a href="{gh}/{r["repo"]}">' + img(card_progress_row(f, r, i), f"{r['title']}: {r['desc']}") + "</a>")
    P(img(card_connect(f, p, today.isoformat(), nx()), "Let's build something that ships."))
    if pills:
        P(pills_html)
    (ROOT / "README.md").write_text("\n".join(out), encoding="utf-8")
    total = sum(x.stat().st_size for x in CARD_DIR.glob("*.svg")) / 1024
    log.info("README.md written · %d cards · %.0f KB total", len(list(CARD_DIR.glob('*.svg'))), total)


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
