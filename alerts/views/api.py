"""Public JSON endpoints: customer sign-up and the stock webhook."""

from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from alerts import notifier
from alerts.models import AlertRequest, StockEvent
from alerts.services import stock
from alerts.validation import clean_email, clean_sku

logger = logging.getLogger(__name__)

SIGNUP_STOCK_TIMEOUT = 3.0

ERROR_MESSAGES = {
    "invalid_json": "The request body must be a JSON object with an email and a SKU.",
    "invalid_email": "Please enter a valid email address.",
    "invalid_sku": "That product code doesn't look right.",
}


def _cors(response: HttpResponse) -> HttpResponse:
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Content-Type"
    response["Access-Control-Max-Age"] = "86400"
    return response


def _error(code: str, status: int = 400) -> JsonResponse:
    return _cors(JsonResponse({"error": code, "message": ERROR_MESSAGES[code]}, status=status))


def _parse_body(request, allow_form: bool = False) -> dict | None:
    """JSON object body (or form fields, if allowed). None if unusable."""
    try:
        raw = request.body
    except RequestDataTooBig:
        return None
    content_type = request.content_type or ""
    if allow_form and content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        return {key: request.POST.get(key) for key in request.POST}
    try:
        data = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _request_payload(alert: AlertRequest, created: bool, in_stock: bool | None, product: dict | None) -> dict:
    return {
        "id": str(alert.public_id),
        "created": created,
        "status": alert.status,
        "email": alert.email,
        "sku": alert.sku,
        "in_stock": in_stock,
        "product": product,
    }


@csrf_exempt
def create_request(request):
    if request.method == "OPTIONS":
        return _cors(HttpResponse(status=204))
    if request.method != "POST":
        response = _cors(JsonResponse({"error": "method_not_allowed"}, status=405))
        response["Allow"] = "POST, OPTIONS"
        return response

    data = _parse_body(request, allow_form=True)
    if data is None:
        return _error("invalid_json")
    email = clean_email(data.get("email"))
    if email is None:
        return _error("invalid_email")
    sku = clean_sku(data.get("sku"))
    if sku is None:
        return _error("invalid_sku")
    sku = stock.canonical_sku(sku)

    # Live stock: if it's in stock right now, the customer is emailed straight away.
    quantity: int | None = None
    product: dict | None = None
    try:
        result = stock.check_stock(sku, timeout=SIGNUP_STOCK_TIMEOUT)
        quantity, product = result.quantity, result.product
    except stock.StockAPIError as exc:
        logger.warning("stock check at signup failed for %s: %s", sku, exc)
        product = stock.get_product(sku, lookup=False)
        if product:  # catalogue copy: its quantity may be stale, so don't present it as live
            product = {**product, "quantity": 0, "in_stock": False}
    if product is not None and not product["name"]:
        product = None
    in_stock = None if quantity is None else quantity > 0

    alert, created = _create_or_get_open(
        email=email,
        sku=sku,
        product_name=(product or {}).get("name", "")[:200],
        stock_at_signup=quantity,
        out_of_stock_seen=quantity is None or quantity == 0,
    )
    if alert is None:  # pathological: kept colliding; let the client retry
        return _cors(JsonResponse({"error": "busy", "message": "Please try again."}, status=503))
    if in_stock:
        _queue_now(alert)
    if product is not None:
        product = {**product, "sku": alert.sku}
    payload = _request_payload(alert, created, in_stock, product)
    return _cors(JsonResponse(payload, status=201 if created else 200))


def _queue_now(alert: AlertRequest) -> None:
    """In stock at sign-up: send now rather than waiting for a sell-out and restock.

    Conditional on WAITING with nothing queued, so a duplicate sign-up never queues a
    second send. The notifier still re-checks stock and claims the row before sending.
    """
    now = timezone.now()
    queued = AlertRequest.objects.filter(
        pk=alert.pk, status=AlertRequest.Status.WAITING, due_at__isnull=True
    ).update(due_at=now, updated_at=now)
    if queued:
        alert.due_at = now
        notifier.wake()


def _create_or_get_open(**fields) -> tuple[AlertRequest | None, bool]:
    """Insert a new open request, or return the existing open one for (email, sku).

    The partial unique constraint is the guard; this never checks-then-inserts.
    """
    open_statuses = [AlertRequest.Status.WAITING, AlertRequest.Status.SENDING, AlertRequest.Status.FAILED]
    for _ in range(3):
        try:
            with transaction.atomic():
                return AlertRequest.objects.create(**fields), True
        except IntegrityError:
            pass
        existing = AlertRequest.objects.filter(
            email=fields["email"], sku=fields["sku"], status__in=open_statuses
        ).first()
        if existing is None:
            continue  # it was notified between our insert and this read: try again
        if existing.status == AlertRequest.Status.FAILED:
            # They asked again: give the failed request a fresh start.
            AlertRequest.objects.filter(pk=existing.pk, status=AlertRequest.Status.FAILED).update(
                status=AlertRequest.Status.WAITING,
                due_at=None,
                attempts=0,
                out_of_stock_seen=existing.out_of_stock_seen or fields["out_of_stock_seen"],
            )
            existing.refresh_from_db()
        return existing, False
    return None, False


# --- webhook ---------------------------------------------------------------------


def _clean_event_id(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= 128 else None


@csrf_exempt
def stock_webhook(request):
    if request.method != "POST":
        response = JsonResponse({"error": "method_not_allowed"}, status=405)
        response["Allow"] = "POST"
        return response

    expected = settings.WEBHOOK_SECRET
    if not expected:
        logger.error("WEBHOOK_SECRET is not set: refusing webhooks")
        return JsonResponse({"error": "not_configured"}, status=503)
    supplied = request.headers.get("X-Webhook-Secret", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        logger.warning("webhook rejected: bad secret")
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)
    event_id = _clean_event_id(data.get("event_id"))
    if event_id is None:
        return JsonResponse({"error": "invalid_event_id"}, status=400)
    sku = clean_sku(data.get("sku"))
    if sku is None:
        return JsonResponse({"error": "invalid_sku"}, status=400)
    sku = stock.canonical_sku(sku, fetch=False)  # no network before the ack; the worker re-canonicalises
    reported = stock.coerce_int(data.get("quantity"))

    try:
        with transaction.atomic():
            StockEvent.objects.create(
                event_id=event_id, sku=sku, reported_quantity=reported, payload=data
            )
    except IntegrityError:
        # Retry or concurrent duplicate: count it, and if processing gave up, let this
        # delivery revive it. The event is still processed exactly once per success.
        StockEvent.objects.filter(event_id=event_id).update(deliveries=F("deliveries") + 1)
        revived = StockEvent.objects.filter(event_id=event_id, status=StockEvent.Status.FAILED).update(
            status=StockEvent.Status.PENDING, attempts=0, next_attempt_at=None
        )
        if revived:
            notifier.wake()
        logger.info("duplicate webhook delivery %s%s", event_id, " (revived)" if revived else "")
        return JsonResponse({"received": True, "duplicate": True})

    notifier.wake()
    logger.info("webhook %s recorded: %s reported qty %s", event_id, sku, reported)
    return JsonResponse({"received": True})
