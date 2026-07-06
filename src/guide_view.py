"""In-app visual User Guide — a graphical, click-through walkthrough of every feature.

Content is data (the TOPIC lists) so it is easy to edit and unit-test; rendering is
pure Tkinter/ttk with a small Canvas layer for the graphical bits:

  * a left-hand topic rail (emoji + name),
  * a per-topic HERO FLOW strip — rounded boxes joined by arrows showing the workflow
    at a glance (drawn on a Canvas, background matched to the current theme),
  * numbered STEP CARDS (a circular badge + emoji + bold title + wrapped body), and
  * colour-coded CALLOUTS (tip / note / warning).

`build_guide(parent, app, app_name)` builds the whole panel. `app` is duck-typed: it
must expose `_make_scrollable`, the `_COLOR_*` palette, and `_theme_is_dark`, so the
guide re-themes with the rest of the app. `topics_for(app_name)` returns the content
only, which the tests exercise without a display.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

# --- content model ----------------------------------------------------------

CALLOUT_KINDS = ("tip", "note", "warning")


@dataclass(frozen=True)
class Step:
    icon: str          # one emoji
    title: str
    body: str


@dataclass(frozen=True)
class Callout:
    kind: str          # tip | note | warning
    text: str


@dataclass(frozen=True)
class Topic:
    key: str
    icon: str
    title: str
    tagline: str
    flow: tuple[str, ...]              # hero-diagram box labels, in order
    steps: tuple[Step, ...]
    callouts: tuple[Callout, ...] = ()


def S(icon: str, title: str, body: str) -> Step:
    return Step(icon=icon, title=title, body=body)


def C(kind: str, text: str) -> Callout:
    return Callout(kind=kind, text=text)


# --- shared topics ----------------------------------------------------------

_EMAIL_TOPIC = Topic(
    key="email",
    icon="✉️",
    title="Bulk Mailer — Email",
    tagline="One personalised email per recipient, each with its own attachment.",
    flow=("Pick Excel", "Preview rows", "Test to self", "Draft or Send"),
    steps=(
        S("📄", "Two ways to feed recipients",
          "Classic: a mapping sheet with Email · Name · File · CC · BCC. Or use "
          "Split & Send — one main sheet with an email column, and the app makes "
          "one file per address for you (see the Split & Send guide)."),
        S("📎", "Point at the attachments folder",
          "The File column is looked up inside the folder you pick. Separate several "
          "files in one cell with ; or | for multi-attachment rows."),
        S("✍️", "Write the message once",
          "Use {name} or ANY column as a placeholder, e.g. “Dear {name}, your "
          "{FFP Level} miles…”. Unknown placeholders are left as-is, never crash a run."),
        S("👁️", "Preview, then Test to yourself",
          "Load + preview validates every row (email + attachment) — bad rows turn "
          "red. Always Send test to myself first to see the real thing."),
        S("📤", "Draft (safe) or Send",
          "Default is Create drafts so you review before anything leaves. Switch to "
          "Send now when you're confident. Skip-already-sent protects re-runs; the "
          "Delay slows a big run to look human."),
    ),
    callouts=(
        C("tip", "Choose your sender under Send via: Outlook desktop, Microsoft 365 "
                 "sign-in, or any SMTP host (Gmail app-password, etc.)."),
        C("note", "Nothing is sent until you confirm the count in the dialog."),
    ),
)

_WHATSAPP_TOPIC = Topic(
    key="whatsapp",
    icon="🟢",
    title="Bulk Mailer — WhatsApp",
    tagline="Free WhatsApp text (+ one shared image) from your own number.",
    flow=("Pick sheet + phone col", "Scan QR once", "Preview 2–3", "Test to self", "Send"),
    steps=(
        S("📱", "Pick the sheet and phone column",
          "Load your Excel, choose the sheet, then the column with mobile numbers "
          "(auto-guessed). Set the country code (default +880). Local numbers like "
          "01812… become +88018…; unreachable rows are parked, never dialled."),
        S("🖼️", "Optional: one shared image",
          "Attach a single flyer/photo sent to everyone with your text as the caption. "
          "Text-only works too."),
        S("🔑", "Open WhatsApp & scan the QR — once",
          "Click the login button; a WhatsApp Web window opens. Scan the QR with your "
          "phone (Linked devices). The session is remembered for next time."),
        S("🐢", "Choose a speed preset",
          "🟢 Safe is slow with long gaps — start here. 🟡 Normal and 🔴 Fast go quicker "
          "but raise the risk. A daily cap and per-message delay protect your number."),
        S("🚀", "Preview a few you own, then Send",
          "Preview with 2–3 of your OWN numbers first and WATCH the browser as it "
          "sends. Then run the batch."),
    ),
    callouts=(
        C("warning", "WhatsApp forbids automation. For your own members in small, slow "
                     "batches the risk is low — but WhatsApp CAN ban the sending number. "
                     "There is no free official bulk API; this is the trade-off for free."),
        C("tip", "Keep batches modest and the speed on 🟢 Safe for anything important. "
                 "Skip-already-sent means a re-run only messages who was missed."),
    ),
)

_HEALTH_TOPIC = Topic(
    key="health",
    icon="🩺",
    title="Health",
    tagline="One click tells you whether every feature can work right now.",
    flow=("Open Health", "Run all checks", "Read green / amber / red"),
    steps=(
        S("▶️", "Click “Run all checks”",
          "It probes the local engine (browser, caches, report stack, Outlook) and "
          "whether each external site the tools depend on is reachable."),
        S("🚦", "Read the colours",
          "Green = good. Amber = works but watch it. Red = broken — with a fix hint in "
          "the last column pointing you at the cause."),
        S("🔎", "Use it when something feels off",
          "If a feature suddenly stops (a site changed, you're offline, Outlook isn't "
          "running), Health points straight at the culprit instead of guessing."),
    ),
    callouts=(
        C("note", "A red row is not always your fault — an external website being down "
                  "shows red here too. The hint tells you which."),
    ),
)

_GETTING_STARTED_CONSOLE = Topic(
    key="start",
    icon="🚀",
    title="Getting Started",
    tagline="What each tab does and how to move around.",
    flow=("Sign in", "Pick a tab", "Follow the guide", "Check Health if stuck"),
    steps=(
        S("🔐", "Sign in once",
          "Sign in with your Google account at launch. The app remembers you; use "
          "Sign out (top-right) to switch users."),
        S("🗂️", "Each tab is a tool",
          "IATA Code Validator, BD Travel Agency Lookup, Traffic Movement, Zenith, "
          "Bulk Mailer, and Health. Tabs build when you first open them, so switching "
          "is instant after that."),
        S("💾", "Your work is cached locally",
          "Lookups are saved on your machine (the “(N cached)” count on a tab), so "
          "re-running skips what's already done and works even offline for cached data."),
        S("🎨", "Light or dark",
          "Toggle the theme any time with the ☾/☀ button, top-right. The window "
          "remembers its size and position for next launch."),
    ),
    callouts=(
        C("tip", "Stuck on any feature? Open this Guide's matching topic on the left, "
                 "or the Health tab to check if something's actually down."),
    ),
)


# --- app-specific topic sets ------------------------------------------------

_CONSOLE_TOPICS: tuple[Topic, ...] = (
    _GETTING_STARTED_CONSOLE,
    Topic(
        key="iata", icon="🔢", title="IATA Code Validator",
        tagline="Bulk-validate IATA numeric codes against IATA's CheckACode page.",
        flow=("Pick Excel", "Sheet + column", "Run", "Excel out"),
        steps=(
            S("📄", "Load the Excel of codes",
              "Pick the file, then the sheet and the column that holds the IATA codes. "
              "Optionally set a start/end row."),
            S("▶️", "Run — it validates each code",
              "The app checks every code against IATA's public page. Pause, Resume, and "
              "Stop are there for long runs; a CAPTCHA prompt appears only if needed."),
            S("📊", "Get the Excel back",
              "Agency name, city, country and status are written to a new workbook in "
              "your output folder. Re-runs skip codes already cached."),
        ),
        callouts=(C("note", "If validation stalls, check the Health tab — IATA's site "
                            "may have changed or be unreachable."),),
    ),
    Topic(
        key="bd", icon="🏢", title="BD Travel Agency Lookup",
        tagline="Look up Bangladesh travel agencies from regtravelagency.gov.bd.",
        flow=("Refresh list", "Full export or Lookup", "Excel out"),
        steps=(
            S("↻", "Refresh the agency list",
              "Downloads the full active register once and caches it locally (the "
              "“(N cached)” count). Refresh again only when you want fresh data."),
            S("🔀", "Full export or Lookup mode",
              "Full writes the whole register to Excel. Lookup matches YOUR Excel of "
              "names/licences against the register (Exact → Contains → Fuzzy)."),
            S("📊", "Export to Excel",
              "Each matched row is tagged with how it matched. Toggle name / licence / "
              "address matching and whether to include expired agencies."),
        ),
    ),
    Topic(
        key="traffic", icon="✈️", title="Traffic Movement",
        tagline="Air-traffic movement + BD overseas-employment (OEP) data.",
        flow=("Pick source", "Date range", "Run", "View + Excel"),
        steps=(
            S("🗃️", "Pick a source",
              "Choose the movement data source and an optional date range. The sub-tabs "
              "cover air traffic and BD overseas movement (OEP)."),
            S("▶️", "Run the report",
              "Results fill the grid; wide pivots scroll sideways. Double-click a row "
              "(where supported) to drill into detail."),
            S("📊", "Export to Excel",
              "Write the current view to a workbook for sharing or further analysis."),
        ),
    ),
    Topic(
        key="zenith", icon="🛫", title="Zenith",
        tagline="Customer / PNR / flight-load / history tools for the Zenith GDS.",
        flow=("Sign in to Zenith", "Pick a sub-tab", "Run", "Excel out"),
        steps=(
            S("🔐", "Sign in to Zenith",
              "Enter your Zenith credentials once per session. The Server picker sits "
              "in the header — the default direct-origin host avoids the 504 storm."),
            S("👥", "Customer & PNR lookups",
              "Bulk-look-up customers (persons AND travel agencies, incl. IATA number), "
              "or PNRs by code OR dossier ID — the app auto-detects which."),
            S("📈", "Flight loads, history & inspection",
              "Pull flight loads, run the Flight History Analyzer, and Inspect load "
              "factor to see WHY a flight filled the way it did — all to Excel."),
        ),
        callouts=(C("tip", "Skip-cached is on by default, so re-running a bulk job only "
                          "retries what failed."),),
    ),
    _EMAIL_TOPIC,
    Topic(
        key="split", icon="🪄", title="Bulk Mailer — Split & Send",
        tagline="One main sheet → one file + email per recipient. No manual prep.",
        flow=("Main Excel", "Pick email column", "Split", "Load → Send"),
        steps=(
            S("📄", "One sheet, an email on each row",
              "Keep all your data in ONE sheet where every row carries the recipient's "
              "email address in some column."),
            S("🎯", "Pick the email column",
              "Choose the sheet and the email column (auto-guessed). Set CC/BCC to apply "
              "to every message."),
            S("✂️", "Create split files",
              "The app writes ONE Excel per address (only that person's rows) into your "
              "folder. Blank/invalid addresses go to _UNMATCHED_ROWS.xlsx — never sent."),
            S("📤", "Split + load into mailer",
              "Loads those files straight into the Preview grid; each recipient is "
              "emailed only their own rows. Then Test / Draft / Send as usual."),
        ),
        callouts=(C("note", "A cell with several addresses (a@x.com; b@y.com) sends that "
                           "row to each. Placeholders: {email} {name} {rows} {file}."),),
    ),
    _WHATSAPP_TOPIC,
    _HEALTH_TOPIC,
)

_MAILER_TOPICS: tuple[Topic, ...] = (
    Topic(
        key="start", icon="🚀", title="Getting Started",
        tagline="Send personalised email or WhatsApp from one Excel list.",
        flow=("Load a sheet", "Write message", "Test to self", "Send"),
        steps=(
            S("📄", "Bring your list",
              "A mapping sheet (Email · Name · File · CC · BCC) or one main sheet with "
              "an email/phone column that the app splits for you."),
            S("✍️", "Write once, personalise with {placeholders}",
              "Use {name} or any column. The same message goes to everyone, filled in "
              "per row."),
            S("🧪", "Always test first",
              "Send a test to yourself before any real run, on email or WhatsApp."),
        ),
        callouts=(C("tip", "Default mode is Drafts for email — review before sending. "
                          "Use the Health tab if something isn't working."),),
    ),
    _EMAIL_TOPIC,
    Topic(
        key="split", icon="🪄", title="Split & Send by email column",
        tagline="One main sheet → one file + email per recipient. No manual prep.",
        flow=("Main Excel", "Pick email column", "Split", "Load → Send"),
        steps=(
            S("🎯", "Pick sheet + email column",
              "Choose the sheet and the email column (auto-guessed); optionally CC/BCC "
              "for every message."),
            S("✂️", "Create split files",
              "One Excel per address (only that person's rows) into your folder; "
              "blank/invalid go to _UNMATCHED_ROWS.xlsx, never sent."),
            S("📤", "Split + load into mailer",
              "Loads the files into Preview; each recipient gets only their own rows. "
              "Test / Draft / Send as usual."),
        ),
    ),
    _WHATSAPP_TOPIC,
    _HEALTH_TOPIC,
)


def topics_for(app_name: str) -> tuple[Topic, ...]:
    return _MAILER_TOPICS if app_name == "mailer" else _CONSOLE_TOPICS


# --- rendering --------------------------------------------------------------

_ACCENT = {"tip": "#107C10", "note": "#0F6CBD", "warning": "#9D5D00"}
_TINT_LIGHT = {"tip": "#DFF6DD", "note": "#E5F1FB", "warning": "#FFF4CE"}
_TINT_DARK = {"tip": "#1E3A24", "note": "#16324A", "warning": "#4A3A14"}
_CALLOUT_ICON = {"tip": "💡", "note": "ℹ️", "warning": "⚠️"}


def _frame_bg(is_dark: bool) -> str:
    try:
        bg = ttk.Style().lookup("TFrame", "background")
        if bg:
            return bg
    except tk.TclError:
        pass
    return "#1c1c1c" if is_dark else "#fafafa"


def _round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    cv.create_polygon(
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        smooth=True, **kw)


def _draw_flow(parent, labels, *, is_dark, bg):
    """Rounded boxes joined by arrows — the workflow at a glance."""
    accent = "#4C9AFF" if is_dark else "#0F6CBD"
    text_c = "#e6e6e6" if is_dark else "#0f1720"
    per_row, bw, bh, gap, ah = 4, 168, 52, 34, 22
    n = len(labels)
    rows = (n + per_row - 1) // per_row
    width = per_row * bw + (per_row - 1) * gap + 8
    height = rows * (bh + ah) + 8
    cv = tk.Canvas(parent, width=width, height=height, bg=bg,
                   highlightthickness=0, bd=0)
    for i, label in enumerate(labels):
        row, col = divmod(i, per_row)
        x1 = 4 + col * (bw + gap)
        y1 = 4 + row * (bh + ah)
        _round_rect(cv, x1, y1, x1 + bw, y1 + bh, 12,
                    fill="", outline=accent, width=2)
        cv.create_text(x1 + bw / 2, y1 + bh / 2, text=label, fill=text_c,
                       font=("Segoe UI Semibold", 9), width=bw - 16)
        if i < n - 1 and col < per_row - 1:
            ax = x1 + bw
            ay = y1 + bh / 2
            cv.create_line(ax + 4, ay, ax + gap - 4, ay, fill=accent, width=2,
                           arrow="last", arrowshape=(8, 9, 4))
    return cv


def _number_badge(parent, n, *, color, bg):
    cv = tk.Canvas(parent, width=30, height=30, bg=bg, highlightthickness=0, bd=0)
    cv.create_oval(2, 2, 28, 28, fill=color, outline="")
    cv.create_text(15, 15, text=str(n), fill="white", font=("Segoe UI Semibold", 11))
    return cv


def _step_card(parent, n, step: Step, *, is_dark, bg, accent):
    card = ttk.Frame(parent)
    card.pack(fill="x", pady=(0, 10))
    _number_badge(card, n, color=accent, bg=bg).grid(
        row=0, column=0, rowspan=2, sticky="n", padx=(0, 12), pady=(2, 0))
    head = ttk.Label(card, text=f"{step.icon}  {step.title}",
                     font=("Segoe UI Semibold", 11))
    head.grid(row=0, column=1, sticky="w")
    body = ttk.Label(card, text=step.body, wraplength=680, justify="left",
                     foreground="#94a3b8" if is_dark else "#475569")
    body.grid(row=1, column=1, sticky="w", pady=(2, 0))
    card.columnconfigure(1, weight=1)
    return card


def _callout(parent, c: Callout, *, is_dark, bg):
    tint = (_TINT_DARK if is_dark else _TINT_LIGHT)[c.kind]
    accent = _ACCENT[c.kind]
    wrap = tk.Frame(parent, bg=tint, highlightthickness=1, highlightbackground=accent)
    wrap.pack(fill="x", pady=(0, 10))
    tk.Label(wrap, text=_CALLOUT_ICON[c.kind], bg=tint,
             font=("Segoe UI", 12)).pack(side="left", padx=(10, 6), pady=8)
    tk.Label(wrap, text=c.text, bg=tint,
             fg="#e6e6e6" if is_dark else "#1f2937",
             wraplength=680, justify="left", font=("Segoe UI", 9)).pack(
        side="left", fill="x", expand=True, padx=(0, 10), pady=8)
    return wrap


def build_guide(parent, app, app_name: str) -> None:
    """Build the two-pane visual guide into `parent` (a tab frame)."""
    topics = topics_for(app_name)
    is_dark = bool(getattr(app, "_theme_is_dark", False))
    bg = _frame_bg(is_dark)
    accent = getattr(app, "_COLOR_PRIMARY", "#0078D4")

    outer = ttk.Frame(parent)
    outer.pack(fill="both", expand=True, padx=6, pady=6)

    # left rail
    rail = ttk.Frame(outer)
    rail.pack(side="left", fill="y", padx=(0, 8))
    ttk.Label(rail, text="User Guide", font=("Segoe UI Semibold", 12)).pack(
        anchor="w", pady=(2, 8))
    content_host = ttk.Frame(outer)
    content_host.pack(side="left", fill="both", expand=True)

    state: dict = {"body": None, "buttons": {}}

    def show(topic: Topic) -> None:
        if state["body"] is not None:
            state["body"].destroy()
        body = app._make_scrollable(content_host)
        state["body"] = body.master.master  # the outer frame _make_scrollable created
        # header
        ttk.Label(body, text=f"{topic.icon}  {topic.title}",
                  font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=4, pady=(2, 2))
        ttk.Label(body, text=topic.tagline, foreground=app._COLOR_MUTED,
                  wraplength=720, justify="left").pack(anchor="w", padx=4, pady=(0, 10))
        _draw_flow(body, topic.flow, is_dark=is_dark, bg=bg).pack(
            anchor="w", padx=4, pady=(0, 14))
        for i, step in enumerate(topic.steps, 1):
            _step_card(body, i, step, is_dark=is_dark, bg=bg, accent=accent)
        if topic.callouts:
            ttk.Separator(body).pack(fill="x", pady=(4, 10))
            for c in topic.callouts:
                _callout(body, c, is_dark=is_dark, bg=bg)
        for key, btn in state["buttons"].items():
            btn.configure(style="Primary.TButton" if key == topic.key else "TButton")

    for topic in topics:
        b = ttk.Button(rail, text=f"{topic.icon}  {topic.title}", width=26,
                       command=lambda t=topic: show(t))
        b.pack(fill="x", pady=1)
        state["buttons"][topic.key] = b

    if topics:
        show(topics[0])
