"""Configuration constants."""

from pathlib import Path

# Target site
IATA_URL = "https://store.iata.org/ieccacfree"

# DOM selectors (update if IATA changes the page)
SELECTOR_INPUT = 'input[name="cacnum"], input[type="text"]:not([style*="display:none"])'
SELECTOR_VALIDATE_BTN = 'button:has-text("Validate"), input[value="Validate"]'
SELECTOR_RECAPTCHA_FRAME = 'iframe[src*="recaptcha/api2/anchor"]'
SELECTOR_RECAPTCHA_CHECKBOX = '#recaptcha-anchor'
SELECTOR_RECAPTCHA_CHECKED = '#recaptcha-anchor[aria-checked="true"]'
SELECTOR_RECAPTCHA_CHALLENGE_FRAME = 'iframe[src*="recaptcha/api2/bframe"]'
SELECTOR_RESULT_BLOCK = 'text=/is a Valid IATA|is not a valid|Invalid/i'

# Timings (seconds)
PAGE_LOAD_TIMEOUT = 30
CAPTCHA_AUTOPASS_WAIT = 6      # how long to wait for green check after clicking
CAPTCHA_MANUAL_WAIT = 300      # how long to wait for human to solve image challenge
RESULT_WAIT = 15               # how long to wait for result to render
DELAY_BETWEEN_LOOKUPS = (1.5, 3.5)  # random delay range to look human

# Stealth: clear *.google.com / *.recaptcha.net cookies every N lookups so
# the reCAPTCHA per-cookie risk score doesn't accumulate. Empirically the
# checkbox-only silent pass returns to ~95% after a clean rotation.
COOKIE_ROTATE_EVERY = 20

# Paths
APP_DIR = Path.home() / "AppData" / "Local" / "IATAChecker"
PROFILE_DIR = APP_DIR / "browser_profile"
CACHE_DB = APP_DIR / "cache.sqlite"
LOG_FILE = APP_DIR / "iata_checker.log"

# Output
OUTPUT_COLUMNS = [
    "IATA Number",
    "Trading Name",
    "Country",
    "Accredited",
    "Status",
    "Checked At",
    "Notes",
]

# Status values
STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_ERROR = "ERROR"
STATUS_CACHED = "CACHED"

# ---------------------------------------------------------------------------
# BD Travel Agency Lookup tab
# ---------------------------------------------------------------------------

BD_CACHE_DB = APP_DIR / "bd_agencies.sqlite"
BD_CACHE_STALE_AFTER_DAYS = 7  # warn the user when the cached list is older

BD_OUTPUT_COLUMNS_LOOKUP = [
    "Searched Input",
    "Match Method",
    "Matched Field",
    "Match Score",
    "Agency Name",
    "License No",
    "Email",
    "Mobile",
    "Website",
    "Address",
    "License Expiry",
    "Status",
    "Other Matches",
]

BD_OUTPUT_COLUMNS_FULL = [
    "Agency Name",
    "License No",
    "Email",
    "Mobile",
    "Website",
    "Address",
    "License Expiry",
    "Status",
]
