"""Background worker: turns recorded stock events into exactly-once emails.

One daemon thread per process (gunicorn runs a single worker). Each pass:

1. recover stale SENDING claims (all of them on startup),
2. process pending stock events against the live stock API,
3. send due requests, after re-checking live stock, claiming each row atomically,
4. every 60 s, reconcile waiting requests against the catalogue (lost webhooks).

All state lives in the database, so a restart picks up exactly where it left off.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import timedelta

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from alerts.models import AlertRequest, StockEvent
from alerts.services import email as email_service
from alerts.services import stock

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
RECONCILE_SECONDS = 60.0
STALE_CLAIM = timedelta(minutes=2)
SEND_BATCH = 50
EVENT_BATCH = 20
MAX_SEND_ATTEMPTS = 15
MAX_EVENT_ATTEMPTS = 10
STOCK_RECHECK_DELAY = timedelta(seconds=10)
RECONCILE_LOOKUP_CAP = 25
BACKOFF_STEPS = [3, 10, 30, 60, 120, 300, 600]
BACKOFF_MAX = 1800

W = AlertRequest.Status

_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_reconcile_cursor = 0


def backoff(attempt: int) -> timedelta:
    """Delay before retry number `attempt` (1-based)."""
    seconds = BACKOFF_STEPS[attempt - 1] if 1 <= attempt <= len(BACKOFF_STEPS) else BACKOFF_MAX
    return timedelta(seconds=seconds)


def wake() -> None:
    _wake.set()


def start(recover_all: bool = False) -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, args=(recover_all,), name="notifier", daemon=True)
        _thread.start()
    logger.info("notifier started")


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    _wake.set()
    thread = _thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)


def _step(name: str, fn, *args):
    try:
        return fn(*args)
    except Exception:  # never let one failure kill the loop
        logger.exception("notifier step %s failed", name)
        return 0


def _run(recover_all: bool) -> None:
    last_reconcile = float("-inf")  # reconcile right away: catch up on anything missed while down
    try:
        close_old_connections()
        if recover_all:
            _step("recover_all", recover_stale_claims, True)
        while not _stop.is_set():
            _wake.clear()
            close_old_connections()
            _step("recover", recover_stale_claims)
            busy = _step("events", process_pending_events)
            busy = _step("send", send_due_requests) or busy
            if time.monotonic() - last_reconcile >= RECONCILE_SECONDS:
                last_reconcile = time.monotonic()
                _step("reconcile", reconcile)
            if busy:
                continue  # more work may be waiting: go again without sleeping
            _wake.wait(POLL_SECONDS)
    finally:
        connection.close()
        logger.info("notifier stopped")


def run_once() -> None:
    """One synchronous pass (used by tests)."""
    recover_stale_claims()
    process_pending_events()
    send_due_requests()
    reconcile()


# --- 1. stale claims -------------------------------------------------------------


def recover_stale_claims(recover_all: bool = False) -> int:
    """Put SENDING rows back in the queue. Safe because the retry reuses the Idempotency-Key."""
    now = timezone.now()
    rows = AlertRequest.objects.filter(status=W.SENDING)
    if not recover_all:
        rows = rows.filter(Q(claimed_at__lt=now - STALE_CLAIM) | Q(claimed_at__isnull=True))
    recovered = rows.update(status=W.WAITING, due_at=now, claimed_at=None, updated_at=now)
    if recovered:
        logger.warning("recovered %d in-flight send(s)", recovered)
    return recovered


# --- 2. stock events ---------------------------------------------------------------


def process_pending_events() -> int:
    now = timezone.now()
    events = list(
        StockEvent.objects.filter(status=StockEvent.Status.PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("received_at", "id")[:EVENT_BATCH]
    )
    for event in events:
        try:
            process_event(event)
        except Exception as exc:
            logger.exception("processing event %s failed", event.event_id)
            _event_failed(event, f"internal error: {exc}")
    return len(events)


def _event_failed(event: StockEvent, error: str) -> None:
    attempts = event.attempts + 1
    now = timezone.now()
    fields = {"attempts": attempts, "last_error": error[:2000]}
    if attempts >= MAX_EVENT_ATTEMPTS:
        fields.update(status=StockEvent.Status.FAILED, next_attempt_at=None, processed_at=now)
    else:
        fields["next_attempt_at"] = now + backoff(attempts)
    StockEvent.objects.filter(pk=event.pk, status=StockEvent.Status.PENDING).update(**fields)


def process_event(event: StockEvent) -> None:
    """Verify the event against the stock API and apply the verified quantity.

    The webhook's own quantity is only a hint and is never used to send.
    """
    sku = stock.canonical_sku(event.sku)
    if sku != event.sku:
        StockEvent.objects.filter(pk=event.pk).update(sku=sku)
        event.sku = sku
    checked_at = timezone.now()
    try:
        result = stock.check_stock(event.sku)
    except stock.StockAPIError as exc:
        logger.warning("event %s: stock check failed: %s", event.event_id, exc)
        _event_failed(event, str(exc))
        return

    quantity = result.quantity
    now = timezone.now()
    with transaction.atomic():
        queued = 0
        if quantity > 0:
            queued = AlertRequest.objects.filter(sku=event.sku, status=W.WAITING, due_at__isnull=True).update(
                due_at=now, triggered_by=event, updated_at=now
            )
            outcome = StockEvent.Outcome.RESTOCKED
        else:
            AlertRequest.objects.filter(sku=event.sku, status=W.WAITING, created_at__lt=checked_at).update(
                out_of_stock_seen=True, due_at=None, updated_at=now
            )
            reported = event.reported_quantity
            outcome = StockEvent.Outcome.PHANTOM if reported and reported > 0 else StockEvent.Outcome.SOLD_OUT
        StockEvent.objects.filter(pk=event.pk, status=StockEvent.Status.PENDING).update(
            status=StockEvent.Status.PROCESSED,
            verified_quantity=quantity,
            outcome=outcome,
            queued_count=queued,
            attempts=F("attempts") + 1,
            next_attempt_at=None,
            last_error="",
            processed_at=now,
        )
    logger.info(
        "event %s: %s verified qty %d (reported %s) → %s, %d queued",
        event.event_id, event.sku, quantity, event.reported_quantity, outcome or "no change", queued,
    )


# --- 3. sending ------------------------------------------------------------------------


def send_due_requests() -> int:
    now = timezone.now()
    due = list(
        AlertRequest.objects.filter(status=W.WAITING, due_at__lte=now).order_by("due_at", "id")[:SEND_BATCH]
    )
    by_sku: dict[str, list[AlertRequest]] = defaultdict(list)
    for alert in due:
        by_sku[alert.sku].append(alert)

    for sku, alerts in by_sku.items():
        ids = [a.pk for a in alerts]
        # Time passes between queueing and sending: confirm the stock right now.
        try:
            result = stock.check_stock(sku)
        except stock.StockAPIError as exc:
            logger.warning("send %s: stock re-check failed (%s); retrying shortly", sku, exc)
            AlertRequest.objects.filter(pk__in=ids, status=W.WAITING, due_at__isnull=False).update(
                due_at=timezone.now() + STOCK_RECHECK_DELAY, updated_at=timezone.now()
            )
            continue
        if result.quantity <= 0:
            unqueued = AlertRequest.objects.filter(pk__in=ids, status=W.WAITING).update(
                due_at=None, out_of_stock_seen=True, updated_at=timezone.now()
            )
            logger.info("send %s: sold out again before sending, %d back to waiting", sku, unqueued)
            continue
        product = result.product if result.product["name"] else stock.get_product(sku, lookup=False)
        for alert in alerts:
            if _stop.is_set():
                return len(due)
            send_one(alert, product, result.quantity)
    return len(due)


def send_one(alert: AlertRequest, product: dict | None, quantity: int | None) -> bool:
    """Claim one request and send it. Returns True if it ended up NOTIFIED."""
    now = timezone.now()
    claimed = AlertRequest.objects.filter(pk=alert.pk, status=W.WAITING, due_at__isnull=False).update(
        status=W.SENDING, claimed_at=now, updated_at=now
    )
    if claimed != 1:
        return False  # someone else (or a sell-out) got there first

    try:
        payload = email_service.build_payload(alert, product, quantity)
        result = email_service.send(payload, alert.idempotency_key)
    except email_service.EmailSendError as exc:
        _send_failed(alert, str(exc), exc.retryable)
        return False
    except Exception as exc:  # e.g. a template error: retry later, don't spin
        logger.exception("building email for %s failed", alert.public_id)
        _send_failed(alert, f"internal error: {exc}", retryable=True)
        return False

    done = timezone.now()
    AlertRequest.objects.filter(pk=alert.pk).update(
        status=W.NOTIFIED,
        notified_at=done,
        provider_message_id=result.message_id,
        last_error="",
        claimed_at=None,
        due_at=None,
        attempts=F("attempts") + 1,
        updated_at=done,
    )
    logger.info("notified %s about %s (message %s)", alert.email, alert.sku, result.message_id or "?")
    return True


def _send_failed(alert: AlertRequest, error: str, retryable: bool) -> None:
    attempts = alert.attempts + 1
    now = timezone.now()
    fields = {"attempts": attempts, "last_error": error[:2000], "claimed_at": None, "updated_at": now}
    if not retryable or attempts >= MAX_SEND_ATTEMPTS:
        fields.update(status=W.FAILED, due_at=None)
        logger.error("giving up on %s after %d attempt(s): %s", alert.public_id, attempts, error)
    else:
        fields.update(status=W.WAITING, due_at=now + backoff(attempts))
        logger.warning("send %s failed (attempt %d), retrying: %s", alert.public_id, attempts, error)
    AlertRequest.objects.filter(pk=alert.pk, status=W.SENDING).update(**fields)


# --- 4. reconciliation ----------------------------------------------------------------


def repair_sku_casing(skus, catalogue: dict[str, dict]) -> int:
    """Rewrite open requests stored with non-catalogue casing (signed up while the
    catalogue was unreachable). If that person already has an open request under the
    canonical SKU, the extra row is a duplicate and is removed, so they can't be
    emailed twice."""
    fixed = 0
    for sku in skus:
        match = catalogue.get(sku.upper())
        if match is None or match["sku"] == sku:
            continue
        rows = AlertRequest.objects.filter(sku=sku, status__in=[W.WAITING, W.FAILED])
        for alert in rows:
            try:
                with transaction.atomic():
                    AlertRequest.objects.filter(pk=alert.pk).update(sku=match["sku"], updated_at=timezone.now())
            except IntegrityError:
                AlertRequest.objects.filter(pk=alert.pk, status__in=[W.WAITING, W.FAILED]).delete()
                logger.warning("merged duplicate request %s (%s → %s)", alert.public_id, sku, match["sku"])
            fixed += 1
    return fixed


def reconcile() -> int:
    """Safety net: any waiting request whose product has stock right now gets its email.

    Runs at startup and every RECONCILE_SECONDS. It covers restocks whose webhooks never
    arrived, and sign-ups made while the item was in stock whose immediate send was lost.
    """
    global _reconcile_cursor
    skus = sorted(
        set(AlertRequest.objects.filter(status=W.WAITING, due_at__isnull=True).values_list("sku", flat=True))
    )
    if not skus:
        return 0

    checked_at = timezone.now()
    try:
        catalogue = {p["sku"].upper(): p for p in stock.fetch_catalogue()}
    except stock.StockAPIError as exc:
        logger.warning("reconcile: catalogue unavailable: %s", exc)
        catalogue = {}
    if catalogue and repair_sku_casing(skus, catalogue):
        skus = sorted(
            set(AlertRequest.objects.filter(status=W.WAITING, due_at__isnull=True).values_list("sku", flat=True))
        )

    levels: dict[str, int] = {}
    others = []
    for sku in skus:
        match = catalogue.get(sku.upper())
        if match is not None and match["sku"] == sku:
            levels[sku] = match["quantity"]
        else:
            others.append(sku)
    if others:  # rotate through non-catalogue SKUs so none starves
        start = _reconcile_cursor % len(others)
        batch = (others[start:] + others[:start])[:RECONCILE_LOOKUP_CAP]
        _reconcile_cursor = start + len(batch)
        for sku in batch:
            try:
                levels[sku] = stock.check_stock(sku).quantity
            except stock.StockAPIError as exc:
                logger.warning("reconcile: stock check for %s failed: %s", sku, exc)

    queued_total = 0
    now = timezone.now()
    for sku, quantity in levels.items():
        waiting = AlertRequest.objects.filter(sku=sku, status=W.WAITING, due_at__isnull=True)
        if quantity > 0:
            queued = waiting.update(due_at=now, updated_at=now)
            if queued:
                logger.warning("reconcile: %s is in stock (%d); queued %d waiting request(s)", sku, quantity, queued)
                queued_total += queued
        else:
            waiting.filter(out_of_stock_seen=False, created_at__lt=checked_at).update(
                out_of_stock_seen=True, updated_at=now
            )
    if queued_total:
        wake()
    return queued_total
