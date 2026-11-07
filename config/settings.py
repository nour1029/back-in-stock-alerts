"""Django settings for the Back-in-Stock alerts service.

Everything is driven by environment variables. Importing this module must work
with no environment at all (tests, `manage.py check`, Docker build steps).
"""

import hashlib
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader for local development. Never overrides real env vars."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --- Service configuration -------------------------------------------------

STOCK_API_URL = env("STOCK_API_URL").rstrip("/")
STOCK_API_KEY = env("STOCK_API_KEY")
RESEND_API_KEY = env("RESEND_API_KEY")
RESEND_BASE_URL = env("RESEND_BASE_URL").rstrip("/")
WEBHOOK_SECRET = env("WEBHOOK_SECRET")
# The admin password falls back to the webhook secret; empty means "refuse every login".
ADMIN_PASSWORD = env("ADMIN_PASSWORD") or WEBHOOK_SECRET

STORE_NAME = env("STORE_NAME", "Common Goods")
EMAIL_FROM = env("EMAIL_FROM") or f"{STORE_NAME} <alerts@example.com>"
PRODUCT_URL_TEMPLATE = env("PRODUCT_URL_TEMPLATE", "https://example.com/products/{sku}")

DATA_DIR = Path(env("DATA_DIR") or BASE_DIR / "data")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:  # read-only FS at import time (e.g. a build step); the DB open will report it
    pass

# --- Django core -----------------------------------------------------------

SECRET_KEY = env("SECRET_KEY") or hashlib.sha256(
    "|".join(
        ["back-in-stock", STOCK_API_KEY, RESEND_API_KEY, WEBHOOK_SECRET, ADMIN_PASSWORD]
    ).encode()
).hexdigest()

DEBUG = env("DEBUG").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*")
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS", "https://*.ngrok-free.app,https://*.ngrok.app,https://*.up.railway.app"
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = ["alerts"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
APPEND_SLASH = False

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csrf",
            ],
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 12 * 60 * 60
SESSION_COOKIE_NAME = "bis_admin"
X_FRAME_OPTIONS = "DENY"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "alerts": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
