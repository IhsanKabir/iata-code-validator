"""Filter presets for the OEP tab.

Each preset captures every dimension the user can tweak so a single
click reproduces an earlier view. Stored as JSON in `APP_DIR` — humans
can edit the file in a text editor if needed.

A preset name must be unique; saving with an existing name replaces it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OEPPreset:
    """A reproducible filter set for the OEP tab."""

    name: str
    mode: str                       # country | division | category | gender | timeseries | pivot
    date_from: str
    date_to: str
    gender_id: str = ""             # "", "1", "2", "3"
    country_ids: list[str] = field(default_factory=list)
    country_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OEPPreset":
        return cls(
            name=str(d.get("name", "")).strip(),
            mode=str(d.get("mode", "country")),
            date_from=str(d.get("date_from", "")),
            date_to=str(d.get("date_to", "")),
            gender_id=str(d.get("gender_id", "")),
            country_ids=[str(x) for x in (d.get("country_ids") or [])],
            country_labels=[str(x) for x in (d.get("country_labels") or [])],
        )


class PresetStore:
    """JSON-backed preset list. Cheap and human-readable.

    The file lives in `APP_DIR / oep_presets.json`. It's read on every
    public call so two app instances can't clobber each other silently —
    each save reads first, merges, writes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------- I/O -------------------

    def _read_all(self) -> list[OEPPreset]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.warning("OEP preset file is corrupt (%s) — starting fresh", e)
            return []
        if not isinstance(raw, list):
            log.warning("OEP preset file root is not a list — ignoring")
            return []
        out: list[OEPPreset] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(OEPPreset.from_dict(entry))
            except (TypeError, ValueError) as e:
                log.warning("Skipping malformed preset %r: %s", entry, e)
        return out

    def _write_all(self, presets: list[OEPPreset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([p.to_dict() for p in presets], indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ------------------- public API -------------------

    def list_names(self) -> list[str]:
        return [p.name for p in self._read_all()]

    def list(self) -> list[OEPPreset]:
        return self._read_all()

    def get(self, name: str) -> OEPPreset | None:
        for p in self._read_all():
            if p.name == name:
                return p
        return None

    def save(self, preset: OEPPreset) -> None:
        """Insert or replace by name. No-op if name is blank."""
        if not preset.name.strip():
            raise ValueError("Preset name cannot be empty.")
        presets = self._read_all()
        presets = [p for p in presets if p.name != preset.name]
        presets.append(preset)
        presets.sort(key=lambda p: p.name.lower())
        self._write_all(presets)

    def delete(self, name: str) -> bool:
        before = self._read_all()
        after = [p for p in before if p.name != name]
        if len(after) == len(before):
            return False
        self._write_all(after)
        return True
