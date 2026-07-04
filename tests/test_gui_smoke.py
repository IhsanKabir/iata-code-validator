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
