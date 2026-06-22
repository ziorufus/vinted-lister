"""Resolve human-friendly names to Vinted IDs (brand, colour, size, condition,
material, category). Fetches and caches the live lists where needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .client import VintedClient

ROOT = Path(__file__).resolve().parent.parent
LOOKUP = ROOT / "data" / "catalog_lookup_se.json"

# Confirmed condition ids (dynamic item_attribute "condition").
CONDITION_IDS = {
    "new_with_tags": 6, "ny med prislapp": 6, "nytt med prislapp": 6,
    "new_without_tags": 1, "ny utan prislapp": 1, "nytt utan prislapp": 1,
    "very_good": 2, "mycket bra": 2,
    "good": 3, "bra": 3,
    "satisfactory": 4, "tillfredsstallande": 4, "tillfredsställande": 4,
}

# Confirmed materials (extend as you discover more ids).
MATERIAL_IDS = {"cotton": 44, "bomull": 44}


def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class Resolver:
    def __init__(self, client: VintedClient) -> None:
        self.c = client
        self._colors: Optional[dict[str, int]] = None
        self._sizes: dict[int, dict[str, int]] = {}
        self._lookup: Optional[dict] = None

    # ----- catalog ---------------------------------------------------------
    @property
    def lookup(self) -> dict:
        if self._lookup is None:
            self._lookup = json.loads(LOOKUP.read_text(encoding="utf-8")) if LOOKUP.exists() else {}
        return self._lookup

    def catalog_path(self, cid) -> str:
        return self.lookup.get(str(cid), {}).get("path", "?")

    def search_catalogs(self, keyword: str) -> list[tuple[int, str]]:
        r = self.c.get(
            f"{self.c.base}/item_upload/catalogs/search",
            params={"keyword": keyword}, timeout=30,
        )
        ids = r.json().get("catalog_ids", []) if r.status_code == 200 else []
        return [(i, self.catalog_path(i)) for i in ids]

    def catalog(self, value: str) -> tuple[Optional[int], str]:
        cid = _as_int(value)
        if cid is not None:
            return cid, self.catalog_path(cid)
        hits = self.search_catalogs(value)
        if not hits:
            return None, "?"
        # prefer men's (Herr) results
        hits.sort(key=lambda h: (0 if "Herr" in h[1] else 1))
        return hits[0]

    # ----- colours ---------------------------------------------------------
    def colors(self) -> dict[str, int]:
        if self._colors is None:
            r = self.c.get(f"{self.c.base}/colors", timeout=30)
            self._colors = {}
            for col in (r.json().get("colors", []) if r.status_code == 200 else []):
                cid = col.get("id")
                for key in (col.get("title"), col.get("code")):
                    if key:
                        self._colors[str(key).lower()] = cid
        return self._colors

    def color_id(self, value: str) -> Optional[int]:
        cid = _as_int(value)
        return cid if cid is not None else self.colors().get(str(value).lower())

    # ----- sizes -----------------------------------------------------------
    def sizes(self, group: int = 14) -> dict[str, int]:
        if group not in self._sizes:
            r = self.c.get(f"{self.c.base}/size_groups", timeout=30)
            table: dict[str, int] = {}
            for g in (r.json().get("size_groups", []) if r.status_code == 200 else []):
                if g.get("id") == group:
                    for s in g.get("sizes", []):
                        table[str(s.get("title", "")).lower()] = s.get("id")
            self._sizes[group] = table
        return self._sizes[group]

    def size_id(self, value: str, group: int = 14) -> Optional[int]:
        sid = _as_int(value)
        return sid if sid is not None else self.sizes(group).get(str(value).lower())

    # ----- condition / material -------------------------------------------
    @staticmethod
    def condition_id(value: str) -> Optional[int]:
        cid = _as_int(value)
        return cid if cid is not None else CONDITION_IDS.get(str(value).lower())

    @staticmethod
    def material_ids(values) -> list[int]:
        out: list[int] = []
        for v in values or []:
            mid = _as_int(v)
            if mid is None:
                mid = MATERIAL_IDS.get(str(v).lower())
            if mid is not None:
                out.append(mid)
        return out

    # ----- brand -----------------------------------------------------------
    def brand(self, value: str) -> tuple[Optional[int], Optional[str]]:
        bid = _as_int(value)
        if bid is not None:
            return bid, None
        r = self.c.get(f"{self.c.base}/brands", params={"keyword": value}, timeout=30)
        brands = r.json().get("brands", []) if r.status_code == 200 else []
        if not brands:
            return None, None
        exact = next((b for b in brands if str(b.get("title", "")).lower() == value.lower()), None)
        b = exact or brands[0]
        return b.get("id"), b.get("title")
