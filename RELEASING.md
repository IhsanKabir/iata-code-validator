# Releasing TravelOpsConsole

**GitHub Actions builds every release.** Pushing a `v*.*.*` tag runs
`.github/workflows/build-release.yml`, which:

1. installs `requirements.txt` (including the vendored reporting wheel in `wheels/`) and runs the tests;
2. writes `src/_build_config.py` from the repository secrets (Google client id, API base URL);
3. builds the onefile exe with `IATACodeValidator.spec`;
4. copies it to the legacy name `IATACodeValidator.exe` (installs up to v1.39.0 update by that name);
5. writes a `.sha256` sidecar for each exe: the bare 64-character hash, no newline;
6. attaches all four files to the release, replacing any assets with the same names.

**Never upload the exes or `.sha256` files by hand.** The workflow replaces same-named assets,
so a manual upload is either overwritten or ends up racing the workflow. Earlier releases whose
assets "came out the wrong size" were the workflow replacing a manual upload, not a broken upload.
The update service (`/api/v1/app/latest`) reads the first word of the `.sha256` asset, and the app
checks the downloaded exe against it before swapping.

## Steps

1. **Reporting wheel** (only when the reporting package changed): bump the version in the reporting
   package (both `pyproject.toml` and `__init__.py`; its build script refuses a mismatch), build the
   wheel, copy it into `wheels/`, install it into `.venv`
   (`pip install --force-reinstall --no-deps wheels/<wheel>`), and update the pin in
   `requirements.txt` (the file starts with a UTF-8 BOM; keep it).
2. **App version**: bump `__version__` in `src/__init__.py`.
3. **Tests**: `.venv\Scripts\python.exe -m pytest -q`.
4. **Optional local build** to check before tagging:
   `.venv\Scripts\python.exe -m PyInstaller IATACodeValidator.spec --noconfirm --distpath dist_new --workpath build_new`,
   then `dist_new\TravelOpsConsole.exe --selftest` and read
   `%LOCALAPPDATA%\IATAChecker\selftest_result.txt` (frozen build, reporting version, Instant Reports set).
   This build is for checking only; it is not what ships.
5. **Commit and push `main`.** Check `git log origin/main..` first: any unpushed commits ship in
   the release too, so name them in the notes.
6. **Release**: `gh release create v<version> --target main --title "TravelOpsConsole v<version>" --notes-file <notes>`.
   This creates the tag and starts the workflow (about 8 minutes, `gh run list`). The notes you
   give are kept.
7. **Verify what was published**:
   - `gh release download v<version> --pattern "TravelOpsConsole.exe*"`;
   - the exe's `Get-FileHash -Algorithm SHA256` must equal the `.sha256` content;
   - `IATACodeValidator.exe.sha256` must hold the same hash;
   - run the downloaded exe with `--selftest` and check the reporting version and report list.

## Adding a report to Instant Reports

A builder in the reporting package's `builders/`, an entry in its `registry.py`, a label in
`instant.INSTANT_REPORTS`, a branch in `instant.build_reports`, and (if it takes options) a row in
`instant_ui.py`. The self-test prints the Instant Reports set, which confirms the report is in the
frozen build.
