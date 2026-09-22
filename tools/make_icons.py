"""Draw the app's icon set, so it is one family rather than one per source.

Run from the repo root, with Pillow installed:

    python tools/make_icons.py

Every icon is drawn once, in logical units on a 24x24 grid at 8x supersample,
into a single-channel mask. The mask is then tinted per tone and resampled
down - so an icon's *shape* is defined in exactly one place and its four
colours can never drift apart.

Why tones, rather than recolouring at runtime: Tk's PhotoImage has no cheap
tint, and ``ttk`` takes a state-keyed image list directly (see
``theme.set_button_icon``). Pre-rendering ``muted`` is what lets a disabled
button grey its icon along with its label instead of leaving a saturated
glyph next to dead text.

Pillow is a build-time dependency only - backupov2.spec excludes it, and the
app reads nothing but the .png files this writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backupov2" / "ui" / "assets" / "icons"

GRID = 24        # logical units per side
SUPER = 8        # supersample factor; the mask is drawn at GRID * SUPER
STROKE = 2.15    # the one stroke weight, in logical units
SIZES = (16, 24)

# Every icon is drawn to a comfortable 24-unit grid and then blown up about
# the centre, because 16px is the size that actually ships on a button and at
# that size the polite margin a 24-grid invites is most of the glyph.
SCALE = 1.12

# Kept in step with backupov2/ui/theme.py by hand - importing the theme would
# drag tkinter into a build script that has no display.
TONES = {
    "": "#0636f0",          # BRAND, on a white button
    "muted": "#9aa3b2",     # DISABLED_FG, on a disabled button
    "invert": "#ffffff",    # on a filled accent button
    "danger": "#a4161a",    # DANGER, on a destructive button
    "amber": "#7a5800",     # AMBER_DEEP, on the "collecting discs" banner
}

BORDER_STRONG = "#c2c9d8"
SURFACE = "#ffffff"
BRAND = "#0636f0"
DISABLED_FG = "#9aa3b2"


# -- drawing helpers, all in logical units --------------------------------


def _p(value: float) -> float:
    """A coordinate: scaled about the grid centre, then supersampled."""
    return ((value - GRID / 2) * SCALE + GRID / 2) * SUPER


def _d(value: float) -> float:
    """A length - a radius or a stroke width. Scaled, but not re-centred."""
    return value * SCALE * SUPER


def stroke(d: ImageDraw.ImageDraw, points, width: float = STROKE, closed: bool = False) -> None:
    """A polyline with round caps and joins.

    Pillow rounds joins with ``joint="curve"`` but leaves the two ends square,
    which at 16px reads as a different, heavier weight than the middle. The
    discs at every vertex make cap and join identical.
    """
    pts = [(_p(x), _p(y)) for x, y in points]
    if closed:
        pts = pts + [pts[0]]
    d.line(pts, fill=255, width=round(_d(width)), joint="curve")
    radius = _d(width) / 2
    for x, y in pts:
        d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=255)


def fill_poly(d: ImageDraw.ImageDraw, points) -> None:
    d.polygon([(_p(x), _p(y)) for x, y in points], fill=255)


def fill_rrect(d: ImageDraw.ImageDraw, top_left, bottom_right, radius: float) -> None:
    (x0, y0), (x1, y1) = top_left, bottom_right
    d.rounded_rectangle([_p(x0), _p(y0), _p(x1), _p(y1)], radius=_d(radius), fill=255)


def outline_rrect(
    d: ImageDraw.ImageDraw, top_left, bottom_right, radius: float, width: float = STROKE
) -> None:
    (x0, y0), (x1, y1) = top_left, bottom_right
    d.rounded_rectangle(
        [_p(x0), _p(y0), _p(x1), _p(y1)],
        radius=_d(radius),
        outline=255,
        width=round(_d(width)),
    )


def ring(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, width: float = STROKE) -> None:
    d.ellipse(
        [_p(cx - r), _p(cy - r), _p(cx + r), _p(cy + r)],
        outline=255,
        width=round(_d(width)),
    )


def dot(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    d.ellipse([_p(cx - r), _p(cy - r), _p(cx + r), _p(cy + r)], fill=255)


def arc(
    d: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    r: float,
    start: float,
    end: float,
    width: float = STROKE,
) -> None:
    """An arc with round caps, angles clockwise from 3 o'clock."""
    import math

    d.arc(
        [_p(cx - r), _p(cy - r), _p(cx + r), _p(cy + r)],
        start,
        end,
        fill=255,
        width=round(_d(width)),
    )
    for angle in (start, end):
        rad = math.radians(angle)
        dot(d, cx + r * math.cos(rad), cy + r * math.sin(rad), width / 2)


# -- the icons ------------------------------------------------------------
# Media transport is filled, everything else is stroked at STROKE. That is the
# convention every player on the machine already uses, so play/pause/stop read
# as a set and the rest reads as a set, without the two ever being confusable.


def play(d):
    fill_poly(d, [(8, 4.8), (19.2, 12), (8, 19.2)])


def pause(d):
    fill_rrect(d, (7.6, 5), (10.8, 19), 1.3)
    fill_rrect(d, (13.2, 5), (16.4, 19), 1.3)


def stop(d):
    fill_rrect(d, (6.2, 6.2), (17.8, 17.8), 2.2)


def skip(d):
    fill_poly(d, [(5.6, 5), (15, 12), (5.6, 19)])
    fill_rrect(d, (16.3, 5), (19, 19), 1.2)


def eject(d):
    fill_poly(d, [(12, 4.6), (20, 13.4), (4, 13.4)])
    fill_rrect(d, (4, 16.4), (20, 19.4), 1.3)


def warning(d):
    # Rounded triangle, then the bang. The dot is a separate disc so it keeps
    # its weight when the whole thing is resampled to 16px.
    stroke(d, [(12, 3.6), (21.6, 20.4), (2.4, 20.4)], closed=True)
    stroke(d, [(12, 9.4), (12, 14.4)])
    dot(d, 12, 17.9, 1.25)


def add(d):
    stroke(d, [(12, 4.6), (12, 19.4)], width=2.4)
    stroke(d, [(4.6, 12), (19.4, 12)], width=2.4)


def close(d):
    stroke(d, [(6.4, 6.4), (17.6, 17.6)], width=2.4)
    stroke(d, [(17.6, 6.4), (6.4, 17.6)], width=2.4)


def up(d):
    stroke(d, [(5.8, 15.2), (12, 9), (18.2, 15.2)], width=2.4)


def down(d):
    stroke(d, [(5.8, 8.8), (12, 15), (18.2, 8.8)], width=2.4)


def check(d):
    stroke(d, [(5, 12.6), (9.8, 17.4), (19, 6.8)], width=2.6)


def help_(d):
    # A bare question mark rather than one inside a circle: at 16px the ring
    # steals more than half the width and what is left of the mark is a smudge.
    arc(d, 12, 8.6, 4.4, 165, 15)
    stroke(d, [(15.3, 11.5), (12.6, 13.6), (12, 15.8)])
    dot(d, 12, 19.3, 1.3)


def folder(d):
    stroke(
        d,
        [(3, 19.6), (3, 5.6), (9.4, 5.6), (11.6, 8.6), (21, 8.6), (21, 19.6)],
        closed=True,
    )


def open_(d):
    # A folder with its front panel swung down and out. Distinct in silhouette
    # from ``folder`` at a glance, which matters - "Abrir..." and "Abrir pasta"
    # sit two inches apart in the window.
    stroke(d, [(3, 18.6), (3, 5.6), (9.4, 5.6), (11.6, 8.6), (19.4, 8.6), (19.4, 11.6)])
    stroke(d, [(3, 18.6), (6.6, 11.6), (22, 11.6), (18.4, 18.6)], closed=True)


def photo(d):
    stroke(d, [(8, 6.4), (9.6, 3.9), (14.4, 3.9), (16, 6.4)])
    outline_rrect(d, (2.4, 6.4), (21.6, 20.6), 2.4)
    ring(d, 12, 13.8, 4.2)


def clean(d):
    # A broom, held at the angle you actually sweep at. The head is solid
    # rather than drawn bristle by bristle: at 16px individual bristles
    # collapse into a grey smear, while a wedge under a handle still reads.
    stroke(d, [(20.2, 3.8), (13.6, 10.4)], width=2.4)
    fill_poly(d, [(11.4, 8.2), (15.8, 12.6), (11.4, 21), (3.4, 14.2)])
    # Two notches cut back out of the wedge, to say "bristles" in the one
    # place there is room for it.
    for (x0, y0), (x1, y1) in (((9.4, 11.6), (7.2, 18.2)), ((12.2, 13.6), (10.2, 19.9))):
        d.line(
            [(_p(x0), _p(y0)), (_p(x1), _p(y1))],
            fill=0,
            width=round(_d(1.0)),
        )


def recent(d):
    ring(d, 12, 12, 8.6)
    stroke(d, [(12, 6.8), (12, 12), (15.8, 13.9)])


def send(d):
    stroke(d, [(4, 12), (19, 12)], width=2.4)
    stroke(d, [(13.4, 6.4), (19.4, 12), (13.4, 17.6)], width=2.4)


ICONS = {
    "play": play,
    "pause": pause,
    "stop": stop,
    "skip": skip,
    "eject": eject,
    "warning": warning,
    "add": add,
    "close": close,
    "up": up,
    "down": down,
    "check": check,
    "help": help_,
    "folder": folder,
    "open": open_,
    "photo": photo,
    "clean": clean,
    "recent": recent,
    "send": send,
}


# -- rendering ------------------------------------------------------------


def render_mask(draw_fn) -> Image.Image:
    side = GRID * SUPER
    mask = Image.new("L", (side, side), 0)
    draw_fn(ImageDraw.Draw(mask))
    return mask


def tint(mask: Image.Image, colour: str, size: int) -> Image.Image:
    small = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), colour)
    out.putalpha(small)
    return out


def write_icons() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, draw_fn in ICONS.items():
        mask = render_mask(draw_fn)
        for size in SIZES:
            for suffix, colour in TONES.items():
                stem = f"{name}-{size}" + (f"-{suffix}" if suffix else "")
                tint(mask, colour, size).save(OUT / f"{stem}.png")
                written += 1
        # The unsuffixed name is the 16px brand tone: the fallback
        # theme.load_icon() reaches for when a size is missing.
        tint(mask, TONES[""], 16).save(OUT / f"{name}.png")
        written += 1
    return written


# -- the checkbox indicator ----------------------------------------------
# clam draws a checked box as a dark X on grey, which is the one shape a user
# reads as "off". These replace the element outright (see theme.apply_theme),
# so "Copia automatica" looks like what it is.


# Transparent space to the right of the box, baked into the image. A ttk
# image element has no -indicatormargin, and the -padding it does take is
# interior 9-patch padding, so this is the one place the gap between the box
# and its label can come from.
CHECKBOX_GAP = 7


def checkbox(size: int, *, on: bool, border: str, fill: str, mark: str) -> Image.Image:
    scale = SUPER
    big = size * scale
    img = Image.new("RGBA", (big + CHECKBOX_GAP * scale, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = 0.9 * scale
    d.rounded_rectangle(
        [inset, inset, big - inset, big - inset],
        radius=2.8 * scale,
        fill=fill,
        outline=border,
        width=round(1.4 * scale),
    )
    if on:
        d.line(
            [
                (big * 0.26, big * 0.52),
                (big * 0.44, big * 0.71),
                (big * 0.76, big * 0.31),
            ],
            fill=mark,
            width=round(1.7 * scale),
            joint="curve",
        )
    return img.resize((size + CHECKBOX_GAP, size), Image.LANCZOS)


CHECKBOXES = {
    "checkbox-off": dict(on=False, border=BORDER_STRONG, fill=SURFACE, mark=SURFACE),
    "checkbox-off-hover": dict(on=False, border=BRAND, fill=SURFACE, mark=SURFACE),
    "checkbox-on": dict(on=True, border=BRAND, fill=BRAND, mark=SURFACE),
    "checkbox-on-hover": dict(on=True, border="#0427b4", fill="#0427b4", mark=SURFACE),
    "checkbox-off-muted": dict(on=False, border="#dfe3ec", fill="#f4f6fb", mark=SURFACE),
    "checkbox-on-muted": dict(on=True, border=DISABLED_FG, fill=DISABLED_FG, mark=SURFACE),
}


def write_checkboxes(size: int = 16) -> int:
    for name, kwargs in CHECKBOXES.items():
        checkbox(size, **kwargs).save(OUT / f"{name}.png")
    return len(CHECKBOXES)


def main() -> int:
    icons = write_icons()
    boxes = write_checkboxes()
    print(f"{icons} icon files and {boxes} checkbox files written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
