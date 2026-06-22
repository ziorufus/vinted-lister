"""`vinted` command-line interface.

Usable by a human (manually add an item) and by an AI agent (which reads
.skills/vinted-lister/SKILL.md and drives these subcommands).

Run:  PYTHONPATH=. .venv/bin/python -m vinted <command> ...
  or:  ./vinted <command> ...
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from .client import VintedClient
from .lister import create_listing
from .resolve import CONDITION_IDS, Resolver

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


# --------------------------------------------------------------------------- helpers
def _client() -> VintedClient:
    try:
        return VintedClient()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"setup/auth error: {e}\nFill .env (see .env.example / `vinted auth import`).")


def _jwt_sub(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("sub", ""))
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- commands
def cmd_whoami(_a) -> None:
    c = _client()
    uid = c.env.get("VINTED_USER_ID") or _jwt_sub(c.env.get("VINTED_ACCESS_TOKEN_WEB", ""))
    r = c.get(f"{c.base}/users/{uid}", timeout=30)
    if r.status_code != 200:
        sys.exit(f"not authenticated ({r.status_code}). Re-import tokens: `vinted auth import`.")
    u = r.json().get("user", {})
    print(f"Logged in as {u.get('login')} (id {u.get('id')}, {u.get('country_code')})")


def cmd_catalogs(a) -> None:
    for cid, path in Resolver(_client()).search_catalogs(a.keyword):
        print(f"{cid}\t{path}")


def cmd_brands(a) -> None:
    c = _client()
    r = c.get(f"{c.base}/brands", params={"keyword": a.keyword}, timeout=30)
    for b in r.json().get("brands", [])[: a.limit]:
        print(f"{b.get('id')}\t{b.get('title')}\t({b.get('item_count')} items)")


def cmd_colors(_a) -> None:
    c = _client()
    r = c.get(f"{c.base}/colors", timeout=30)
    for col in r.json().get("colors", []):
        print(f"{col.get('id')}\t{col.get('title')}\t{col.get('code')}")


def cmd_sizes(a) -> None:
    c = _client()
    r = c.get(f"{c.base}/size_groups", timeout=30)
    for g in r.json().get("size_groups", []):
        if a.group is None or g.get("id") == a.group:
            print(f"# group {g.get('id')} — {g.get('caption')}")
            for s in g.get("sizes", []):
                print(f"{s.get('id')}\t{s.get('title')}")
            if a.group is not None:
                break


def cmd_conditions(_a) -> None:
    print("6\tNew with tags / Nytt med prislapp")
    print("1\tNew without tags / Ny utan prislapp")
    print("2\tVery good / Mycket bra")
    print("3\tGood / Bra")
    print("4\tSatisfactory / Tillfredsställande")


def cmd_price(a) -> None:
    from .client import new_session_id
    from .pricing import resolve_price

    c = _client()
    res = c.upload_photo(a.image, new_session_id())
    image_id = res.get("high_resolution", {}).get("id")
    ps = c.pricing_suggestion(image_id).get("price_suggestion", {})
    if ps:
        print(f"band: {ps['minimum']['amount']}–{ps['maximum']['amount']} "
              f"(mid {ps['midpoint']['amount']}) {ps.get('currency','SEK')}")
        print(f"resolved price: {resolve_price(a.brand, {'price_suggestion': ps})}")


def cmd_draft(a) -> None:
    c = _client()
    if a.json:
        spec = json.loads(a.json)
    else:
        rv = Resolver(c)
        cat_id, cat_path = rv.catalog(a.category)
        brand_id, brand_title = rv.brand(a.brand) if a.brand else (None, None)
        spec = {
            "files": a.photo,
            "title": a.title,
            "description": a.description or "",
            "catalog_id": cat_id,
            "brand_id": brand_id,
            "brand": brand_title or a.brand,
            "color_ids": [cid for cid in (rv.color_id(x) for x in a.color) if cid],
            "size_id": rv.size_id(a.size) if a.size else None,
            "condition_id": rv.condition_id(a.condition) if a.condition else 2,
            "material_ids": rv.material_ids(a.material),
            "price": a.price,
            "package_size_id": a.package_size,
            "n": a.n,
            "slug": a.slug,
            "labels": {
                "product": a.product or (a.title or "").split(",")[0][:40],
                "size": a.size or "",
                "condition": a.condition or "Very good",
                "colour": ", ".join(a.color),
            },
        }
        print(f"category -> {cat_id} ({cat_path})")
        if brand_id:
            print(f"brand -> {brand_id} ({brand_title})")

    result = create_listing(spec, client=c, organize=not a.no_move, log=not a.no_log)
    print(f"OK draft {result['draft_id']} | {result['price']} kr | "
          f"{result['photos']} photos" + (f" -> {result['moved_to']}" if result['moved_to'] else ""))
    print(result["edit_url"])


def cmd_auth_status(_a) -> None:
    cmd_whoami(_a)


def _update_env(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^(\w+)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def _read_clipboard() -> str:
    for cmd in (["pbpaste"], ["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    return ""


def cmd_auth_import(a) -> None:
    raw = a.curl
    if a.file:
        raw = Path(a.file).read_text(encoding="utf-8")
    if not raw.strip():
        raw = _read_clipboard()
        if raw.strip():
            print("Read 'Copy as cURL' from your clipboard.")
    if not raw.strip():
        print("Paste the DevTools 'Copy as cURL' of a vinted.se /api/v2 request, then press Ctrl-D:")
        raw = sys.stdin.read()

    def header(name: str) -> str:
        # handles -H 'name: v', -H "name: v", and Chrome's -H $'name: v'
        m = re.search(rf"-H\s+\$?(['\"])\s*{name}:\s*(.*?)\1", raw, re.I | re.S)
        return m.group(2).strip() if m else ""

    cookie_blob = header("cookie")
    if not cookie_blob:
        m = re.search(r"(?:-b|--cookie)\s+\$?(['\"])(.*?)\1", raw, re.S)
        cookie_blob = m.group(2) if m else ""
    cookies = dict(re.findall(r"(\w[\w\-.]*)=([^;]+?)(?:;|$)", cookie_blob))

    values = {}
    for ck, env_key in [
        ("access_token_web", "VINTED_ACCESS_TOKEN_WEB"),
        ("refresh_token_web", "VINTED_REFRESH_TOKEN_WEB"),
        ("_vinted_fr_session", "VINTED_COOKIE_VINTED_SESSION"),
        ("datadome", "VINTED_COOKIE_DATADOME"),
        ("anon_id", "VINTED_COOKIE_ANON_ID"),
    ]:
        if cookies.get(ck):
            values[env_key] = cookies[ck].strip()
    if header("x-csrf-token"):
        values["VINTED_CSRF_TOKEN"] = header("x-csrf-token")
    if header("x-anon-id") or cookies.get("anon_id"):
        values["VINTED_ANON_ID"] = header("x-anon-id") or cookies.get("anon_id", "").strip()

    if not values:
        sys.exit("Could not find tokens in that cURL. Make sure it is a /api/v2 request.")
    _update_env(values)
    print(f"Wrote {len(values)} value(s) to .env: {', '.join(sorted(values))}")
    print("Verifying...")
    cmd_whoami(None)


# --------------------------------------------------------------------------- parser
def _ad_brand(it) -> str:
    b = it.get("brand_title") or it.get("brand")
    return (b.get("title") if isinstance(b, dict) else b) or ""


def _ad_price(it) -> float:
    p = it.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return 0.0


def cmd_ads(a) -> None:
    c = _client()
    for it in c.all_my_ads():
        b, pr = _ad_brand(it), _ad_price(it)
        if a.brand and a.brand.lower() not in b.lower():
            continue
        if a.price is not None and pr != a.price:
            continue
        print(f"{it.get('id')}\t{int(pr)} kr\t{b}\t{(it.get('title') or '')[:48]}")


def cmd_reprice(a) -> None:
    c = _client()
    wanted = [x.strip().lower() for x in a.brand.split(",") if x.strip()]
    targets = []
    for it in c.all_my_ads():
        b, pr = _ad_brand(it), _ad_price(it)
        if pr == a.from_price and any(w in b.lower() for w in wanted):
            targets.append(it)
    print(f"{len(targets)} match(es): brand~'{a.brand}' price={a.from_price} -> {a.to_price}")
    for it in targets:
        print(f"  {it.get('id')}  {_ad_brand(it)}  {int(_ad_price(it))} kr  {(it.get('title') or '')[:40]}")
    if a.dry_run:
        print("dry-run: nothing changed.")
        return
    for it in targets:
        try:
            c.update_price(it["id"], a.to_price)
            print(f"  updated {it['id']} -> {int(a.to_price)} kr")
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED {it['id']}: {e}")


def _item_id(ref: str) -> int:
    m = re.search(r"(\d{5,})", str(ref))
    if not m:
        sys.exit(f"could not parse an item id from: {ref}")
    return int(m.group(1))


def cmd_item(a) -> None:
    c = _client()
    iid = _item_id(a.ref)
    it = c.get_item_edit(iid)
    price = it.get("price")
    price = price.get("amount") if isinstance(price, dict) else price
    print(f"id:          {iid}")
    print(f"title:       {it.get('title')}")
    print(f"price:       {price} {it.get('currency')}")
    print(f"catalog_id:  {it.get('catalog_id')}   brand_id: {it.get('brand_id')}   size_id: {it.get('size_id')}")
    print(f"colors:      {[x for x in (it.get('color1_id'), it.get('color2_id')) if x]}   status_id: {it.get('status_id')}")
    print(f"photos:      {[p.get('id') for p in it.get('photos') or []]}")
    print("description:")
    print(it.get("description", ""))


def cmd_edit(a) -> None:
    c = _client()
    rv = Resolver(c)
    iid = _item_id(a.ref)
    changes: dict = {}
    if a.title is not None:
        changes["title"] = a.title
    if a.description is not None:
        changes["description"] = a.description
    if a.price is not None:
        changes["price"] = a.price
    if a.category:
        cid, path = rv.catalog(a.category)
        changes["catalog_id"] = cid
        print(f"category -> {cid} ({path})")
    if a.brand:
        bid, bt = rv.brand(a.brand)
        changes["brand_id"] = bid
        changes["brand"] = bt or a.brand
    if a.size:
        changes["size_id"] = rv.size_id(a.size)
    if a.condition:
        changes["condition_id"] = rv.condition_id(a.condition)
    if a.color:
        changes["color_ids"] = [x for x in (rv.color_id(v) for v in a.color) if x]
    if a.material:
        changes["material_ids"] = rv.material_ids(a.material)
    if a.package_size is not None:
        changes["package_size_id"] = a.package_size
    if not changes:
        sys.exit("nothing to change; pass --title/--description/--price/--brand/...")
    c.update_item(iid, changes)
    print(f"OK updated {iid}: {', '.join(changes)}")
    print(f"https://www.vinted.se/items/{iid}/edit")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vinted", description="Vinted draft-listing CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami").set_defaults(func=cmd_whoami)

    sp = sub.add_parser("catalogs", help="search categories"); sp.add_argument("keyword"); sp.set_defaults(func=cmd_catalogs)
    sp = sub.add_parser("brands", help="search brands"); sp.add_argument("keyword"); sp.add_argument("--limit", type=int, default=10); sp.set_defaults(func=cmd_brands)
    sub.add_parser("colors", help="list colours").set_defaults(func=cmd_colors)
    sp = sub.add_parser("sizes", help="list sizes"); sp.add_argument("--group", type=int, default=14, nargs="?"); sp.set_defaults(func=cmd_sizes)
    sub.add_parser("conditions", help="list condition ids").set_defaults(func=cmd_conditions)

    sp = sub.add_parser("price", help="image-based price suggestion")
    sp.add_argument("image"); sp.add_argument("--brand"); sp.set_defaults(func=cmd_price)

    sp = sub.add_parser("draft", help="create a draft listing")
    sp.add_argument("--photo", action="append", default=[], help="photo file (repeatable)")
    sp.add_argument("--title"); sp.add_argument("--description")
    sp.add_argument("--category", help="catalog id or keyword")
    sp.add_argument("--brand", help="brand id or name")
    sp.add_argument("--size", help="size id or label (e.g. L)")
    sp.add_argument("--condition", help="id or name (default very_good)")
    sp.add_argument("--color", action="append", default=[], help="colour id or name (repeatable)")
    sp.add_argument("--material", action="append", default=[], help="material id or name (repeatable)")
    sp.add_argument("--price", type=float)
    sp.add_argument("--package-size", type=int, default=1)
    sp.add_argument("--product", help="short product label for the log")
    sp.add_argument("--n", type=int, help="product number (for posted/NN-slug + log)")
    sp.add_argument("--slug", help="folder slug under posted/")
    sp.add_argument("--no-move", action="store_true", help="don't move photos to posted/")
    sp.add_argument("--no-log", action="store_true", help="don't append to LISTINGS.md")
    sp.add_argument("--json", help="full JSON spec (overrides flags)")
    sp.set_defaults(func=cmd_draft)

    sp = sub.add_parser("ads", help="list your account's ads (id, price, brand, title)")
    sp.add_argument("--brand", help="filter: brand substring")
    sp.add_argument("--price", type=float, help="filter: exact current price")
    sp.set_defaults(func=cmd_ads)

    sp = sub.add_parser("reprice", help="bulk-change price of matching ads")
    sp.add_argument("--brand", required=True, help="brand substring(s), comma-separated")
    sp.add_argument("--from", dest="from_price", type=float, required=True, help="current price to match")
    sp.add_argument("--to", dest="to_price", type=float, required=True, help="new price")
    sp.add_argument("--dry-run", action="store_true", help="show matches without changing")
    sp.set_defaults(func=cmd_reprice)

    sp = sub.add_parser("item", help="show an item's current data (by id or URL)")
    sp.add_argument("ref", help="item id or vinted.se URL")
    sp.set_defaults(func=cmd_item)

    sp = sub.add_parser("edit", help="edit any field of an item (by id or URL)")
    sp.add_argument("ref", help="item id or vinted.se URL")
    sp.add_argument("--title")
    sp.add_argument("--description")
    sp.add_argument("--price", type=float)
    sp.add_argument("--category")
    sp.add_argument("--brand")
    sp.add_argument("--size")
    sp.add_argument("--condition")
    sp.add_argument("--color", action="append", default=[])
    sp.add_argument("--material", action="append", default=[])
    sp.add_argument("--package-size", type=int)
    sp.set_defaults(func=cmd_edit)

    ap = sub.add_parser("auth", help="auth helpers")
    asub = ap.add_subparsers(dest="auth_cmd", required=True)
    asub.add_parser("status").set_defaults(func=cmd_auth_status)
    ip = asub.add_parser("import", help="parse a DevTools 'Copy as cURL' into .env")
    ip.add_argument("--curl", default=""); ip.add_argument("--file")
    ip.set_defaults(func=cmd_auth_import)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
