"""The Server picker must name the host actually in use.

Field symptom: the header read "usba.ttinteractive.com" while the picker said
"Direct origin — asia". The label came from string-matching the saved override
file, so any variant in it — or an older build with a different default — fell
through to the DEFAULT label while every request went elsewhere. The picker now
reads the live BASE_URL, so it cannot disagree with the header.
"""

from __future__ import annotations

import pytest

from src import gui


class _App:
    """Just the label logic, without building a window."""

    _zenith_host_saved_label = gui.App._zenith_host_saved_label


def _label(monkeypatch, live: str) -> str:
    monkeypatch.setattr(gui.zenith_client, "BASE_URL", live)
    return _App()._zenith_host_saved_label()


def test_shows_the_default_option_when_running_on_asia(monkeypatch):
    assert "asia" in _label(monkeypatch, "https://asia.ttinteractive.com")


def test_shows_usba_when_actually_running_on_usba(monkeypatch):
    """The exact mismatch seen in the field."""
    label = _label(monkeypatch, "https://usba.ttinteractive.com")
    assert "usba" in label and "Direct origin" not in label


def test_trailing_slash_and_case_do_not_change_the_answer(monkeypatch):
    assert "usba" in _label(monkeypatch, "https://USBA.ttinteractive.com/")


def test_an_unlisted_host_is_named_rather_than_mislabelled(monkeypatch):
    label = _label(monkeypatch, "https://something-else.example")
    assert "something-else.example" in label and "in use" in label


def test_every_offered_option_round_trips_to_its_own_label(monkeypatch):
    """No option may resolve to a different option's label."""
    for label, url in gui.ZENITH_HOST_OPTIONS.items():
        live = url or gui._ZENITH_DEFAULT_HOST
        assert _label(monkeypatch, live) == label, live
