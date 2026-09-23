"""How many seats an aircraft carries, and how sure we are of the number.

Seat counts decide the whole competitive picture: on DAC-CCU an IndiGo A320
against a US-Bangla ATR is 180 seats against 72, so counting FLIGHTS says we
hold a third of the market and counting SEATS says a tenth. The second is the
one that matters, and it needs a number for every aircraft type in the data.

Those numbers come from three places, and the difference is recorded rather
than smoothed over, because two of them are weaker than the third:

* OPERATOR -- read from our own load workbook. US-Bangla's ATR 72-600 really
  does carry 72 and its A330-300 really does carry 436, because those are the
  capacities the airline files against its own flights. Nothing beats this.
* AIRLINE -- a published configuration for a specific carrier's sub-fleet,
  from general aviation knowledge. Biman's Q400 at 74 and its 777-300ER at
  419 are this kind.
* TYPE -- a typical configuration for the aircraft, with no carrier named.
  The weakest, used only where nothing better exists.

An operator flies the same type in several layouts, and a figure here can be
wrong by a dozen seats either way. `SeatEstimate.confidence` says which basis
produced it so a report can show the shares as approximate rather than exact,
and `unknown` is returned rather than a guessed default where the type is not
recognised at all -- a made-up number in a share calculation is worse than a
visible gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OPERATOR = "operator"      # from the airline's own filed capacity
AIRLINE = "airline"        # published config for that carrier
TYPE = "type"              # typical config for the aircraft
UNKNOWN = "unknown"

#: Per-carrier configurations, where a carrier differs enough from the
#: generic type to matter. General aviation knowledge, not read from data.
BY_AIRLINE = {
    "BG": {"q400": 74, "dash8": 74, "dh8": 74, "737-800": 162,
           "787-8": 271, "787-9": 298, "777-300": 419},
    "6E": {"a320": 180, "a320neo": 186, "a321": 222, "a321neo": 232,
           "atr72": 78},
    "QR": {"a320": 132, "a350-900": 283, "a350-1000": 327, "787-8": 254,
           "777-300": 354, "a330-300": 260},
    "EK": {"777-300": 354, "777-200": 302, "a380": 489},
    "SV": {"777-300": 417, "a320": 117, "a321": 203, "787-9": 298},
    "MH": {"737-800": 160, "737 max 8": 162, "a330-300": 290,
           "a350-900": 286},
    "SQ": {"737-800": 162, "737 max 8": 154, "787-10": 337,
           "a350-900": 253, "a380": 471},
    "TG": {"a320": 174, "787-8": 264, "787-9": 298, "a350-900": 321},
    "UL": {"a320neo": 180, "a321neo": 199, "a330-300": 297},
    "FZ": {"737-800": 174, "737 max 8": 174},
    "CZ": {"a320": 174, "a321": 208, "787-9": 276, "a330-300": 283},
    "MU": {"737-800": 164, "a320": 158, "a330-300": 292},
    "VQ": {"atr72": 72},
    "2A": {"atr72": 70},
    "WY": {"737-800": 162, "737 max 8": 162, "787-9": 288},
    "EY": {"787-9": 299, "a320": 162, "a321": 192},
    "TK": {"a321": 194, "a321neo": 196, "737 max 8": 151},
    "GF": {"a320": 150, "a320neo": 150, "787-9": 282},
}

#: Typical all-economy or common two-class layouts, by type. Used only where
#: the carrier is not listed above.
BY_TYPE = {
    "atr72": 72, "atr42": 48,
    "q400": 78, "dash8": 78, "dh8": 78,
    "a319": 144, "a320": 180, "a320neo": 186,
    "a321": 220, "a321neo": 232,
    "737-700": 149, "737-800": 189, "737-900": 215, "737 max 8": 189,
    "757": 200, "767": 245,
    "777-200": 313, "777-300": 396,
    "787-8": 242, "787-9": 296, "787-10": 337,
    "a330-200": 247, "a330-300": 277, "a330-900": 287,
    "a350-900": 315, "a350-1000": 350,
    "a380": 525, "747": 410,
    "e190": 100, "e175": 76, "crj900": 90,
    # Bare families, for feeds that write 'Boeing-787' with no variant.
    # Matched only after every longer key has failed, because '787-9' is a
    # better answer than '787' whenever the variant is actually given.
    "787": 290, "777": 350, "767": 245, "757": 200,
    "737": 180, "330": 280, "350": 320, "320": 180, "321": 220,
}

@dataclass(frozen=True)
class SeatEstimate:
    seats: int | None
    confidence: str
    basis: str = ""

    @property
    def known(self) -> bool:
        return self.seats is not None

    def __str__(self) -> str:
        if not self.known:
            return "unknown"
        return f"{self.seats} ({self.confidence})"


def normalise(aircraft) -> str:
    """Collapse a feed's spelling to letters and digits only.

    'Boeing 737-800 Winglets' -> '737800winglets', 'ATR 72 - 600' ->
    'atr72600', 'Airbus-A330-300' -> 'a330300'. Separators are dropped on
    both sides of the comparison rather than guessed at, because the feed
    writes one aircraft as 'ATR-72-600', 'ATR 72 - 600' and 'ATR72'.
    """
    text = str(aircraft or "").lower()
    text = text.replace("boeing", " ").replace("airbus", " ")
    return re.sub(r"[^a-z0-9]+", "", text)


def _collapse(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _match(text: str, table: dict):
    """Longest collapsed key that appears in `text`.

    The table's own keys are collapsed too, so a hand-written 'a330-300'
    and an operator table's 'a330300' both match, and longest-first stops
    'a320' claiming an 'a320neo'.
    """
    collapsed = {_collapse(k): v for k, v in table.items()}
    for key in sorted(collapsed, key=len, reverse=True):
        if key and key in text:
            return collapsed[key]
    return None


def seats_for(aircraft, airline: str = "",
              operator_seats: dict | None = None) -> SeatEstimate:
    """Seats for one aircraft, with where the number came from.

    `operator_seats` maps a normalised type to a capacity read from the
    airline's own records; it wins over everything, because it is the
    capacity that carrier actually files.
    """
    text = normalise(aircraft)
    if not text:
        return SeatEstimate(None, UNKNOWN)
    code = str(airline or "").upper().strip()

    if operator_seats:
        got = _match(text, operator_seats)
        if got:
            return SeatEstimate(int(got), OPERATOR, f"{code} own records")
    if code in BY_AIRLINE:
        got = _match(text, BY_AIRLINE[code])
        if got:
            return SeatEstimate(int(got), AIRLINE, f"{code} published config")
    got = _match(text, BY_TYPE)
    if got:
        return SeatEstimate(int(got), TYPE, "typical configuration")
    # Nothing recognised. A guessed default inside a share calculation is
    # worse than a gap somebody can see.
    return SeatEstimate(None, UNKNOWN)


def operator_table(legs) -> dict:
    """Modal capacity per aircraft type, from an airline's own load records.

    The modal value, not the mean: the capacity column moves a little with
    saleable seats, so 72 appears far more often than the 62-78 around it,
    and the mode picks the real layout where an average would not.
    """
    from collections import Counter, defaultdict

    seen = defaultdict(Counter)
    for leg in legs:
        key = normalise(getattr(leg, "aircraft", ""))
        cap = getattr(leg, "capacity", None)
        if key and cap:
            seen[key][int(cap)] += 1
    return {k: counts.most_common(1)[0][0] for k, counts in seen.items()}
