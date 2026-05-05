"""Entry point.

Both Chromium (via patchright) and the Whisper tiny.en model are bundled
inside the .exe by PyInstaller (see build_exe.bat). No first-run download,
no admin access, no Python install needed on the target laptops.
"""

from __future__ import annotations

import os
import sys

from . import config


def _configure_bundled_browser_path() -> None:
    """When frozen by PyInstaller, point patchright at the bundled Chromium.

    PLAYWRIGHT_BROWSERS_PATH=0 tells patchright/playwright to look inside the
    package directory (which PyInstaller extracts to sys._MEIPASS at runtime).
    """
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


def main() -> int:
    _configure_bundled_browser_path()
    config.APP_DIR.mkdir(parents=True, exist_ok=True)

    # Late import so PLAYWRIGHT_BROWSERS_PATH is set before any
    # patchright/playwright import touches the browser path resolver.
    from .gui import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
