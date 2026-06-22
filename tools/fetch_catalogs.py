"""Fetch the full Vinted SE catalog tree and build an id -> path lookup.

Saves the raw tree to data/catalogs_se.json and a flat lookup to
data/catalog_lookup_se.json so the agent can resolve catalog_id -> name/path
without calling the API every time.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


ENV = load_env()
DOMAIN = ENV.get("VINTED_DOMAIN", "se")
BASE = f"https://www.vinted.{DOMAIN}/api/v2"
ACCESS = ENV["VINTED_ACCESS_TOKEN_WEB"]

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {ACCESS}",
        "X-Requested-With": "XMLHttpRequest",
    }
)


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
    r = session.get(f"{BASE}/item_upload/catalogs", timeout=30)
    r.raise_for_status()
    data = r.json()
    catalogs = data.get("catalogs", [])

    (DATA / "catalogs_se.json").write_text(
        json.dumps(catalogs, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lookup: dict[str, dict] = {}
    flatten(catalogs, [], lookup)
    (DATA / "catalog_lookup_se.json").write_text(
        json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Top-level roots: {[c.get('title') for c in catalogs]}")
    print(f"Total categories flattened: {len(lookup)}")

    print("\nResolving the shirt catalog_ids from catalogs/search?keyword=skjorta:")
    for cid in [1537, 1664, 222, 1865, 1801, 1805, 1803, 1804, 1802, 1665, 2914, 1666, 1540]:
        entry = lookup.get(str(cid))
        if entry:
            print(f"  {cid:>5}  ->  {entry['path']}")
        else:
            print(f"  {cid:>5}  ->  (not found in tree)")


if __name__ == "__main__":
    main()
