"""Tests for the self-updater: backend-first channel, GitHub fallback, and
SHA-256 integrity verification on download.

No real network, GitHub, or backend is touched — urllib is faked and the
staging dir is redirected to a tmp path.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src import updater


# ---------------------------------------------------------------------------
# Fake urllib response
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._body):
            return b""
        if n is None or n < 0:
            n = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, body: bytes, headers: dict | None = None) -> None:
    monkeypatch.setattr(
        updater.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResp(body, headers),
    )


# ---------------------------------------------------------------------------
# check_for_update: backend-first, GitHub fallback
# ---------------------------------------------------------------------------


def test_check_prefers_backend(monkeypatch):
    """When the backend responds, GitHub must not be contacted at all."""
    sentinel = updater.UpdateInfo(
        latest_version="9.9.9", download_url="https://api.example/app/download",
        notes="n", is_newer=True, sha256="abc",
    )
    monkeypatch.setattr(updater, "_check_backend_update", lambda timeout=10: sentinel)

    def _boom(*a, **k):
        raise AssertionError("GitHub must not be called when the backend responds")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom)
    assert updater.check_for_update() is sentinel


def test_check_falls_back_to_github(monkeypatch):
    """Backend unavailable -> GitHub Releases path, unchanged behaviour."""
    monkeypatch.setattr(updater, "_check_backend_update", lambda timeout=10: None)
    body = json.dumps({
        "draft": False, "prerelease": False, "tag_name": "v9.9.9",
        "body": "release notes",
        "assets": [{"name": updater.ASSET_NAME, "browser_download_url": "https://gh/d.exe"}],
    }).encode()
    _patch_urlopen(monkeypatch, body)
    info = updater.check_for_update()
    assert info is not None
    assert info.latest_version == "9.9.9"
    assert info.download_url == "https://gh/d.exe"
    assert info.sha256 == ""  # GitHub path carries no manifest hash


# ---------------------------------------------------------------------------
# _check_backend_update
# ---------------------------------------------------------------------------


def test_backend_check_none_when_signed_out(monkeypatch):
    from src import auth
    monkeypatch.setattr(auth, "get_token", lambda: None)
    monkeypatch.setattr(auth, "API_BASE_URL", "https://api.example")
    assert updater._check_backend_update() is None


def test_backend_check_parses_manifest(monkeypatch):
    from src import auth
    monkeypatch.setattr(auth, "get_token", lambda: "sess-token")
    monkeypatch.setattr(auth, "API_BASE_URL", "https://api.example")
    body = json.dumps({
        "version": "2.0.0", "notes": "hi",
        "download_url": "https://api.example/api/v1/app/download",
        "sha256": "DEADBEEF",
    }).encode()
    _patch_urlopen(monkeypatch, body)
    info = updater._check_backend_update()
    assert info is not None
    assert info.latest_version == "2.0.0"
    assert info.download_url == "https://api.example/api/v1/app/download"
    assert info.sha256 == "DEADBEEF"


def test_backend_check_none_on_garbage(monkeypatch):
    from src import auth
    monkeypatch.setattr(auth, "get_token", lambda: "t")
    monkeypatch.setattr(auth, "API_BASE_URL", "https://api.example")
    _patch_urlopen(monkeypatch, b"not json at all")
    assert updater._check_backend_update() is None


# ---------------------------------------------------------------------------
# download_update: SHA-256 integrity gate
# ---------------------------------------------------------------------------


_DATA = b"pretend-exe-bytes" * 1000


def test_download_verifies_sha256_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_staging_dir", lambda: tmp_path)
    _patch_urlopen(monkeypatch, _DATA, {"Content-Length": str(len(_DATA))})
    good = hashlib.sha256(_DATA).hexdigest()
    out = updater.download_update("https://x/d.exe", expected_sha256=good)
    assert out.read_bytes() == _DATA


def test_download_rejects_sha256_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_staging_dir", lambda: tmp_path)
    _patch_urlopen(monkeypatch, _DATA, {"Content-Length": str(len(_DATA))})
    with pytest.raises(ValueError):
        updater.download_update("https://x/d.exe", expected_sha256="00bad")
    # A tampered/corrupt download must NOT be staged for the swap step.
    assert not updater.update_pending_path().exists()


def test_download_no_hash_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_staging_dir", lambda: tmp_path)
    _patch_urlopen(monkeypatch, _DATA, {"Content-Length": str(len(_DATA))})
    out = updater.download_update("https://x/d.exe")  # no expected hash (GitHub path)
    assert out.read_bytes() == _DATA
