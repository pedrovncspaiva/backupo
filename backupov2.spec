# PyInstaller build recipe.  Build with:  pyinstaller backupov2.spec --noconfirm
#
# Produces a single windowed .exe in dist/.  The package is pure stdlib plus
# Tkinter, so there is nothing to collect beyond our own assets - the two
# things that must be right are the assets folder (the logo and the window
# icon are read from disk at runtime) and console=False (a .pyw entry point
# with a console window defeats the point).
#
# For a faster-starting build, see ONEFILE below.

import os
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

# False (the default) builds dist/backupov2/ - a folder to ship whole, or to
# point a shortcut at. True builds a single .exe that unpacks itself to a temp
# folder on every launch: easier to hand over, about two seconds slower to
# start, and considerably more likely to be quarantined by antivirus.
#
# build.ps1 sets this through the environment; edit the default here if you
# always want the other one.
ONEFILE = os.environ.get("BACKUPOV2_ONEFILE", "0") == "1"

NAME = "Assistente de Backup de Discos"
VERSION = "2.0.0"
ROOT = Path(SPECPATH)
ICON = ROOT / "backupov2" / "ui" / "assets" / "app-icon.ico"

# Shown in the .exe's Properties > Details, and in the taskbar tooltip.
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(2, 0, 0, 0),
        prodvers=(2, 0, 0, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
    ),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Sondotecnica"),
            StringStruct("FileDescription", NAME),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "backupov2"),
            StringStruct("OriginalFilename", f"{NAME}.exe"),
            StringStruct("ProductName", NAME),
            StringStruct("ProductVersion", VERSION),
        ])]),
        # 0x0409 = en-US, 1200 = Unicode. Windows wants a language it knows;
        # the strings themselves are what the user reads.
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

analysis = Analysis(
    ["run.pyw"],
    pathex=[str(ROOT)],
    binaries=[],
    # Target path must stay backupov2/ui/assets - theme._assets_dir() looks
    # for exactly that under sys._MEIPASS.
    datas=[(str(ROOT / "backupov2" / "ui" / "assets"), "backupov2/ui/assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # Only what nothing reaches at runtime. Keep this list short and dull:
    # "email" and "http" look unused but urllib.request imports both, and
    # vision.py imports urllib.request at module level - excluding them
    # builds a perfectly happy .exe that dies on the first line it runs.
    excludes=[
        "PIL", "numpy", "pandas", "pytest", "setuptools", "pip",
        "pydoc", "doctest", "tkinter.test", "test", "tests",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name=NAME,
        icon=str(ICON),
        version=version_info,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,          # windowed: no console flashing behind the UI
        disable_windowed_traceback=False,
    )
else:
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        icon=str(ICON),
        version=version_info,
        debug=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
    COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="backupov2",
    )
