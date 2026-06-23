"""Render a real chameleon per-edit injection block to a clean terminal-style SVG."""

from html import escape

# (text, color) per line. None color = default light gray.
FG = "#c9d1d9"
DIM = "#8b949e"
GREEN = "#3fb950"
AMBER = "#d29922"
RED = "#f85149"
BLUE = "#79c0ff"

LINES = [
    ("# you start editing  src/services/payment_service.ts", DIM),
    ("", None),
    ("🦎 chameleon  archetype=service · confidence=high · match_quality=exact", GREEN),
    ("", None),
    ("Canonical witness (how this archetype is written here):", DIM),
    ('  import { logger } from "@/lib/logger";', BLUE),
    ('  import { fmt } from "@/lib/date";', BLUE),
    ("  export class Svc { run() { logger.info(fmt(new Date())); } }", FG),
    ("", None),
    ("⚠  Known off-pattern in this repo. Do NOT write it this way:", AMBER),
    ('     import winston from "winston";', RED),
    ("✓  Use @/lib/logger instead of winston. The witness above is the", GREEN),
    ("   conforming form.", GREEN),
]

PAD_X = 28
TOP = 70
LINE_H = 26
FONT = 15
WIDTH = 880
HEIGHT = TOP + LINE_H * len(LINES) + 24
BG = "#0d1117"
BAR = "#161b22"
BORDER = "#30363d"

parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
    f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">',
    f'<rect x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="12" fill="{BG}" stroke="{BORDER}"/>',
    f'<rect x="1" y="1" width="{WIDTH - 2}" height="40" rx="12" fill="{BAR}"/>',
    f'<rect x="1" y="28" width="{WIDTH - 2}" height="14" fill="{BAR}"/>',
    '<circle cx="24" cy="21" r="6" fill="#ff5f56"/>',
    '<circle cx="44" cy="21" r="6" fill="#ffbd2e"/>',
    '<circle cx="64" cy="21" r="6" fill="#27c93f"/>',
    f'<text x="{WIDTH // 2}" y="26" fill="{DIM}" font-size="13" text-anchor="middle">'
    "chameleon · PreToolUse (Edit / Write)</text>",
]
y = TOP
for text, color in LINES:
    if text:
        fill = color or FG
        parts.append(
            f'<text x="{PAD_X}" y="{y}" fill="{fill}" font-size="{FONT}" '
            f'xml:space="preserve">{escape(text)}</text>'
        )
    y += LINE_H
parts.append("</svg>")
open("/Users/crisn/Documents/Projects/chameleon/assets/chameleon-injection.svg", "w").write(
    "\n".join(parts)
)
print("wrote assets/chameleon-injection.svg", WIDTH, "x", HEIGHT)
