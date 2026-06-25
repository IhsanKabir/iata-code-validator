"""Zenith host override — route around the CloudFront-fronted `usba.` host (which
504-storms on slow Dossier renders) to the direct `asia.` origin.

The HAR proved `asia.ttinteractive.com` is the direct origin (no CloudFront) and waits a
55-second render out (200 + 123 KB), while `usba.` (CloudFront) returns 504. The override is
read at import time, so the cross-module test runs in a subprocess.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_env_override_flows_to_every_url():
    code = (
        "import src.zenith_client as z, src.zenith_pnr_client as p;"
        "print(z.BASE_URL); print(z.LOGIN_URL); print(z.CUSTOMER_LOOKUP_URL);"
        "print(p.QUICK_SEARCH_URL)"
    )
    env = dict(os.environ, ZENITH_BASE_URL="https://asia.ttinteractive.com")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO), env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("asia.ttinteractive.com") >= 4
    assert "usba" not in r.stdout


def test_default_host_is_asia():
    code = "import src.zenith_client as z; print(z.BASE_URL)"
    env = {k: v for k, v in os.environ.items() if k != "ZENITH_BASE_URL"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO), env=env)
    assert r.returncode == 0, r.stderr
    assert "asia.ttinteractive.com" in r.stdout      # asia is now the default (direct origin)


def test_main_applies_saved_host_file(tmp_path, monkeypatch):
    from src import config, main
    hostfile = tmp_path / "zenith_host.txt"
    hostfile.write_text("https://asia.ttinteractive.com", encoding="utf-8")
    monkeypatch.setattr(config, "ZENITH_HOST_FILE", hostfile)
    monkeypatch.delenv("ZENITH_BASE_URL", raising=False)
    try:
        main._apply_zenith_host_override()
        assert os.environ.get("ZENITH_BASE_URL") == "https://asia.ttinteractive.com"
    finally:
        os.environ.pop("ZENITH_BASE_URL", None)


def test_explicit_env_var_wins_over_saved_file(tmp_path, monkeypatch):
    from src import config, main
    hostfile = tmp_path / "zenith_host.txt"
    hostfile.write_text("https://asia.ttinteractive.com", encoding="utf-8")
    monkeypatch.setattr(config, "ZENITH_HOST_FILE", hostfile)
    monkeypatch.setenv("ZENITH_BASE_URL", "https://usba.ttinteractive.com")
    main._apply_zenith_host_override()
    assert os.environ["ZENITH_BASE_URL"] == "https://usba.ttinteractive.com"  # env wins


def test_no_file_no_env_leaves_default(tmp_path, monkeypatch):
    from src import config, main
    monkeypatch.setattr(config, "ZENITH_HOST_FILE", tmp_path / "missing.txt")
    monkeypatch.delenv("ZENITH_BASE_URL", raising=False)
    main._apply_zenith_host_override()
    assert "ZENITH_BASE_URL" not in os.environ          # nothing set -> default usba stands
