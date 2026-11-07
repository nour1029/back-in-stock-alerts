"""Client for the store's stock API.

The stock API is the only source of truth for stock levels. Its responses are
coerced and validated here, so callers always get the same product-dict shape:

    {"sku", "name", "price", "currency", "category", "image", "quantity", "in_stock"}
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

CATALOGUE_TTL = 60.0
DEFAULT_TIMEOUT = 5.0


class StockAPIError(Exception):
    """The stock API could not give a trustworthy answer."""


@dataclass(frozen=True)
class StockResult:
    quantity: int
    product: dict


# --- coercion ----------------------------------------------------------------


def _coerce_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value == int(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return _coerce_int(number)
    return None


def _coerce_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _coerce_str(value, limit: int = 500) -> str:
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)[:limit]
    return ""


def normalise_product(data: dict, fallback_sku: str = "") -> dict:
    """Build the canonical product dict from untrusted API JSON."""
    quantity = _coerce_int(data.get("quantity"))
    quantity = max(quantity, 0) if quantity is not None else 0
    image = _coerce_str(data.get("image"), 1000)
    if not image.startswith(("https://", "http://")):
        image = ""
    return {
        "sku": _coerce_str(data.get("sku"), 64) or fallback_sku,
        "name": _coerce_str(data.get("name"), 200),
        "price": _coerce_float(data.get("price")),
        "currency": _coerce_str(data.get("currency"), 8).upper(),
        "category": _coerce_str(data.get("category"), 100),
        "image": image,
        "quantity": quantity,
        "in_stock": quantity > 0,
    }


# --- HTTP ----------------------------------------------------------------------


def _get_json(path: str, timeout: float):
    base = settings.STOCK_API_URL
    if not base:
        raise StockAPIError("STOCK_API_URL is not configured")
    url = base + path
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.STOCK_API_KEY}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)),
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise StockAPIError(f"stock API unreachable: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise StockAPIError(f"stock API returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise StockAPIError("stock API returned invalid JSON") from exc


def check_stock(sku: str, timeout: float = DEFAULT_TIMEOUT) -> StockResult:
    """Live stock level for one SKU. Raises StockAPIError rather than guessing."""
    data = _get_json("/" + quote(sku, safe=""), timeout)
    if not isinstance(data, dict):
        raise StockAPIError("stock API response is not an object")
    quantity = _coerce_int(data.get("quantity"))
    if quantity is None:
        raise StockAPIError("stock API response has no numeric quantity")
    product = normalise_product(data, fallback_sku=sku)
    product["quantity"] = max(quantity, 0)
    product["in_stock"] = product["quantity"] > 0
    return StockResult(quantity=product["quantity"], product=product)


# --- catalogue cache -------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {"products": None, "fetched_at": 0.0}


def fetch_catalogue(timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Fetch the catalogue now (no cache). Raises StockAPIError."""
    data = _get_json("", timeout)
    items = data.get("products") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise StockAPIError("catalogue response has no product list")
    products = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if _coerce_int(item.get("quantity")) is None:
            continue  # without a trustworthy quantity the row is useless
        product = normalise_product(item)
        if product["sku"]:
            products.append(product)
    with _cache_lock:
        _cache["products"] = products
        _cache["fetched_at"] = time.monotonic()
    return [dict(p) for p in products]


def get_catalogue(max_age: float = CATALOGUE_TTL) -> list[dict]:
    """Cached catalogue. On error serves stale data, or [] if there is none."""
    with _cache_lock:
        products, fetched_at = _cache["products"], _cache["fetched_at"]
    if products is not None and time.monotonic() - fetched_at < max_age:
        return [dict(p) for p in products]
    try:
        return fetch_catalogue(timeout=3.0)
    except StockAPIError as exc:
        logger.warning("catalogue fetch failed, serving %s: %s", "stale data" if products else "nothing", exc)
        return [dict(p) for p in products or []]


def clear_cache() -> None:
    with _cache_lock:
        _cache["products"] = None
        _cache["fetched_at"] = 0.0


def catalogue_by_sku() -> dict[str, dict]:
    """Catalogue keyed by upper-cased SKU for case-insensitive lookups."""
    return {p["sku"].upper(): p for p in get_catalogue()}


def canonical_sku(sku: str) -> str:
    """Return the catalogue's casing for `sku` if it matches case-insensitively, else `sku` unchanged."""
    match = catalogue_by_sku().get(sku.upper())
    return match["sku"] if match else sku


def get_product(sku: str, lookup: bool = True) -> dict | None:
    """Product dict for display. Catalogue first, then (optionally) a per-SKU lookup.

    Returns None for SKUs the store doesn't know by name.
    """
    if not sku:
        return None
    match = catalogue_by_sku().get(sku.upper())
    if match:
        return match
    if not lookup:
        return None
    try:
        product = check_stock(sku, timeout=3.0).product
    except StockAPIError:
        return None
    return product if product["name"] else None
