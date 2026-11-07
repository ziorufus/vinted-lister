"""Fetch the full Vinted catalog tree for the configured market and build an
id -> path lookup (display cache only; resolution itself is live).

Uses VINTED_DOMAIN and the market's locale (config/markets.json), so labels
come out in that market's language. Writes:
  data/catalogs_<domain>.json        raw tree
  data/catalog_lookup_<domain>.json  flat id -> {title, path, code}

Run:  .venv/bin/python tools/fetch_catalogs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vinted.client import VintedClient  # noqa: E402

DATA = ROOT / "data"


def flatten(nodes: list[dict], parents: list[str], out: dict[str, dict]) -> None:
    for node in nodes:
        cid = node.get("id")
        title = node.get("title", "")
        path = parents + [title]
        out[str(cid)] = {
            "id": cid,
            "title": title,
            "path": " > ".join(p for p in path if p),
            "code": node.get("code"),
        }
        children = node.get("catalogs") or []
        if children:
            flatten(children, path, out)


def main() -> None:
    client = VintedClient()
    domain = client.domain
    r = client.get(f"{client.base}/item_upload/catalogs", timeout=30)
    if r.status_code != 200:
        sys.exit(f"catalogs -> {r.status_code}: {r.text[:200]}\n"
                 "Re-import tokens with `vinted auth import`.")
    catalogs = r.json().get("catalogs", [])

    DATA.mkdir(exist_ok=True)
    (DATA / f"catalogs_{domain}.json").write_text(
        json.dumps(catalogs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lookup: dict[str, dict] = {}
    flatten(catalogs, [], lookup)
    (DATA / f"catalog_lookup_{domain}.json").write_text(
        json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Market: vinted.{domain} ({client.market.locale})")
    print(f"Top-level roots: {[c.get('title') for c in catalogs]}")
    print(f"Total categories flattened: {len(lookup)}")
    print(f"Wrote data/catalogs_{domain}.json and data/catalog_lookup_{domain}.json")


if __name__ == "__main__":
    main()
