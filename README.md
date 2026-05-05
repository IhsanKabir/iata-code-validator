# IATA Code Validator

Bulk-validates IATA Numeric Codes against IATA's public CheckACode page
([store.iata.org/ieccacfree](https://store.iata.org/ieccacfree)) and
writes the agency details back to Excel.

For travel-industry teams that need to verify many IATA agency codes
without paying for the licensed CheckACode Professional API.

> Distributed as a single Windows .exe — no Python install, no admin
> rights, no first-run download. Three colleagues can run independent
> copies on three laptops in parallel without coordination.

---

## Output

A new timestamped Excel file is written for each run:
`iata_results_YYYYMMDD_HHMMSS.xlsx` with columns:

| IATA Number | Trading Name | Country | Accredited | Status | Checked At | Notes |
|---|---|---|---|---|---|---|

`Status` is `VALID`, `INVALID`, or `ERROR`. `Accredited` is `Y`, `N`, or blank.

---

## How to use the .exe

1. Download the latest `IATACodeValidator.exe` from the
   [Releases page](../../releases).
2. Double-click. Windows SmartScreen will warn the first time —
   click **More info → Run anyway** (one click, no admin password).
3. In the GUI:
   - Browse → pick the source Excel
   - Pick the Sheet (auto-detected if only one)
   - Pick the IATA column (auto-guessed by name)
   - Optional: enter Start row and End row to process a subset
   - Browse → pick the output folder
   - Click **Start**

A Chrome window opens. Most lookups complete unattended. Occasionally
a reCAPTCHA image puzzle appears — the tool plays a sound and brings
the GUI to front; just solve the puzzle in the Chrome window and the
tool continues automatically.

### Multi-laptop workflow

For larger workloads, split the IATA codes across machines using the
GUI's row-range filter so each laptop owns a non-overlapping range.
Each laptop is independent — different IPs, different browser sessions.

---

## Controls

- **Pause** — finish the current lookup, then wait. Resume to continue.
- **Stop** — exit cleanly. Already-saved rows are kept.
- **Local cache** — every successful lookup is stored in
  `%LOCALAPPDATA%\IATAChecker\cache.sqlite`. Re-running the same range
  skips already-known codes (no extra reCAPTCHA cost).
- **Crash-safe** — output is flushed every 10 rows; the cache holds
  every prior success, so re-running picks up where it left off.

---

## Build from source (developer)

Requires Python 3.13+ on Windows.

```cmd
pip install -r requirements.txt
set PLAYWRIGHT_BROWSERS_PATH=0
python -m patchright install chromium
```

Pre-download the Whisper tiny.en model into `assets/whisper_model/`:

```cmd
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8', download_root='assets/whisper_tmp')"
```

Then move the snapshot files (`config.json`, `model.bin`, `tokenizer.json`,
`vocabulary.txt`) flat into `assets/whisper_model/`.

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

### Run tests

```cmd
python -m pytest tests/ -v
```

### Smoke test against the live page

```cmd
python smoke_test.py 32302491
```

Expected output: `Status: VALID, Name: TRAVEL POINT PTE. LTD., Country: SINGAPORE, Accredited: Y`.

---

## Architecture

```
run_app.py / IATACodeValidator.exe
        │
        ▼
   src/main.py    ── points to bundled Chromium + Whisper, launches GUI
        │
        ▼
   src/gui.py     ── Tkinter app, runs validation in a worker thread
        │
        ├── src/validator.py    ── browser automation, reCAPTCHA flow
        ├── src/audio_solver.py ── faster-whisper audio CAPTCHA fallback
        ├── src/excel_io.py     ── openpyxl reader + streaming writer
        ├── src/cache.py        ── SQLite cache of prior lookups
        ├── src/parser.py       ── parses Valid/Invalid/Trading Name/Country
        └── src/config.py       ── selectors, timings, paths
```

Persistent state lives in `%LOCALAPPDATA%\IATAChecker\`:

- `browser_profile/` — persistent browser profile
- `cache.sqlite`     — successful lookups, keyed by IATA number
- `iata_checker.log` — runtime log (rotated at 5 MB)

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Tool says "result did not render in time" | Network may be flaky. Stop, wait a few minutes, resume — cache picks up where you left off. |
| `is not a valid IATA Numeric Code` for codes that should be valid | Confirm the column has 7-, 8-, 11-, or 12-digit codes (no leading apostrophes from Excel formatting). |
| Tool can't find the form input on a fresh page | The site changed. Update `SELECTOR_*` constants in [src/config.py](src/config.py). |
| Many manual prompts in a single session | Browser profile is "cold". Solve a few manually — after about 10 lookups the silent-pass rate climbs to ~95%. |

---

## Notes

- Uses IATA's public CheckACode page; respects normal usage patterns
  (sequential single-browser, human-paced delays, periodic backoff).
- For high-volume or commercial workloads, license IATA's
  CheckACode Professional API directly.

---

## License

Internal/private use. Not for redistribution.
