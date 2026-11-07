"""HTML pages: the demo storefront, the embeddable form, and the health check."""

import re

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from alerts.services import stock
from alerts.validation import clean_sku

ACCENT_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _accent(value: str) -> str | None:
    match = ACCENT_RE.fullmatch((value or "").strip())
    return f"#{match.group(1).lower()}" if match else None


@require_GET
def home(request):
    return render(
        request,
        "alerts/home.html",
        {"store_name": settings.STORE_NAME, "products": stock.get_catalogue()},
    )


@require_GET
@xframe_options_exempt
def embed(request):
    sku = clean_sku(request.GET.get("sku", "")) or ""
    product = None
    if sku:
        sku = stock.canonical_sku(sku)
        product = stock.get_product(sku)
    response = render(
        request,
        "alerts/embed.html",
        {
            "sku": sku,
            "product": product,
            "store_name": settings.STORE_NAME,
            "api_url": "/api/requests",
            "accent": _accent(request.GET.get("accent", "")),
        },
    )
    response["Content-Security-Policy"] = "frame-ancestors *"
    return response


def healthz(request):
    return JsonResponse({"ok": True})
