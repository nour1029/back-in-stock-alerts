"""In-process fakes for the stock API and the Resend API. No real network in tests."""

from __future__ import annotations

import itertools
import threading
from urllib.parse import unquote

import httpx

STOCK_URL = "http://stock.test/api/stock"
RESEND_URL = "http://resend.test"

CATALOGUE = {
    "TEE-ORG-WHT-M": ("Organic Cotton Tee, White, M", 24),
    "KNF-CHF-20": ("Chef's Knife, 20 cm", 89),
    "MUG-STN-SAGE": ("Stoneware Mug, Sage", 18),
}


class FakeStockAPI:
    def __init__(self):
        self.stock: dict[str, int] = {sku: 0 for sku in CATALOGUE}
        self.down = False
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def _product(self, sku: str) -> dict:
        name, price = CATALOGUE.get(sku.upper(), (None, None))
        quantity = self.stock.get(sku, self.stock.get(sku.upper(), 0))
        body = {"sku": sku, "quantity": quantity, "in_stock": quantity > 0}
        if name:
            body.update(name=name, price=price, currency="GBP", category="Test",
                        image=f"https://img.test/{sku.upper()}.jpg")
        return body

    def get(self, url, headers=None, timeout=None, follow_redirects=None, **kwargs):
        assert url.startswith(STOCK_URL), f"unexpected GET {url}"
        assert headers and headers.get("Authorization", "").startswith("Bearer ")
        with self.lock:
            self.calls.append(url)
        if self.down:
            raise httpx.ConnectError("stock API down")
        path = url[len(STOCK_URL):]
        if not path:
            products = [{**self._product(sku), "sku": sku} for sku in CATALOGUE]
            return httpx.Response(200, json={"products": products})
        return httpx.Response(200, json=self._product(unquote(path.lstrip("/"))))


class FakeResend:
    """Honours Idempotency-Key like the harness: a repeat of an accepted key isn't re-sent."""

    def __init__(self):
        self.fail_next = 0
        self.status_for_failures = 500
        self.timeout_after_accept = 0  # accept the email, then lose the response
        self.requests: list[dict] = []  # every POST we received
        self.accepted: dict[str, dict] = {}  # idempotency key -> accepted email
        self.deduped = 0
        self.lock = threading.Lock()
        self._ids = itertools.count(1)

    @property
    def delivered(self) -> list[dict]:
        return list(self.accepted.values())

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        assert url == RESEND_URL + "/emails", f"unexpected POST {url}"
        key = (headers or {}).get("Idempotency-Key")
        with self.lock:
            self.requests.append({"json": json, "key": key})
            if key in self.accepted:
                self.deduped += 1
                return httpx.Response(200, json={"id": self.accepted[key]["id"]})
            if self.fail_next > 0:
                self.fail_next -= 1
                return httpx.Response(self.status_for_failures, json={"message": "injected failure"})
            email = {**json, "id": f"msg_{next(self._ids)}", "key": key}
            self.accepted[key] = email
            if self.timeout_after_accept > 0:
                self.timeout_after_accept -= 1
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(200, json={"id": email["id"]})
