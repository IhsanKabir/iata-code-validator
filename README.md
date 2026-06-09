# Travel Ops Console

A five-tab Windows desktop tool for travel-industry teams. Single `.exe`
(`IATACodeValidator.exe`), no Python or admin required.

[Download the latest release →](../../releases)

---

## Tabs

| Tab | One-liner | Source |
| --- | --- | --- |
| **IATA Code Validator** | Validate IATA Numeric Codes from an Excel column | IATA CheckACode |
| **BD Travel Agency Lookup** | Resolve / export Bangladesh travel-agency records | regtravelagency.gov.bd |
| **BD Overseas Movement** | 8 views of BD outflow (destinations, divisions, time series, pivots…) | oep.gov.bd |
| **Zenith** | 4 sub-tabs against US-Bangla Zenith — *login required* | asia.ttinteractive.com |
| **Bulk Mailer** | Mail-merge reports — per-recipient attachments, one click | Outlook / Graph / SMTP |

**Zenith sub-tabs:** *Customer Lookup* · *Flight Loads* · *Flight History Analyzer* · *PNR Bulk Lookup*

---

## Quick start

1. Download `IATACodeValidator.exe` from [Releases](../../releases).
2. Double-click. Click *More info → Run anyway* on the SmartScreen warning.
3. Sign in with Google when prompted.
4. Pick a tab. Pick input + output. Click **Run** / **Start**.

The scraper tabs each write a timestamped `.xlsx` into the output folder you
choose. **Bulk Mailer** works the other way round: point it at a mapping
sheet (Email · Name · File · CC · BCC) plus an attachments folder, **Preview**
to validate every row, then **Draft** for review or **Send**.

---

<details>
<summary><strong>Persistent state</strong> (in <code>%LOCALAPPDATA%\IATAChecker\</code>)</summary>

| File | Contents |
| --- | --- |
| `cache.sqlite` | IATA lookups |
| `bd_agencies.sqlite` | BD agency directory + match index |
| `zenith_cache.sqlite` | Zenith customer records |
| `zenith_pnr.sqlite` | Zenith PNR Dossiers |
| `oep_presets.json` | OEP filter presets |
| `mailer_log.sqlite` | Bulk Mailer send-log (resume-safe, skips already-sent) |
| `mailer_outlook_account.txt` | Last-used Outlook send-from account |
| `graph_token.bin` | Microsoft Graph token cache (mailer device-code sign-in) |
| `browser_profile/` | Chromium profile |
| `iata_checker.log` | Rotating log (5 MB × 2) |

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Symptom | Fix |
| --- | --- |
| IATA: "result did not render in time" | Stop, wait, resume — cache picks up. |
| IATA: many manual CAPTCHAs | Warm the browser profile — silent-pass climbs to ~95% after ~10 lookups. |
| OEP: HTTP 401 mid-run | IP rate-limited. Wait 5-10 min; retry with smaller scope. |
| Zenith: HTTP 500 on history download | Sign out → Sign in. If still failing, site likely changed. |
| Zenith / Flight Loads slow | First run pays the network cost; caches make subsequent runs instant. |
| Mailer: drafts land in the wrong mailbox | Pick the send-from account in the dropdown; drafts move to that account's Drafts. |
| Mailer: "admin approval required" (Graph) | Tenant blocks self-service consent — use the Outlook or SMTP transport instead. |
| Mailer: provider throttles a large run | Raise the delay slider; re-run safely — already-sent rows are skipped. |

</details>

<details>
<summary><strong>Build from source</strong></summary>

Requires Python 3.13+ on Windows.

```cmd
pip install -r requirements.txt
set PLAYWRIGHT_BROWSERS_PATH=0
python -m patchright install chromium
```

Pre-download the Whisper tiny.en model (IATA tab's audio fallback):

```cmd
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='assets/whisper_tmp')"
```

Move `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt`
flat into `assets/whisper_model/`. Then:

```cmd
build_exe.bat
```

Output: `dist\IATACodeValidator.exe` (~380 MB).

Run from source:

```cmd
python run_app.py
```

Or `.\launch.ps1` (auto-picks the right Python).

Tests: `python -m pytest tests/ -v` (235+ tests).

</details>

<details>
<summary><strong>Architecture</strong></summary>

```text
src/main.py            ── launches GUI
src/gui.py             ── Tkinter, 5 tabs, 1 worker thread per tab
src/auth.py            ── Google OAuth (per-team licensing)
src/updater.py         ── in-app auto-update from Releases
src/excel_io.py        ── shared openpyxl I/O
src/config.py          ── selectors, timings, paths

IATA tab
    validator.py · audio_solver.py · cache.py · parser.py

BD Agency tab
    bd_agency_client.py · bd_cache.py · bd_matcher.py

BD Overseas tab
    oep_client.py · oep_presets.py

Zenith tabs
    zenith_client.py             ── login + customer + flight loads
    zenith_cache.py              ── customer cache
    zenith_history_parser.py     ── .xls log parser
    zenith_history_analyzer.py   ── 7-sheet audit
    zenith_history_downloader.py ── per-flight log fetcher
    zenith_loads_index.py        ── load-factor lookup
    zenith_pnr_client.py         ── PNR Dossier resolver
    zenith_pnr_cache.py          ── PNR cache

Bulk Mailer tab
    mailer_io.py     ── mapping reader + {name} templating + row validation
    mailer_client.py ── Outlook COM + SMTP backends, MX auto-detect
    graph_mailer.py  ── Microsoft Graph backend (device-code sign-in)
    mailer_log.py    ── resume-safe send-log
```

</details>

---

## Notes

- IATA tab respects normal CheckACode usage patterns. For commercial
  high-volume work, license IATA's CheckACode Professional API.
- Zenith tabs are read-only against a private US-Bangla system; require
  a valid Zenith account.
- Bulk Mailer is provider-agnostic — works with Outlook (desktop COM),
  Microsoft Graph (device-code sign-in), or any SMTP host. No recipient
  cap; large runs are bounded only by your provider's daily send limit.
- Auto-updater checks GitHub Releases on launch.

**License:** © 2025-2026 A K M Ihsan Kabir. All Rights Reserved. See [LICENSE](LICENSE).
