"""Shared listing engine used by the CLI and any automation.

create_listing(spec) uploads photos, creates a DRAFT, optionally files the photos
into posted/NN-slug/ and appends a row to LISTINGS.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .client import VintedClient, build_draft, new_session_id
from .pricing import resolve_price

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox"
POSTED = ROOT / "posted"
LISTINGS = ROOT / "LISTINGS.md"

_HEADER = (
    "# Listings Log\n\n"
    "Every draft created by the auto-lister. Newest at the bottom.\n\n"
    "| # | Product | Brand | Size | Condition | Colour | Price | Photos | Draft | Status |\n"
    "|---|---------|-------|------|-----------|--------|-------|--------|-------|--------|\n"
)


def _resolve_file(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    if (INBOX / name).exists():
        return INBOX / name
    return p  # let upload raise a clear error


def create_listing(
    spec: dict[str, Any],
    client: Optional[VintedClient] = None,
    organize: bool = True,
    log: bool = True,
) -> dict[str, Any]:
    client = client or VintedClient()
    sid = new_session_id()

    paths = [_resolve_file(f) for f in spec["files"]]
    photo_ids: list[int] = []
    first_image_id = None
    for p in paths:
        res = client.upload_photo(p, sid)
        photo_ids.append(res["id"])
        if first_image_id is None:
            first_image_id = res.get("high_resolution", {}).get("id")

    price = spec.get("price")
    if price is None:
        suggestion = client.pricing_suggestion(first_image_id) if first_image_id else None
        price = resolve_price(spec.get("brand"), suggestion) or 0

    draft = build_draft(
        temp_uuid=sid,
        title=spec["title"],
        description=spec.get("description", ""),
        catalog_id=spec["catalog_id"],
        brand_id=spec.get("brand_id"),
        brand=spec.get("brand"),
        price=price,
        package_size_id=spec.get("package_size_id", 1),
        color_ids=spec.get("color_ids", []),
        photo_ids=photo_ids,
        condition_id=spec.get("condition_id"),
        size_id=spec.get("size_id"),
        material_ids=spec.get("material_ids"),
    )
    res = client.create_draft(draft, sid)
    item = res.get("draft") or res.get("item") or res
    draft_id = item.get("id") if isinstance(item, dict) else None

    moved_to = None
    if organize and spec.get("n") is not None and spec.get("slug"):
        dest = POSTED / f"{int(spec['n']):02d}-{spec['slug']}"
        dest.mkdir(parents=True, exist_ok=True)
        for p in paths:
            try:
                if INBOX in p.resolve().parents:
                    p.rename(dest / p.name)
            except (OSError, ValueError):
                pass
        moved_to = dest.name

    if log:
        if not LISTINGS.exists():
            LISTINGS.write_text(_HEADER, encoding="utf-8")
        lab = spec.get("labels", {})
        n = spec.get("n")
        num = f"{int(n):02d}" if n is not None else "--"
        row = (
            f"| {num} | {lab.get('product','')} | {spec.get('brand','')} | "
            f"{lab.get('size','')} | {lab.get('condition','')} | {lab.get('colour','')} | "
            f"{price} kr | {len(photo_ids)} | "
            f"[{draft_id}](https://www.vinted.se/items/{draft_id}/edit) | draft |\n"
        )
        with LISTINGS.open("a", encoding="utf-8") as fh:
            fh.write(row)

    return {
        "draft_id": draft_id,
        "price": price,
        "photos": len(photo_ids),
        "moved_to": moved_to,
        "edit_url": f"https://www.vinted.se/items/{draft_id}/edit",
    }
