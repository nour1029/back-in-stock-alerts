import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


def clean_email(value) -> str | None:
    """Stripped, lower-cased email, or None if invalid."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > 254:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email


def clean_sku(value) -> str | None:
    """Stripped SKU in its original case, or None if invalid."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    sku = value.strip()
    return sku if SKU_RE.fullmatch(sku) else None
