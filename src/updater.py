"""In-app self-updater for the IATA Code Validator .exe.

Workflow:
  1. check_for_update() asks GitHub Releases API for the latest non-draft
     non-prerelease tag and compares to the running app's __version__.
  2. download_update(url, on_progress) streams the new .exe into
     %LOCALAPPDATA%\\IATAChecker\\update_pending.exe.
  3. apply_update() writes a tiny Windows batch helper that:
       - waits 2 seconds for this process to exit
       - replaces the running .exe with update_pending.exe
       - relaunches the new .exe
       - deletes itself
     then spawns the batch and exits the current Python process.

Only runs in frozen (PyInstaller) mode — when running from source we
just log a "would update" message and return False.

Network errors return None / False instead of raising — the GUI shows
the error from the result, not a traceback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "IhsanKabir/iata-code-validator"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "IATACodeValidator.exe"

USER_AGENT = f"IATACodeValidator-Updater/{__version__}"


@dataclass(frozen=True)
class UpdateInfo:
    latest_version: str
    download_url: str
    notes: str
    is_newer: bool
    sha256: str = ""  # from the backend manifest; verified before the swap


# ---------------------------------------------------------------------------
# Version comparison (semver-ish)
# ---------------------------------------------------------------------------


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


# ---------------------------------------------------------------------------
# Primary channel — the app's own authenticated backend
# ---------------------------------------------------------------------------
# Corporate networks frequently block GitHub (api.github.com /
# objects.githubusercontent.com) but DO allow the backend the app already
# signs in to. So we check the backend FIRST and fall back to GitHub. The
# backend returns {version, notes, download_url, sha256}; the download_url
# points at the backend's own (reachable) download route, and the sha256 is
# verified before the new exe is ever staged.


def _check_backend_update(timeout: int = 10) -> UpdateInfo | None:
    """Ask the app's authenticated backend for the latest release.

    Returns None on ANY problem (not signed in, endpoint absent / 404,
    network or proxy error, malformed JSON) so the caller silently falls
    back to GitHub — behaviour is unchanged until the backend ships the
    endpoint.
    """
    try:
        from . import auth
    except Exception:  # noqa: BLE001
        return None
    base = (getattr(auth, "API_BASE_URL", "") or "").rstrip("/")
    try:
        token = auth.get_token()
    except Exception:  # noqa: BLE001
        token = None
    if not base or not token:
        return None
    req = urllib.request.Request(
        f"{base}/api/v1/app/latest",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-User-Session": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure => fall back to GitHub
        log.info("Backend update check unavailable (%s); trying GitHub", exc)
        return None
    version = str(data.get("version") or "").strip()
    download_url = str(data.get("download_url") or "").strip()
    if not version or not download_url:
        return None
    return UpdateInfo(
        latest_version=version.lstrip("v"),
        download_url=download_url,
        notes=str(data.get("notes") or ""),
        is_newer=_is_newer(version, __version__),
        sha256=str(data.get("sha256") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Step 1 — check_for_update
# ---------------------------------------------------------------------------


def check_for_update(timeout: int = 15) -> UpdateInfo | None:
    """Hit GitHub releases. Returns UpdateInfo or None on any failure.

    `is_newer` indicates whether the user should be offered an update.
    Even when not newer, we still return an UpdateInfo so the GUI can
    show "You're up to date."

    Tries the app's authenticated backend FIRST (reachable where GitHub is
    blocked), then falls back to GitHub Releases.
    """
    backend = _check_backend_update(timeout=min(timeout, 10))
    if backend is not None:
        return backend

    # FALLBACK: GitHub Releases — works for users who can reach GitHub.
    req = urllib.request.Request(
        GITHUB_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("GitHub releases HTTP %s: %s", exc.code, exc.reason)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub releases fetch failed: %s", exc)
        return None

    if data.get("draft") or data.get("prerelease"):
        return None

    tag = (data.get("tag_name") or "").strip()
    notes = data.get("body") or ""
    asset = next(
        (a for a in (data.get("assets") or []) if a.get("name") == ASSET_NAME),
        None,
    )
    if not tag or not asset:
        log.warning("Latest release has no tag or no %s asset", ASSET_NAME)
        return None
    return UpdateInfo(
        latest_version=tag.lstrip("v"),
        download_url=asset.get("browser_download_url", ""),
        notes=notes,
        is_newer=_is_newer(tag, __version__),
    )


# ---------------------------------------------------------------------------
# Step 2 — download_update
# ---------------------------------------------------------------------------


def _staging_dir() -> Path:
    """%LOCALAPPDATA%\\IATAChecker for staging the new exe."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    d = base / "IATAChecker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_pending_path() -> Path:
    return _staging_dir() / "update_pending.exe"


ProgressCallback = Callable[[int, int], None]


def _ensure_free_space(folder: Path, needed_bytes: int) -> None:
    """Raise OSError if `folder`'s volume can't hold the download + headroom."""
    if needed_bytes <= 0:
        return
    try:
        free = shutil.disk_usage(str(folder)).free
    except OSError:
        return  # can't determine — let the write attempt surface any real error
    required = needed_bytes + 100 * 1024 * 1024  # staged exe + swap headroom
    if free < required:
        raise OSError(
            f"Not enough disk space for the update: need ~{required // (1024 * 1024)} MB, "
            f"only {free // (1024 * 1024)} MB free on {folder}."
        )


def _download_headers(url: str) -> dict[str, str]:
    """Headers for the asset download.

    Our own backend gates the download with the same session token the app
    uses everywhere else, so attach X-User-Session when (and only when) the
    URL points at the backend host. GitHub / presigned storage URLs get just
    the User-Agent.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        from . import auth
        base = (getattr(auth, "API_BASE_URL", "") or "").rstrip("/")
        if base and url.startswith(base):
            token = auth.get_token()
            if token:
                headers["X-User-Session"] = token
    except Exception:  # noqa: BLE001 — auth is optional; download still tries
        pass
    return headers


def download_update(
    url: str,
    on_progress: ProgressCallback | None = None,
    timeout: int = 600,
    expected_sha256: str = "",
) -> Path:
    """Stream the .exe asset to the staging directory and verify it.

    `on_progress(downloaded_bytes, total_bytes)` is called every chunk —
    the GUI uses it to drive a progress bar. Total may be 0 if the
    server doesn't send a Content-Length.

    When `expected_sha256` is supplied (from the backend manifest) the
    downloaded bytes are hashed and compared before the file is staged; a
    mismatch discards the download and raises, so a corrupt or tampered
    binary can never reach the swap step.
    """
    target = update_pending_path()
    tmp = target.with_suffix(".part")
    if tmp.exists():
        tmp.unlink(missing_ok=True)

    req = urllib.request.Request(url, headers=_download_headers(url))
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            _ensure_free_space(target.parent, total)
            downloaded = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)  # 256 KB
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if on_progress is not None:
                        try:
                            on_progress(downloaded, total)
                        except Exception:  # noqa: BLE001 — never let UI crash the download
                            pass
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    # Integrity gate: never stage an exe that will replace the running one
    # unless its hash matches the manifest.
    if expected_sha256:
        actual = hasher.hexdigest().lower()
        if actual != expected_sha256.strip().lower():
            tmp.unlink(missing_ok=True)
            raise ValueError(
                "Update integrity check failed (SHA-256 mismatch) — download "
                "discarded; the running app was left untouched."
            )

    # Atomic-ish rename — partial -> final.
    if target.exists():
        target.unlink(missing_ok=True)
    tmp.replace(target)
    return target


# ---------------------------------------------------------------------------
# Step 3 — apply_update
# ---------------------------------------------------------------------------


def _running_exe_path() -> Path | None:
    """Path to the running .exe when frozen by PyInstaller, else None."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


_SWAP_BAT_TEMPLATE = textwrap.dedent("""\
    @echo off
    REM Swap the running IATA Code Validator with the freshly-downloaded copy.
    REM Spawned by updater.apply_update() right before the running app exits.

    setlocal enableextensions

    REM 1. Wait briefly for the running .exe to release its file handle.
    ping 127.0.0.1 -n 3 > nul

    set "STAGED={staged}"
    set "TARGET={target}"
    set "OLDBAK=%TARGET%.old"

    REM 2. Try to move the running exe out of the way (handles AV/file locks
    REM    by retrying a few times).
    set /a tries=0
    :try_move
        if exist "%OLDBAK%" del /f /q "%OLDBAK%" > nul 2>&1
        move /y "%TARGET%" "%OLDBAK%" > nul 2>&1
        if exist "%OLDBAK%" goto have_bak
        set /a tries+=1
        if %tries% geq 8 goto fail_move
        ping 127.0.0.1 -n 2 > nul
        goto try_move

    :have_bak
        REM 3. Move the staged exe into place.
        move /y "%STAGED%" "%TARGET%" > nul 2>&1
        if not exist "%TARGET%" goto fail_install

        REM 4. Relaunch and clean up.
        start "" "%TARGET%"
        del /f /q "%OLDBAK%" > nul 2>&1
        goto cleanup

    :fail_move
        REM Could not move the running exe. Bail out with a popup and
        REM leave the staged copy in place so the user can replace
        REM manually. The original keeps working.
        msg * "Update failed: could not replace IATACodeValidator.exe.{newline}"^
            "Please close the app and copy:{newline}"^
            "%STAGED%{newline}over %TARGET%."
        goto cleanup

    :fail_install
        REM Move worked the wrong way — restore the backup.
        if exist "%OLDBAK%" move /y "%OLDBAK%" "%TARGET%" > nul 2>&1
        msg * "Update failed during install. Reverted to the previous version."
        goto cleanup

    :cleanup
        del /f /q "%~f0" > nul 2>&1
        endlocal
""")


def _write_swap_script(staged: Path, target: Path) -> Path:
    bat_path = _staging_dir() / "update_swap.bat"
    bat_path.write_text(
        _SWAP_BAT_TEMPLATE.format(
            staged=str(staged),
            target=str(target),
            newline="\\n",
        ),
        encoding="utf-8",
    )
    return bat_path


def apply_update_and_exit() -> bool:
    """Spawn the swap helper, then call sys.exit().

    Returns False (and does NOT exit) only when:
      - we're not running as a frozen exe
      - the staged update file is missing
    Otherwise this function never returns; it terminates the process.
    """
    target = _running_exe_path()
    staged = update_pending_path()
    if target is None:
        log.warning("apply_update_and_exit: not frozen — skipping swap")
        return False
    if not staged.exists():
        log.warning("apply_update_and_exit: staged file missing")
        return False

    bat = _write_swap_script(staged=staged, target=target)
    log.info("Spawning update swap helper: %s", bat)

    # `start` detaches the bat from this process; CREATE_NEW_PROCESS_GROUP
    # ensures it survives our exit.
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        creationflags=creationflags,
        close_fds=True,
    )
    # Give the helper a moment to start before this process holds onto the
    # file lock for one more breath.
    time.sleep(0.3)
    sys.exit(0)
    return True  # not reached
