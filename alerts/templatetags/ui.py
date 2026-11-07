"""Presentation helpers for the UI templates (icons, money, durations, status display)."""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# Lucide icon paths (ISC licence), drawn at 24x24 with a shared stroke style.
_ICONS = {
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
             '<path d="M12 9v4"/><path d="M12 17h.01"/>',
    "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 '
              '4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "eye": '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/>'
           '<circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/>'
               '<path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/>'
               '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="m2 2 20 20"/>',
    "package": '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 '
               '1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "zap": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 '
           '.78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "layers": '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 '
              '0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    "ghost": '<path d="M9 10h.01"/><path d="M15 10h.01"/><path d="M12 2a8 8 0 0 0-8 8v12l3-3 2.5 2.5L12 19l2.5 2.5L17 19l3 3V10a8 8 0 0 0-8-8z"/>',
    "timer": '<path d="M10 2h4"/><path d="m12 14 3-3"/><circle cx="12" cy="14" r="8"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 '
             '16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "code": '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    "bag": '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/>',
}


@register.simple_tag
def icon(name, size=16, cls=""):
    """Inline SVG icon. Decorative by default (aria-hidden); pair it with visible text or an aria-label."""
    paths = _ICONS.get(name, "")
    return format_html(
        '<svg class="icon {}" width="{}" height="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{}</svg>',
        cls, size, size, mark_safe(paths),
    )


_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "JPY": "¥"}


@register.filter
def money(product):
    """Format a product dict's price, e.g. {"price": 24, "currency": "GBP"} -> "£24.00". Empty if unknown."""
    if not isinstance(product, dict) or product.get("price") in (None, ""):
        return ""
    try:
        amount = Decimal(str(product["price"]))
    except (InvalidOperation, ValueError):
        return ""
    currency = str(product.get("currency") or "").upper()
    symbol = _CURRENCY_SYMBOLS.get(currency)
    text = f"{amount:,.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}".strip()


@register.filter
def duration(seconds):
    """Compact human duration: 8 -> "8s", 72 -> "1m 12s", 7500 -> "2h 5m". Empty for None."""
    if seconds in (None, ""):
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{max(total, 0)}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{hours // 24}d"


@register.simple_tag
def seconds_between(later, earlier):
    """Seconds from `earlier` to `later` (datetimes), or None if either is missing."""
    if not isinstance(later, datetime) or not isinstance(earlier, datetime):
        return None
    return max((later - earlier).total_seconds(), 0)


# Display status for an AlertRequest: splits WAITING into waiting / queued / retrying.
_STATUS_DISPLAY = {
    "waiting": ("Waiting", "clock", "neutral"),
    "queued": ("Queued", "send", "info"),
    "retrying": ("Retrying", "refresh", "warn"),
    "sending": ("Sending", "send", "info"),
    "notified": ("Notified", "check", "ok"),
    "failed": ("Failed", "alert", "bad"),
}


def _status_key(req):
    status = getattr(req, "status", "")
    if status == "waiting" and getattr(req, "due_at", None):
        return "retrying" if getattr(req, "attempts", 0) else "queued"
    return status if status in _STATUS_DISPLAY else "waiting"


@register.inclusion_tag("alerts/_status_pill.html")
def status_pill(req):
    key = _status_key(req)
    label, icon_name, tone = _STATUS_DISPLAY[key]
    return {"label": label, "icon_name": icon_name, "tone": tone, "key": key}


_OUTCOME_DISPLAY = {
    "restocked": ("Restocked", "package", "ok"),
    "sold_out": ("Sold out", "x", "neutral"),
    "phantom": ("Phantom: ignored", "ghost", "warn"),
}


@register.inclusion_tag("alerts/_status_pill.html")
def outcome_pill(event):
    status = getattr(event, "status", "")
    outcome = getattr(event, "outcome", "")
    if status == "pending":
        label, icon_name, tone = ("Verifying", "clock", "info")
    elif status == "failed":
        label, icon_name, tone = ("Unverified", "alert", "bad")
    else:
        label, icon_name, tone = _OUTCOME_DISPLAY.get(outcome, ("No change", "check", "neutral"))
    return {"label": label, "icon_name": icon_name, "tone": tone, "key": outcome or status}


@register.filter
def initial(email):
    """First letter for an avatar chip."""
    text = str(email or "?").strip()
    return (text[:1] or "?").upper()


@register.filter
def minus_one(value):
    try:
        return max(int(value) - 1, 0)
    except (TypeError, ValueError):
        return 0
