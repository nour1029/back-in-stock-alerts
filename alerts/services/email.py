"""Render and send the back-in-stock email through a Resend-compatible API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.template import Context, Engine
from django.template.backends.django import get_installed_libraries
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 15.0
# Statuses that may succeed if we try again later. Everything else 4xx is permanent.
RETRYABLE_STATUSES = {401, 403, 408, 409, 425, 429}

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


_text_engine: Engine | None = None


def render_text(template_name: str, context: dict) -> str:
    """Render a plain-text template without HTML auto-escaping."""
    global _text_engine
    if _text_engine is None:
        _text_engine = Engine(app_dirs=True, autoescape=False, libraries=get_installed_libraries())
    return _text_engine.get_template(template_name).render(Context(context, autoescape=False))


class EmailSendError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class SendResult:
    message_id: str


def price_display(product: dict | None) -> str:
    if not product or product.get("price") is None:
        return ""
    currency = product.get("currency") or ""
    amount = f"{product['price']:,.2f}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}".strip()


def build_context(alert, product: dict | None, quantity: int | None) -> dict:
    product_name = (product or {}).get("name") or alert.product_name or alert.sku
    return {
        "store_name": settings.STORE_NAME,
        "email": alert.email,
        "sku": alert.sku,
        "product_name": product_name,
        "product": product,
        "price_display": price_display(product),
        "image_url": (product or {}).get("image") or "",
        "product_url": settings.PRODUCT_URL_TEMPLATE.format(sku=alert.sku),
        "quantity": quantity,
        "requested_at": alert.created_at,
    }


def build_payload(alert, product: dict | None, quantity: int | None) -> dict:
    context = build_context(alert, product, quantity)
    return {
        "from": settings.EMAIL_FROM,
        "to": [alert.email],
        "subject": f"{context['product_name']} is back in stock",
        "html": render_to_string("alerts/email/back_in_stock.html", context),
        "text": render_text("alerts/email/back_in_stock.txt", context),
    }


def send(payload: dict, idempotency_key: str) -> SendResult:
    """POST the payload to {RESEND_BASE_URL}/emails. Raises EmailSendError."""
    base = settings.RESEND_BASE_URL
    if not base:
        raise EmailSendError("RESEND_BASE_URL is not configured", retryable=True)
    try:
        response = httpx.post(
            base + "/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "back-in-stock/1.0",
            },
            timeout=httpx.Timeout(SEND_TIMEOUT, connect=5.0),
        )
    except httpx.HTTPError as exc:
        # Includes timeouts after the request was sent: the provider may have
        # accepted it. Retrying with the same Idempotency-Key is safe.
        raise EmailSendError(f"network error: {exc.__class__.__name__}: {exc}", retryable=True) from exc

    status = response.status_code
    if 200 <= status < 300:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message_id = body.get("id") if isinstance(body, dict) else None
        return SendResult(message_id=str(message_id or "")[:128])

    detail = response.text[:300]
    message = f"HTTP {status}: {detail}"
    retryable = status >= 500 or status in RETRYABLE_STATUSES
    raise EmailSendError(message, retryable=retryable)
