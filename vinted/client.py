"""Vinted client — authenticated reads + writes from pure Python.

Auth recipe (reverse-engineered 2026-06-21): cookies (access_token_web,
refresh_token_web, _vinted_fr_session, datadome, anon_id) + headers
(x-csrf-token, x-anon-id, locale). curl_cffi impersonates Chrome's TLS so
DataDome lets the request through. The blocker was never DataDome alone — it
was the missing x-csrf-token header.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from curl_cffi import CurlMime
from curl_cffi import requests as cffi

from .config import Market, load_env, load_market, update_env


class VintedError(RuntimeError):
    pass


class VintedClient:
    def __init__(
        self, env: Optional[dict[str, str]] = None, market: Optional[Market] = None
    ) -> None:
        env = env or load_env()
        self.env = env
        self.market = market or load_market(env)
        self.domain = self.market.domain
        self.host = self.market.host
        self.base = self.market.base_url

        self.anon = env.get("VINTED_ANON_ID") or env.get("VINTED_COOKIE_ANON_ID", "")
        self.csrf = env.get("VINTED_CSRF_TOKEN", "")

        cookies = {
            "access_token_web": env.get("VINTED_ACCESS_TOKEN_WEB", ""),
            "refresh_token_web": env.get("VINTED_REFRESH_TOKEN_WEB", ""),
            "_vinted_fr_session": env.get("VINTED_COOKIE_VINTED_SESSION", ""),
            "datadome": env.get("VINTED_COOKIE_DATADOME", ""),
            "anon_id": self.anon,
        }
        cookies = {k: v for k, v in cookies.items() if v}

        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": self.market.accept_language,
            "authorization": f"Bearer {env.get('VINTED_ACCESS_TOKEN_WEB', '')}",
            "x-csrf-token": self.csrf,
            "x-anon-id": self.anon,
            "locale": self.market.locale,
            "origin": f"https://{self.host}",
            "referer": f"https://{self.host}/items/new",
        }
        self.session = cffi.Session(impersonate="chrome", headers=headers, cookies=cookies)
        self.access = env.get("VINTED_ACCESS_TOKEN_WEB", "")
        self.user_id = env.get("VINTED_USER_ID", "")
        if not self.user_id and self.access:
            try:
                p = self.access.split(".")[1]
                p += "=" * (-len(p) % 4)
                self.user_id = str(json.loads(base64.urlsafe_b64decode(p)).get("sub", ""))
            except Exception:  # noqa: BLE001
                self.user_id = ""

    # ---- auth / requests --------------------------------------------------
    def refresh(self) -> bool:
        """Mint a fresh access token from the refresh token; persist to .env."""
        try:
            r = self.session.post(
                f"https://{self.host}/web/api/auth/refresh",
                headers={"accept": "application/json"}, timeout=30,
            )
        except Exception:  # noqa: BLE001
            return False
        if r.status_code != 200:
            return False
        try:
            new_access = r.json().get("access_token")
        except Exception:  # noqa: BLE001
            new_access = None
        if not new_access:
            return False
        self.access = new_access
        self.session.headers["authorization"] = f"Bearer {new_access}"
        self.session.cookies.set("access_token_web", new_access, domain=self.host)
        values = {"VINTED_ACCESS_TOKEN_WEB": new_access}
        for ck, key in (
            ("refresh_token_web", "VINTED_REFRESH_TOKEN_WEB"),
            ("datadome", "VINTED_COOKIE_DATADOME"),
            ("_vinted_fr_session", "VINTED_COOKIE_VINTED_SESSION"),
        ):
            try:
                v = self.session.cookies.get(ck)
            except Exception:  # noqa: BLE001
                v = None
            if v:
                values[key] = v
        try:
            update_env(values)
        except Exception:  # noqa: BLE001
            pass
        return True

    def _request(self, method: str, url: str, _retried: bool = False, **kwargs):
        r = self.session.request(method, url, **kwargs)
        if r.status_code == 401 and not _retried and self.env.get("VINTED_REFRESH_TOKEN_WEB"):
            if self.refresh():
                return self._request(method, url, _retried=True, **kwargs)
        return r

    def get(self, url: str, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self._request("PUT", url, **kwargs)

    # ---- internal ---------------------------------------------------------
    def _json(self, r, what: str) -> dict[str, Any]:
        if r.status_code != 200:
            raise VintedError(f"{what} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception as e:  # noqa: BLE001
            raise VintedError(f"{what}: non-JSON response: {r.text[:200]}") from e

    # ---- API --------------------------------------------------------------
    def upload_photo(self, path: str | Path, temp_uuid: str) -> dict[str, Any]:
        """Upload one image; returns the photo object (numeric `id`, high_resolution.id)."""
        path = Path(path)
        mp = CurlMime()
        mp.addpart(name="photo[type]", data=b"item")
        mp.addpart(
            name="photo[file]",
            filename=path.name,
            content_type="image/jpeg",
            local_path=str(path),
        )
        mp.addpart(name="photo[temp_uuid]", data=temp_uuid.encode())
        r = self.post(f"{self.base}/photos", multipart=mp, timeout=120)
        return self._json(r, f"upload {path.name}")

    def pricing_suggestion(
        self, image_id: str, item_attributes: Optional[list] = None
    ) -> dict[str, Any]:
        r = self.post(
            f"{self.base}/item_upload/pricing_suggestions",
            headers={"content-type": "application/json"},
            json={
                "image_metadata": {"image_id": image_id, "orientation": "0"},
                "item_attributes": item_attributes or [],
            },
            timeout=30,
        )
        return self._json(r, "pricing_suggestion")

    def create_draft(self, draft: dict[str, Any], upload_session_id: str) -> dict[str, Any]:
        body = {
            "draft": draft,
            "feedback_id": None,
            "parcel": None,
            "upload_session_id": upload_session_id,
        }
        r = self.post(
            f"{self.base}/item_upload/drafts",
            headers={
                "content-type": "application/json",
                "x-enable-dynamic-attribute-condition": "true",
                "x-enable-dynamic-attribute-size": "true",
                "x-enable-dynamic-attribute-video-game-rating": "true",
            },
            json=body,
            timeout=60,
        )
        return self._json(r, "create_draft")

    def update_draft(
        self, draft_id: int, draft: dict[str, Any], upload_session_id: str
    ) -> dict[str, Any]:
        d = dict(draft)
        d["id"] = draft_id
        body = {
            "draft": d,
            "feedback_id": None,
            "parcel": None,
            "upload_session_id": upload_session_id,
        }
        r = self.put(
            f"{self.base}/item_upload/drafts/{draft_id}",
            headers={
                "content-type": "application/json",
                "x-enable-dynamic-attribute-condition": "true",
                "x-enable-dynamic-attribute-size": "true",
                "x-enable-dynamic-attribute-video-game-rating": "true",
            },
            json=body,
            timeout=60,
        )
        return self._json(r, "update_draft")

    def update_live_item(
        self, item_id: int, draft: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a published (live) item via the items endpoint.

        Published listings are not drafts, so PUT /item_upload/drafts/{id}
        returns 403. The live-edit endpoint is PUT /item_upload/items/{id}
        with the item wrapped in {"item": {...}}.
        """
        d = dict(draft)
        d["id"] = item_id
        r = self.put(
            f"{self.base}/item_upload/items/{item_id}",
            headers={
                "content-type": "application/json",
                "x-enable-dynamic-attribute-condition": "true",
                "x-enable-dynamic-attribute-size": "true",
                "x-enable-dynamic-attribute-video-game-rating": "true",
            },
            json={"item": d},
            timeout=60,
        )
        return self._json(r, "update_live_item")

    # ---- ad management (read account, update existing) --------------------
    def my_ads(self, page: int = 1, per_page: int = 20) -> list[dict[str, Any]]:
        """List the current user's items from their wardrobe (the live account)."""
        r = self.get(
            f"{self.base}/wardrobe/{self.user_id}/items",
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        return self._json(r, "my_ads").get("items", [])

    def all_my_ads(self, max_pages: int = 25) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.my_ads(page=page, per_page=20)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 20:
                break
        return out

    def get_item_edit(self, item_id: int) -> dict[str, Any]:
        """Full editable data for one item (all ids + current photo ids)."""
        r = self.get(f"{self.base}/item_upload/items/{item_id}", timeout=30)
        return self._json(r, "get_item_edit").get("item", {})

    def _edit_draft(self, it: dict[str, Any], changes: dict[str, Any]):
        """Build a draft PUT body from current edit-data + field overrides."""
        attrs = [dict(a) for a in (it.get("item_attributes") or [])]
        codes = {a.get("code") for a in attrs}
        if it.get("status_id") and "condition" not in codes:
            attrs.append({"code": "condition", "ids": [it["status_id"]]})
        if it.get("size_id") and "size" not in codes:
            attrs.append({"code": "size", "ids": [it["size_id"]]})
        color_ids = [c for c in (it.get("color1_id"), it.get("color2_id")) if c]
        price = it.get("price")
        if isinstance(price, dict):
            try:
                price = float(price.get("amount"))
            except (TypeError, ValueError):
                price = None
        sid = str(uuid.uuid4())
        draft = {
            "id": it.get("id"),
            "currency": it.get("currency") or self.market.currency,
            "temp_uuid": sid,
            "title": it.get("title", ""),
            "description": it.get("description", ""),
            "brand_id": it.get("brand_id"),
            "brand": (it.get("brand_dto") or {}).get("title"),
            "catalog_id": it.get("catalog_id"),
            "isbn": it.get("isbn"),
            "is_unisex": it.get("is_unisex", False),
            "ai_photo": False,
            "price": price,
            "package_size_id": it.get("package_size_id"),
            "shipment_prices": it.get("shipment_prices") or {"domestic": None, "international": None},
            "color_ids": color_ids,
            "assigned_photos": [
                {"id": p.get("id"), "orientation": p.get("orientation") or 0}
                for p in (it.get("photos") or [])
            ],
            "measurement_length": it.get("measurement_length"),
            "measurement_width": it.get("measurement_width"),
            "item_attributes": attrs,
            "manufacturer": it.get("manufacturer"),
            "manufacturer_labelling": it.get("manufacturer_labelling"),
        }

        def _set_attr(code: str, ids: list) -> None:
            draft["item_attributes"] = [a for a in draft["item_attributes"] if a.get("code") != code]
            if ids:
                draft["item_attributes"].append({"code": code, "ids": ids})

        for k, v in changes.items():
            if k == "condition_id":
                _set_attr("condition", [v])
            elif k == "size_id":
                _set_attr("size", [v])
            elif k == "material_ids":
                _set_attr("material", v)
            else:
                draft[k] = v
        return draft, sid

    def update_item(self, item_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        """Read an item's current data, apply field changes, and PUT it back.

        `changes` may include any of: title, description, price, catalog_id,
        brand_id, brand, color_ids, condition_id, size_id, material_ids,
        package_size_id, is_unisex, measurement_length, measurement_width.
        Photos are preserved (reuses the item's current photo ids).
        """
        it = self.get_item_edit(item_id)
        it["id"] = item_id
        draft, _sid = self._edit_draft(it, changes)
        return self.update_live_item(item_id, draft)

    def update_price(self, item_id: int, new_price: float) -> dict[str, Any]:
        return self.update_item(item_id, {"price": new_price})


def new_session_id() -> str:
    return str(uuid.uuid4())


def build_draft(
    *,
    temp_uuid: str,
    title: str,
    description: str,
    catalog_id: int,
    brand_id: Optional[int],
    brand: Optional[str],
    price: float,
    package_size_id: int,
    color_ids: list[int],
    photo_ids: list[int],
    condition_id: Optional[int] = None,
    size_id: Optional[int] = None,
    material_ids: Optional[list[int]] = None,
    isbn: Optional[str] = None,
    currency: str,
    is_unisex: bool = False,
    draft_id: Optional[int] = None,
) -> dict[str, Any]:
    """Build the `draft` object matching the captured Vinted payload shape.

    size / condition / material are dynamic item_attributes (not top-level).
    assigned_photos uses the numeric photo `id` returned by upload_photo().
    """
    item_attributes: list[dict[str, Any]] = []
    if material_ids:
        item_attributes.append({"code": "material", "ids": material_ids})
    if condition_id is not None:
        item_attributes.append({"code": "condition", "ids": [condition_id]})
    if size_id is not None:
        item_attributes.append({"code": "size", "ids": [size_id]})

    return {
        "id": draft_id,
        "currency": currency,
        "temp_uuid": temp_uuid,
        "title": title,
        "description": description,
        "brand_id": brand_id,
        "brand": brand,
        "catalog_id": catalog_id,
        "isbn": isbn or None,
        "is_unisex": is_unisex,
        "ai_photo": False,
        "price": price,
        "package_size_id": package_size_id,
        "shipment_prices": {"domestic": None, "international": None},
        "color_ids": color_ids,
        "assigned_photos": [{"id": pid, "orientation": 0} for pid in photo_ids],
        "measurement_length": None,
        "measurement_width": None,
        "item_attributes": item_attributes,
        "manufacturer": None,
        "manufacturer_labelling": None,
    }
