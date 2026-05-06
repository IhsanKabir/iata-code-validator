# -*- mode: python ; coding: utf-8 -*-
"""Explicit PyInstaller spec for IATA Code Validator.

Bundles patchright Chromium and the faster-whisper tiny.en model.
Use: pyinstaller --noconfirm IATACodeValidator.spec
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

PROJECT = Path(SPECPATH)
WHISPER_SRC = PROJECT / "assets" / "whisper_model"

# Sanity check at build time
if not (WHISPER_SRC / "model.bin").exists():
    raise SystemExit(
        f"Whisper model.bin not found at {WHISPER_SRC}. "
        "Run the model download first."
    )

datas = []
binaries = []
hiddenimports = [
    "openpyxl",
    "openpyxl.cell._writer",
    "tkinter",
    # Windows Credential Manager backend for `keyring` — required for
    # token persistence in the auth module.
    "keyring.backends.Windows",
]

# Bundle whisper model files individually (more reliable than dir-tuple form
# on some PyInstaller versions on Windows).
for f in WHISPER_SRC.iterdir():
    if f.is_file():
        datas.append((str(f), "whisper_model"))

# Pull in everything from these packages
for pkg in [
    "patchright",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "keyring",
    "rapidfuzz",
]:
    pkg_datas, pkg_bins, pkg_hi = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_bins
    hiddenimports += pkg_hi


# Drop ~190 MB of chromium_headless_shell and other unused chromium artifacts.
# We always launch in non-headless mode (need to show the reCAPTCHA to the
# user when the audio fallback fails), so the headless shell is dead weight.
def _strip_unused_chromium(entries):
    drop_substrings = (
        "chromium_headless_shell",      # 190 MB — we never run headless
        "chrome-headless-shell",
    )
    kept = []
    dropped_bytes = 0
    for entry in entries:
        # entry is a tuple (src, dest) or (src, dest, type)
        src = entry[0]
        if any(s in src for s in drop_substrings):
            try:
                from os.path import getsize
                dropped_bytes += getsize(src)
            except OSError:
                pass
            continue
        kept.append(entry)
    if dropped_bytes:
        print(f"[spec] Stripped {dropped_bytes / 1e6:.0f} MB of unused chromium artifacts")
    return kept


datas = _strip_unused_chromium(datas)
binaries = _strip_unused_chromium(binaries)

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="IATACodeValidator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
