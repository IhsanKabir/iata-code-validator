# IATA Code Validator

A four-tab Windows desktop tool for travel-industry teams. Shipped as a
single .exe — no Python, no admin rights, no first-run download.

> **What started as a single-purpose tool** (bulk IATA code lookups)
> has grown into a small productivity suite. The .exe name and binary
> stayed the same so existing users keep their auto-updater pointed at
> the same release stream; only the inside got bigger.

---

## Tabs at a glance

| Tab | What it does | Where the data comes from |
| --- | --- | --- |
| **IATA Code Validator** | Bulk-validates IATA Numeric Codes; writes agency name, country, accreditation back to Excel. Handles reCAPTCHA. | IATA's public CheckACode page |
| **BD Travel Agency Lookup** | Bulk-resolves Bangladesh travel-agency licence numbers to full agency records, or exports the full agency directory. | regtravelagency.gov.bd |
| **BD Overseas Movement** | 8 views of Bangladesh's overseas-employment outflow: top destinations, source districts, job categories, gender breakdown, monthly time series (chart), country×division pivot, full report, country×division×month. | oep.gov.bd |
| **Zenith** | Four sub-tabs against US-Bangla's Zenith reservation system. | asia.ttinteractive.com (login required) |

### Zenith sub-tabs

| Sub-tab | What it does |
| --- | --- |
| **Customer Lookup** | Bulk-extracts customer details (name, email, phone, address) for a list of Customer IDs. SQLite cache; resume-safe. |
| **Flight Loads** | Pulls View-PNL data (tickets issued/WL, seats confirmed/options/WL/available, load %, inventory status) for a date range, one row per (flight, leg, cabin). |
| **Flight History Analyzer** | Reads ModificationHistory `.xls` exports → multi-sheet audit (class downgrades, downgrade leaders, G-class issuance, agent activity, revenue mgmt, suspicious activity, raw events). Optional Flight Loads input adds a load-factor-aware "Downgrade Justification" sheet with tunable QUESTIONABLE / SITUATIONAL / JUSTIFIED thresholds. Includes a "Download from Zenith" button to auto-fetch fresh logs. |
| **PNR Bulk Lookup** | Takes an Excel column of PNRs → outputs Excel with full booked route, flown route (refund/void-aware), customer, traveler surname, phone, payment method, pax count, per-segment fares & status. Cached. |

---

## How to use the .exe

1. Download the latest `IATACodeValidator.exe` from the
   [Releases page](../../releases).
2. Double-click. Windows SmartScreen will warn the first time —
   click **More info → Run anyway** (one click, no admin password).
3. Sign in with your Google account when prompted (per-team licensing).
4. Pick a tab and follow the in-tab instructions; every tab writes a
   timestamped Excel file to a folder you choose.

### Per-tab notes

**IATA Code Validator** — pick input Excel, sheet, IATA column,
optional row range, output folder, click **Start**. A Chrome window
opens; reCAPTCHAs are handled silently in most cases, with an audio
fallback when needed. Sound + window pop indicate manual help is
required.

**BD Travel Agency Lookup** — click **Refresh** once to cache the full
agency directory (~6k records). Then either *Lookup names from Excel*
or *Export full list*. Match fields: Agency Name / License Number /
Address (any subset). The full-list export can be filtered by status
(active vs expired-pending).

**BD Overseas Movement** — pick a date range and a view, click **Run**.
For monthly time series and pivot views, also pick countries from the
multi-select (Top 5 / 10 / 20 / All buttons). Filter presets are saved
to `oep_presets.json`.

**Zenith** — sign in once at the top of the tab; the same session is
shared across all four sub-tabs. Per-sub-tab inputs vary; each has a
sane defaults pre-fill.

### Multi-laptop workflow

For the IATA tab specifically, split codes across machines using the
row-range filter so each laptop owns a non-overlapping range. Each
laptop is independent (different IPs, different browser sessions).

---

## Controls

- **Pause / Resume / Stop** — long-running tabs honour these
  cooperatively; partial output is always preserved.
- **Local caches** — every tab persists what it can:
  - IATA: `cache.sqlite` (per-code)
  - BD Travel Agency: `bd_cache.sqlite` (full directory + match index)
  - Zenith Customer: `zenith_cache.sqlite` (per-Customer-ID)
  - Zenith PNR: `zenith_pnr.sqlite` (per-PNR Dossier)
- **Crash-safe** — outputs flush periodically; re-runs pick up where
  the previous run left off thanks to the caches.

---

## Build from source (developer)

Requires Python 3.13+ on Windows.

```cmd
pip install -r requirements.txt
set PLAYWRIGHT_BROWSERS_PATH=0
python -m patchright install chromium
```

Pre-download the Whisper tiny.en model into `assets/whisper_model/`
(needed only by the IATA tab's audio reCAPTCHA fallback):

```cmd
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='assets/whisper_tmp')"
```

Then move the snapshot files (`config.json`, `model.bin`,
`tokenizer.json`, `vocabulary.txt`) flat into `assets/whisper_model/`.

Build the .exe:

```cmd
build_exe.bat
```

Output: `dist\IATACodeValidator.exe` (~380 MB — bundles Chromium and
the Whisper model so the .exe is portable and offline).

### Run from source instead

```cmd
python run_app.py
```

Or use the bundled launcher (auto-picks the right Python install):

```powershell
.\launch.ps1
```

### Run tests

```cmd
python -m pytest tests/ -v
```

178+ tests covering all four tabs, including parser regression tests
for legacy ASP markup quirks (multi-stop flight loads, whitespace-padded
flight headers, etc.).

---

## Architecture

```text
run_app.py / IATACodeValidator.exe
        │
        ▼
   src/main.py        ── bundled Chromium + Whisper, launches GUI
        │
        ▼
   src/gui.py         ── Tkinter app, four tabs, one worker thread per tab
        │
        ├── IATA TAB
        │     ├── src/validator.py        ── browser automation, reCAPTCHA
        │     ├── src/audio_solver.py     ── faster-whisper audio CAPTCHA
        │     ├── src/cache.py            ── per-code SQLite cache
        │     └── src/parser.py           ── CheckACode response parser
        │
        ├── BD AGENCY TAB
        │     ├── src/bd_agency_client.py ── regtravelagency.gov.bd client
        │     ├── src/bd_cache.py         ── agency directory cache
        │     └── src/bd_matcher.py       ── fuzzy match index
        │
        ├── BD OVERSEAS TAB
        │     ├── src/oep_client.py       ── oep.gov.bd client
        │     └── src/oep_presets.py      ── JSON filter-preset store
        │
        ├── ZENITH TABS
        │     ├── src/zenith_client.py            ── login + customer + flight loads
        │     ├── src/zenith_cache.py             ── customer cache
        │     ├── src/zenith_history_parser.py    ── ModificationHistory .xls parser
        │     ├── src/zenith_history_analyzer.py  ── 7-sheet audit
        │     ├── src/zenith_history_downloader.py── per-flight .xls fetcher
        │     ├── src/zenith_loads_index.py       ── load-factor lookup
        │     ├── src/zenith_pnr_client.py        ── PNR Dossier resolver
        │     └── src/zenith_pnr_cache.py         ── PNR cache
        │
        ├── src/excel_io.py     ── shared openpyxl reader + writers
        ├── src/auth.py         ── Google OAuth (per-team licensing)
        ├── src/updater.py      ── in-app auto-updater (GitHub Releases)
        └── src/config.py       ── selectors, timings, paths
```

Persistent state lives in `%LOCALAPPDATA%\IATAChecker\`:

| File | Contents |
| --- | --- |
| `browser_profile/` | Chromium profile (cookies, recaptcha warm-up) |
| `cache.sqlite` | IATA lookups, keyed by IATA number |
| `bd_cache.sqlite` | BD agency directory + match index |
| `zenith_cache.sqlite` | Zenith customer records, keyed by Customer ID |
| `zenith_pnr.sqlite` | Zenith PNR Dossiers, keyed by PNR code |
| `oep_presets.json` | BD Overseas Movement saved filter presets |
| `iata_checker.log` | Rotating runtime log (5 MB × 2 backups) |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Tool says "result did not render in time" (IATA) | Network may be flaky. Stop, wait a few minutes, resume — cache picks up where you left off. |
| `is not a valid IATA Numeric Code` for codes that should be valid | Confirm the column has 7-, 8-, 11-, or 12-digit codes (no leading apostrophes from Excel formatting). |
| Tool can't find the form input on a fresh page (IATA) | The site changed. Update `SELECTOR_*` constants in [src/config.py](src/config.py). |
| Many manual reCAPTCHA prompts in one session | Browser profile is "cold". Solve a few manually — after ~10 lookups the silent-pass rate climbs to ~95%. |
| Zenith returns HTTP 500 on history download | The session may have lapsed (Sign out → Sign in again). If still failing, the export URL or form layout may have changed — capture a HAR and update `src/zenith_history_downloader.py`. |
| OEP returns HTTP 401 mid-run | IP rate-limited. Wait 5-10 min, then re-run with a tighter date range / smaller country selection. The client already throttles + retries once. |
| Flight Loads / Flight History audit run is slow | Both honour caches; first run on a fresh date range pays the network cost, subsequent runs are instant. |

---

## Notes & limits

- The IATA tab uses IATA's public CheckACode page and respects normal
  usage patterns (sequential single-browser, human-paced delays,
  periodic backoff). For commercial high-volume workloads, license
  IATA's CheckACode Professional API directly.
- The Zenith tabs talk to a private US-Bangla system and require a
  valid Zenith user account. Read-only — no PNR/customer mutations.
- Auto-updater fetches the latest release from this repo; users see a
  one-click "Update available" prompt the next time they launch.

---

## License

Internal/private use. Not for redistribution.
