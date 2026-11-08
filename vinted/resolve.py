"""Resolve human-friendly names to Vinted IDs (brand, colour, size, condition,
material, category). Fetches and caches the live lists where needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .client import VintedClient

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Condition ids are global (dynamic item_attribute "condition"). These English
# keys work in every market; local names come from config/markets.json
# (`condition_aliases`).
CONDITION_IDS = {
    "new_with_tags": 6,
    "new_without_tags": 1,
    "very_good": 2,
    "good": 3,
    "satisfactory": 4,
}

# Confirmed materials (English); local names: `material_aliases` in markets.json.
MATERIAL_IDS = {"cotton": 44}


def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class Resolver:
    def __init__(self, client: VintedClient) -> None:
        self.c = client
        self.market = client.market
        self._conditions = {**CONDITION_IDS, **self.market.condition_aliases}
        self._materials = {**MATERIAL_IDS, **self.market.material_aliases}
        self._colors: Optional[dict[str, int]] = None
        self._sizes: dict[int, dict[str, int]] = {}
        self._lookup: Optional[dict] = None

    # ----- catalog ---------------------------------------------------------
    @property
    def lookup(self) -> dict:
        """Display-only id -> path cache. Resolution itself is live (the API
        search endpoint); this just labels ids for humans. Picks the cache for
        the client's market (catalog_lookup_<domain>.json) and falls back to the
        bundled SE cache, then to empty. Refresh/localize via tools/fetch_catalogs.py.
        """
        if self._lookup is None:
            domain = getattr(self.c, "domain", "se")
            self._lookup = {}
            for path in (DATA / f"catalog_lookup_{domain}.json", DATA / "catalog_lookup_se.json"):
                if path.exists():
                    self._lookup = json.loads(path.read_text(encoding="utf-8"))
                    break
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

    def catalog_aliases(self) -> list[tuple[str, int, str]]:
        """Configured (alias, id, path) from markets.json `catalog_aliases`."""
        return [(k, v, self.catalog_path(v)) for k, v in self.market.catalog_aliases.items()]

    def catalog(self, value: str) -> tuple[Optional[int], str]:
        cid = _as_int(value)
        if cid is None:
            cid = self.market.catalog_aliases.get(str(value).strip().lower())
        if cid is not None:
            return cid, self.catalog_path(cid)
        hits = self.search_catalogs(value)
        if not hits:
            return None, "?"
        # optionally prefer one root (e.g. men's), configured per market
        pref = self.market.preferred_catalog_root
        if pref:
            hits.sort(key=lambda h: (0 if pref in h[1] else 1))
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
    def sizes(self, group: Optional[int] = None) -> dict[str, int]:
        group = group if group is not None else self.market.default_size_group
        if group not in self._sizes:
            r = self.c.get(f"{self.c.base}/size_groups", timeout=30)
            table: dict[str, int] = {}
            for g in (r.json().get("size_groups", []) if r.status_code == 200 else []):
                if g.get("id") == group:
                    for s in g.get("sizes", []):
                        table[str(s.get("title", "")).lower()] = s.get("id")
            self._sizes[group] = table
        return self._sizes[group]

    def size_id(self, value: str, group: Optional[int] = None) -> Optional[int]:
        sid = _as_int(value)
        return sid if sid is not None else self.sizes(group).get(str(value).lower())

    # ----- condition / material -------------------------------------------
    def condition_id(self, value: str) -> Optional[int]:
        cid = _as_int(value)
        return cid if cid is not None else self._conditions.get(str(value).strip().lower())

    def material_ids(self, values) -> list[int]:
        out: list[int] = []
        for v in values or []:
            mid = _as_int(v)
            if mid is None:
                mid = self._materials.get(str(v).strip().lower())
            if mid is not None:
                out.append(mid)
        return out

    # ----- other dynamic attributes (e.g. language_book) --------------------
    def attribute_ids(self, code: str, values) -> list[int]:
        """Resolve ids or names (markets.json `attribute_aliases[code]`) to ids."""
        names = self.market.attribute_aliases.get(code, {})
        out: list[int] = []
        for v in values:
            aid = _as_int(v)
            if aid is None:
                aid = names.get(str(v).strip().lower())
            if aid is None:
                raise ValueError(
                    f"unknown value '{v}' for attribute '{code}': pass the numeric id "
                    f"or add it to attribute_aliases.{code} in config/markets.json"
                )
            out.append(aid)
        return out

    def attributes(self, pairs) -> dict[str, list[int]]:
        """['language_book=inglese', 'x=1,2'] -> {'language_book': [6435], 'x': [1, 2]}"""
        out: dict[str, list[int]] = {}
        for pair in pairs or []:
            code, sep, vals = str(pair).partition("=")
            if not sep or not code.strip() or not vals.strip():
                raise ValueError(f"bad attribute '{pair}', expected CODE=VALUE[,VALUE]")
            out[code.strip()] = self.attribute_ids(code.strip(), vals.split(","))
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
