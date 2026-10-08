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
    assert len(app._tab_builders) == 6               # bd/traffic/zenith/mailer/health/guide pending


def test_all_lazy_tabs_build_without_errors(app):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    assert not app._tab_builders                     # everything built exactly once
    # Key widgets from each tab exist afterwards (incl. WhatsApp + Health):
    for attr in ("log_text", "bd_log_text", "mail_tree", "zenith_bulk_log",
                 "btn_zenith_fh_inspect", "oep_tree", "wa_tree", "btn_wa_send",
                 "health_tree", "btn_health_run",
                 "zenith_sm_tree", "btn_sm_run"):
        assert hasattr(app, attr), f"missing {attr} after full build"
    # Mixin dispatch: unknown kinds return False (not swallowed).
    assert app._wa_handle_msg("wa_unknown", None) is False
    assert app._health_handle_msg("health_unknown", None) is False


def test_health_handler_safe_when_widgets_destroyed(app):
    """Closing the Health surface mid-run must not let a late message crash the
    queue pump (a TclError there freezes ALL background messaging)."""
    import tkinter as _tk
    app._ensure_tab_built(app._tab_widgets["health"])
    tree = app.health_tree
    tree.destroy()                                   # simulate the surface closing
    from src.health_checks import HealthResult
    from src.health_gui import HEALTH_MSG_RESULT
    # must consume the message (True) and NOT raise
    r = HealthResult("iata", "IATA", "Connectivity", "OK", "ok", "")
    assert app._health_handle_msg(HEALTH_MSG_RESULT, r) is True
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


def test_the_schedule_history_section_lives_in_the_history_tab(app):
    """It reads the same folder and the same files as the history audit, so a
    tab of its own would have duplicated three pickers to add one button."""
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    labels = {str(b.cget("text")).strip() for b in _all_buttons(app.root)}
    assert "Build schedule history" in labels
    # and it owns no folder pickers of its own
    assert not hasattr(app, "fsh_input_dir")
    assert not hasattr(app, "fsh_output_dir")
    # nor a month picker: the period is read from the files
    assert not hasattr(app, "fsh_month")
    assert not hasattr(app, "fsh_year")


def test_the_flight_loads_tab_can_draw_the_schedule_from_the_last_pull(app):
    """The schedule needs no second search: the flight listing already carries
    every leg's own local time, aircraft and tail number."""
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    labels = {str(b.cget("text")).strip() for b in _all_buttons(app.root)}
    assert "Draw airline schedule…" in labels
    # disabled until a pull has actually produced rows
    assert str(app.btn_zenith_fl_schedule.cget("state")) == "disabled"


def test_an_extra_report_format_still_finishes_the_run(app, monkeypatch, tmp_path):
    """Choosing any format other than the flat one used to return early, which
    left the rows unretained, the Append and Schedule buttons disabled, the
    legs grid empty and Stop still live — a finished pull that looked hung."""
    from src import excel_io, zenith_client

    rows = [object()]
    monkeypatch.setattr(zenith_client, "fetch_flight_loads",
                        lambda *a, **k: rows)
    monkeypatch.setattr(excel_io, "write_zenith_flight_loads",
                        lambda *a, **k: None)
    monkeypatch.setattr(excel_io, "write_flight_loads_daily_snapshots",
                        lambda *a, **k: [])
    posted: list = []
    monkeypatch.setattr(app, "_post", lambda kind, payload=None:
                        posted.append(kind))

    # the app fixture is module-scoped, so this state is restored rather than
    # left dirty for whatever test runs next
    monkeypatch.setattr(app, "_zenith_fl_last_rows", [], raising=False)
    monkeypatch.setattr(app, "_zenith_fl_last_range", ("", ""), raising=False)
    app._zenith_fl_worker_run({
        "date_from": "01/09/2026", "date_to": "06/09/2026",
        "page_size": 100, "chunk_days": 5, "delay_s": 0.0,
        "out_path": tmp_path / "loads.xlsx",
        "fmt": "Daily Flight Load snapshot",
    })

    from src.gui import MSG_ZENITH_FL_DONE
    assert MSG_ZENITH_FL_DONE in posted     # the run reports that it finished
    assert app._zenith_fl_last_rows == rows  # and the rows are still usable


# --------------------------------------------------------------------------
# Sales Movement — the form is the only place a wrong setting can enter, and
# a silently misread one changes every number on the sheet.
# --------------------------------------------------------------------------
def _sm_app(app):
    app._ensure_tab_built(app._tab_widgets["zenith"])
    return app


def test_sales_movement_defaults_to_the_last_complete_month(app):
    """Today's month is always partial in the warehouse, and a part-month
    against whole ones shows the whole book collapsing."""
    from datetime import date
    _sm_app(app)
    s = app._sm_settings()
    today = date.today()
    assert s.period_from.day == 1
    assert (s.period_to < today.replace(day=1))
    assert s.period_from.month == s.period_to.month


def test_sales_movement_reads_the_form_into_settings(app):
    from src import sales_movement as sm
    _sm_app(app)
    app.zenith_sm_from.set("2026-08-01")
    app.zenith_sm_to.set("2026-08-31")
    app.zenith_sm_trailing.set(6)
    app.zenith_sm_threshold.set(35.0)
    app.zenith_sm_direction.set("Dropped only")
    app.zenith_sm_floor.set("1,500,000")          # typed with separators
    app.zenith_sm_measure.set("Gross ticket sales")
    app.zenith_sm_who.set("All customers")
    s = app._sm_settings()
    assert s.trailing == 6
    assert s.threshold == 0.35                    # per cent -> fraction
    assert s.direction == sm.DROPPED
    assert s.floor == 1_500_000
    assert s.measure == sm.MEASURE_GROSS
    assert s.channels == ()                       # no channel filter at all


def test_sales_movement_defaults_to_agencies_and_net(app):
    from src import sales_movement as sm
    _sm_app(app)
    app.zenith_sm_measure.set("Net of refunds and voids")
    app.zenith_sm_who.set("Agencies only")
    s = app._sm_settings()
    assert s.channels == sm.AGENCY_CHANNELS
    assert s.measure == sm.MEASURE_NET


def test_sales_movement_refuses_a_backwards_period(app):
    import pytest as _pytest
    _sm_app(app)
    app.zenith_sm_from.set("2026-08-31")
    app.zenith_sm_to.set("2026-08-01")
    with _pytest.raises(ValueError, match="ends before it starts"):
        app._sm_settings()
    app.zenith_sm_from.set("2026-08-01")
    app.zenith_sm_to.set("2026-08-31")


def test_sales_movement_says_what_a_bad_date_should_look_like(app):
    import pytest as _pytest
    _sm_app(app)
    app.zenith_sm_from.set("01/08/2026")
    with _pytest.raises(ValueError, match="YYYY-MM-DD"):
        app._sm_settings()
    app.zenith_sm_from.set("2026-08-01")


def test_sales_movement_reads_the_baseline_mode_from_the_form(app):
    from src import sales_movement as sm
    _sm_app(app)
    app.zenith_sm_baseline.set("The same period a year earlier")
    s = app._sm_settings()
    assert s.baseline == sm.BASELINE_LAST_YEAR
    app.zenith_sm_baseline.set("An average of the periods before it")
    assert app._sm_settings().baseline == sm.BASELINE_TRAILING


def test_the_period_count_is_greyed_out_when_there_is_nothing_to_average(app):
    """Last-year is a single window on purpose; leaving the spinbox live
    invites someone to set it to 6 and wonder why nothing changes."""
    _sm_app(app)
    app.zenith_sm_baseline.set("The same period a year earlier")
    assert str(app.zenith_sm_trailing_box.cget("state")) == "disabled"
    app.zenith_sm_baseline.set("An average of the periods before it")
    assert str(app.zenith_sm_trailing_box.cget("state")) == "normal"


def test_the_floor_follows_the_measure_between_money_and_tickets(app):
    """500,000 meant half a million TICKETS and excluded every agency."""
    _sm_app(app)
    app.zenith_sm_measure.set("Net of refunds and voids")
    app.zenith_sm_floor.set(app._SM_FLOOR_MONEY)
    app.zenith_sm_measure.set("Tickets issued")
    assert app.zenith_sm_floor.get() == app._SM_FLOOR_TICKETS
    app.zenith_sm_measure.set("Net of refunds and voids")
    assert app.zenith_sm_floor.get() == app._SM_FLOOR_MONEY


def test_a_floor_the_user_typed_is_never_overwritten(app):
    _sm_app(app)
    app.zenith_sm_measure.set("Net of refunds and voids")
    app.zenith_sm_floor.set("750000")
    app.zenith_sm_measure.set("Tickets issued")
    assert app.zenith_sm_floor.get() == "750000"
    app.zenith_sm_measure.set("Net of refunds and voids")
    app.zenith_sm_floor.set(app._SM_FLOOR_MONEY)


def test_the_call_list_button_waits_for_a_result(app):
    _sm_app(app)
    assert str(app.btn_sm_calls.cget("state")) == "disabled"
    assert app._sm_last_result is None


def test_an_emptied_spinbox_reaches_the_error_box_not_the_traceback(app):
    """IntVar.get() raises tk.TclError, which `except ValueError` misses."""
    import pytest as _pytest
    _sm_app(app)
    app.zenith_sm_trailing_box.delete(0, "end")
    with _pytest.raises(ValueError, match="whole number of"):
        app._sm_settings()
    app.zenith_sm_trailing.set(3)


def test_sales_movement_offers_the_measure_that_counts_penalties(app):
    from src import sales_movement as sm
    _sm_app(app)
    app.zenith_sm_measure.set("Net, plus penalties and reissues")
    assert app._sm_settings().measure == sm.MEASURE_ALL
    app.zenith_sm_measure.set("Net of refunds and voids")
    assert app._sm_settings().measure == sm.MEASURE_NET


def test_sales_movement_can_ask_for_both_baselines_at_once(app):
    from src import sales_movement as sm
    _sm_app(app)
    app.zenith_sm_baseline.set("Both — the average AND a year earlier")
    s = app._sm_settings()
    assert s.baseline == sm.BASELINE_BOTH
    # the period count still matters under BOTH, so it must stay editable
    assert str(app.zenith_sm_trailing_box.cget("state")) == "normal"
    app.zenith_sm_baseline.set("An average of the periods before it")


def test_the_scorecard_section_is_on_the_sales_movement_tab(app):
    """Same warehouse, a different question -- it shares the period and the
    output folder, so it does not need a tab of its own."""
    _sm_app(app)
    assert hasattr(app, "zenith_sc_tree")
    assert hasattr(app, "btn_sc_run")
    assert hasattr(app, "zenith_sc_term")


def test_the_scorecard_refuses_an_empty_search(app, monkeypatch):
    _sm_app(app)
    app.zenith_sc_term.set("   ")
    shown = {}
    monkeypatch.setattr("src.gui.messagebox.showerror",
                        lambda t, m: shown.setdefault("msg", m))
    app._sc_run()
    assert "account number" in shown.get("msg", "")


def test_ambiguous_candidates_are_listed_rather_than_picked(app):
    """'TRAVELS' matches 1,825 agency names in the real data."""
    _sm_app(app)
    app._handle_msg("sc_choose", [("Alpha Travels", 900.0, "1"),
                                  ("Beta Travels", 100.0, "2")])
    rows = app.zenith_sc_tree.get_children()
    assert len(rows) == 2
    assert app._sc_candidates == ["1", "2"]
    assert "Double-click" in str(app.zenith_sc_status.cget("text"))


def test_a_scorecard_shows_the_bsp_split_under_the_total(app):
    _sm_app(app)
    app._handle_msg("sc_done", {
        "name": "BE FRESH LIMITED  (2 accounts)", "span": "1 May to 19 Sep",
        "share_now": 0.02292, "share_before": 0.01948, "share_move": 0.00344,
        "sales_now": 668982748, "sales_before": 504774230, "growth": 0.325,
        "bsp_now": 86948326, "bsp_before": 82437721, "growth_bsp": 0.055,
        "other_now": 582034422, "other_before": 422336509,
        "growth_other": 0.378,
        "rank_now": 7, "rank_before": 13, "n_now": 3238, "n_before": 2441,
        "band_now": "top 1%", "band_before": "top 1%", "rank_move": 6,
        "accounts": [("10000277", "BE FRESH LIMITED", 1004370931.0)],
        "warnings": [], "headline": "x"})
    vals = [app.zenith_sc_tree.item(i, "values")
            for i in app.zenith_sc_tree.get_children()]
    metrics = [v[0] for v in vals]
    assert "Market share with BS" in metrics
    assert any("BSP accounts" in m for m in metrics)
    assert any("Non-IATA accounts" in m for m in metrics)
    # the share renders as a percentage, not a fraction
    assert vals[0][1] == "2.292%"
    assert vals[0][3] == "+0.344 pts"


def test_a_missing_last_year_leaves_the_cell_blank_not_zero(app):
    _sm_app(app)
    app._handle_msg("sc_done", {
        "name": "Brand New Co", "span": "x", "share_now": 0.01,
        "share_before": None, "share_move": None,
        "sales_now": 5, "sales_before": 0, "growth": None,
        "bsp_now": 0, "bsp_before": 0, "growth_bsp": None,
        "other_now": 5, "other_before": 0, "growth_other": None,
        "rank_now": 4, "rank_before": None, "n_now": 9, "n_before": 0,
        "band_now": "top half", "band_before": "", "rank_move": None,
        "accounts": [], "warnings": ["no sales last year"], "headline": "x"})
    vals = [app.zenith_sc_tree.item(i, "values")
            for i in app.zenith_sc_tree.get_children()]
    assert vals[0][2] == ""          # last-year share blank, not 0.000%
    assert vals[0][3] == ""


def test_the_route_optimisation_tab_is_built(app):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    for attr in ("zenith_ro_tree", "btn_ro_run", "zenith_ro_loads",
                 "zenith_ro_sched"):
        assert hasattr(app, attr), f"missing {attr}"


def test_the_route_tab_names_the_stored_pull_it_will_use(app):
    """Leaving which schedule was used implied is how a stale pull gets
    read as current."""
    _sm_app(app)
    said = str(app.zenith_ro_sched.cget("text"))
    assert "firsttrip_" in said or "no stored schedule pull" in said


def test_the_route_tab_refuses_to_run_without_our_own_load_file(app,
                                                                monkeypatch):
    """Our frequency must come from our records, never from the market
    search, which sees only what is on sale."""
    _sm_app(app)
    app.zenith_ro_loads.set("")
    shown = {}
    monkeypatch.setattr("src.gui.messagebox.showerror",
                        lambda t, m: shown.setdefault("msg", m))
    app._ro_run()
    assert "never from the market search" in shown.get("msg", "")


def test_a_route_with_no_share_shows_a_blank_not_a_zero(app):
    _sm_app(app)
    app._handle_msg("ro_done", {
        "path": "", "summary": "x", "warnings": [],
        "rows": [("DAC-XXX", None, 0.0, 0.0, None, None, "", "thin", False,
                  "leg")]})
    vals = app.zenith_ro_tree.item(
        app.zenith_ro_tree.get_children()[0], "values")
    assert vals[1] == "" and vals[4] == "" and vals[5] == ""


def test_the_route_grid_shows_each_pair_then_both_directions(app):
    """DAC-CGP and CGP-DAC sit together, the combined row first."""
    from src import route_optimisation as ro

    _sm_app(app)
    res = ro.Result(routes=[ro.RouteView("CGP-DAC", our_seats=100,
                                         our_taken=90, our_legs=20),
                            ro.RouteView("DAC-CCU", our_seats=72,
                                         our_taken=70, our_legs=20),
                            ro.RouteView("DAC-CGP", our_seats=100,
                                         our_taken=80, our_legs=20)])
    app._handle_msg("ro_done", {"path": "", "summary": "x", "warnings": [],
                                "rows": app._ro_grid_rows(res)})
    t = app.zenith_ro_tree
    labels = [str(t.item(i, "values")[0]).strip() for i in t.get_children()]
    cgp = labels.index("DAC ⇄ CGP")
    assert labels[cgp + 1:cgp + 3] == ["→ DAC-CGP", "← CGP-DAC"]
    assert "pair" in t.item(t.get_children()[cgp], "tags")


def test_a_failed_route_run_keeps_its_reason_on_the_tab(app, monkeypatch):
    _sm_app(app)
    monkeypatch.setattr("src.gui.messagebox.showerror", lambda *a: None)
    app._handle_msg("ro_error", "No stored market schedule was found.")
    assert "No stored market schedule" in str(
        app.zenith_ro_warning.cget("text"))


# ---- FFP Customers: behind a password -------------------------------------

_TEST_PASSWORD = "not-the-real-one"


def _use_test_password(monkeypatch):
    from src import tab_lock
    salt = b"0123456789abcdef"
    monkeypatch.setattr(tab_lock, "FFP_SALT", salt)
    monkeypatch.setattr(tab_lock, "FFP_HASH",
                        tab_lock.derive(_TEST_PASSWORD, salt))


def _unlock_ffp(app, monkeypatch):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    if not app._ffp_unlocked:
        _use_test_password(monkeypatch)
        app.ffp_pwd.insert(0, _TEST_PASSWORD)
        app._ffp_unlock()


def test_the_ffp_tab_is_locked_until_the_password(app, monkeypatch):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    tabs = [app.zenith_inner_notebook.tab(t, "text")
            for t in app.zenith_inner_notebook.tabs()]
    assert tabs[:2] == ["Customer Lookup", "FFP Customers"]
    assert not app._ffp_unlocked and not hasattr(app, "btn_ffp_run")
    _use_test_password(monkeypatch)
    app.ffp_pwd.insert(0, "wrong")
    app._ffp_unlock()
    assert not app._ffp_unlocked
    assert "Wrong password" in str(app.ffp_gate_msg.cget("text"))
    assert app.ffp_pwd.get() == ""               # not left on screen
    # a message for the locked tab is swallowed, not crashed on
    assert app._ffp_handle_msg("ffp_log", "x") is True


def test_the_right_password_opens_the_tab(app, monkeypatch):
    _unlock_ffp(app, monkeypatch)
    assert app._ffp_unlocked
    assert str(app.btn_ffp_run.cget("text")) == "Collect FFP members"
    # the small levels are ticked, Silver is left for the user to choose
    ticked = {lv: v.get() for lv, v in app.ffp_levels.items()}
    assert ticked == {"Gold": True, "Platinum": True, "Titanium": True,
                      "Silver": False}
    assert app.ffp_workers.get() == 3


def test_ffp_collect_asks_for_a_sign_in_first(app, monkeypatch):
    _unlock_ffp(app, monkeypatch)
    monkeypatch.setattr(app, "_zenith_session", None, raising=False)
    shown = {}
    monkeypatch.setattr("src.ffp_gui.messagebox.showerror",
                        lambda t, m: shown.setdefault("msg", m))
    app._ffp_run()
    assert "Sign in to Zenith" in shown.get("msg", "")


def test_an_ffp_failure_keeps_its_reason_on_the_tab(app, monkeypatch):
    _unlock_ffp(app, monkeypatch)
    app._handle_msg("ffp_error", "Zenith signed the session out.")
    assert "signed the session out" in str(app.ffp_warning.cget("text"))
    assert str(app.btn_ffp_run.cget("state")) == "normal"


def test_an_ffp_stop_still_shows_the_level_counts(app, monkeypatch, tmp_path):
    """The message says 'the level counts are exact' -- so they are shown."""
    from src import ffp_collect as fc
    _unlock_ffp(app, monkeypatch)
    path = tmp_path / "ffp.sqlite"
    store = fc.FFPStore(path)
    store.put("level_total:Silver", 119848)
    monkeypatch.setattr(app, "_ffp_store_path", lambda: path)
    monkeypatch.setattr(app, "_ffp_store_obj", store)
    app._handle_msg("ffp_error", "Only whole numbers match.")
    rows = [app.ffp_tree.item(i, "values") for i in app.ffp_tree.get_children()]
    assert ("Silver", "119,848", "0", "119,848") in rows
    store.close()


def test_the_limit_test_reports_what_zenith_did(app, monkeypatch):
    _unlock_ffp(app, monkeypatch)
    app._handle_msg("ffp_limit", {"where": "form", "name": "PageSize",
                                  "value": 5000, "listed": 1050})
    assert "up to 1,050 rows" in str(app.ffp_status.cget("text"))
    app._handle_msg("ffp_limit", None)
    assert "cannot be lifted" in str(app.ffp_status.cget("text"))
    assert str(app.btn_ffp_limit.cget("state")) == "normal"


def test_the_status_says_which_level_is_being_collected(app, monkeypatch):
    from src import ffp_collect as fc
    _unlock_ffp(app, monkeypatch)
    app._handle_msg("ffp_progress", fc.Progress(members=1, searches=1,
                                                last="Titanium|Ak: 16"))
    assert "Collecting Titanium" in str(app.ffp_status.cget("text"))


def test_the_ffp_tab_runs_quick_unless_thorough_is_ticked(app, monkeypatch):
    _unlock_ffp(app, monkeypatch)
    assert app.ffp_thorough.get() is False


# ---- Pricing: the season check, behind the same password -------------------

def _unlock_pricing(app, monkeypatch):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    if not app._pricing_unlocked:
        _use_test_password(monkeypatch)
        app.pricing_pwd.insert(0, _TEST_PASSWORD)
        app._pricing_unlock()


def test_the_pricing_tab_is_locked_until_the_password(app, monkeypatch):
    for widget in list(app._tab_widgets.values()):
        app._ensure_tab_built(widget)
    tabs = [app.zenith_inner_notebook.tab(t, "text")
            for t in app.zenith_inner_notebook.tabs()]
    assert "Pricing" in tabs
    assert not app._pricing_unlocked and not hasattr(app, "btn_pricing_run")
    _use_test_password(monkeypatch)
    app.pricing_pwd.insert(0, "wrong")
    app._pricing_unlock()
    assert not app._pricing_unlocked
    assert "Wrong password" in str(app.pricing_gate_msg.cget("text"))
    assert app.pricing_pwd.get() == ""
    assert app._pricing_handle_msg("pricing_log", "x") is True


def test_the_pricing_tab_opens_with_next_season_and_every_part(
        app, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.APP_DIR", str(tmp_path), raising=False)
    _unlock_pricing(app, monkeypatch)
    assert str(app.btn_pricing_run.cget("text")) == "Run season check"
    assert app.pricing_mode.get() == "next"
    assert all(v.get() for v in app.pricing_parts.values())
    assert app.pricing_return.get() is True


def test_the_season_check_asks_for_a_sign_in_first(app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    monkeypatch.setattr(app, "_zenith_session", None, raising=False)
    shown = {}
    monkeypatch.setattr("src.pricing_gui.messagebox.showerror",
                        lambda t, m: shown.setdefault("msg", m))
    app._pricing_run()
    assert "Sign in to Zenith" in shown.get("msg", "")


def test_a_finished_season_check_shows_its_summary(app, monkeypatch):
    from src.pricing_check import CheckResult, Finding
    _unlock_pricing(app, monkeypatch)
    res = CheckResult(base="Summer 2026", others=["Winter 2026-27"],
                      routes=["DAC-CCU"], compared={"Fares": 3},
                      problems=["DAC-CCU: Quick Sale said 'x'."])
    res.findings.append(Finding("Fares", "missing", "DAC-CCU", "Y · CH", "",
                                "Summer 2026", "present", "Winter 2026-27",
                                "MISSING"))
    app._handle_msg("pricing_done", ("C:/x/Season_Check.xlsx", res))
    assert "Fares: 3 compared — 1 to look at" in str(
        app.pricing_summary.cget("text"))
    assert "could not be checked" in str(app.pricing_warning.cget("text"))
    assert str(app.btn_pricing_open.cget("state")) == "normal"


def test_a_pricing_failure_keeps_its_reason_on_the_tab(app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    app._handle_msg("pricing_error", "Zenith signed the session out.")
    assert "signed the session out" in str(
        app.pricing_warning.cget("text"))
    assert str(app.btn_pricing_run.cget("state")) == "normal"


def test_the_emailed_code_is_asked_for_on_the_window(app, monkeypatch):
    import threading
    monkeypatch.setattr("tkinter.simpledialog.askstring",
                        lambda title, prompt, parent=None: "XLOHJN")
    answer, done = {}, threading.Event()
    app._handle_msg("zenith_need_code", ("ab***@x.com", "", answer, done))
    assert done.is_set() and answer["code"] == "XLOHJN"


def test_picking_the_season_after_next_reaches_the_check(app, monkeypatch,
                                                         tmp_path):
    from src.pricing_check import SEASON_MODES
    monkeypatch.setattr("src.config.APP_DIR", str(tmp_path), raising=False)
    _unlock_pricing(app, monkeypatch)
    assert list(app.pricing_parts) == ["Fares", "Quick Sale", "Taxes",
                                       "Penalties"]
    app.pricing_mode_pick.set(SEASON_MODES["after_next"])
    app.pricing_mode_pick.event_generate("<<ComboboxSelected>>")
    app.root.update()
    assert app.pricing_mode.get() == "after_next"


def test_the_pricing_sections_start_folded_and_open_on_click(
        app, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.APP_DIR", str(tmp_path), raising=False)
    _unlock_pricing(app, monkeypatch)
    for key in ("check", "lookup"):
        head, body, _title = app._pricing_folds[key]
        assert not body.winfo_manager()          # folded
        assert str(head.cget("text")).startswith("▸")
        app._pricing_toggle(key)
        assert body.winfo_manager() == "pack"
        assert str(head.cget("text")).startswith("▾")
        app._pricing_toggle(key)
        assert not body.winfo_manager()


def test_lookup_seasons_come_from_the_last_read_with_today_ticked(
        app, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr("src.config.APP_DIR", str(tmp_path), raising=False)
    (tmp_path / "pricing_seasons.json").write_text(json.dumps([
        {"id": 1, "name": "Long ago", "start": "2001/01/01",
         "end": "2001/06/01"},
        {"id": 2, "name": "Forever", "start": "2001/06/01",
         "end": "2999/01/01"}]), encoding="utf-8")
    _unlock_pricing(app, monkeypatch)
    app.lookup_seasons = {}                  # as when the tab first opens
    app._lookup_show_seasons(app._lookup_saved_seasons())
    assert {i: v.get() for i, v in app.lookup_seasons.items()} == {
        1: False, 2: True}


def test_a_lookup_needs_a_fare_basis_or_route(app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    monkeypatch.setattr(app, "_zenith_session", object(), raising=False)
    shown = {}
    monkeypatch.setattr("src.pricing_lookup_gui.messagebox.showerror",
                        lambda t, m: shown.setdefault("msg", m))
    app.lookup_fare_bases.set("")
    app.lookup_routes.set("")
    app._lookup_run()
    assert "fare basis or a route" in shown.get("msg", "")


def test_a_finished_lookup_shows_what_it_read(app, monkeypatch):
    from src.pricing_lookup import LookupResult
    _unlock_pricing(app, monkeypatch)
    res = LookupResult(seasons=["Summer 2026"], routes=["DAC-CCU"],
                       fare_bases=[{"Code": "YBDCCUO"}],
                       fares=[{}, {}], taxes=[{}], notes=["x"])
    app._handle_msg("pricing_lookup_done", ("C:/x/Pricing_Lookup.xlsx", res))
    assert "1 fare bases" in str(app.pricing_summary.cget("text"))
    assert "Fares 2 rows" in str(app.pricing_summary.cget("text"))
    assert str(app.btn_lookup_open.cget("state")) == "normal"
    assert str(app.btn_pricing_open.cget("state")) == "normal"


# ---- Exchange rates (ROE) ------------------------------------------------------

def _roe_plan():
    from datetime import date
    from tests.test_roe import FakeRoe, plan_for
    z = FakeRoe()
    return z, plan_for(z, {"KWD": "399.84", "AED": "33.643623"})


def test_the_roe_section_is_folded_with_save_off_until_a_preview(
        app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    head, body, _t = app._pricing_folds["roe"]
    assert not body.winfo_manager()
    assert str(app.btn_roe_save.cget("state")) == "disabled"
    shown = {}
    monkeypatch.setattr("src.roe_gui.messagebox.showinfo",
                        lambda t, m: shown.setdefault("msg", m))
    app._roe_plan = None
    app._roe_save()
    assert "Preview first" in shown["msg"]


def test_a_roe_preview_lists_the_changes_and_allows_saving(app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    _z, plan = _roe_plan()
    app._handle_msg("pricing_roe_plan", plan)
    rows = [app.roe_tree.item(i, "values")
            for i in app.roe_tree.get_children()]
    assert rows[0][:4] == ("KWD", "399.84", "0.002498", "0.002501")
    assert any(r[0] == "AED" and r[4] == "same" for r in rows)
    assert str(app.btn_roe_save.cget("state")) == "normal"
    assert "1 rate(s) to change" in str(app.pricing_status.cget("text"))


def test_a_roe_save_asks_first_and_a_no_saves_nothing(app, monkeypatch):
    _unlock_pricing(app, monkeypatch)
    _z, plan = _roe_plan()
    app._handle_msg("pricing_roe_plan", plan)
    asked = {}
    monkeypatch.setattr(app, "_zenith_session", object(), raising=False)
    monkeypatch.setattr("src.roe_gui.messagebox.askyesno",
                        lambda t, m, **k: asked.setdefault("msg", m) and False)
    started = []
    monkeypatch.setattr(app, "_pricing_start",
                        lambda *a, **k: started.append(a))
    app._roe_save()
    assert "KWD: 0.002498 → 0.002501" in asked["msg"]
    assert not started


def test_a_finished_roe_save_shows_each_check(app, monkeypatch):
    from datetime import date
    from src import roe
    _unlock_pricing(app, monkeypatch)
    z, plan = _roe_plan()
    res = roe.apply(z, plan, today=date(2026, 10, 8))
    app._handle_msg("pricing_roe_done", ("C:/x/ROE_Update.xlsx", plan, res))
    row = app.roe_tree.item(app.roe_tree.get_children()[0], "values")
    assert row[0] == "KWD" and "history ✓" in row[5]
    assert "Saved and checked ✓" in str(app.pricing_status.cget("text"))
    assert str(app.btn_roe_open.cget("state")) == "normal"


def test_the_newest_rate_sheet_is_offered(tmp_path):
    import os
    import time
    from src.roe_gui import latest_sheet
    old = tmp_path / "ROE 04th October.xlsx"
    new = tmp_path / "07th Oct ROW.xlsx"
    other = tmp_path / "Schedule.xlsx"
    for i, p in enumerate((old, new, other)):
        p.write_bytes(b"x")
        os.utime(p, (time.time() + i, time.time() + i))
    (tmp_path / "~$07th Oct ROW.xlsx").write_bytes(b"x")
    assert latest_sheet(tmp_path) == str(new)
