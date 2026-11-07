"""Password-protected admin dashboard (no django.contrib.admin)."""

from __future__ import annotations

import csv
import hmac
import statistics
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from alerts.models import AlertRequest, StockEvent
from alerts.services import requeue, stock

W = AlertRequest.Status
PAGE_SIZE = 50
STATUS_FILTERS = {
    "": Q(),
    "waiting": Q(status=W.WAITING, due_at__isnull=True),
    "queued": Q(status=W.WAITING, due_at__isnull=False),
    "sending": Q(status=W.SENDING),
    "notified": Q(status=W.NOTIFIED),
    "failed": Q(status=W.FAILED),
}

# --- login throttle ------------------------------------------------------------------

THROTTLE_MAX_FAILURES = 10
THROTTLE_WINDOW = 300.0
_failures: dict[str, deque] = defaultdict(deque)
_failures_lock = threading.Lock()


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()  # added by the nearest proxy
    return request.META.get("REMOTE_ADDR", "")


def _prune(ip: str, now: float) -> deque:
    attempts = _failures[ip]
    while attempts and now - attempts[0] > THROTTLE_WINDOW:
        attempts.popleft()
    return attempts


def _is_throttled(ip: str) -> bool:
    with _failures_lock:
        attempts = _prune(ip, time.monotonic())
        throttled = len(attempts) >= THROTTLE_MAX_FAILURES
        if not attempts:
            _failures.pop(ip, None)
        return throttled


def _record_failure(ip: str) -> None:
    with _failures_lock:
        _prune(ip, time.monotonic()).append(time.monotonic())


def reset_throttle() -> None:
    with _failures_lock:
        _failures.clear()


# --- helpers -------------------------------------------------------------------------


def _is_admin(request) -> bool:
    return request.session.get("admin") is True


def _safe_next(request, value: str | None) -> str:
    value = (value or "").strip()
    if (
        value.startswith("/admin")
        and "\\" not in value
        and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()})
    ):
        return value
    return "/admin"


def _login_page(request, error: str | None = None, status: int = 401, next_url: str = "/admin"):
    return render(
        request,
        "alerts/admin/login.html",
        {"store_name": settings.STORE_NAME, "error": error, "next": next_url},
        status=status,
    )


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not _is_admin(request):
            if request.method == "GET":
                return _login_page(request, next_url=_safe_next(request, request.get_full_path()))
            return HttpResponseRedirect("/admin")
        return view(request, *args, **kwargs)

    return wrapper


# --- views ---------------------------------------------------------------------------


@never_cache
@require_POST
def login(request):
    next_url = _safe_next(request, request.POST.get("next"))
    ip = _client_ip(request)
    if _is_throttled(ip):
        return _login_page(request, "Too many attempts. Wait a few minutes and try again.", 429, next_url)
    expected = settings.ADMIN_PASSWORD
    supplied = request.POST.get("password", "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        _record_failure(ip)
        return _login_page(request, "That password isn't right.", 401, next_url)
    request.session.cycle_key()
    request.session["admin"] = True
    return HttpResponseRedirect(next_url)


@never_cache
@require_POST
def logout(request):
    request.session.flush()
    return HttpResponseRedirect("/admin")


def _stats(now) -> dict:
    day_ago = now - timedelta(hours=24)
    events = StockEvent.objects.aggregate(total=Sum("deliveries"))
    event_count = StockEvent.objects.count()
    durations = [
        max((notified_at - received_at).total_seconds(), 0.0)
        for notified_at, received_at in AlertRequest.objects.filter(
            status=W.NOTIFIED, triggered_by__isnull=False, notified_at__isnull=False
        )
        .order_by("-notified_at")
        .values_list("notified_at", "triggered_by__received_at")[:200]
    ]
    return {
        "notified_24h": AlertRequest.objects.filter(status=W.NOTIFIED, notified_at__gte=day_ago).count(),
        "requests_24h": AlertRequest.objects.filter(created_at__gte=day_ago).count(),
        "events_24h": StockEvent.objects.filter(received_at__gte=day_ago).count(),
        "duplicate_deliveries": (events["total"] or 0) - event_count,
        "phantom_events": StockEvent.objects.filter(outcome=StockEvent.Outcome.PHANTOM).count(),
        "retries_pending": AlertRequest.objects.filter(status=W.WAITING, attempts__gt=0).count(),
        "median_seconds_to_notify": statistics.median(durations) if durations else None,
    }


@never_cache
@require_GET
@admin_required
def dashboard(request):
    now = timezone.now()
    status = request.GET.get("status", "")
    if status not in STATUS_FILTERS:
        status = ""
    q = request.GET.get("q", "").strip()[:200]

    counts = {key or "all": AlertRequest.objects.filter(cond).count() for key, cond in STATUS_FILTERS.items()}

    rows = AlertRequest.objects.select_related("triggered_by").filter(STATUS_FILTERS[status])
    if q:
        rows = rows.filter(Q(email__icontains=q) | Q(sku__icontains=q))
    page_obj = Paginator(rows.order_by("-created_at", "-id"), PAGE_SIZE).get_page(request.GET.get("page"))
    catalogue = stock.catalogue_by_sku()
    for alert in page_obj.object_list:
        alert.product = catalogue.get(alert.sku.upper())

    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "store_name": settings.STORE_NAME,
        "now": now,
        "status": status,
        "q": q,
        "counts": counts,
        "stats": _stats(now),
        "page_obj": page_obj,
        "events": list(StockEvent.objects.order_by("-received_at", "-id")[:25]),
        "query_string": params.urlencode(),
    }
    return render(request, "alerts/admin/dashboard.html", context)


@never_cache
@require_POST
@admin_required
def retry(request, public_id):
    requeue(AlertRequest.objects.filter(public_id=public_id))
    return HttpResponseRedirect(_safe_next(request, request.POST.get("next")))


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    # Neutralise spreadsheet formulas in user-supplied fields.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@never_cache
@require_GET
@admin_required
def export_csv(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="back-in-stock-requests.csv"'
    writer = csv.writer(response)
    writer.writerow(
        ["public_id", "email", "sku", "product_name", "status", "created_at", "notified_at",
         "attempts", "last_error", "triggered_by"]
    )
    rows = AlertRequest.objects.select_related("triggered_by").order_by("created_at", "id")
    for alert in rows.iterator(chunk_size=500):
        writer.writerow(
            [_csv_cell(v) for v in (
                alert.public_id,
                alert.email,
                alert.sku,
                alert.product_name,
                alert.status,
                alert.created_at.isoformat(),
                alert.notified_at.isoformat() if alert.notified_at else "",
                alert.attempts,
                alert.last_error,
                alert.triggered_by.event_id if alert.triggered_by else "",
            )]
        )
    return response
