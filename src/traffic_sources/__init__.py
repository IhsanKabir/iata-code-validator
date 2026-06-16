"""Registry of Traffic data sources.

Every source is a singleton that conforms to the `TrafficSource` Protocol and
is registered in `SOURCES` (insertion-ordered = dropdown order). The GUI and
worker only ever know about this registry + the `TrafficRow` shape, so adding
a source is purely additive: write `traffic_sources/<x>.py` returning
list[TrafficRow], import its singleton here, and add it to SOURCES.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import requests

from ..traffic_client import ProgressCallback, TrafficRow


@dataclass(frozen=True)
class Option:
    """A selectable filter option (e.g. a country or period) for a source."""

    value: str
    label: str


@runtime_checkable
class TrafficSource(Protocol):
    """Contract every source module's singleton implements."""

    id: str               # registry key, e.g. "malaysia_arrivals"
    label: str            # human label for the dropdown
    granularity: str      # "country" | "airport" | "route" (drives default view)
    needs_credentials: bool  # True for paid/API-key sources (Phase 4)
    needs_file: bool      # True if fetch() needs a local file (filters['csv_path'])

    def list_filter_options(
        self, session: requests.Session
    ) -> dict[str, list[Option]]:
        """Optional filter choices (e.g. {'country': [...]}); {} if none."""
        ...

    def fetch(
        self,
        filters: dict,
        *,
        session: requests.Session | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> list[TrafficRow]:
        """Pull + normalize the source into unified rows. Raises TrafficError."""
        ...


# --- registered sources (ordered = dropdown order) ------------------------
# Import singletons after the Protocol is defined to avoid a forward ref.
# Clean reachable APIs first; India needs GitHub; BTS is file-based.
from .malaysia_arrivals import SOURCE as _malaysia  # noqa: E402
from .singapore_changi import SOURCE as _singapore  # noqa: E402
from .qatar_hamad import SOURCE as _qatar  # noqa: E402
from .india_dgca import SOURCE as _india  # noqa: E402
from .bts_t100 import SOURCE as _bts  # noqa: E402

SOURCES: "dict[str, TrafficSource]" = {
    _malaysia.id: _malaysia,
    _singapore.id: _singapore,
    _qatar.id: _qatar,
    _india.id: _india,
    _bts.id: _bts,
}
