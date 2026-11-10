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
import os
import re
import subprocess
import sys
from pathlib import Path

from .client import VintedClient
from .lister import create_listing
from .config import env_file, load_market, update_env
from .resolve import CONDITION_IDS, Resolver

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- helpers
def _client() -> VintedClient:
    try:
        return VintedClient()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"setup/auth error: {e}\n"
                 f"Fill {env_file()} (see .env.example / `vinted auth import`).")


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
    rv = Resolver(_client())
    if not a.keyword:
        rows = rv.catalog_aliases()
        if not rows:
            print("no catalog_aliases configured for this market (config/markets.json)")
        for alias, cid, path in rows:
            print(f"{cid}\t{alias}\t{path}")
        return
    for cid, path in rv.search_catalogs(a.keyword):
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
    group = None if a.all else (a.group if a.group is not None else c.market.default_size_group)
    r = c.get(f"{c.base}/size_groups", timeout=30)
    for g in r.json().get("size_groups", []):
        if group is None or g.get("id") == group:
            print(f"# group {g.get('id')} — {g.get('caption')}")
            for s in g.get("sizes", []):
                print(f"{s.get('id')}\t{s.get('title')}")
            if group is not None:
                break


def cmd_conditions(_a) -> None:
    try:
        aliases = load_market().condition_aliases
    except Exception:  # noqa: BLE001
        aliases = {}
    for name, cid in CONDITION_IDS.items():
        local = [k for k, v in aliases.items() if v == cid]
        print(f"{cid}\t{name}" + (f"\t({', '.join(local)})" if local else ""))


def cmd_price(a) -> None:
    from .client import new_session_id
    from .pricing import resolve_price

    c = _client()
    res = c.upload_photo(a.image, new_session_id())
    image_id = res.get("high_resolution", {}).get("id")
    ps = c.pricing_suggestion(image_id).get("price_suggestion", {})
    if ps:
        print(f"band: {ps['minimum']['amount']}–{ps['maximum']['amount']} "
              f"(mid {ps['midpoint']['amount']}) {ps.get('currency') or c.market.currency}")
        price = resolve_price(a.brand, {'price_suggestion': ps}, c.market.brand_prices)
        print(f"resolved price: {c.market.format_price(price)}")


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
            "isbn": a.isbn,
            "attributes": rv.attributes(a.attr),
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
    print(f"OK draft {result['draft_id']} | {c.market.format_price(result['price'])} | "
          f"{result['photos']} photos" + (f" -> {result['moved_to']}" if result['moved_to'] else ""))
    print(result["edit_url"])


def cmd_auth_status(_a) -> None:
    print(f"Settings file: {env_file()}")
    cmd_whoami(_a)


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
    # Sources, in order: --curl, --file, piped stdin, local clipboard.
    # Pasting into the prompt is never asked for: macOS terminals truncate pasted
    # lines at ~1024 bytes (canonical-mode limit) and the cookie line is longer.
    raw = a.curl
    if not raw.strip() and a.file:
        raw = Path(a.file).read_text(encoding="utf-8")
    if not raw.strip() and not sys.stdin.isatty():
        raw = sys.stdin.read()  # e.g. `pbpaste | vinted auth import`, also over ssh
    if not raw.strip():
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            # The clipboard here is the remote machine's, not the one you copied on.
            sys.exit(
                "Over SSH this machine's clipboard is not yours. From your laptop run:\n"
                "  pbpaste | ssh <user>@<host> <path>/.venv/bin/vinted auth import\n"
                "or copy the cURL into a file here and run:\n"
                "  vinted auth import --file request.txt"
            )
        raw = _read_clipboard()
        if raw.strip():
            print("Read 'Copy as cURL' from your clipboard.")
        else:
            input("Copy a vinted /api/v2 request in DevTools (Copy as cURL (bash)), "
                  "then press Enter here... ")
            raw = _read_clipboard()
            if not raw.strip():
                sys.exit("Clipboard is empty. Alternatives:\n"
                         "  pbpaste | vinted auth import\n"
                         "  vinted auth import --file request.txt")

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
    target = env_file()
    update_env(values, target)
    print(f"Wrote {len(values)} value(s) to {target}: {', '.join(sorted(values))}")
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
        print(f"{it.get('id')}\t{c.market.format_price(pr)}\t{b}\t{(it.get('title') or '')[:48]}")


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
        print(f"  {it.get('id')}  {_ad_brand(it)}  {c.market.format_price(_ad_price(it))}  {(it.get('title') or '')[:40]}")
    if a.dry_run:
        print("dry-run: nothing changed.")
        return
    for it in targets:
        try:
            c.update_price(it["id"], a.to_price)
            print(f"  updated {it['id']} -> {c.market.format_price(a.to_price)}")
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
    print(f"isbn:        {it.get('isbn')}   draft: {it.get('is_draft')}   hidden: {it.get('is_hidden')}")
    print(f"attributes:  {it.get('item_attributes')}")
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
    if a.isbn is not None:
        from .lister import normalize_isbn
        changes["isbn"] = normalize_isbn(a.isbn)
    if a.attr:
        changes["attributes"] = rv.attributes(a.attr)
    if a.package_size is not None:
        changes["package_size_id"] = a.package_size
    if not changes:
        sys.exit("nothing to change; pass --title/--description/--price/--brand/...")
    c.update_item(iid, changes)
    print(f"OK updated {iid}: {', '.join(changes)}")
    print(c.market.item_url(iid, edit=True))


def cmd_search(a) -> None:
    from .pricing import item_amount, price_summary

    c = _client()
    rv = Resolver(c)
    cats = []
    for v in a.category:
        cid, path = rv.catalog(v)
        if cid is None:
            sys.exit(f"unknown category: {v}")
        cats.append(cid)
    statuses = [x for x in (rv.condition_id(v) for v in a.condition) if x is not None]
    items = c.search_all(a.text, limit=a.limit, catalog_ids=cats, status_ids=statuses,
                         filters=rv.attributes(a.filter), order=a.order)
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for it in items:
        # svc-catalogue puts the condition (for books) in item_box.second_line
        cond = it.get("status") or (it.get("item_box") or {}).get("second_line") or ""
        url = it.get("url") or ""
        url = f"https://{c.host}{url}" if url.startswith("/") else (url or c.market.item_url(it.get("id")))
        print(f"{c.market.format_price(item_amount(it))}\t{cond}\t{(it.get('title') or '')[:50]}\t{url}")
    s = price_summary(items)
    if s:
        f = c.market.format_price
        print(f"\n{s['count']} item(s): min {f(s['min'])} | median {f(s['median'])} | max {f(s['max'])}")
    else:
        print("no results")


def cmd_publish(a) -> None:
    c = _client()
    iid = _item_id(a.ref)
    changes = {"attributes": Resolver(c).attributes(a.attr)} if a.attr else {}
    c.publish_draft(iid, changes)
    print(f"OK published {iid}")
    print(c.market.item_url(iid))


def cmd_hide(a) -> None:
    c = _client()
    iid = _item_id(a.ref)
    c.set_hidden(iid, True)
    print(f"OK hidden {iid} (undo: vinted unhide {iid})")


def cmd_unhide(a) -> None:
    c = _client()
    iid = _item_id(a.ref)
    c.set_hidden(iid, False)
    print(f"OK visible again {iid}")
    print(c.market.item_url(iid))


def cmd_delete(a) -> None:
    c = _client()
    iid = _item_id(a.ref)
    if not a.yes:
        if not sys.stdin.isatty():
            sys.exit("refusing to delete without confirmation: pass --yes")
        title = c.get_item_edit(iid).get("title", "?")
        ans = input(f"Permanently delete {iid} '{title}'? This cannot be undone. [y/N] ")
        if ans.strip().lower() not in ("y", "yes", "s", "si", "sì"):
            sys.exit("aborted")
    c.delete_item(iid)
    print(f"OK deleted {iid}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vinted", description="Vinted draft-listing CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami").set_defaults(func=cmd_whoami)

    sp = sub.add_parser("catalogs", help="search categories (no keyword: list configured aliases)")
    sp.add_argument("keyword", nargs="?"); sp.set_defaults(func=cmd_catalogs)
    sp = sub.add_parser("brands", help="search brands"); sp.add_argument("keyword"); sp.add_argument("--limit", type=int, default=10); sp.set_defaults(func=cmd_brands)
    sub.add_parser("colors", help="list colours").set_defaults(func=cmd_colors)
    sp = sub.add_parser("sizes", help="list sizes (default: the market's default_size_group)")
    sp.add_argument("--group", type=int, help="size group id")
    sp.add_argument("--all", action="store_true", help="list every size group")
    sp.set_defaults(func=cmd_sizes)
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
    sp.add_argument("--isbn", help="ISBN-10/13 (books); hyphens/spaces are stripped")
    sp.add_argument("--attr", action="append", default=[], metavar="CODE=VALUE",
                    help="extra attribute, id or alias (e.g. language_book=inglese); repeatable")
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
    sp.add_argument("ref", help="item id or vinted item URL")
    sp.set_defaults(func=cmd_item)

    sp = sub.add_parser("edit", help="edit any field of an item (by id or URL)")
    sp.add_argument("ref", help="item id or vinted item URL")
    sp.add_argument("--title")
    sp.add_argument("--description")
    sp.add_argument("--price", type=float)
    sp.add_argument("--category")
    sp.add_argument("--brand")
    sp.add_argument("--size")
    sp.add_argument("--condition")
    sp.add_argument("--color", action="append", default=[])
    sp.add_argument("--material", action="append", default=[])
    sp.add_argument("--isbn")
    sp.add_argument("--attr", action="append", default=[], metavar="CODE=VALUE")
    sp.add_argument("--package-size", type=int)
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("search", help="search live listings + price summary")
    sp.add_argument("text", help="search text (title, author, ISBN...)")
    sp.add_argument("--category", action="append", default=[], help="catalog id or alias (repeatable)")
    sp.add_argument("--condition", action="append", default=[], help="condition id or name (repeatable)")
    sp.add_argument("--filter", action="append", default=[], metavar="CODE=VALUE[,VALUE]",
                    help="other attribute filter, ids or aliases (e.g. language_book=inglese)")
    sp.add_argument("--order", default="relevance",
                    choices=["relevance", "price_low_to_high", "price_high_to_low", "newest_first"])
    sp.add_argument("--limit", type=int, default=20, help="max items (default 20)")
    sp.add_argument("--json", action="store_true", help="print raw items as JSON")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("publish", help="publish a draft (by id or URL)")
    sp.add_argument("ref", help="draft id or vinted URL")
    sp.add_argument("--attr", action="append", default=[], metavar="CODE=VALUE",
                    help="set/override an attribute while publishing (e.g. language_book=inglese)")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("hide", help="hide a published item (reversible)")
    sp.add_argument("ref", help="item id or vinted URL"); sp.set_defaults(func=cmd_hide)

    sp = sub.add_parser("unhide", help="make a hidden item visible again")
    sp.add_argument("ref", help="item id or vinted URL"); sp.set_defaults(func=cmd_unhide)

    sp = sub.add_parser("delete", help="permanently delete an item (asks for confirmation)")
    sp.add_argument("ref", help="item id or vinted URL")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sp.set_defaults(func=cmd_delete)

    ap = sub.add_parser("auth", help="auth helpers")
    asub = ap.add_subparsers(dest="auth_cmd", required=True)
    asub.add_parser("status").set_defaults(func=cmd_auth_status)
    ip = asub.add_parser("import", help="parse a DevTools 'Copy as cURL' into .env")
    ip.add_argument("--curl", default=""); ip.add_argument("--file")
    ip.set_defaults(func=cmd_auth_import)
    return p


def main(argv=None) -> None:
    from .client import VintedError

    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (VintedError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
