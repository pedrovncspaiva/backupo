"""The single source of colour, type and ttk styling for the whole UI.

Every panel pulls its colours from here rather than spelling out hex codes
next to the widget, so the brand can move in one file instead of nine. The
palette is sampled from the Sondotecnica mark itself: the blue is the logo
blue, the amber is the folder in the app icon.

The base ttk theme is ``clam`` on purpose. The native ``vista`` theme draws
buttons and tabs with Windows' own bitmaps and ignores background colours,
which makes a primary action impossible to distinguish from a secondary one.
``clam`` is fully colourable, so the hierarchy below is actually visible.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def _assets_dir() -> Path:
    """Where the logo and the icon live, running from source or frozen.

    PyInstaller unpacks bundled data under ``sys._MEIPASS`` and rewrites
    ``__file__`` to point inside it, so the plain relative path usually still
    works - but only as long as the .spec keeps the same target path. Asking
    ``_MEIPASS`` first makes that independent of how the bundle is laid out.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = Path(base) / "backupov2" / "ui" / "assets"
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().parent / "assets"


ASSETS = _assets_dir()

# -- palette --------------------------------------------------------------
# Brand, straight off the logo.
BRAND = "#0636f0"          # Sondotecnica blue
BRAND_DARK = "#0427b4"     # pressed / hovered primary
BRAND_DEEP = "#03197a"     # headings on light backgrounds
BRAND_TINT = "#eaefff"     # banners, selected rows
BRAND_TINT_2 = "#d5e0ff"   # hovered rows, borders on tinted surfaces

# The disc-folder gold, used only for "this folder is swallowing every disc".
AMBER = "#d9a923"
AMBER_TINT = "#fdf3d7"
AMBER_DEEP = "#7a5800"

# Neutrals, lightest to darkest.
SURFACE = "#ffffff"        # cards
CANVAS = "#f4f6fb"         # the window behind the cards
STRIPE = "#f8f9fc"         # alternating table rows
BORDER = "#dfe3ec"
BORDER_STRONG = "#c2c9d8"
INK = "#101828"            # primary text
INK_SOFT = "#475467"       # secondary text
INK_MUTED = "#8a94a6"      # hints, placeholders, disabled

# Semantic.
SUCCESS = "#0b6b3a"
SUCCESS_TINT = "#e6f5ec"
WARNING = "#8a5a00"
WARNING_TINT = "#fdf3d7"
DANGER = "#a4161a"
DANGER_TINT = "#fdeaea"
INFO = BRAND
DISABLED_FG = "#9aa3b2"

# -- type -----------------------------------------------------------------
UI_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"

FONT_DISPLAY = (UI_FAMILY, 17, "bold")   # the batch name
FONT_TITLE = (UI_FAMILY, 13, "bold")     # panel headings
FONT_SUBTITLE = (UI_FAMILY, 11, "bold")
FONT_BODY = (UI_FAMILY, 10)
FONT_BODY_BOLD = (UI_FAMILY, 10, "bold")
FONT_SMALL = (UI_FAMILY, 9)
FONT_SMALL_BOLD = (UI_FAMILY, 9, "bold")
FONT_MONO = (MONO_FAMILY, 9)
FONT_MONO_SMALL = (MONO_FAMILY, 8)
FONT_WORDMARK = (UI_FAMILY, 13, "bold")

# Glyphs. Plain Unicode rather than an icon font, so nothing has to ship or
# be installed - these all render in Segoe UI on Windows 10 and 11.
GLYPH = {
    "new": "✦",        # ✦
    "open": "\U0001f4c2",   # 📂
    "recent": "⏱",     # ⏱
    "folder": "\U0001f4c1",  # 📁
    "add": "＋",        # ＋
    "photo": "\U0001f4f7",  # 📷
    "up": "▲",
    "down": "▼",
    "play": "▶",
    "pause": "⏸",
    "stop": "⏹",
    "skip": "⏭",
    "eject": "⏏",
    "send": "→",
    "close": "✕",
    "help": "?",
    "warning": "⚠",
    "check": "✓",
    "disc": "●",
    "clean": "\U0001f9f9",  # 🧹
}

# Status dots for the list, so a row reads at a glance without colour alone.
STATUS_DOT = {
    "pending": "○",      # ○
    "in_progress": "◐",  # ◐
    "done": "●",         # ●
    "skipped": "◌",      # ◌
    "failed": "✕",       # ✕
    "collecting": "◆",   # ◆
}


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Install the palette on ``root`` and return the configured style."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - clam ships with every Tk build
        pass

    root.configure(background=CANVAS)
    root.option_add("*Font", FONT_BODY)
    # tk.Menu and the messagebox family are not ttk, so they are dressed here.
    root.option_add("*Menu.background", SURFACE)
    root.option_add("*Menu.foreground", INK)
    root.option_add("*Menu.activeBackground", BRAND)
    root.option_add("*Menu.activeForeground", SURFACE)
    root.option_add("*Menu.activeBorderWidth", 0)
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.relief", "solid")
    root.option_add("*Menu.font", FONT_BODY)
    root.option_add("*Menu.disabledForeground", INK_MUTED)

    # -- surfaces ---------------------------------------------------------
    style.configure(".", background=CANVAS, foreground=INK, font=FONT_BODY)
    style.configure("TFrame", background=CANVAS)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure("Brand.TFrame", background=BRAND)
    style.configure("Tint.TFrame", background=BRAND_TINT)
    style.configure("Amber.TFrame", background=AMBER_TINT)
    style.configure("StatusBar.TFrame", background=SURFACE)

    style.configure("TLabel", background=CANVAS, foreground=INK)
    style.configure("Card.TLabel", background=SURFACE, foreground=INK)
    style.configure("CardTitle.TLabel", background=SURFACE, foreground=INK,
                    font=FONT_TITLE)
    style.configure("CardMuted.TLabel", background=SURFACE, foreground=INK_SOFT,
                    font=FONT_SMALL)
    style.configure("CardMono.TLabel", background=SURFACE, foreground=INK_SOFT,
                    font=FONT_MONO)
    style.configure("Display.TLabel", background=SURFACE, foreground=BRAND_DEEP,
                    font=FONT_DISPLAY)
    style.configure("DisplayIdle.TLabel", background=SURFACE, foreground=INK_MUTED,
                    font=FONT_DISPLAY)
    style.configure("Muted.TLabel", background=CANVAS, foreground=INK_SOFT,
                    font=FONT_SMALL)
    style.configure("Hint.TLabel", background=SURFACE, foreground=INK_MUTED,
                    font=FONT_SMALL)
    style.configure("Wordmark.TLabel", background=SURFACE, foreground=BRAND,
                    font=FONT_WORDMARK)
    style.configure("Tagline.TLabel", background=SURFACE, foreground=INK_MUTED,
                    font=FONT_SMALL)
    style.configure("Step.TLabel", background=SURFACE, foreground=BRAND,
                    font=FONT_SMALL_BOLD)
    style.configure("StatusBar.TLabel", background=SURFACE, foreground=INK_SOFT,
                    font=FONT_SMALL)
    style.configure("Amber.TLabel", background=AMBER_TINT, foreground=AMBER_DEEP,
                    font=FONT_SMALL_BOLD)
    style.configure("Success.TLabel", background=SURFACE, foreground=SUCCESS,
                    font=FONT_SMALL_BOLD)
    style.configure("Danger.TLabel", background=SURFACE, foreground=DANGER,
                    font=FONT_SMALL_BOLD)
    style.configure("Warning.TLabel", background=SURFACE, foreground=WARNING,
                    font=FONT_SMALL_BOLD)

    # -- buttons ----------------------------------------------------------
    # One shape, four weights: primary, default, quiet, destructive.
    style.configure(
        "TButton",
        background=SURFACE,
        foreground=INK,
        bordercolor=BORDER_STRONG,
        darkcolor=SURFACE,
        lightcolor=SURFACE,
        focusthickness=0,
        focuscolor=BRAND_TINT,
        relief="flat",
        borderwidth=1,
        padding=(12, 7),
        font=FONT_BODY,
    )
    style.map(
        "TButton",
        background=[("disabled", CANVAS), ("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
        foreground=[("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        bordercolor=[("disabled", BORDER), ("active", BRAND)],
        lightcolor=[("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
        darkcolor=[("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
    )

    style.configure(
        "Accent.TButton",
        background=BRAND,
        foreground=SURFACE,
        bordercolor=BRAND,
        lightcolor=BRAND,
        darkcolor=BRAND,
        font=FONT_BODY_BOLD,
        padding=(14, 8),
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", BORDER), ("pressed", BRAND_DEEP), ("active", BRAND_DARK)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("disabled", BORDER), ("active", BRAND_DARK)],
        lightcolor=[("pressed", BRAND_DEEP), ("active", BRAND_DARK)],
        darkcolor=[("pressed", BRAND_DEEP), ("active", BRAND_DARK)],
    )

    style.configure(
        "Quiet.TButton",
        background=SURFACE,
        foreground=INK_SOFT,
        bordercolor=SURFACE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        padding=(9, 6),
        font=FONT_SMALL,
    )
    style.map(
        "Quiet.TButton",
        background=[("disabled", SURFACE), ("active", BRAND_TINT)],
        foreground=[("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        bordercolor=[("active", BRAND_TINT)],
        lightcolor=[("active", BRAND_TINT)],
        darkcolor=[("active", BRAND_TINT)],
    )

    # The same quiet weight, for a button sitting on a tinted card rather than
    # on white. Quiet.TButton keeps its SURFACE background even when disabled,
    # which on the destination card reads as an empty white input box dropped
    # into the middle of a blue panel - so it borrows the card's own colour
    # instead and disappears when it has nothing to do.
    style.configure(
        "Tint.TButton",
        background=BRAND_TINT,
        foreground=BRAND_DEEP,
        bordercolor=BRAND_TINT_2,
        lightcolor=BRAND_TINT,
        darkcolor=BRAND_TINT,
        padding=(9, 5),
        font=FONT_SMALL,
    )
    style.map(
        "Tint.TButton",
        background=[("disabled", BRAND_TINT), ("active", SURFACE)],
        foreground=[("disabled", INK_MUTED), ("active", BRAND_DEEP)],
        bordercolor=[("disabled", BRAND_TINT), ("active", BRAND)],
        lightcolor=[("disabled", BRAND_TINT), ("active", SURFACE)],
        darkcolor=[("disabled", BRAND_TINT), ("active", SURFACE)],
    )

    style.configure(
        "Danger.TButton",
        background=SURFACE,
        foreground=DANGER,
        bordercolor=BORDER_STRONG,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", CANVAS), ("active", DANGER_TINT)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("disabled", BORDER), ("active", DANGER)],
        lightcolor=[("active", DANGER_TINT)],
        darkcolor=[("active", DANGER_TINT)],
    )

    style.configure("Icon.TButton", padding=(8, 6), font=FONT_SMALL)
    style.map("Icon.TButton", **_button_map())

    style.configure("Toolbar.TButton", background=SURFACE, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, padding=(11, 7))
    style.map("Toolbar.TButton", **_button_map())

    style.configure("TMenubutton", background=SURFACE, foreground=INK,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                    relief="flat", borderwidth=1, padding=(11, 7), arrowcolor=INK_SOFT)
    style.map(
        "TMenubutton",
        background=[("disabled", CANVAS), ("active", BRAND_TINT)],
        foreground=[("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        bordercolor=[("active", BRAND)],
        arrowcolor=[("disabled", DISABLED_FG), ("active", BRAND)],
    )

    # -- inputs -----------------------------------------------------------
    style.configure(
        "TEntry",
        fieldbackground=SURFACE,
        background=SURFACE,
        foreground=INK,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        insertcolor=INK,
        padding=(8, 6),
        relief="flat",
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", CANVAS), ("readonly", CANVAS)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("focus", BRAND), ("disabled", BORDER)],
        lightcolor=[("focus", BRAND)],
        darkcolor=[("focus", BRAND)],
    )

    # clam's indicator elements take -indicatorbackground (the box) and
    # -indicatorforeground (the mark). They do *not* take -indicatorcolor,
    # which ttk accepts without complaint and then ignores - so setting it was
    # leaving both controls in clam's default grey. The drawn indicator
    # installed at the end of this function replaces the checkbox outright;
    # these colours are what the radiobutton uses, and what the checkbox falls
    # back to if the images are missing.
    style.configure(
        "TCheckbutton",
        background=SURFACE,
        foreground=INK,
        indicatorbackground=SURFACE,
        indicatorforeground=SURFACE,
        bordercolor=BORDER_STRONG,
        focusthickness=0,
        padding=(2, 4),
    )
    style.map(
        "TCheckbutton",
        background=[("active", SURFACE)],
        foreground=[("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        indicatorbackground=[("disabled", CANVAS), ("selected", BRAND), ("!selected", SURFACE)],
        indicatorforeground=[("selected", SURFACE)],
        bordercolor=[("selected", BRAND), ("active", BRAND)],
    )
    style.configure("Canvas.TCheckbutton", background=CANVAS)
    style.map("Canvas.TCheckbutton", background=[("active", CANVAS)])

    style.configure("TRadiobutton", background=SURFACE, foreground=INK,
                    indicatorbackground=SURFACE, indicatorforeground=BRAND,
                    bordercolor=BORDER_STRONG, focusthickness=0, padding=(2, 4))
    style.map(
        "TRadiobutton",
        background=[("active", SURFACE)],
        foreground=[("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        indicatorbackground=[("disabled", CANVAS), ("selected", SURFACE)],
        indicatorforeground=[("disabled", DISABLED_FG), ("selected", BRAND)],
        bordercolor=[("selected", BRAND), ("active", BRAND)],
    )

    # -- containers -------------------------------------------------------
    style.configure("TLabelframe", background=SURFACE, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, relief="solid",
                    borderwidth=1, padding=12)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=BRAND_DEEP,
                    font=FONT_SUBTITLE)

    style.configure("TNotebook", background=CANVAS, bordercolor=BORDER,
                    tabmargins=(0, 4, 0, 0), borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=CANVAS,
        foreground=INK_SOFT,
        bordercolor=BORDER,
        lightcolor=CANVAS,
        darkcolor=CANVAS,
        padding=(16, 8),
        font=FONT_SMALL_BOLD,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SURFACE), ("active", BRAND_TINT)],
        foreground=[("selected", BRAND_DEEP), ("active", BRAND_DEEP)],
        lightcolor=[("selected", SURFACE)],
        darkcolor=[("selected", SURFACE)],
        expand=[("selected", (0, 0, 0, 1))],
    )

    style.configure("TPanedwindow", background=CANVAS)
    style.configure("Sash", sashthickness=8, gripcount=0, background=CANVAS)

    style.configure("TSeparator", background=BORDER)

    # -- progress ---------------------------------------------------------
    style.configure(
        "TProgressbar",
        background=BRAND,
        troughcolor=BRAND_TINT,
        bordercolor=BRAND_TINT,
        lightcolor=BRAND,
        darkcolor=BRAND,
        thickness=8,
        borderwidth=0,
    )
    style.configure("Header.Horizontal.TProgressbar", thickness=10,
                    troughcolor=BRAND_TINT, background=BRAND)
    style.configure("Disc.Horizontal.TProgressbar", thickness=14,
                    troughcolor=BRAND_TINT, background=BRAND)
    style.configure("Success.Horizontal.TProgressbar", thickness=14,
                    troughcolor=SUCCESS_TINT, background=SUCCESS)

    # -- table ------------------------------------------------------------
    style.configure(
        "Treeview",
        background=SURFACE,
        fieldbackground=SURFACE,
        foreground=INK,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        borderwidth=0,
        rowheight=26,
        font=FONT_BODY,
    )
    style.map(
        "Treeview",
        background=[("selected", BRAND)],
        foreground=[("selected", SURFACE)],
    )
    style.configure(
        "Treeview.Heading",
        background=CANVAS,
        foreground=INK_SOFT,
        bordercolor=BORDER,
        lightcolor=CANVAS,
        darkcolor=CANVAS,
        relief="flat",
        padding=(8, 7),
        font=FONT_SMALL_BOLD,
    )
    style.map(
        "Treeview.Heading",
        background=[("active", BRAND_TINT)],
        foreground=[("active", BRAND_DEEP)],
    )

    # -- scrollbars -------------------------------------------------------
    for orient in ("Vertical", "Horizontal"):
        style.configure(
            f"{orient}.TScrollbar",
            background=BORDER,
            troughcolor=CANVAS,
            bordercolor=CANVAS,
            arrowcolor=INK_SOFT,
            lightcolor=BORDER,
            darkcolor=BORDER,
            borderwidth=0,
            relief="flat",
        )
        style.map(
            f"{orient}.TScrollbar",
            background=[("pressed", BRAND), ("active", BORDER_STRONG)],
            arrowcolor=[("pressed", BRAND_DEEP), ("active", BRAND_DEEP)],
        )

    _install_checkbox_indicator(root, style)
    return style


def _button_map() -> dict:
    return {
        "background": [("disabled", CANVAS), ("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
        "foreground": [("disabled", DISABLED_FG), ("active", BRAND_DEEP)],
        "bordercolor": [("disabled", BORDER), ("active", BRAND)],
        "lightcolor": [("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
        "darkcolor": [("pressed", BRAND_TINT_2), ("active", BRAND_TINT)],
    }


# -- assets ---------------------------------------------------------------
# Tk drops a PhotoImage the moment nothing references it and draws nothing in
# its place, so every image loaded here is cached. The cache is keyed by
# interpreter as well as size: an image belongs to the Tk instance that
# created it, and handing a second window one from the first - which is what a
# test suite that builds a fresh root per case does - fails with
# "image pyimageN doesn't exist".
_images: dict[tuple[int, int], tk.PhotoImage] = {}


def load_logo(master: tk.Misc, size: int = 32) -> tk.PhotoImage | None:
    """The app mark at one of the pre-rendered sizes, or None if unavailable."""
    key = (id(master.tk), size)
    cached = _images.get(key)
    if cached is not None:
        try:
            cached.width()  # still alive in this interpreter?
            return cached
        except Exception:
            _images.pop(key, None)

    path = ASSETS / f"logo-{size}.png"
    if not path.is_file():
        return None
    try:
        image = tk.PhotoImage(master=master, file=str(path))
    except tk.TclError:  # pragma: no cover - only on a Tk built without PNG
        return None
    _images[key] = image
    return image


def apply_window_icon(window: tk.Tk | tk.Toplevel) -> None:
    """Set the title-bar and taskbar icon, quietly doing nothing if it fails."""
    icon = ASSETS / "app-icon.ico"
    if not icon.is_file():
        return
    try:
        window.iconbitmap(default=str(icon))
    except tk.TclError:  # pragma: no cover - non-Windows Tk
        try:
            image = load_logo(window, 48)
            if image is not None:
                window.iconphoto(True, image)
        except tk.TclError:
            pass


# -- icons ----------------------------------------------------------------
# Drawn by tools/make_icons.py, four tones deep. ``muted`` exists so a
# disabled button greys its icon along with its label: ttk takes a state-keyed
# image list, so both are handed over once at build time and Tk swaps them.

ICON_SIZE = 16

# The tone to ask for on each of the button styles that is not plain.
TONE_FOR_STYLE = {
    "Accent.TButton": "invert",
    "Danger.TButton": "danger",
}

_icons: dict[tuple[int, str, int, str], tk.PhotoImage] = {}


def load_icon(
    master: tk.Misc, name: str, size: int = ICON_SIZE, tone: str = ""
) -> tk.PhotoImage | None:
    """One icon, or None if it is not on disk.

    Cached per interpreter for the same reason ``load_logo`` is: an image
    belongs to the Tk instance that made it, and a test suite building a fresh
    root per case would otherwise be handed a dead one.
    """
    key = (id(master.tk), name, size, tone)
    cached = _icons.get(key)
    if cached is not None:
        try:
            cached.width()  # still alive in this interpreter?
            return cached
        except Exception:
            _icons.pop(key, None)

    suffix = f"-{tone}" if tone else ""
    for candidate in (f"{name}-{size}{suffix}.png", f"{name}{suffix}.png", f"{name}.png"):
        path = ASSETS / "icons" / candidate
        if path.is_file():
            try:
                image = tk.PhotoImage(master=master, file=str(path))
            except tk.TclError:  # pragma: no cover - Tk built without PNG
                return None
            _icons[key] = image
            return image
    return None


def set_button_icon(
    button: ttk.Button | ttk.Menubutton,
    name: str,
    text: str = "",
    size: int = ICON_SIZE,
    tone: str | None = None,
) -> None:
    """Put an icon on a button, in the tone its style calls for.

    ``tone`` defaults to the one that is legible on whatever style the button
    already has - white on the filled accent button, red on the destructive
    one - because a brand-blue icon on a brand-blue button is an invisible
    button.

    Falls back to the Unicode glyph if the .png is missing, so a stripped
    assets folder costs the icon and not the label.
    """
    if tone is None:
        tone = TONE_FOR_STYLE.get(str(button.cget("style")), "")

    icon = load_icon(button, name, size, tone)
    if icon is None:
        glyph = GLYPH.get(name, "")
        button.configure(text=f"{glyph}  {text}".strip() if glyph else text)
        return

    muted = load_icon(button, name, size, "muted")
    button.configure(
        image=(icon, "disabled", muted) if muted is not None else icon,
        compound="left" if text else "image",
        text=f"  {text}" if text else "",
    )


# -- the checkbox ---------------------------------------------------------

_checkbox_images: dict[int, dict[str, tk.PhotoImage]] = {}

CHECKBOX_STATES = (
    "checkbox-off",
    "checkbox-off-hover",
    "checkbox-off-muted",
    "checkbox-on",
    "checkbox-on-hover",
    "checkbox-on-muted",
)


def _install_checkbox_indicator(root: tk.Misc, style: ttk.Style) -> bool:
    """Replace clam's checkbox indicator with the drawn one.

    clam paints a *checked* box as a dark X on grey - and an X is the one mark
    every user reads as "off". The app's most consequential toggle is a
    checkbox ("Copia automatica" decides whether a disc starts copying on its
    own), so it cannot be showing the opposite of its own state.

    Returns False if the images are not on disk, leaving the caller to fall
    back to clam's indicator with at least its colours corrected.
    """
    images: dict[str, tk.PhotoImage] = {}
    for name in CHECKBOX_STATES:
        path = ASSETS / "icons" / f"{name}.png"
        if not path.is_file():
            return False
        try:
            images[name] = tk.PhotoImage(master=root, file=str(path))
        except tk.TclError:  # pragma: no cover - Tk built without PNG
            return False
    # Tk drops an image the moment nothing references it, so these outlive the
    # function that made them.
    _checkbox_images[id(root.tk)] = images

    try:
        style.element_create(
            "Brand.Checkbutton.indicator",
            "image",
            images["checkbox-off"],
            # First match wins, so the two-state specs come before the one
            # that would otherwise swallow them.
            ("disabled", "selected", images["checkbox-on-muted"]),
            ("disabled", images["checkbox-off-muted"]),
            ("selected", "active", images["checkbox-on-hover"]),
            ("selected", images["checkbox-on"]),
            ("active", images["checkbox-off-hover"]),
            border=0,
            sticky="",
        )
    except tk.TclError:  # pragma: no cover - already created on this root
        return False

    style.layout(
        "TCheckbutton",
        [
            (
                "Checkbutton.padding",
                {
                    "sticky": "nswe",
                    "children": [
                        ("Brand.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                        (
                            "Checkbutton.focus",
                            {
                                "side": "left",
                                "sticky": "w",
                                "children": [("Checkbutton.label", {"sticky": "nswe"})],
                            },
                        ),
                    ],
                },
            )
        ],
    )
    return True
