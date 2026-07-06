"""Tests for the in-app visual User Guide.

Content integrity runs without a display; the render smoke test (guarded on Tk)
builds the guide and clicks through EVERY topic so a layout/destroy bug in any
topic fails here rather than on a user's machine.
"""

from __future__ import annotations

import pytest

from src import guide_view
from src.guide_view import CALLOUT_KINDS, topics_for


def test_both_apps_have_topics():
    assert len(topics_for("console")) >= 6
    assert len(topics_for("mailer")) >= 3
    # unknown app name falls back to the console set, never empty
    assert topics_for("nope") == topics_for("console")


@pytest.mark.parametrize("app", ["console", "mailer"])
def test_topic_content_is_well_formed(app):
    keys = set()
    for t in topics_for(app):
        assert t.key and t.key not in keys, f"duplicate/empty key {t.key!r}"
        keys.add(t.key)
        assert t.icon and t.title and t.tagline
        assert len(t.flow) >= 2, f"{t.key}: flow needs >=2 boxes for a diagram"
        assert t.steps, f"{t.key}: no steps"
        for s in t.steps:
            assert s.icon and s.title and s.body
            assert len(s.body) <= 400, f"{t.key}/{s.title}: body too long for a card"
        for c in t.callouts:
            assert c.kind in CALLOUT_KINDS, f"{t.key}: bad callout kind {c.kind!r}"
            assert c.text


def test_whatsapp_topic_carries_a_risk_warning():
    # The ToS/ban risk must always be surfaced on the WhatsApp guide.
    for app in ("console", "mailer"):
        wa = next(t for t in topics_for(app) if t.key == "whatsapp")
        assert any(c.kind == "warning" for c in wa.callouts)
        assert any("ban" in c.text.lower() for c in wa.callouts)


# --- render smoke (needs Tk) ------------------------------------------------

tk = pytest.importorskip("tkinter")


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


class _StubApp:
    """Minimal duck-typed app for guide_view.build_guide."""
    _COLOR_PRIMARY = "#0078D4"
    _COLOR_MUTED = "#64748b"
    _theme_is_dark = False

    @staticmethod
    def _make_scrollable(parent):
        from tkinter import ttk
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.pack(fill="both", expand=True)
        return inner


@pytest.mark.parametrize("app_name", ["console", "mailer"])
def test_build_guide_and_click_every_topic(root, app_name):
    from tkinter import ttk
    frame = ttk.Frame(root)
    frame.pack()
    guide_view.build_guide(frame, _StubApp(), app_name)

    def all_buttons(w, out):
        for c in w.winfo_children():
            if isinstance(c, ttk.Button):
                out.append(c)
            all_buttons(c, out)

    btns: list = []
    all_buttons(frame, btns)
    topic_btns = [b for b in btns if "User Guide" not in str(b.cget("text"))]
    assert len(topic_btns) == len(topics_for(app_name))
    for b in topic_btns:          # clicking each destroys+rebuilds the body
        b.invoke()


def test_theme_variants_render(root):
    from tkinter import ttk
    for dark in (False, True):
        app = _StubApp()
        app._theme_is_dark = dark
        frame = ttk.Frame(root)
        frame.pack()
        guide_view.build_guide(frame, app, "console")
        frame.destroy()
