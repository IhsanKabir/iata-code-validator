"""Entry point.

Both Chromium (via patchright) and the Whisper tiny.en model are bundled
inside the .exe by PyInstaller (see build_exe.bat). No first-run download,
no admin access, no Python install needed on the target laptops.
"""

from __future__ import annotations

import os
import sys

from . import config


def _apply_zenith_host_override() -> None:
    """Set ZENITH_BASE_URL from the saved host file BEFORE the Zenith modules import.

    Lets the user route around the CloudFront-fronted `usba.` host (which 504-storms on
    slow Dossier renders) to the direct `asia.` origin — chosen once in the GUI, persisted
    here. An explicit env var always wins over the saved file.
    """
    if os.environ.get("ZENITH_BASE_URL"):
        return
    try:
        saved = config.ZENITH_HOST_FILE.read_text(encoding="utf-8").strip()
    except (OSError, AttributeError):
        saved = ""
    if saved:
        os.environ["ZENITH_BASE_URL"] = saved


def _configure_bundled_browser_path() -> None:
    """When frozen by PyInstaller, point patchright at the bundled Chromium.

    PLAYWRIGHT_BROWSERS_PATH=0 tells patchright/playwright to look inside the
    package directory (which PyInstaller extracts to sys._MEIPASS at runtime).
    """
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


def _selftest() -> int:
    """Headless smoke test of the bundled Instant-Reports engine.

    Run as ``IATACodeValidator.exe --selftest``. Build-environment imports passing
    does NOT prove the FROZEN bundle works: PyInstaller can miss a native library
    (pyarrow's Arrow DLLs / duckdb's engine) that only fails at runtime, after
    which a manager's "Build from data" would crash. This exercises those exact
    native paths inside the frozen exe and records a verdict.

    The exe is windowed (console=False), so the process never attaches to a parent
    console — stdout is invisible. We therefore write the result to
    ``APP_DIR/selftest_result.txt`` and signal pass/fail via the EXIT CODE
    (0 = ok, 1 = fail) so a caller can assert without a console.
    """
    import datetime
    import io
    import traceback

    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    out = config.APP_DIR / "selftest_result.txt"
    try:
        import duckdb
        import openpyxl  # noqa: F401 — import IS the test (every builder uses it)
        import pandas as pd
        import pyarrow

        from reporting import __version__ as rep_ver
        from reporting import instant

        # duckdb native engine
        n = duckdb.connect().execute("SELECT count(*) FROM range(5)").fetchone()[0]
        # pyarrow native path, via the same parquet round-trip the builders rely on
        buf = io.BytesIO()
        pd.DataFrame({"x": [1, 2, 3]}).to_parquet(buf)                 # -> pyarrow
        rows = pd.read_parquet(io.BytesIO(buf.getvalue())).shape[0]    # <- pyarrow
        if n != 5 or rows != 3:
            raise RuntimeError(f"sanity check failed (duckdb={n}, parquet_rows={rows})")

        out.write_text(
            f"SELFTEST OK  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"frozen        : {bool(getattr(sys, 'frozen', False))}\n"
            f"reporting     : {rep_ver}\n"
            f"duckdb        : {duckdb.__version__}  (range->{n})\n"
            f"pandas        : {pd.__version__}\n"
            f"pyarrow       : {pyarrow.__version__}  (parquet round-trip->{rows} rows)\n"
            f"instant set   : {list(instant.INSTANT_REPORTS)}\n",
            encoding="utf-8")
        return 0
    except Exception:                                          # noqa: BLE001 — record everything
        out.write_text("SELFTEST FAIL\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def main() -> int:
    # Headless engine check for QA/CI of the frozen build — handled before any
    # GUI/browser init so it proves the report stack in isolation.
    if "--selftest" in sys.argv:
        return _selftest()

    _configure_bundled_browser_path()
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    _apply_zenith_host_override()   # MUST precede the gui import (which imports zenith_client)

    # Late import so PLAYWRIGHT_BROWSERS_PATH is set before any
    # patchright/playwright import touches the browser path resolver.
    from .gui import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
