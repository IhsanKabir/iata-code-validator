"""Match input agency names/license numbers/addresses against the cached BD list.

Caller selects which fields to match against (any subset of
{name, license, address}). Default is name+license to preserve the v1.1.0
behaviour. The priority chain runs once per enabled field group:

  1. EXACT on every selected field
  2. CONTAINS on every selected field
        - 1 hit  → CONTAINS
        - N hits → MULTIPLE_CONTAINS, pick best by length similarity
  3. FUZZY (rapidfuzz partial_ratio ≥ FUZZY_THRESHOLD) on every selected field
  4. NO_MATCH

Each MatchResult also carries a `matched_field` ("Name" | "License" |
"Address" | "") so the operator knows which field produced the hit.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .bd_agency_client import Agency


FUZZY_THRESHOLD = 85  # rapidfuzz partial_ratio score (0-100)

# Public field tags
FIELD_NAME = "Name"
FIELD_LICENSE = "License"
FIELD_ADDRESS = "Address"

DEFAULT_FIELDS = (FIELD_NAME, FIELD_LICENSE)
ALL_FIELDS = (FIELD_NAME, FIELD_LICENSE, FIELD_ADDRESS)


@dataclass(frozen=True)
class MatchResult:
    searched_input: str
    match_method: str         # EXACT | CONTAINS | MULTIPLE_CONTAINS | FUZZY | NO_MATCH
    match_score: int          # 0-100
    matched_field: str        # Name | License | Address | "" on no match
    agency: Agency | None     # None on NO_MATCH
    other_matches: int        # how many additional candidates we discarded


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class AgencyIndex:
    """Pre-computed index over the agency list for fast matching.

    Build once per run; query many times.
    """

    def __init__(self, agencies: list[Agency]) -> None:
        self.agencies = agencies
        # exact-lookup dicts (key = normalised value)
        self._by_name: dict[str, list[Agency]] = {}
        self._by_license: dict[str, list[Agency]] = {}
        self._by_address: dict[str, list[Agency]] = {}
        # ordered (norm, agency) lists for substring + fuzzy passes
        self._name_norms: list[tuple[str, Agency]] = []
        self._license_norms: list[tuple[str, Agency]] = []
        self._address_norms: list[tuple[str, Agency]] = []

        for a in agencies:
            n, l, ad = _norm(a.agency_name), _norm(a.license_no), _norm(a.address)
            if n:
                self._by_name.setdefault(n, []).append(a)
                self._name_norms.append((n, a))
            if l:
                self._by_license.setdefault(l, []).append(a)
                self._license_norms.append((l, a))
            if ad:
                self._by_address.setdefault(ad, []).append(a)
                self._address_norms.append((ad, a))

    # ------------------------------------------------------------------
    # Per-input lookup
    # ------------------------------------------------------------------

    def lookup(
        self,
        input_value: str,
        fields: tuple[str, ...] = DEFAULT_FIELDS,
    ) -> MatchResult:
        original = input_value
        q = _norm(input_value)
        if not q:
            return _no_match(original)
        if not fields:
            fields = DEFAULT_FIELDS

        # 1. EXACT — try selected fields in priority order
        for field in fields:
            agencies = self._exact_for(field).get(q)
            if agencies:
                return MatchResult(
                    searched_input=original,
                    match_method="EXACT",
                    match_score=100,
                    matched_field=field,
                    agency=agencies[0],
                    other_matches=max(0, len(agencies) - 1),
                )

        # 2. CONTAINS — search each selected field, deduplicating by agency.id
        contains_hits = self._contains(q, fields)
        if contains_hits:
            agencies = [a for a, _f in contains_hits]
            if len(agencies) == 1:
                return MatchResult(
                    searched_input=original,
                    match_method="CONTAINS",
                    match_score=100,
                    matched_field=contains_hits[0][1],
                    agency=agencies[0],
                    other_matches=0,
                )
            # Multiple hits → pick best by name-length similarity
            best_idx = min(
                range(len(contains_hits)),
                key=lambda i: abs(len(_norm(contains_hits[i][0].agency_name)) - len(q)),
            )
            best_agency, best_field = contains_hits[best_idx]
            return MatchResult(
                searched_input=original,
                match_method="MULTIPLE_CONTAINS",
                match_score=100,
                matched_field=best_field,
                agency=best_agency,
                other_matches=len(contains_hits) - 1,
            )

        # 3. FUZZY — try each selected field, take the best winning score
        fuzzy_hit = self._fuzzy(q, fields)
        if fuzzy_hit is not None:
            agency, score, count, field = fuzzy_hit
            return MatchResult(
                searched_input=original,
                match_method="FUZZY",
                match_score=int(score),
                matched_field=field,
                agency=agency,
                other_matches=max(0, count - 1),
            )

        return _no_match(original)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _exact_for(self, field: str) -> dict[str, list[Agency]]:
        if field == FIELD_NAME:
            return self._by_name
        if field == FIELD_LICENSE:
            return self._by_license
        if field == FIELD_ADDRESS:
            return self._by_address
        return {}

    def _norms_for(self, field: str) -> list[tuple[str, Agency]]:
        if field == FIELD_NAME:
            return self._name_norms
        if field == FIELD_LICENSE:
            return self._license_norms
        if field == FIELD_ADDRESS:
            return self._address_norms
        return []

    def _contains(
        self, q: str, fields: tuple[str, ...]
    ) -> list[tuple[Agency, str]]:
        """Substring search across selected fields. Returns list of
        (agency, field_label) deduped by agency id, in field-priority order."""
        seen: set[int] = set()
        hits: list[tuple[Agency, str]] = []
        for field in fields:
            for n, a in self._norms_for(field):
                if q in n and a.raw_id not in seen:
                    hits.append((a, field))
                    seen.add(a.raw_id)
        return hits

    def _fuzzy(
        self, q: str, fields: tuple[str, ...]
    ) -> tuple[Agency, float, int, str] | None:
        """Return (best_agency, score, total_candidates_above_threshold, field)
        or None if no fuzzy match clears FUZZY_THRESHOLD across any field.
        """
        best_overall: tuple[Agency, float, int, str] | None = None
        for field in fields:
            norms = self._norms_for(field)
            if not norms:
                continue
            choices = [n for (n, _) in norms]
            candidates = process.extract(
                q,
                choices,
                scorer=fuzz.partial_ratio,
                score_cutoff=FUZZY_THRESHOLD,
                limit=10,
            )
            if not candidates:
                continue
            top_choice, top_score, top_idx = candidates[0]
            agency = norms[top_idx][1]
            if best_overall is None or top_score > best_overall[1]:
                best_overall = (agency, top_score, len(candidates), field)
        return best_overall


def _no_match(searched_input: str) -> MatchResult:
    return MatchResult(
        searched_input=searched_input,
        match_method="NO_MATCH",
        match_score=0,
        matched_field="",
        agency=None,
        other_matches=0,
    )
