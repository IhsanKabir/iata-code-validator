"""Smoke test: validate one known IATA number end-to-end against the live site.

Use this once on each laptop after install to confirm the validator still
works and selectors haven't drifted.

Usage:
    python smoke_test.py 32302491

If a reCAPTCHA image puzzle appears, just solve it in the Chrome window —
the script auto-detects the green check and continues. Don't press Enter
in the terminal; that does nothing here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.validator import CaptchaChallenge, IATAValidator


def main() -> int:
    iata = sys.argv[1] if len(sys.argv) > 1 else "32302491"

    profile = Path.home() / "AppData" / "Local" / "IATAChecker" / "browser_profile"

    s = IATAValidator(profile_dir=profile, on_log=print)
    print(f"Starting browser for IATA={iata}...")
    s.start()
    try:
        try:
            result = s.lookup(iata)
        except CaptchaChallenge as e:
            print(f"\n>>> CAPTCHA: {e}")
            print(">>> Solve the image puzzle in the Chrome window.")
            print(">>> The script will continue automatically when the green check appears.")
            ok = s.wait_for_user_captcha(timeout=300)
            if not ok:
                print("Timed out waiting for CAPTCHA.")
                return 2
            print(">>> Green check detected — submitting and reading result.")
            # IMPORTANT: complete_after_captcha (not lookup) — keeps the solved
            # state and just submits + reads. Calling lookup would re-navigate
            # and force a fresh CAPTCHA.
            result = s.complete_after_captcha(iata)

        print("\n---- RESULT ----")
        print(f"  IATA       : {result.iata_number}")
        print(f"  Status     : {result.status}")
        print(f"  Name       : {result.trading_name}")
        print(f"  Country    : {result.country}")
        print(f"  Accredited : {result.accredited}")
        print(f"  Notes      : {result.notes}")
        print("----------------")
        return 0 if result.status in ("VALID", "INVALID") else 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
