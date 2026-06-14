"""Generate the desktop-shortcut icon (dashboard.ico) in the app's dark/sky theme.

Re-run after a palette change:  python local/assets/make_icon.py
Motif: a dark rounded tile (the app surface) with a stacked "triage list" — the
top row highlighted in the sky accent with a check (a seen/triaged job), the
rows below muted (still-unseen). Rendered 8x then downsampled for clean edges,
and packed into a multi-resolution .ico (16-256 px).
"""
from pathlib import Path

from PIL import Image, ImageDraw

# Palette mirrors local/ui.py.
SURFACE = (24, 29, 39)      # #181d27 tile
BORDER = (43, 51, 64)       # #2b3340
ACCENT = (56, 189, 248)     # #38bdf8 sky (selected/triaged row)
ACCENT_INK = (6, 34, 47)    # #06222f check on the accent row
ROW_MUTED = (58, 67, 82)    # unseen rows

S = 1024                    # supersample canvas
R = round(S * 0.225)        # tile corner radius


def _rr(d, box, radius, **kw):
    d.rounded_rectangle(box, radius=radius, **kw)


def build() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bw = round(S * 0.016)
    _rr(d, (bw, bw, S - bw, S - bw), R, fill=SURFACE, outline=BORDER, width=bw)

    # Three list rows. Top row = accent (triaged) with a check; rest muted.
    left, right = round(S * 0.24), round(S * 0.78)
    h = round(S * 0.115)
    rad = round(h * 0.34)
    ys = [round(S * 0.29), round(S * 0.50), round(S * 0.71)]

    _rr(d, (left, ys[0], right, ys[0] + h), rad, fill=ACCENT)
    cx = left + round(h * 0.55)
    cy = ys[0] + h // 2
    u = h * 0.20
    d.line([(cx - u, cy), (cx - u * 0.1, cy + u), (cx + u * 1.1, cy - u * 0.9)],
           fill=ACCENT_INK, width=round(h * 0.13), joint="curve")

    _rr(d, (left, ys[1], round(S * 0.66), ys[1] + h), rad, fill=ROW_MUTED)
    _rr(d, (left, ys[2], round(S * 0.71), ys[2] + h), rad, fill=ROW_MUTED)

    return img.resize((256, 256), Image.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parent / "dashboard.ico"
    icon = build()
    icon.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
