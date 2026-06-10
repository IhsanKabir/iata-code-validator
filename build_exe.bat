@echo off
REM ================================================================
REM Build IATACodeValidator.exe with PyInstaller
REM ================================================================
REM Bundles: patchright Chromium + faster-whisper tiny.en model.
REM Output:  dist\IATACodeValidator.exe (~380 MB)
REM
REM End-user laptops need:
REM   - NO Python install
REM   - NO admin access
REM   - NO internet on first launch (Chromium + Whisper bundled)
REM ================================================================

setlocal

echo [1/4] Installing build deps...
REM Build in a Python 3.13 venv. The pinned stack (matplotlib, faster-whisper,
REM etc.) has no prebuilt wheels for Python 3.14, and source-building them needs a
REM C compiler. Requires `py -3.13` (check with: py --list).
if not exist ".venv\Scripts\python.exe" py -3.13 -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo [2/4] Installing Chromium INTO patchright package (PLAYWRIGHT_BROWSERS_PATH=0)...
set PLAYWRIGHT_BROWSERS_PATH=0
python -m patchright install chromium
if errorlevel 1 goto :err

echo [3/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] Building one-file exe (this takes a few minutes)...
python -m PyInstaller --noconfirm IATACodeValidator.spec
if errorlevel 1 goto :err

echo.
echo ===============================================
echo  Build done.  dist\IATACodeValidator.exe
echo  Bundled: Chromium + whisper tiny.en model
echo ===============================================
echo.
goto :eof

:err
echo.
echo *** Build failed. See errors above.
exit /b 1
