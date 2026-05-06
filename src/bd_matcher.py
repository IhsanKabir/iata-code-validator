"""Match input agency names/license numbers against the cached BD agency list.

Priority:
  1. EXACT             — case-insensitive trimmed equality on name OR license_no
  2. CONTAINS          — substring (case-insensitive) on name OR license_no
                         If multiple hits → MULTIPLE_CONTAINS, pick best by
                         length-similarity to the input
  3. FUZZY             — rapidfuzz partial_ratio ≥ FUZZY_THRESHOLD on name
  4. NO_MATCH

Each input row produces exactly one output row, tagged with the method that
won. `other_matches` reports how many additional candidates we discarded
(useful for manual review of MULTIPLE_CONTAINS or FUZZY hits).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rapidfuzz import fuzz, process

from .bd_agency_client import Agency


FUZZY_THRESHOLD = 85  # rapidfuzz partial_ratio score (0-100)


@dataclass(frozen=True)
class MatchResult:
    searched_input: str
    match_method: str        # EXACT | CONTAINS | MULTIPLE_CONTAINS | FUZZY | NO_MATCH
    match_score: int          # 0-100, 100 for EXACT/CONTAINS, rapidfuzz score for FUZZY
    agency: Agency | None     # None on NO_MATCH
    other_matches: int        # how many additional candidates were dropped


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class AgencyIndex:
    """Pre-computed index over the agency list for fast matching.

    Build once per run; query many times.
    """

    def __init__(self, agencies: list[Agency]) -> None:
        self.agencies = agencies
        self._by_name_norm: dict[str, list[Agency]] = {}
        self._by_license_norm: dict[str, list[Agency]] = {}
        # Pre-norm name + license once for substring loops
        self._name_norms: list[tuple[str, Agency]] = []
        self._license_norms: list[tuple[str, Agency]] = []

        for a in agencies:
            n = _norm(a.agency_name)
            l = _norm(a.license_no)
            if n:
                self._by_name_norm.setdefault(n, []).append(a)
                self._name_norms.append((n, a))
            if l:
                self._by_license_norm.setdefault(l, []).append(a)
                self._license_norms.append((l, a))

    # ------------------------------------------------------------------
    # Per-input lookup
    # ------------------------------------------------------------------

    def lookup(self, input_value: str) -> MatchResult:
        original = input_value
        q = _norm(input_value)
        if not q:
            return MatchResult(
                searched_input=original,
                match_method="NO_MATCH",
                match_score=0,
                agency=None,
                other_matches=0,
            )

        # 1. EXACT on name OR license
        exact = self._by_name_norm.get(q) or self._by_license_norm.get(q)
        if exact:
            return MatchResult(
                searched_input=original,
                match_method="EXACT",
                match_score=100,
                agency=exact[0],
                other_matches=max(0, len(exact) - 1),
            )

        # 2. CONTAINS on name OR license
        contains_hits = self._contains(q)
        if contains_hits:
            if len(contains_hits) == 1:
                return MatchResult(
                    searched_input=original,
                    match_method="CONTAINS",
                    match_score=100,
                    agency=contains_hits[0],
                    other_matches=0,
                )
            # Multiple → pick the one whose name length is closest to the input length
            best = min(
                contains_hits,
                key=lambda a: abs(len(_norm(a.agency_name)) - len(q)),
            )
            return MatchResult(
                searched_input=original,
                match_method="MULTIPLE_CONTAINS",
                match_score=100,
                agency=best,
                other_matches=len(contains_hits) - 1,
            )

        # 3. FUZZY on name (rapidfuzz)
        fuzzy = self._fuzzy(q)
        if fuzzy is not None:
            agency, score, count_above = fuzzy
            return MatchResult(
                searched_input=original,
                match_method="FUZZY",
                match_score=int(score),
                agency=agency,
                other_matches=max(0, count_above - 1),
            )

        return MatchResult(
            searched_input=original,
            match_method="NO_MATCH",
            match_score=0,
            agency=None,
            other_matches=0,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _contains(self, q: str) -> list[Agency]:
        seen_ids: set[int] = set()
        hits: list[Agency] = []
        for n, a in self._name_norms:
            if q in n and a.raw_id not in seen_ids:
                hits.append(a)
                seen_ids.add(a.raw_id)
        for l, a in self._license_norms:
            if q in l and a.raw_id not in seen_ids:
                hits.append(a)
                seen_ids.add(a.raw_id)
        return hits

    def _fuzzy(self, q: str) -> tuple[Agency, float, int] | None:
        """Return (best_agency, score, count_of_candidates_above_threshold)
        or None if no fuzzy match clears FUZZY_THRESHOLD.
        """
        if not self._name_norms:
            return None
        # Use rapidfuzz process.extract for vectorised scoring
        choices = [n for (n, _) in self._name_norms]
        candidates = process.extract(
            q,
            choices,
            scorer=fuzz.partial_ratio,
            score_cutoff=FUZZY_THRESHOLD,
            limit=10,
        )
        if not candidates:
            return None
        # Best is the first
        best_choice, best_score, best_idx = candidates[0]
        best_agency = self._name_norms[best_idx][1]
        return best_agency, best_score, len(candidates)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def match_many(
    inputs: Iterable[str], agencies: list[Agency]
) -> list[MatchResult]:
    """Match a batch of input values against `agencies`. Builds the index once."""
    idx = AgencyIndex(agencies)
    return [idx.lookup(v) for v in inputs]
