"""Optional Whisper-based audio fallback for reCAPTCHA v2 image challenges.

When an image puzzle appears, click the audio button, download the audio
clip, transcribe with faster-whisper (CPU, int8 tiny.en — ~75 MB), enter
the transcript, and submit.

This is a *fallback*, not a replacement: Google rate-limits the audio
button per IP after just a few solves. After about 3-5 audio solves the
button shows "Your computer or network may be sending automated queries"
and this function returns False — the validator then falls back to the
human-solve path.

The whisper model is bundled into the .exe by PyInstaller from
`assets/whisper_model/`. If the model fails to load (missing files,
ctranslate2 issues, etc.) `try_solve_audio_challenge` returns False
without raising — the validator continues to work, just without the
fallback safety net.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.sync_api import Page

log = logging.getLogger(__name__)

# Lazy singleton — loading the model takes ~2 seconds.
_model = None
_model_load_attempted = False


def _resolve_model_path() -> Path:
    """Find the bundled whisper model directory.

    Frozen by PyInstaller → sys._MEIPASS / whisper_model
    Running from source → <project_root>/assets/whisper_model
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "whisper_model"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "assets" / "whisper_model"


def _get_model():
    """Lazy-load the whisper model. Returns None if unavailable."""
    global _model, _model_load_attempted
    if _model is not None or _model_load_attempted:
        return _model
    _model_load_attempted = True
    try:
        from faster_whisper import WhisperModel  # heavy import — lazy
    except ImportError as e:
        log.warning("faster-whisper not installed: %s — audio fallback disabled", e)
        return None

    model_path = _resolve_model_path()
    if not (model_path / "model.bin").exists():
        log.warning(
            "whisper model missing at %s — audio fallback disabled", model_path
        )
        return None
    try:
        _model = WhisperModel(
            str(model_path), device="cpu", compute_type="int8"
        )
        log.info("whisper tiny.en loaded for audio fallback")
        return _model
    except Exception as e:  # noqa: BLE001 — never fatal
        log.warning("whisper load failed: %s — audio fallback disabled", e)
        return None


def try_solve_audio_challenge(page: "Page", timeout_s: float = 45.0) -> bool:
    """Attempt to solve the current reCAPTCHA via audio.

    Returns True if a green check appears, False on any failure (including
    "automated queries" lockout, missing audio button, transcription error).
    Never raises — caller treats False as "fall back to human solve."
    """
    model = _get_model()
    if model is None:
        return False

    deadline = time.monotonic() + timeout_s

    bframe = next(
        (f for f in page.frames if "recaptcha/api2/bframe" in (f.url or "")),
        None,
    )
    if bframe is None:
        log.debug("audio fallback: no challenge bframe")
        return False

    # Click the headphones icon to switch to audio mode.
    try:
        bframe.locator("#recaptcha-audio-button").click(timeout=5_000)
    except Exception as e:  # noqa: BLE001
        log.debug("audio button click failed: %s", e)
        return False

    time.sleep(1.5)

    # Did Google block the audio option entirely?
    try:
        if bframe.get_by_text("automated queries", exact=False).count() > 0:
            log.warning("audio fallback: Google blocked audio (automated queries)")
            return False
    except Exception:
        pass

    # Wait for the audio source URL.
    try:
        bframe.locator("#audio-source").wait_for(state="attached", timeout=10_000)
        audio_src = bframe.locator("#audio-source").get_attribute("src")
    except Exception as e:  # noqa: BLE001
        log.debug("audio source not found: %s", e)
        return False

    if not audio_src:
        return False

    # Download the audio clip.
    audio_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            audio_path = Path(f.name)
        urllib.request.urlretrieve(audio_src, audio_path)
    except Exception as e:  # noqa: BLE001
        log.warning("audio download failed: %s", e)
        return False

    # Transcribe.
    try:
        segments, _info = model.transcribe(
            str(audio_path),
            language="en",
            vad_filter=True,
            beam_size=1,
        )
        text = " ".join(s.text for s in segments).strip().lower()
    except Exception as e:  # noqa: BLE001
        log.warning("whisper transcription failed: %s", e)
        return False
    finally:
        if audio_path is not None and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass

    if not text:
        log.debug("audio transcription empty")
        return False

    log.info("audio transcribed: %r", text[:80])

    # Enter the transcript and submit.
    try:
        bframe.locator("#audio-response").fill(text)
        bframe.locator("#recaptcha-verify-button").click()
    except Exception as e:  # noqa: BLE001
        log.warning("audio submit failed: %s", e)
        return False

    # Confirm the green check on the anchor frame.
    anchor = next(
        (f for f in page.frames if "recaptcha/api2/anchor" in (f.url or "")),
        None,
    )
    if anchor is None:
        return False
    while time.monotonic() < deadline:
        try:
            checked = (
                anchor.locator("#recaptcha-anchor").get_attribute("aria-checked")
                == "true"
            )
        except Exception:
            checked = False
        if checked:
            log.info("audio fallback: green check confirmed")
            return True
        time.sleep(0.4)

    log.info("audio fallback: timed out waiting for green check")
    return False
