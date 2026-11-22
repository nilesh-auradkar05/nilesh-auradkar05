# Profile README — setup

This repo **must be named `nilesh-auradkar05`** (same as the username) for GitHub to show `README.md` on the profile.

```
profile.json                  ← all content lives here; edit this
scripts/build.py              ← renders assets/cards/*.svg + README.md
data/contributions.json       ← contribution calendar (refreshed by CI)
data/avatar.jpg               ← portrait (downloaded by CI; delete + set "portrait": "monogram" to use initials)
.github/workflows/refresh.yml ← daily rebuild at 06:17 UTC, and on any push touching content
```

Local build: `pip install "fonttools[woff]" pillow && python scripts/build.py` (add `--live` with `GITHUB_TOKEN` set to pull fresh data).

Why SVG cards: GitHub strips CSS from Markdown and renders images in a sandbox that blocks external fonts and images,
so each card embeds a woff2 subset of only the glyphs it uses (Instrument Serif, JetBrains Mono, Inter; all SIL OFL).
Every card is rendered twice (assets/cards/light, assets/cards/dark) and served through <picture> with
prefers-color-scheme, so the README follows the visitor's GitHub theme. Colors live in the LIGHT / DARK
Theme objects at the top of scripts/build.py (Primer-aligned neutrals + one blue accent); change them there only.

Private-repo contributions: add a classic PAT with `read:user` as repo secret `GH_STATS_TOKEN`.
Links: fill `links[].value` / `links[].url` in `profile.json`; empty entries are skipped.

Publications: add an entry to `publications` in `profile.json` (title, authors, `self`, arXiv id, summary, schematic `flow`).
The section, its card, and a collapsible BibTeX block are generated; section numbers renumber automatically.
