"""Configuration constants."""

import os
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

OEP_PRESET_FILE = APP_DIR / "oep_presets.json"

# Optional Zenith host override (one line, e.g. https://asia.ttinteractive.com). The
# default `usba.` host is CloudFront-fronted and 504s on slow Dossier renders; the direct
# `asia.` origin waits them out. main.py reads this BEFORE the Zenith client modules import
# and sets ZENITH_BASE_URL so every endpoint URL picks it up.
ZENITH_HOST_FILE = APP_DIR / "zenith_host.txt"

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

# ---------------------------------------------------------------------------
# BD Overseas Movement tab (oep.gov.bd)
# ---------------------------------------------------------------------------

OEP_OUTPUT_COLUMNS_COUNTRY_SUMMARY = [
    "Rank",
    "Destination Country",
    "Total Employees",
    "Job Categories",
    "Share %",
]

OEP_OUTPUT_COLUMNS_COUNTRY_RAW = [
    "Country ID",
    "Country Name",
    "Job Category",
    "Total Employees",
]

OEP_OUTPUT_COLUMNS_DIVISION_SUMMARY = [
    "Rank",
    "Division",
    "Total Employees",
    "Districts",
    "Share %",
]

OEP_OUTPUT_COLUMNS_DIVISION_RAW = [
    "Division",
    "District",
    "Total Employees",
]

OEP_OUTPUT_COLUMNS_CATEGORY_SUMMARY = [
    "Rank",
    "Job Category",
    "Total Employees",
    "Destination Countries",
    "Share %",
]

OEP_OUTPUT_COLUMNS_GENDER_SUMMARY = [
    "Rank",
    "Destination Country",
    "Male",
    "Female",
    "Other",
    "Total",
    "Female %",
]

# ---------------------------------------------------------------------------
# Zenith Customer Lookup tab
# ---------------------------------------------------------------------------

ZENITH_OUTPUT_COLUMNS = [
    "Customer ID",
    "Status",
    "Title",
    "First Name",
    "Middle Name",
    "Last Name",
    "Date of Birth",
    "Email",
    "Home Phone",
    "Home Phone (Intl)",
    "Mobile Phone",
    "Mobile Phone (Intl)",
    "Office Phone",
    "Nationality",
    "Language",
    "Spoken Language",
    "Address",
    "City",
    "Postal Code",
    "Country",
    "Registration Date",
    "Error",
    "Checked At",
]

ZENITH_CACHE_DB = APP_DIR / "zenith_cache.sqlite"

# Bulk Mailer tab — send-log so re-runs skip already-sent rows.
MAILER_LOG_DB = APP_DIR / "mailer_log.sqlite"

# Zenith Flight Loads sub-tab
ZENITH_FLIGHT_OUTPUT_COLUMNS = [
    "Flight Number",
    "Day",
    "Flight Date",
    "Departure Time",
    "Aircraft",
    "Registration",
    "Total Tickets Issued",
    "Leg Route",
    "Origin",
    "Destination",
    "Leg Local Time",
    "Cabin",
    "Tickets Issued",
    "Tickets WL",
    "Seats Confirmed",
    "Seats Options",
    "Seats WL",
    "Seats Available",
    "Inventory Status",
    "Comments",
]

# ---------------------------------------------------------------------------
# Zenith -> Reports sub-tab (download-only; gated by a 14-day rotating password)
# ---------------------------------------------------------------------------
# Pre-built analytics workbooks are produced by the separate pipeline and shared
# read-only to each manager (per-folder NTFS ACLs are the privacy boundary). This
# tab only authenticates + downloads — no data engine is bundled.
#
# Point this at the manager's reports folder. Override per-machine with the
# USBA_REPORTS_DIR environment variable; set the default below to the real UNC
# share once provisioned (currently the analyst's local publish folder).
REPORTS_DIR = Path(os.environ.get("USBA_REPORTS_DIR", r"E:\Analysis\shared_reports"))
REPORTS_AUTH_FILE = REPORTS_DIR / "auth.json"

# ---------------------------------------------------------------------------
# Zenith -> PNR History / Audit sub-tab (investigative; access-gated)
# ---------------------------------------------------------------------------
# Bulk-scrapes each PNR's event-history tabs (Tickets / Changes / Reissue) and
# mines them for trends + potential misuse. Sensitive: rows name staff logins,
# PAX contact details, and payment/transaction ids. The raw scrape is cached as
# gzipped HTML per (dossier, tab) so a parser fix never re-hits Zenith.
#
# The cache holds PII + accusatory data — point USBA_PNR_HISTORY_DB at an ACL'd
# share in production; it falls back to the local app dir for development.
ZENITH_PNR_HISTORY_CACHE_DB = Path(
    os.environ.get("USBA_PNR_HISTORY_DB", str(APP_DIR / "zenith_pnr_history.sqlite"))
)
# Re-scrape a dossier's history if the cached copy is older than this.
PNR_HISTORY_STALE_AFTER_DAYS = 3
# Hard per-run request-budget ceiling (the GDS is fragile — recently 504-stormed).
PNR_HISTORY_MAX_REQUESTS = 5000
# Conservative throughput defaults (lighter than the per-PNR lookup, which is 1 GET).
PNR_HISTORY_CONCURRENCY = 2
PNR_HISTORY_DELAY_S = 1.5

# ---------------------------------------------------------------------------
# Traffic tab — air-traffic / passenger-movement, multi-source (Malaysia, BTS, …)
# ---------------------------------------------------------------------------
# One unified TrafficRow schema feeds one Treeview + one Excel writer, so every
# source exports the same Raw sheet (the per-view Summary sheet is built in
# excel_io.write_traffic_report).
TRAFFIC_OUTPUT_COLUMNS_RAW = [
    "Source", "Country", "Airport", "Origin", "Destination", "Carrier",
    "Period", "Granularity", "Direction", "Flight Type",
    "Metric", "Unit", "Value", "Nationality", "Gender", "Raw Label",
]
