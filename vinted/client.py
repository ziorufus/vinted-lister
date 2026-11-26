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
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from curl_cffi import CurlMime
from curl_cffi import requests as cffi

from .config import Market, env_file, load_env, load_market, update_env


class VintedError(RuntimeError):
    pass


class Sale(NamedTuple):
    """An item's actual sale amount (offers included, buyer fee excluded).

    For an item sold in a bundle, `amount` is its share of the bundle amount
    (see VintedClient.order_item_amounts), `bundle_items` is the number of
    items in the bundle and `bundle_total` the whole bundle amount; for an item
    sold alone they are 1 and `amount`.

    `completed` is True once the buyer has confirmed receipt (transaction
    `is_completed`, status 450; a sale is status 200-499, see SALE_STATUSES): only then does Vinted credit the seller.
    `completed_at` is when that happened (the transaction's status_updated_at,
    ISO 8601), if known. `transaction_id` and `conversation_id` identify the
    order (read again with VintedClient.conversation_sales).
    """

    amount: float
    bundle_items: int = 1
    bundle_total: Optional[float] = None
    completed: bool = False
    completed_at: Optional[str] = None
    transaction_id: Optional[int] = None
    conversation_id: Optional[int] = None

    @property
    def is_bundle(self) -> bool:
        return self.bundle_items > 1


def _money(value: Any) -> Optional[float]:
    """{"amount": "5.0", ...} (or a bare number) -> 5.0; None if missing."""
    amount = value.get("amount") if isinstance(value, dict) else value
    try:
        return round(float(amount), 2)
    except (TypeError, ValueError):
        return None


# Transaction status in a conversation (seen November 2026): 1 offer (even an
# accepted one, not paid yet), 230 sold (paid, to ship or shipped), 450 completed
# (buyer confirmed receipt), 500 item no longer available (bought by someone
# else). purchase_id is set for all of them: only the status tells a sale.
SALE_STATUSES = range(200, 500)
COMPLETED_STATUS = 450


def _is_sale(tx: dict[str, Any]) -> bool:
    try:
        status = int(tx.get("status"))
    except (TypeError, ValueError):
        return False
    return status in SALE_STATUSES


def _when(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class VintedClient:
    # Minimum seconds between two requests (0 = no limit), set per instance.
    # The time of the last request is shared by every client in the process,
    # so callers that create a client per operation still space their calls.
    min_interval: float = 0.0
    _last_request_at: float = 0.0
    _throttle = threading.Lock()

    def __init__(
        self,
        env: Optional[dict[str, str]] = None,
        market: Optional[Market] = None,
        env_path: Path | str | None = None,
    ) -> None:
        # Settings/tokens file: explicit env_path, else $VINTED_ENV_FILE, else
        # <repo>/.env. Used to load `env` (when not given) and by refresh().
        self.env_path = Path(env_path).expanduser() if env_path is not None else env_file()
        env = env or load_env(self.env_path)
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
        # transaction_id -> (item id, listing price) for each item in that order;
        # transactions never change.
        self._tx_items: dict[int, list[tuple[int, Optional[float]]]] = {}

    # ---- auth / requests --------------------------------------------------
    def refresh(self) -> bool:
        """Mint a fresh access token from the refresh token; persist to env_path."""
        # The refresh endpoint rejects the request ("Authorization failed") when
        # it carries the expired access token as a Bearer header, as the browser
        # never does. Drop it for this call only; the cookies are enough.
        bearer = self.session.headers.pop("authorization", None)
        try:
            r = self.session.post(
                f"https://{self.host}/web/api/auth/refresh",
                headers={"accept": "application/json"}, timeout=30,
            )
        except Exception:  # noqa: BLE001
            r = None
        if r is None or r.status_code != 200:
            if bearer is not None:
                self.session.headers["authorization"] = bearer
            return False
        try:
            new_access = r.json().get("access_token")
        except Exception:  # noqa: BLE001
            new_access = None
        if not new_access:
            if bearer is not None:
                self.session.headers["authorization"] = bearer
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
            update_env(values, self.env_path)
        except Exception:  # noqa: BLE001
            pass
        return True

    def _wait_turn(self) -> None:
        with VintedClient._throttle:
            if self.min_interval > 0:
                wait = VintedClient._last_request_at + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            VintedClient._last_request_at = time.monotonic()

    def _request(self, method: str, url: str, _retried: bool = False, **kwargs):
        self._wait_turn()
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

    def delete(self, url: str, **kwargs):
        return self._request("DELETE", url, **kwargs)

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
            elif k == "attributes":  # {code: [ids]} for any other dynamic attribute
                for code, ids in (v or {}).items():
                    _set_attr(code, list(ids))
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

    # ---- search (catalog) -------------------------------------------------
    def search_items(
        self,
        text: str = "",
        catalog_ids: Optional[list[int]] = None,
        status_ids: Optional[list[int]] = None,
        order: str = "relevance",
        page: int = 1,
        per_page: int = 96,
        extra: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
        filters: Optional[dict[str, list[int]]] = None,
    ) -> dict[str, Any]:
        """Search live listings, like the site's search bar.

        GET https://api.vinted.<domain>/svc-catalogue/items (captured 2026-11-08).
        Filters go in attribute_ids[<code>] (catalog, status, brand, size, color,
        material). order: relevance | price_low_to_high | price_high_to_low |
        newest_first. status_ids are condition ids; `filters` adds other
        attribute filters, e.g. {"language_book": [6435]}. Multiple values are
        comma-separated. Parent catalogs work here (e.g. 2312 = all books).
        `extra` passes any other query parameter through unchanged.
        """
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "time": int(time.time()),
            "search_text": text,
            "order": order,
            "global_search_session_id": session_id or str(uuid.uuid4()),
        }
        attrs = {"catalog": catalog_ids, "status": status_ids, **(filters or {})}
        for code, ids in attrs.items():
            params[f"attribute_ids[{code}]"] = ",".join(str(i) for i in ids or [])
        params.update(extra or {})
        r = self.get(
            self.market.search_url,
            params=params,
            headers={"platform": "web", "x-next-app": "marketplace-web",
                     "referer": f"https://{self.host}/"},
            timeout=30,
        )
        return self._json(r, "search_items")

    def search_all(self, text: str = "", limit: int = 96, **kw) -> list[dict[str, Any]]:
        """search_items across pages until `limit` items or no more results."""
        out: list[dict[str, Any]] = []
        page = 1
        per_page = min(max(limit, 1), 96)
        sid = str(uuid.uuid4())  # one search session across pages, like the site
        while len(out) < limit:
            res = self.search_items(text, page=page, per_page=per_page, session_id=sid, **kw)
            items = res.get("items") or []
            out.extend(items)
            pag = res.get("pagination") or {}
            if len(items) < per_page or page >= int(pag.get("total_pages") or page + 1):
                break
            page += 1
        return out[:limit]

    # ---- lifecycle: publish / hide / unhide / delete ----------------------
    def publish_draft(
        self, draft_id: int, changes: Optional[dict[str, Any]] = None, push_up: bool = False
    ) -> dict[str, Any]:
        """Publish a draft (the site's "Carica" button).

        POST /item_upload/drafts/{id}/completion with the full draft, built from
        the draft's current edit-data plus optional `changes` (same keys as
        update_item, e.g. {"attributes": {"language_book": [6435]}}). Vinted
        rejects it if a field required for the category is missing.
        """
        it = self.get_item_edit(draft_id)
        it["id"] = draft_id
        draft, sid = self._edit_draft(it, changes or {})
        r = self.post(
            f"{self.base}/item_upload/drafts/{draft_id}/completion",
            headers={"content-type": "application/json"},
            json={"draft": draft, "push_up": push_up, "parcel": None, "upload_session_id": sid},
            timeout=60,
        )
        return self._json(r, "publish_draft")

    def set_hidden(self, item_id: int, hidden: bool) -> dict[str, Any]:
        """Hide (True) or show again (False) a published item. Reversible."""
        r = self.put(
            f"{self.base}/items/{item_id}/is_hidden",
            headers={"content-type": "application/json"},
            json={"is_hidden": hidden},
            timeout=30,
        )
        return self._json(r, "hide" if hidden else "unhide")

    def delete_item(self, item_id: int) -> dict[str, Any]:
        """Permanently delete an item. NOT reversible."""
        r = self.post(f"{self.base}/items/{item_id}/delete", timeout=30)
        return self._json(r, "delete")

    def delete_draft(self, draft_id: int) -> dict[str, Any]:
        """Permanently delete a draft. NOT reversible.

        For published items use delete_item (which returns 400 on drafts).
        """
        r = self.delete(f"{self.base}/item_upload/drafts/{draft_id}", timeout=30)
        if r.status_code not in (200, 204):
            raise VintedError(f"delete_draft -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not (r.text or "").strip():
            return {}
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    # ---- sold orders --------------------------------------------------------
    def inbox(self, page: int = 1, per_page: int = 100) -> tuple[list[dict[str, Any]], int]:
        """One page of conversations, most recently updated first, and the number
        of pages. Each has id, description (a preview of the last message, not the
        item title), item_count, updated_at, but not the transaction: use
        get_conversation for that."""
        r = self.get(f"{self.base}/inbox", params={"page": page, "per_page": per_page}, timeout=30)
        data = self._json(r, "inbox")
        pages = (data.get("pagination") or {}).get("total_pages") or 1
        return data.get("conversations") or [], int(pages)

    def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        """Conversation detail: messages and the transaction (item_ids, status,
        is_completed, offer_price, ...), if any."""
        r = self.get(f"{self.base}/conversations/{conversation_id}", timeout=30)
        data = self._json(r, "get_conversation")
        return data.get("conversation", data)

    def get_transaction(self, transaction_id: int) -> dict[str, Any]:
        """Transaction detail (order.item_ids, offer, fees, status_title, ...)."""
        r = self.get(f"{self.base}/transactions/{transaction_id}", timeout=30)
        data = self._json(r, "get_transaction")
        return data.get("transaction", data)

    def transaction_items(self, transaction_id: int) -> list[tuple[int, Optional[float]]]:
        """(item id, listing price) for each item in a transaction's order, in
        order.item_ids order (cached per instance). The price is None when the
        transaction doesn't report it."""
        tid = int(transaction_id)
        if tid not in self._tx_items:
            order = self.get_transaction(tid).get("order") or {}
            listed: dict[int, Optional[float]] = {}
            for it in order.get("items") or []:
                try:
                    listed[int(it["id"])] = float((it.get("price") or {}).get("amount"))
                except (KeyError, TypeError, ValueError):
                    continue
            ids = [int(i) for i in order.get("item_ids") or []] or list(listed)
            self._tx_items[tid] = [(i, listed.get(i)) for i in ids]
        return self._tx_items[tid]

    def transaction_item_ids(self, transaction_id: int) -> list[int]:
        """Item ids contained in a transaction's order (cached per instance)."""
        return [i for i, _ in self.transaction_items(transaction_id)]

    def order_item_amounts(self, order: dict[str, Any]) -> dict[int, float]:
        """Sale amount of each item in a sold order ({"price": {"amount": ...},
        "transaction_id": ...}).

        A single item gets the whole order amount. A bundle's amount (the
        accepted offer for the whole set) is split in proportion to the items'
        listing prices, to the cent, summing exactly to the order amount; if a
        listing price is missing the bundle can't be split and {} is returned.
        """
        try:
            total = float((order.get("price") or {}).get("amount"))
        except (TypeError, ValueError):
            return {}
        items = self.transaction_items(order["transaction_id"])
        if len(items) == 1:
            return {items[0][0]: total}
        if not items or any(p is None for _, p in items):
            return {}
        shares = split_amount(total, [p for _, p in items])
        return {} if shares is None else {i: s for (i, _), s in zip(items, shares)}

    def _conversation_sales(self, conv: dict[str, Any], wanted=None) -> dict[int, Sale]:
        """Sales in a conversation's transaction, if it is a purchase of one of our
        items. With `wanted`, only conversations holding one of those item ids are
        looked at (the others cost no further request)."""
        tx = conv.get("transaction") or {}
        tid = tx.get("id")
        ids = [int(i) for i in tx.get("item_ids") or []] or ([int(tx["item_id"])] if tx.get("item_id") else [])
        if tid is None or not ids or tx.get("current_user_side") != "seller":
            return {}
        if not _is_sale(tx):
            return {}
        if wanted is not None and not any(i in wanted for i in ids):
            return {}
        total = _money(tx.get("offer_price"))
        if total is None:
            paid, fee = _money(tx.get("total_amount_without_tax")), _money(tx.get("service_fee"))
            total = round(paid - (fee or 0), 2) if paid is not None else None
        if total is None:
            return {}
        amounts = {ids[0]: total} if len(ids) == 1 else self.order_item_amounts(
            {"price": {"amount": total}, "transaction_id": tid}
        )
        if not amounts:
            return {}
        completed = bool(tx.get("is_completed")) or int(tx["status"]) == COMPLETED_STATUS
        completed_at = None
        if completed:
            try:
                completed_at = self.get_transaction(tid).get("status_updated_at")
            except VintedError:
                pass
        return {
            i: Sale(a, len(amounts), round(sum(amounts.values()), 2), completed, completed_at, int(tid), conv.get("id"))
            for i, a in amounts.items()
        }

    def conversation_sales(self, conversation_id: int, wanted=None) -> dict[int, Sale]:
        """Sales (per item id) in one conversation, read again: see sales_since."""
        return self._conversation_sales(self.get_conversation(conversation_id), wanted)

    def sales_since(
        self, since: Optional[str] = None, wanted=None, max_conversations: int = 50
    ) -> tuple[dict[int, Sale], Optional[str], bool]:
        """Sales in the conversations updated after `since` (ISO 8601; None = all).

        A conversation is updated by every event of its order (sold, shipped,
        delivered, buyer confirmed receipt), so reading only those updated since
        the previous call is enough to follow every sale. Requests: one inbox
        page per 100 updated conversations, one per conversation, plus one per
        bundle (listing prices) and per completed purchase (completion date).

        Conversations are read oldest first, at most `max_conversations` (never
        splitting ones updated at the same second). Returns the sales per item
        id (the latest conversation wins), the new `since` (updated_at of the
        last conversation read, or `since` if none) and whether some updated
        conversations are left for the next call.
        """
        after = _when(since) if since else None
        new: list[dict[str, Any]] = []
        page, pages = 1, 1
        while page <= pages:
            convs, pages = self.inbox(page=page, per_page=100)
            fresh = [c for c in convs if after is None or (_when(c.get("updated_at")) or after) > after]
            new += fresh
            if not convs or len(fresh) < len(convs):
                break  # newest first: the rest is older than `since`
            page += 1
        new.sort(key=lambda c: _when(c.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc))
        cut = min(len(new), max(1, max_conversations))
        while 0 < cut < len(new) and new[cut].get("updated_at") == new[cut - 1].get("updated_at"):
            cut += 1
        found: dict[int, Sale] = {}
        mark = since
        for conv in new[:cut]:
            found.update(self.conversation_sales(conv["id"], wanted))
            mark = conv.get("updated_at") or mark
        return found, mark, cut < len(new)

def split_amount(total: float, weights: list[float]) -> Optional[list[float]]:
    """Split `total` in proportion to `weights`, to the cent, so that the parts
    sum exactly to `total` (largest remainder: leftover cents go to the largest
    fractional parts). None if the weights don't sum to a positive amount."""
    cents = round(total * 100)
    w = [max(0, round(x * 100)) for x in weights]
    wsum = sum(w)
    if wsum <= 0:
        return None
    parts = [cents * x // wsum for x in w]
    by_remainder = sorted(range(len(w)), key=lambda k: -(cents * w[k] % wsum))
    for k in by_remainder[: cents - sum(parts)]:
        parts[k] += 1
    return [p / 100 for p in parts]


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
    attributes: Optional[dict[str, list[int]]] = None,
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
    for code, ids in (attributes or {}).items():
        item_attributes.append({"code": code, "ids": list(ids)})

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
