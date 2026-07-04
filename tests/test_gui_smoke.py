"""GUI smoke tests — lazy tab construction + log capping + geometry persistence.

Tabs are now built on first visit (startup went from building ~800 widgets to
one tab). These tests force-build EVERY tab headlessly so a handler referencing
another tab's widgets — the one regression lazy building can introduce — fails
here instead of on a user's machine. Skips cleanly where Tk has no display.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def app():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    root.withdraw()
    from src.gui import App
    a = App(root)
    yield a
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_default_tab_is_built_and_others_deferred(app):
    # IATA (default tab) builds eagerly; the other four defer to first visit.
    assert hasattr(app, "log_text")                  # IATA widgets exist
    assert len(app._tab_builders) == 4               # bd/traffic/zenith/mailer pending


def test_all_lazy_tabs_build_without_errors(app):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    assert not app._tab_builders                     # everything built exactly once
    # Key widgets from each tab exist afterwards:
    for attr in ("log_text", "bd_log_text", "mail_tree", "zenith_bulk_log",
                 "btn_zenith_fh_inspect", "oep_tree"):
        assert hasattr(app, attr), f"missing {attr} after full build"
    # Re-running is a no-op, not a rebuild.
    app._ensure_tab_built(app._tab_widgets["bd"])


def test_append_log_caps_line_count(app):
    for i in range(app._LOG_MAX_LINES + 300):
        app._append_log(app.log_text, f"line {i}")
    lines = int(app.log_text.index("end-1c").split(".")[0])
    assert lines <= app._LOG_MAX_LINES + 1           # trimmed, newest kept
    assert f"line {app._LOG_MAX_LINES + 299}" in app.log_text.get("end-2l", "end")


def test_geometry_save_and_restore_roundtrip(app, tmp_path, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "WINDOW_GEOMETRY_FILE", tmp_path / "geom.txt")
    app._save_geometry()
    saved = (tmp_path / "geom.txt").read_text(encoding="utf-8")
    assert saved == "zoomed" or "x" in saved         # WxH+X+Y or zoomed


def test_restore_rejects_offscreen_geometry(app, tmp_path, monkeypatch):
    from src import config
    geom_file = tmp_path / "geom.txt"
    geom_file.write_text("1080x820+99999+99999", encoding="utf-8")  # dead monitor
    monkeypatch.setattr(config, "WINDOW_GEOMETRY_FILE", geom_file)
    app._apply_initial_geometry()                    # must fall back, not vanish
    assert app.root.winfo_x() < app.root.winfo_screenwidth()


# ---------------------------------------------------------------------------
# Visual-hierarchy + zebra rules (guard the v1.24.0 consistency pass)
# ---------------------------------------------------------------------------

def _all_buttons(root):
    from tkinter import ttk as _ttk
    out = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, _ttk.Button):
                out.append(c)
            walk(c)
    walk(root)
    return out


def test_button_hierarchy_rules(app):
    """Stop buttons are Danger; Cancel/Pause/Resume are never Primary/Danger;
    each tool tab exposes at least one Primary action."""
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    for btn in _all_buttons(app.root):
        text = str(btn.cget("text")).strip()
        style = str(btn.cget("style"))
        if text == "Stop":
            assert style == "Danger.TButton", f"'Stop' not Danger (is {style!r})"
        if text in ("Cancel", "Pause", "Resume"):
            assert style not in ("Primary.TButton", "Danger.TButton"), \
                f"{text!r} must be a plain secondary button (is {style!r})"
    for key in ("iata", "bd", "traffic", "zenith", "mailer"):
        tab = app._tab_widgets[key]
        styles = {str(b.cget("style")) for b in _all_buttons(tab)}
        assert "Primary.TButton" in styles, f"tab {key!r} has no primary action"


def test_result_grids_registered_and_striped(app):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    registered = getattr(app, "_striped_trees", [])
    # All five persistent result grids register at build time.
    for attr in ("traffic_tree", "mail_tree", "oep_tree",
                 "zenith_fl_legs_tree", "zenith_fh_tree"):
        assert getattr(app, attr) in registered, f"{attr} not registered"
    # Striping alternates and never clobbers semantic tags.
    tree = app.mail_tree
    tree.delete(*tree.get_children())
    for i in range(4):
        tree.insert("", "end", values=(i, "e", "n", "f", "", "", "OK"),
                    tags=("bad",) if i == 1 else ())
    app._stripe_tree(tree)
    rows = tree.get_children()
    assert "stripe" not in tree.item(rows[0], "tags")
    assert set(tree.item(rows[1], "tags")) == {"bad", "stripe"}   # semantic kept
    assert "stripe" in tree.item(rows[3], "tags")
    # Re-striping is idempotent (no duplicate stripe tags).
    app._stripe_tree(tree)
    assert list(tree.item(rows[1], "tags")).count("stripe") == 1


def test_wide_grids_have_horizontal_scrollbars(app):
    """The OEP pivot / history grids overflow the window — h-scroll required."""
    from tkinter import ttk as _ttk
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    for attr in ("traffic_tree", "mail_tree", "oep_tree",
                 "zenith_fl_legs_tree", "zenith_fh_tree"):
        tree = getattr(app, attr)
        assert str(tree.cget("xscrollcommand")), f"{attr} lacks xscrollcommand"
        assert str(tree.cget("yscrollcommand")), f"{attr} lacks yscrollcommand"
